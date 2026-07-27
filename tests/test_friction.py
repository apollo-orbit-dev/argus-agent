"""Friction log: the loop records where it gave up, and never lets that recording matter.

Every test here guards one half of the contract — the record is written with enough detail to
act on, and writing it can never change what the turn does.
"""
import json
import pathlib
import subprocess

from engine.events import EventBus
from engine.friction import FrictionLog
from engine.loop import LoopDeps, run_loop
from engine.modes.manual import ManualMode
from engine.modes.native import NativeMode
from engine.protocol import ModelResponse
from engine.state import SessionStore
from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Model:
    """Scripted responses; repeats the last one forever so a loop can actually loop."""
    def __init__(self, responses):
        self._r = list(responses)
        self._last = self._r[-1] if self._r else None

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None,
                   think=None, reasoning=None):
        return self._r.pop(0) if self._r else self._last


def calc_call(expr, cid):
    return ModelResponse(content=None, tool_calls=[
        {"id": cid, "function": {"name": "calculator",
                                 "arguments": f'{{"expression": "{expr}"}}'}}])


def _deps(model, friction, mode=None, **kw):
    reg = ToolRegistry()
    reg.register(CalculatorTool())
    return LoopDeps(mode=mode or NativeMode(), registry=reg, model_client=model,
                    store=SessionStore(), events=EventBus(), max_steps=8,
                    friction=friction, model_name="test-model", **kw)


# ---------------------------------------------------------------- 1. stuck_repeating

async def test_stuck_repeating_appends_one_record_with_tool_and_attempts(tmp_path):
    """The give-up on a repeated tool call writes exactly ONE record naming the tool, the
    number of attempts, and the error the model kept hitting."""
    path = tmp_path / "friction.json"
    fl = FrictionLog(str(path))
    # 'nope' is not a valid expression -> every identical call returns the same error
    d = _deps(_Model([calc_call("nope", f"c{i}") for i in range(8)]), fl, observer_threshold=2)
    out = await run_loop(d, "sess-1", "r", "loop please")

    assert "make progress" in out.lower()             # normal stuck_repeating outcome, unchanged
    recs = json.loads(path.read_text())["records"]
    assert len(recs) == 1, recs
    r = recs[0]
    assert r["kind"] == "stuck_repeating"
    assert r["tool"] == "calculator"
    assert r["attempts"] == 3                          # threshold 2 -> stops on the 3rd
    assert r["session_id"] == "sess-1"
    assert r["model"] == "test-model"
    assert r["detail"]                                 # the repeated error text was captured
    assert "nope" in r["detail"] or "error" in r["detail"].lower()
    assert r["at"]


# ---------------------------------------------------------------- 2. parse failure

async def test_repeated_parse_failure_appends_one_record(tmp_path):
    """Two unparseable replies in a row ends the turn ('gave up after reprompt') and writes one
    record. No tool is involved, so `tool` is null."""
    path = tmp_path / "friction.json"
    fl = FrictionLog(str(path))
    d = _deps(_Model([ModelResponse(content="nope"), ModelResponse(content="still nope")]),
              fl, mode=ManualMode())
    out = await run_loop(d, "sess-2", "r", "hi")

    assert "valid response" in out.lower()             # normal give-up outcome, unchanged
    recs = json.loads(path.read_text())["records"]
    assert len(recs) == 1, recs
    r = recs[0]
    assert r["kind"] == "parse_failure"
    assert r["tool"] is None
    assert r["attempts"] == 2
    assert r["session_id"] == "sess-2"
    assert r["detail"]                                 # the parser's reason


# ---------------------------------------------------------------- 3. quiet on success

async def test_normal_turn_records_nothing(tmp_path):
    """A turn that answers — even one that used a tool — is not friction. Nothing is written,
    and the file is not even created."""
    path = tmp_path / "friction.json"
    fl = FrictionLog(str(path))
    d = _deps(_Model([calc_call("47*89", "c1"), ModelResponse(content="It is 4183")]), fl)
    out = await run_loop(d, "sess-3", "r", "what is 47*89?")

    assert out == "It is 4183"
    assert fl.records == []
    assert not path.exists()


# ---------------------------------------------------------------- 4. failure is inert

