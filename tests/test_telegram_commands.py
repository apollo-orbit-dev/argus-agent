from backend.telegram_bot import (
    BOT_COMMANDS,
    build_telegram_app,
    rename_command,
    session_text,
    sessions_text,
)


def test_bot_commands_shape():
    assert BOT_COMMANDS, "at least one slash command should be registered"
    for entry in BOT_COMMANDS:
        cmd, desc = entry
        assert cmd.islower() and " " not in cmd  # valid Telegram command name
        assert 1 <= len(desc) <= 256


def test_registered_commands_have_handlers():
    # every registered slash command must have a matching CommandHandler
    class _Cfg:
        telegram_bot_token = "123:abc"
        allowed_chat_ids = [1]
    app = build_telegram_app(engine=None, config=_Cfg())
    handler_cmds = set()
    for group in app.handlers.values():
        for h in group:
            cmds = getattr(h, "commands", None)
            if cmds:
                handler_cmds.update(cmds)
    for cmd, _ in BOT_COMMANDS:
        assert cmd in handler_cmds, f"/{cmd} is advertised but has no handler"


def test_custom_command_catch_all_registered_and_hidden():
    # A MessageHandler on COMMAND must exist (the custom-alias catch-all), and it has to be the
    # LAST handler so built-in CommandHandlers claim their commands first. Custom aliases are
    # deliberately absent from BOT_COMMANDS so they don't appear in Telegram's `/` menu.
    from telegram.ext import MessageHandler
    from telegram.ext import filters as tg_filters

    class _Cfg:
        telegram_bot_token = "123:abc"
        allowed_chat_ids = [1]
    app = build_telegram_app(engine=None, config=_Cfg())
    group = app.handlers[0]
    catch_alls = [h for h in group if isinstance(h, MessageHandler) and h.filters is tg_filters.COMMAND]
    assert len(catch_alls) == 1, "expected exactly one COMMAND catch-all for custom aliases"
    assert group[-1] is catch_alls[0], "the catch-all must be registered last so built-ins win"


# --------------------------------------------------------------------------
# /sessions, /session, /rename (argus-aeu, with argus-9in folded in)
# --------------------------------------------------------------------------
def _sess(n, name=None, sid=None, updated="2026-07-24T10:15:00"):
    sid = sid or f"ses_{n:04d}"
    return {"id": sid, "name": name if name is not None else sid, "origin": "telegram",
            "updated": updated, "message_count": n}


class _Cfg:
    telegram_bot_token = "123:abc"
    allowed_chat_ids = [1]


class _FakeEngine:
    """Just enough of Engine for rename_command / the /reset alias test."""
    def __init__(self, sessions=None):
        self._sessions = sessions if sessions is not None else []
        self.renamed = []

    def list_sessions(self):
        return self._sessions

    def rename_session(self, session_id, name):
        self.renamed.append((session_id, name))

    def reset(self, session_id):
        pass


def test_reset_advertised_new_hidden_same_callback():
    cmds = [c for c, _ in BOT_COMMANDS]
    assert "reset" in cmds
    assert "new" not in cmds
    app = build_telegram_app(engine=_FakeEngine(), config=_Cfg())
    handlers = {c: h.callback for h in app.handlers[0] for c in (getattr(h, "commands", None) or [])}
    assert "reset" in handlers and "new" in handlers
    assert handlers["reset"] is handlers["new"]


def test_sessions_text_marks_current_once_and_codes_every_id():
    sessions = [_sess(1), _sess(2), _sess(3)]
    current = sessions[1]["id"]
    text = sessions_text(sessions, current)
    assert text.count("← current") == 1
    assert text.count("<code>") == len(sessions)


def test_sessions_text_caps_at_20_but_always_keeps_current():
    sessions = [_sess(i) for i in range(25)]
    current = sessions[24]["id"]
    text = sessions_text(sessions, current)
    assert text.count("<code>") == 21
    assert "…and 4 more." in text
    assert current in text


def test_sessions_text_escapes_hostile_name():
    sessions = [_sess(1, name="<b>x</b> & co")]
    text = sessions_text(sessions, sessions[0]["id"])
    assert "&lt;b&gt;" in text
    assert "&amp;" in text
    assert "<b>" not in text


def test_no_markdownv2_escaping_of_negative_ids():
    sessions = [_sess(1, sid="-1001234567")]
    text = sessions_text(sessions, "-1001234567")
    assert "-1001234567" in text
    assert "\\" not in text
    text2 = session_text(sessions, "-1001234567")
    assert "-1001234567" in text2
    assert "\\" not in text2


def test_session_text_omits_name_when_equal_to_id_includes_when_renamed():
    same = session_text([_sess(1)], "ses_0001")
    assert same.count("ses_0001") == 1          # id shown once, no separate (identical) name line
    renamed = session_text([_sess(1, name="My Chat")], "ses_0001")
    assert "My Chat" in renamed
    assert "<code>ses_0001</code>" in renamed


def test_session_text_empty_sessions_still_shows_current_id():
    text = session_text([], "42")
    assert "<code>42</code>" in text


def test_rename_bare_shows_current_and_does_not_rename():
    eng = _FakeEngine([_sess(1, sid="42")])
    text = rename_command(eng, "42", [])
    assert eng.renamed == []
    assert "42" in text


def test_rename_sets_name():
    eng = _FakeEngine([_sess(1, sid="42")])
    text = rename_command(eng, "42", ["my", "chat"])
    assert eng.renamed == [("42", "my chat")]
    assert "my chat" in text


def test_rename_unknown_session_is_noop():
    eng = _FakeEngine([])
    text = rename_command(eng, "42", ["name"])
    assert eng.renamed == []
    assert "send me a message first" in text


def test_rename_truncates_to_80_chars():
    eng = _FakeEngine([_sess(1, sid="42")])
    long_name = "x" * 100
    rename_command(eng, "42", [long_name])
    assert eng.renamed == [("42", "x" * 80)]


async def test_on_new_reply_mentions_same_session_not_new_conversation():
    from types import SimpleNamespace as NS

    app = build_telegram_app(engine=_FakeEngine(), config=_Cfg())
    handlers = {c: h.callback for h in app.handlers[0] for c in (getattr(h, "commands", None) or [])}
    on_new = handlers["reset"]

    replies = []

    async def reply_text(text, **kw):
        replies.append(text)
    update = NS(effective_chat=NS(id=1), effective_message=NS(reply_text=reply_text))
    await on_new(update, NS(args=[]))
    assert len(replies) == 1
    assert "same session" in replies[0]
    assert "New conversation" not in replies[0]
