"""Dashboard editor for skills and created tools (argus-q8z).

The load-bearing property under test is the OVERRIDE design: `engine/skills/library/` is tracked
in git and `engine/updater.py` blocks an update on a dirty tree, so a skill edit must land in
<data_dir>/created_skills/ and never touch the shipped file. Two tests here exist purely to make
that impossible to regress: `test_saving_a_skill_leaves_git_status_unchanged` and
`test_editing_a_shipped_skill_writes_an_override_and_leaves_the_shipped_file_untouched`.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from backend.app import create_app
from config import Config
from engine.engine import Engine
from engine.tools.base import ToolRegistry

REPO = Path(__file__).resolve().parents[1]
SHIPPED = REPO / "engine" / "skills" / "library"
# A shipped skill with no external dependencies, used as the override target throughout.
TARGET = "compare_options"


def _engine(tmp_path, **cfg_kw):
    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="", **cfg_kw)
    return Engine(cfg, data_dir=str(tmp_path), env_path=str(tmp_path / ".env"))


def _seed_runtime_skill(tmp_path, name, procedure="1. Seeded."):
    """Put a runtime skill on disk BEFORE the engine loads it. Authoring a new skill is the agent's
    job (create_skill) — the dashboard editor only ever edits something that already exists — so
    every runtime-skill test seeds it the way the agent would have."""
    d = tmp_path / "created_skills"
    d.mkdir(exist_ok=True)
    (d / f"{name}.md").write_text(_edited_source(name=name, procedure=procedure))


def _edited_source(name=TARGET, description="edited desc", procedure="1. Do the edited thing."):
    return (f"---\nname: {name}\ndescription: {description}\ntools: []\n---\n{procedure}\n")


# ---- skills: override write ----
# NOTE: the git-status test runs FIRST in this module on purpose. It compares the working tree
# before and after a save, so any earlier test that dirtied the shipped file would already be in
# its `before` snapshot and the check would pass for the wrong reason.

def test_saving_a_skill_leaves_git_status_unchanged(tmp_path):
    """The whole reason the override design exists: a save that dirtied a tracked path would make
    the dashboard's Update button refuse (engine/updater.py's `dirty_tree` blocker)."""
    def status():
        return subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                              capture_output=True, text=True).stdout
    before = status()
    e = _engine(tmp_path)
    assert e.skill_save(TARGET, _edited_source())["ok"] is True
    assert status() == before
    assert f"skills/library/{TARGET}.md" not in status()


def test_editing_a_shipped_skill_writes_an_override_and_leaves_the_shipped_file_untouched(tmp_path):
    e = _engine(tmp_path)
    before = (SHIPPED / f"{TARGET}.md").read_bytes()

    r = e.skill_save(TARGET, _edited_source())
    assert r["ok"] is True and r["origin"] == "override"

    assert (tmp_path / "created_skills" / f"{TARGET}.md").read_text() == _edited_source()
    assert (SHIPPED / f"{TARGET}.md").read_bytes() == before      # byte-identical


def test_override_takes_effect_in_the_live_registry(tmp_path):
    e = _engine(tmp_path)
    assert "edited thing" not in e.skill_registry.get(TARGET).procedure
    e.skill_save(TARGET, _edited_source(procedure="1. Do the edited thing."))
    sk = e.skill_registry.get(TARGET)
    assert sk.procedure.strip() == "1. Do the edited thing."       # no restart needed
    assert sk.description == "edited desc"
    # and it is reported as an override, with the shipped text alongside it
    src = e.skill_source(TARGET)
    assert src["origin"] == "override"
    assert src["shipped_source"] == (SHIPPED / f"{TARGET}.md").read_text()


def test_a_fresh_engine_still_prefers_the_override(tmp_path):
    """created_skills/ loads after the shipped library, so the override survives a restart."""
    _engine(tmp_path).skill_save(TARGET, _edited_source(procedure="1. Persisted edit."))
    assert _engine(tmp_path).skill_registry.get(TARGET).procedure.strip() == "1. Persisted edit."


def test_reverting_restores_the_shipped_skill_without_a_restart(tmp_path):
    e = _engine(tmp_path)
    shipped_procedure = e.skill_registry.get(TARGET).procedure
    e.skill_save(TARGET, _edited_source())
    assert e.skill_registry.get(TARGET).procedure != shipped_procedure

    r = e.skill_revert(TARGET)
    assert r["ok"] is True and r["origin"] == "shipped"
    assert not (tmp_path / "created_skills" / f"{TARGET}.md").exists()
    assert e.skill_registry.get(TARGET).procedure == shipped_procedure   # live, no restart
    assert e.skill_source(TARGET)["origin"] == "shipped"


def test_reverting_one_override_leaves_another_alone(tmp_path):
    """Revert reloads ONE shipped file, not the whole library — a whole-library reload would
    silently wipe every other override in the same pass."""
    e = _engine(tmp_path)
    e.skill_save(TARGET, _edited_source())
    e.skill_save("proofread", _edited_source(name="proofread", procedure="1. Keep me."))
    e.skill_revert(TARGET)
    assert e.skill_registry.get("proofread").procedure.strip() == "1. Keep me."


def test_deleting_an_override_restores_the_shipped_skill(tmp_path):
    """The Developer panel's ✕ hits delete_created_skill, which for an override used to unregister
    the built-in and leave it gone until the next restart."""
    e = _engine(tmp_path)
    shipped_procedure = e.skill_registry.get(TARGET).procedure
    e.skill_save(TARGET, _edited_source())
    r = e.delete_created_skill(TARGET)
    assert r["ok"] is True and r.get("restored") == "shipped"
    assert e.skill_registry.get(TARGET).procedure == shipped_procedure


def test_skills_overview_tags_an_override_and_keeps_it_under_builtin(tmp_path):
    _seed_runtime_skill(tmp_path, "my_runtime_skill")
    e = _engine(tmp_path)
    e.skill_save(TARGET, _edited_source())
    ov = e.skills_overview()
    row = next(x for x in ov["builtin"] if x["name"] == TARGET)
    assert row["origin"] == "override"            # editing a built-in must not move its row
    assert not any(x["name"] == TARGET for x in ov["created"])
    assert next(x for x in ov["created"] if x["name"] == "my_runtime_skill")["origin"] == "runtime"
    assert all(x["origin"] in ("shipped", "override") for x in ov["builtin"])


def test_revert_refuses_when_there_is_no_default_to_go_back_to(tmp_path):
    _seed_runtime_skill(tmp_path, "my_runtime_skill")
    e = _engine(tmp_path)
    assert e.skill_source("my_runtime_skill")["origin"] == "runtime"
    assert e.skill_save("my_runtime_skill",
                        _edited_source(name="my_runtime_skill", procedure="1. Edited."))["ok"] is True
    r = e.skill_revert("my_runtime_skill")
    assert r["ok"] is False and "no default" in r["error"]
    assert (tmp_path / "created_skills" / "my_runtime_skill.md").exists()   # not deleted
    assert e.skill_revert(TARGET)["ok"] is False                            # nothing to reset


def test_the_editor_refuses_to_author_a_skill_that_does_not_exist(tmp_path):
    """Creating a NEW skill is the agent's job (create_skill) — out of scope for the editor, and
    keeping it out stops this endpoint being a write primitive with a caller-chosen filename."""
    e = _engine(tmp_path)
    r = e.skill_save("brand_new_skill", _edited_source(name="brand_new_skill"))
    assert r["ok"] is False and "to edit" in r["error"]
    assert not (tmp_path / "created_skills" / "brand_new_skill.md").exists()


# ---- skills: validation refuses rather than writing ----

@pytest.mark.parametrize("source", [
    "---\nname: compare_options\ndescription: \ntools: []\n---\n1. Steps.\n",   # empty description
    "---\nname: compare_options\ndescription: d\ntools: []\n---\n\n",           # empty procedure
    "no frontmatter at all\n",
])
def test_an_unloadable_skill_is_refused_not_written(tmp_path, source):
    e = _engine(tmp_path)
    r = e.skill_save(TARGET, source)
    assert r["ok"] is False and "would not load" in r["error"]
    assert not (tmp_path / "created_skills" / f"{TARGET}.md").exists()
    # the live skill is untouched
    assert e.skill_registry.get(TARGET).description


def test_a_renaming_edit_is_refused(tmp_path):
    """The registry keys on the frontmatter name but the file is <name>.md — a mismatch would
    strand the file where revert/delete can never find it."""
    e = _engine(tmp_path)
    r = e.skill_save(TARGET, _edited_source(name="something_else"))
    assert r["ok"] is False and "does not match" in r["error"]
    assert not (tmp_path / "created_skills" / f"{TARGET}.md").exists()


@pytest.mark.parametrize("bad", ["../evil", "a/b", "..", "UPPER", "with space", "", "x\\y"])
def test_path_traversal_names_are_rejected_by_the_name_guard(tmp_path, bad):
    """Names reach the filesystem, so every editor entry point rejects anything that isn't a bare
    identifier. Asserted on the guard's OWN message ('invalid ... name'): a traversal name would
    otherwise be turned away incidentally by a later check, which would make this test pass with
    the guard removed."""
    e = _engine(tmp_path, enable_tool_creation=True)
    # frontmatter name == the traversal name, so nothing downstream can refuse it for us
    src = f"---\nname: {json.dumps(bad)}\ndescription: d\ntools: []\n---\n1. Step.\n"
    results = [e.skill_source(bad), e.skill_save(bad, src), e.skill_revert(bad),
               e.tool_source(bad),
               asyncio.run(e.tool_save(bad, "d", {}, "def run(args):\n    return 'x'\n"))]
    for r in results:
        assert r["ok"] is False and "invalid" in r["error"], (bad, r)
    # nothing escaped the created_skills dir, in either direction
    assert not (tmp_path.parent / "evil.md").exists()
    assert not (tmp_path / "evil.md").exists()


def test_a_stale_buffer_is_reported_as_a_conflict(tmp_path):
    e = _engine(tmp_path)
    v = e.skill_source(TARGET)["version"]
    assert e.skill_save(TARGET, _edited_source(procedure="1. First tab."),
                        expected_version=v)["ok"] is True
    # second tab still holds the ORIGINAL version token
    r = e.skill_save(TARGET, _edited_source(procedure="1. Second tab."), expected_version=v)
    assert r["ok"] is False and r["conflict"] is True
    assert e.skill_registry.get(TARGET).procedure.strip() == "1. First tab."


# ---- created tools ----

_GOOD = "def run(args):\n    return 'v1 result'\n"
_BROKEN = "def run(args):\n    raise ValueError('boom')\n"


async def _seed_tool(e, name="greet", description="says hi", code=_GOOD, sandboxed=False):
    """Create a tool the way the AGENT does — CreateToolTool against the engine's own sink and
    persist dir. The editor only ever edits an existing tool, so every tool test starts here."""
    from engine.experimental.tool_creation import CreateToolTool
    ct = CreateToolTool(ToolRegistry(), persist_dir=e._created_tools_dir,
                        created_sink=e._created_tools, sandbox_enabled=False)
    out = await ct.run(ct.Params(name=name, description=description, parameters={}, code=code,
                                 test_args={}, sandboxed=sandboxed))
    assert ct.created[-1]["ok"], out
    return out


def _tool_engine(tmp_path, **kw):
    e = _engine(tmp_path, enable_tool_creation=True)
    asyncio.run(_seed_tool(e, **kw))
    return e


def test_tool_edit_round_trip_and_sandboxed_flag(tmp_path):
    e = _tool_engine(tmp_path)
    r = asyncio.run(e.tool_save("greet", "says hi", {}, _GOOD, sandboxed=True))
    assert r["ok"] is True

    src = e.tool_source("greet")
    assert src["ok"] is True and src["sandboxed"] is True and src["code"] == _GOOD
    assert src["description"] == "says hi"
    # the hint the editor shows is the ONE shared statement of what the container holds
    from engine.experimental.tool_creation import _SANDBOX_STDLIB_FACT
    assert src["sandbox_fact"] == _SANDBOX_STDLIB_FACT
    # ...and the flag actually took effect on the live tool
    assert next(t for t in e._created_tools if t.name == "greet").sandboxed is True

    # flip it off through the editor
    assert asyncio.run(e.tool_save("greet", "says hi", {}, _GOOD, sandboxed=False))["ok"] is True
    assert e.tool_source("greet")["sandboxed"] is False
    live = next(t for t in e._created_tools if t.name == "greet")
    assert live.sandboxed is False
    assert asyncio.run(live.run(live.Params())) == "v1 result"     # and it runs host-side
    assert json.loads((tmp_path / "created_tools" / "greet.json").read_text())["sandboxed"] is False


def test_a_failed_tool_save_leaves_the_previous_tool_registered_and_working(tmp_path):
    e = _tool_engine(tmp_path)
    assert asyncio.run(e.tool_save("greet", "says hi", {}, _GOOD, sandboxed=False))["ok"] is True

    r = asyncio.run(e.tool_save("greet", "now broken", {}, _BROKEN, sandboxed=False))
    assert r["ok"] is False and r["error"]

    live = [t for t in e._created_tools if t.name == "greet"]
    assert len(live) == 1
    assert asyncio.run(live[0].run(live[0].Params())) == "v1 result"   # the WORKING code, still
    on_disk = json.loads((tmp_path / "created_tools" / "greet.json").read_text())
    assert on_disk["code"] == _GOOD and on_disk["description"] == "says hi"
    assert e.tool_source("greet")["code"] == _GOOD


def test_a_tool_save_that_the_import_gate_rejects_changes_nothing(tmp_path):
    e = _tool_engine(tmp_path)
    assert asyncio.run(e.tool_save("greet", "says hi", {}, _GOOD, sandboxed=False))["ok"] is True
    r = asyncio.run(e.tool_save("greet", "sneaky", {},
                                "import os\ndef run(args):\n    return os.getcwd()\n",
                                sandboxed=False))
    assert r["ok"] is False
    assert e.tool_source("greet")["code"] == _GOOD


def test_tool_editing_refuses_a_builtin_name_and_a_tool_that_does_not_exist(tmp_path):
    e = _tool_engine(tmp_path)
    assert e.tool_source("calculator")["ok"] is False          # never a created tool
    r = asyncio.run(e.tool_save("calculator", "d", {}, _GOOD, sandboxed=False))
    assert r["ok"] is False and "built-in" in (r["error"] or "")
    # authoring a brand-new tool is the agent's job, not the editor's
    r = asyncio.run(e.tool_save("brand_new_tool", "d", {}, _GOOD, sandboxed=False))
    assert r["ok"] is False and "not a created tool" in (r["error"] or "")
    assert not (tmp_path / "created_tools" / "brand_new_tool.json").exists()


def test_tool_saving_is_off_when_tool_creation_is_off(tmp_path):
    e = _engine(tmp_path)      # enable_tool_creation defaults off
    asyncio.run(_seed_tool(e))
    r = asyncio.run(e.tool_save("greet", "changed", {}, _GOOD))
    assert r["ok"] is False and "ENABLE_TOOL_CREATION" in r["error"]
    assert json.loads((tmp_path / "created_tools" / "greet.json").read_text())["description"] == "says hi"


# ---- HTTP surface ----

def _client(engine):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)),
                             base_url="http://t")


async def test_endpoints_round_trip_over_http(tmp_path):
    e = _engine(tmp_path, enable_tool_creation=True)
    await _seed_tool(e)
    async with _client(e) as c:
        r = await c.get(f"/library/skill/{TARGET}")
        assert r.status_code == 200 and r.json()["origin"] == "shipped"
        version = r.json()["version"]

        r = await c.post("/library/skill/save",
                         json={"name": TARGET, "source": _edited_source(procedure="1. Via HTTP."),
                               "expected_version": version})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert (await c.get(f"/library/skill/{TARGET}")).json()["origin"] == "override"

        r = await c.post("/library/skill/revert", json={"name": TARGET})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert (await c.get(f"/library/skill/{TARGET}")).json()["origin"] == "shipped"

        # an unloadable save is a 200 {"ok": false}, not a 500 — the editor shows it inline
        r = await c.post("/library/skill/save", json={"name": TARGET, "source": "junk"})
        assert r.status_code == 200 and r.json()["ok"] is False

        assert (await c.get("/library/skill/no_such_skill")).status_code == 404
        assert (await c.get("/library/tool/calculator")).status_code == 404   # not a created tool

        assert (await c.get("/library/tool/greet")).json()["description"] == "says hi"
        new_code = "def run(args):\n    return 'v2 result'\n"
        r = await c.post("/library/tool/save",
                         json={"name": "greet", "description": "says hi v2", "parameters": {},
                               "code": new_code, "sandboxed": False})
        assert r.status_code == 200 and r.json()["ok"] is True
        got = (await c.get("/library/tool/greet")).json()
        assert got["code"] == new_code and got["description"] == "says hi v2"

        # a name is required on every write
        for path in ("/library/skill/save", "/library/skill/revert", "/library/tool/save"):
            assert (await c.post(path, json={})).status_code == 400


async def test_every_editor_endpoint_401s_without_the_admin_token(tmp_path):
    e = _engine(tmp_path, admin_token="s3cret", enable_tool_creation=True)
    async with _client(e) as c:
        gets = [f"/library/skill/{TARGET}", "/library/tool/greet"]
        posts = ["/library/skill/save", "/library/skill/revert", "/library/tool/save"]
        for p in gets:
            assert (await c.get(p)).status_code == 401, p
        for p in posts:
            assert (await c.post(p, json={"name": TARGET})).status_code == 401, p
        # with the token the gate opens (the skill read is the cheap proof)
        r = await c.get(f"/library/skill/{TARGET}", headers={"X-Admin-Token": "s3cret"})
        assert r.status_code == 200
    # and nothing was written by the rejected calls
    assert not (tmp_path / "created_skills" / f"{TARGET}.md").exists()