async def test_unwritable_log_does_not_raise_or_change_the_turn(tmp_path):
    """THE rule: this records, it does not intervene. Point the log at a path that cannot be
    written and the turn must still reach its normal result with its normal events."""
    unwritable = tmp_path / "nonexistent-dir" / "friction.json"    # parent does not exist
    fl = FrictionLog(str(unwritable))
    d = _deps(_Model([calc_call("nope", f"c{i}") for i in range(8)]), fl, observer_threshold=2)

    out = await run_loop(d, "sess-4", "r", "loop please")          # must not raise

    assert "make progress" in out.lower()                          # identical outcome
    issues = [e.data.get("issue") for e in d.events.recent("sess-4") if e.kind == "observer"]
    assert "stuck_repeating" in issues                             # identical events
    assert not unwritable.exists()

    # and the same turn with NO log at all produces the same answer
    d2 = _deps(_Model([calc_call("nope", f"c{i}") for i in range(8)]), None, observer_threshold=2)
    assert await run_loop(d2, "sess-4b", "r", "loop please") == out

    # ...and so does a turn whose log is actively hostile. `LoopDeps.friction` is duck-typed
    # (`object`), so the store's own swallow is not the only thing that has to hold; the loop
    # must refuse to let ANY recording failure reach the turn.
    class _HostileLog:
        def record(self, **kw):
            raise RuntimeError("log is on fire")

    d3 = _deps(_Model([calc_call("nope", f"c{i}") for i in range(8)]), _HostileLog(),
               observer_threshold=2)
    assert await run_loop(d3, "sess-4c", "r", "loop please") == out


def test_record_never_raises_even_when_saving_explodes(tmp_path):
    """Belt and braces at the store level: a FrictionLog whose write blows up returns None
    instead of propagating, so `_record_friction` isn't the only thing standing between a full
    disk and a broken turn."""
    fl = FrictionLog(str(tmp_path / "friction.json"))

    def boom():
        raise OSError("disk full")
    fl._save = boom

    assert fl.record("stuck_repeating", tool="calculator", attempts=3) is None


def test_load_tolerates_a_malformed_file(tmp_path):
    """A truncated/hand-edited log must not stop the engine starting, nor the next write."""
    path = tmp_path / "friction.json"
    path.write_text("{not json at all")
    fl = FrictionLog(str(path))
    assert fl.records == []
    assert fl.record("parse_failure", attempts=2) is not None
    assert len(json.loads(path.read_text())["records"]) == 1


# ---------------------------------------------------------------- 5. privacy

def test_friction_log_path_is_gitignored():
    """`detail` can quote user content out of a tool result. The log lives in the data dir
    (the repo root for a default install) and must NEVER be committable."""
    r = subprocess.run(["git", "check-ignore", "-v", "friction.json"],
                       cwd=str(REPO_ROOT), capture_output=True, text=True)
    assert r.returncode == 0, (
        "friction.json is NOT gitignored — it holds user content and must never be committed. "
        f"git check-ignore said: {r.stdout or r.stderr!r}")


# ---------------------------------------------------------------- reading it back

def test_summary_groups_by_kind_and_tool_most_frequent_first(tmp_path):
    """The grouping IS the feature — the top row is the next thing to fix."""
    fl = FrictionLog(str(tmp_path / "friction.json"))
    for _ in range(3):
        fl.record("stuck_repeating", tool="web_search", attempts=3, detail="proxy blocked")
    fl.record("stuck_repeating", tool="create_tool", attempts=5, detail="no module named requests")
    fl.record("parse_failure", attempts=2, detail="no JSON object found")

    groups = fl.summary()
    assert [(g["kind"], g["tool"], g["count"]) for g in groups][0] == ("stuck_repeating", "web_search", 3)
    assert len(groups) == 3
    assert {(g["kind"], g["tool"]) for g in groups} == {
        ("stuck_repeating", "web_search"), ("stuck_repeating", "create_tool"), ("parse_failure", None)}
    top = groups[0]
    assert top["detail"] == "proxy blocked" and top["attempts_max"] == 3


def test_detail_is_truncated(tmp_path):
    fl = FrictionLog(str(tmp_path / "friction.json"))
    r = fl.record("stuck_repeating", tool="t", attempts=3, detail="x" * 5000)
    assert len(r["detail"]) == 400


def test_cli_friction_prints_the_ranked_list(tmp_path, capsys, monkeypatch):
    import engine.cli as cli
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    fl = FrictionLog(str(tmp_path / "friction.json"))
    for _ in range(2):
        fl.record("stuck_repeating", tool="web_search", attempts=3, detail="proxy blocked")

    assert cli.main(["friction"]) == 0
    out = capsys.readouterr().out
    assert "web_search" in out and "stuck_repeating" in out and "proxy blocked" in out


def test_cli_friction_on_an_empty_log_is_not_an_error(tmp_path, capsys, monkeypatch):
    import engine.cli as cli
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.main(["friction"]) == 0
    assert "No friction recorded yet" in capsys.readouterr().out
