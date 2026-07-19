"""Telegram parity for interactive approvals: the unified ``apv:`` inline-button callback.

Mirrors ``tests/test_dep_approval.py``'s telegram-helper tests (dep_keyboard,
dep_request_text) plus ``tests/test_telegram_commands.py``'s pattern of building a real
Application via ``build_telegram_app`` and reaching into its registered handlers — there is
no existing harness that invokes a handler closure directly, so we build one here: grab the
``CallbackQueryHandler`` matching ``^apv:`` off the built app and call its ``.callback``
with fake update/query objects, exactly like a real Telegram update would.
"""
from backend.telegram_bot import apv_keyboard, apv_request_text, build_telegram_app


class _Cfg:
    telegram_bot_token = "123:abc"
    allowed_chat_ids = [1]


class _FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answered = []
        self.edited = []

    async def answer(self, text=None):
        self.answered.append(text)

    async def edit_message_text(self, text):
        self.edited.append(text)


class _FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, data, chat_id):
        self.callback_query = _FakeQuery(data)
        self.effective_chat = _FakeChat(chat_id)


class _FakeEngine:
    """Records every approvals_decide() call; returns a canned outcome ("live" by default,
    matching what ApprovalBroker.resolve() returns when a turn is actively waiting)."""

    def __init__(self, outcome="live"):
        self.calls = []
        self._outcome = outcome

    def approvals_decide(self, req_id, action):
        self.calls.append((req_id, action))
        return self._outcome


def _apv_handler(engine):
    app = build_telegram_app(engine=engine, config=_Cfg())
    for h in app.handlers[0]:
        pat = getattr(h, "pattern", None)
        if pat is not None and getattr(pat, "pattern", None) == r"^apv:":
            return h.callback
    raise AssertionError("no CallbackQueryHandler registered for pattern ^apv:")


# ---- apv_keyboard ---------------------------------------------------------

def test_apv_keyboard_two_state_dep_install():
    # dep-install's states are ["ask", "deny"] — no "allow" state exists (an unreviewed
    # install can never be blanket-trusted), so only a standing "always deny" is offered.
    kb = apv_keyboard("a1b2c3d4", ["ask", "deny"])
    all_buttons = [b for row in kb.inline_keyboard for b in row]
    data = {b.callback_data for b in all_buttons}
    assert data == {"apv:approve_once:a1b2c3d4", "apv:deny_once:a1b2c3d4", "apv:always_deny:a1b2c3d4"}
    for b in all_buttons:
        assert len(b.callback_data.encode()) <= 64


def test_apv_keyboard_three_state_soul_edit():
    # soul-edit's states are ["allow", "ask", "deny"] — both standing options are valid.
    kb = apv_keyboard("a1b2c3d4", ["allow", "ask", "deny"])
    all_buttons = [b for row in kb.inline_keyboard for b in row]
    data = {b.callback_data for b in all_buttons}
    assert data == {
        "apv:approve_once:a1b2c3d4", "apv:deny_once:a1b2c3d4",
        "apv:always_allow:a1b2c3d4", "apv:always_deny:a1b2c3d4",
    }


def test_apv_keyboard_no_states_no_standing_row():
    kb = apv_keyboard("a1b2c3d4", [])
    assert len(kb.inline_keyboard) == 1   # just the once-row, no standing row at all
    data = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert data == {"apv:approve_once:a1b2c3d4", "apv:deny_once:a1b2c3d4"}


def test_apv_request_text_uses_prompt():
    txt = apv_request_text({"kind": "dep-install", "target": "pandas",
                            "prompt": "Install Python package 'pandas' for tool 'x'."})
    assert "pandas" in txt


# ---- on_apv_callback: parse -> dispatch -> engine.approvals_decide ------

async def test_apv_callback_approve_once_dispatches_and_confirms():
    engine = _FakeEngine()
    cb = _apv_handler(engine)
    update = _FakeUpdate("apv:approve_once:a1b2c3d4", chat_id=1)
    await cb(update, None)
    assert engine.calls == [("a1b2c3d4", "approve_once")]
    assert update.callback_query.answered
    assert any("approved" in (t or "").lower() for t in update.callback_query.edited)


async def test_apv_callback_deny_once_dispatches():
    engine = _FakeEngine()
    cb = _apv_handler(engine)
    update = _FakeUpdate("apv:deny_once:a1b2c3d4", chat_id=1)
    await cb(update, None)
    assert engine.calls == [("a1b2c3d4", "deny_once")]
    assert any("denied" in (t or "").lower() for t in update.callback_query.edited)


async def test_apv_callback_always_allow_dispatches():
    engine = _FakeEngine()
    cb = _apv_handler(engine)
    update = _FakeUpdate("apv:always_allow:a1b2c3d4", chat_id=1)
    await cb(update, None)
    assert engine.calls == [("a1b2c3d4", "always_allow")]


async def test_apv_callback_always_deny_dispatches():
    engine = _FakeEngine()
    cb = _apv_handler(engine)
    update = _FakeUpdate("apv:always_deny:a1b2c3d4", chat_id=1)
    await cb(update, None)
    assert engine.calls == [("a1b2c3d4", "always_deny")]


async def test_apv_callback_unauthorized_chat_never_dispatches():
    engine = _FakeEngine()
    cb = _apv_handler(engine)
    update = _FakeUpdate("apv:approve_once:a1b2c3d4", chat_id=999)   # not in allowed_chat_ids
    await cb(update, None)
    assert engine.calls == []
    assert update.callback_query.answered   # told "not authorized", not silently dropped
    assert not update.callback_query.edited


async def test_apv_callback_unknown_outcome_reports_no_longer_pending():
    engine = _FakeEngine(outcome="unknown")
    cb = _apv_handler(engine)
    update = _FakeUpdate("apv:approve_once:deadbeef", chat_id=1)
    await cb(update, None)
    assert engine.calls == [("deadbeef", "approve_once")]
    assert any("no longer pending" in (t or "") for t in update.callback_query.edited)


def test_apv_handler_registered_alongside_legacy_dep_handler():
    # Migration choice (see task-11 report): keep on_dep_callback for enable_interactive_approvals
    # =False, ADD on_apv_callback alongside it — both must be reachable.
    from telegram.ext import CallbackQueryHandler
    app = build_telegram_app(engine=_FakeEngine(), config=_Cfg())
    patterns = {h.pattern.pattern for h in app.handlers[0] if isinstance(h, CallbackQueryHandler)}
    assert patterns == {r"^dep(ok|no):", r"^apv:"}
