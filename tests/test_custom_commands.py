"""Custom slash-command aliases: the CustomCommandStore + the engine's /alias expansion."""
import pytest

from engine.custom_commands import CustomCommandStore, sanitize_command_name, RESERVED_COMMANDS


# ---- name sanitizing ----
def test_sanitize_strips_slash_lowercases_and_reduces_charset():
    assert sanitize_command_name("/Standup") == "standup"
    assert sanitize_command_name("  My Command! ") == "my_command"
    assert sanitize_command_name("weekly-report") == "weekly_report"
    assert sanitize_command_name("///") == ""


# ---- store CRUD ----
def test_set_get_list_and_remove(tmp_path):
    s = CustomCommandStore(str(tmp_path / "c.yaml"))
    assert s.list() == {}
    name = s.set("/Standup", "Summarize my last week of standup")
    assert name == "standup"
    assert s.get("standup") == "Summarize my last week of standup"
    assert s.get("/STANDUP") == "Summarize my last week of standup"   # lookup is sanitized too
    assert s.list() == {"standup": "Summarize my last week of standup"}
    assert s.remove("standup") is True
    assert s.get("standup") is None
    assert s.remove("standup") is False


def test_set_replaces_by_name(tmp_path):
    s = CustomCommandStore(str(tmp_path / "c.yaml"))
    s.set("hi", "first")
    s.set("Hi", "second")
    assert s.list() == {"hi": "second"}


def test_reserved_and_empty_names_rejected(tmp_path):
    s = CustomCommandStore(str(tmp_path / "c.yaml"))
    for bad in ("status", "compact", "/model"):
        with pytest.raises(ValueError):
            s.set(bad, "x")
    with pytest.raises(ValueError):
        s.set("///", "x")           # sanitizes to empty
    with pytest.raises(ValueError):
        s.set("ok", "   ")          # empty expansion


def test_reserved_covers_the_builtin_menu():
    for cmd in ("start", "help", "new", "compact", "model", "roles", "reembed", "status"):
        assert cmd in RESERVED_COMMANDS


# ---- persistence + hot reload (CLI edits picked up with no restart) ----
def test_persist_across_instances(tmp_path):
    p = str(tmp_path / "c.yaml")
    CustomCommandStore(p).set("standup", "how did I standup")
    assert CustomCommandStore(p).get("standup") == "how did I standup"


def test_reload_picks_up_external_edit(tmp_path):
    p = tmp_path / "c.yaml"
    s = CustomCommandStore(str(p))
    s.set("a", "one")
    # Simulate a hand-edit of the YAML with a NEWER mtime, then confirm a read reflects it.
    import os
    p.write_text("a: one\nb: two\n", encoding="utf-8")
    os.utime(str(p), (p.stat().st_atime, p.stat().st_mtime + 5))
    assert s.get("b") == "two"


def test_bad_yaml_keeps_last_good_copy(tmp_path):
    p = tmp_path / "c.yaml"
    s = CustomCommandStore(str(p))
    s.set("a", "one")
    import os
    p.write_text("this: is: not: valid: yaml:\n  - [", encoding="utf-8")
    os.utime(str(p), (p.stat().st_atime, p.stat().st_mtime + 5))
    assert s.get("a") == "one"       # parse error -> previous items retained


# ---- engine-level expansion (the logic run_task applies) ----
class _ExpandEngine:
    """Just enough of Engine to exercise _expand_command against a real store."""
    def __init__(self, store):
        self.commands = store

    # bound copy of Engine._expand_command
    from engine.engine import Engine
    _expand_command = Engine._expand_command


def test_expand_command(tmp_path):
    store = CustomCommandStore(str(tmp_path / "c.yaml"))
    store.set("standup", "Summarize my standup")
    e = _ExpandEngine(store)
    assert e._expand_command("/standup") == "Summarize my standup"
    assert e._expand_command("/standup last night") == "Summarize my standup last night"
    assert e._expand_command("/standup@argus_bot week") == "Summarize my standup week"   # @botname stripped
    assert e._expand_command("/unknown") == "/unknown"        # not a command -> unchanged
    assert e._expand_command("hello there") == "hello there"  # plain text -> unchanged
    assert e._expand_command("") == ""


# ---- Telegram: an alias whose expansion is a built-in command runs that built-in ----
async def test_alias_to_builtin_command_dispatches(tmp_path):
    from types import SimpleNamespace as NS
    from backend.telegram_bot import build_telegram_app

    class FakeEngine:
        def __init__(self, cmds):
            self._cmds = cmds
            self.reset_calls = []
        def custom_command_expand(self, name):
            return self._cmds.get(name)
        def reset(self, session_id):
            self.reset_calls.append(session_id)

    class Cfg:
        telegram_bot_token = "123:abc"
        allowed_chat_ids = [1]

    eng = FakeEngine({"fresh": "/new"})           # alias /fresh -> built-in /new (engine.reset)
    app = build_telegram_app(engine=eng, config=Cfg())
    catch_all = app.handlers[0][-1].callback      # on_custom_command is registered last

    replies = []
    async def reply_text(text, **kw):
        replies.append(text)
    update = NS(effective_chat=NS(id=1),
                effective_message=NS(text="/fresh", reply_text=reply_text, photo=None))
    await catch_all(update, NS(args=None))
    assert eng.reset_calls == ["1"]               # the built-in actually ran (not sent to the model)
    assert any("New conversation" in r for r in replies)


async def test_unknown_alias_stays_silent(tmp_path):
    from types import SimpleNamespace as NS
    from backend.telegram_bot import build_telegram_app

    class FakeEngine:
        def custom_command_expand(self, name):
            return None                            # not a known alias
        def reset(self, session_id):
            raise AssertionError("should not run anything")

    class Cfg:
        telegram_bot_token = "123:abc"
        allowed_chat_ids = [1]

    app = build_telegram_app(engine=FakeEngine(), config=Cfg())
    catch_all = app.handlers[0][-1].callback
    replies = []
    async def reply_text(text, **kw):
        replies.append(text)
    update = NS(effective_chat=NS(id=1),
                effective_message=NS(text="/typo", reply_text=reply_text, photo=None))
    await catch_all(update, NS(args=None))
    assert replies == []                           # silent — nothing sent
