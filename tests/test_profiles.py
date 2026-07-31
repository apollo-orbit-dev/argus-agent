"""Agent profiles (argus-cd8) — the spec's nine tests, plus the traps around them.

The load-bearing one is `test_unknown_tool_resolves_to_ask_*`: a profile is a SNAPSHOT, so a tool
added after it was written has no entry in its matrix, and the absent case must resolve to `ask` —
never `allow`, and never silently inherited from the global store. Everything else here exists to
keep that rule honest under the surfaces that could quietly bypass it (the registry's deny filter,
the approval gate, skill selection, migration).
"""
import pytest

from config import Config
from engine.approvals.types import TurnPaused
from engine.engine import Engine
from engine.profiles.store import Profile, ProfilePolicy, ProfileStore, widened_tools
from engine.protocol import ModelResponse
from engine.skills.base import Skill


def _engine(tmp_path, **ov):
    ov.setdefault("model_base_url", "http://x/v1")
    ov.setdefault("model_name", "m")
    ov.setdefault("telegram_bot_token", "")
    return Engine(Config(**ov), data_dir=str(tmp_path))


class _CaptureModel:
    """Records the system prompt it was sent; replies with a fixed final answer."""

    def __init__(self):
        self.last_system = ""

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None,
                   reasoning=None):
        self.last_system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        return ModelResponse(content="ok", finish_reason="stop")


def _capture(e) -> _CaptureModel:
    mc = _CaptureModel()
    e._model_client = lambda: mc
    return mc


def _skill(name, trigger="banana protocol"):
    return Skill(name=name, description=f"{name} description", tools=[],
                 procedure=f"PROCEDURE-MARKER-{name.upper()}: do the thing.",
                 triggers=[trigger])


# ------------------------------------------------------------------ 1. round-trip


def test_profile_round_trips_to_the_same_config(tmp_path):
    base = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="")
    path = str(tmp_path / "profiles.json")
    store = ProfileStore(path)
    prof = Profile(name="Research", description="reading mode",
                   soul="You are terse.", system_prompt="Operational text.",
                   flags={"enable_observer": False, "skill_selection_mode": "explicit",
                          "adaptive_thinking": True, "tool_disclosure_mode": "keyword"},
                   tools={"calculator": "allow", "exec_python": "deny"},
                   skills={"research": False}, rules={"abc123": False},
                   model_roles={"chat": "big-model"})
    store.create(prof)
    before = prof.to_config(base)

    reloaded = ProfileStore(path).get("Research")          # a fresh read of the same file
    assert reloaded is not None
    assert reloaded.to_json() == prof.to_json()
    after = reloaded.to_config(base)
    assert after.model_dump() == before.model_dump()       # the SAME config object the engine uses
    assert after.skill_selection_mode == "explicit"
    assert after.enable_observer is False
    assert after.adaptive_thinking is True
    # ...and only the governed fields moved: everything else is still the base config's.
    assert after.model_name == base.model_name
    assert after.max_steps == base.max_steps


def test_profile_file_carries_a_schema_version(tmp_path):
    path = str(tmp_path / "profiles.json")
    ProfileStore(path).create(Profile(name="A"))
    import json
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["schema"] == 1
    assert data["active_profile"] == "A"


# ------------------------------------------------- 2. THE STALENESS RULE (security-critical)


def test_unknown_tool_resolves_to_ask_not_allow_and_not_the_global_value(tmp_path):
    """A tool present in the registry but ABSENT from a profile's matrix resolves to `ask`."""
    e = _engine(tmp_path)
    prof = e.profile_for("")
    tool = e.registry.names()[0]

    # Written into the profile when it was created (migration snapshot) — remove it to model a
    # profile authored before that tool existed.
    prof.tools.pop(tool, None)
    e.profiles.save_profile(prof)

    for global_state in ("allow", "deny", "ask"):
        e.permissions.set(tool, global_state)
        assert prof.permission(tool) == "ask", "an unknown tool must be Ask"
        assert prof.permission(tool) != "allow"
        if global_state != "ask":
            # ...and specifically NOT whatever the global store says.
            assert prof.permission(tool) != e.permissions.get(tool)
    # A tool that has never been heard of anywhere is Ask too, not Allow.
    assert prof.permission("a_tool_invented_next_year") == "ask"
    # dep-install is NOT a tool and is never profile-owned.
    from engine.profiles.store import NON_TOOL_KEYS
    assert "dep-install" in NON_TOOL_KEYS
    assert ProfilePolicy(e.profiles, prof.name, e.permissions).get("dep-install") == \
        e.permissions.get("dep-install")


def test_unknown_tool_is_still_advertised_because_ask_is_visible(tmp_path):
    """`ask` means the tool IS offered — that is what keeps a newly added tool discoverable in
    profiles written before it. Only `deny` disappears from the catalog (argus-52n)."""
    e = _engine(tmp_path)
    prof = e.profile_for("")
    tool = e.registry.names()[0]
    prof.tools.pop(tool, None)
    e.profiles.save_profile(prof)
    assert tool in {f["function"]["name"] for f in e.registry.openai_schema()}
    assert tool in e.registry.text_schema()
    # ...whereas a profile-denied tool is not advertised at all.
    prof.tools[tool] = "deny"
    e.profiles.save_profile(prof)
    assert tool not in {f["function"]["name"] for f in e.registry.openai_schema()}
    assert e.registry.get(tool) is not None            # still dispatchable, still gated


async def test_the_gate_asks_for_an_unknown_tool_instead_of_auto_approving(tmp_path):
    """Visibility is not the security boundary — the GATE has to honour the staleness rule too.
    `ask` + a non-interactive origin pauses the turn; `allow` would have returned auto-approved."""
    e = _engine(tmp_path)
    prof = e.profile_for("")
    prof.tools.pop("calculator", None)
    e.profiles.save_profile(prof)
    e.permissions.set("calculator", "allow")           # the global value must NOT be inherited
    with pytest.raises(TurnPaused):
        await e.approvals.gate("calculator", "t1", "sess", "run1", "run calculator?", "api")


async def test_always_allow_pins_into_the_profile_not_the_global_store(tmp_path):
    """'Prompted once, then pin it into the profile' — the other half of the staleness rule."""
    e = _engine(tmp_path)
    prof = e.profile_for("")
    prof.tools.pop("calculator", None)
    e.profiles.save_profile(prof)
    try:
        await e.approvals.gate("calculator", "t1", "sess", "run1", "run calculator?", "api")
    except TurnPaused as tp:
        req_id = tp.req_id
    assert e.approvals_decide(req_id, "always_allow") in ("live", "deferred")
    assert e.profile_for("sess").permission("calculator") == "allow"
    assert "calculator" not in e.permissions.states_map       # global store untouched


def test_stale_tools_are_reported_for_the_dashboard(tmp_path):
    e = _engine(tmp_path)
    prof = e.profile_for("")
    tool = e.registry.names()[0]
    prof.tools.pop(tool, None)
    e.profiles.save_profile(prof)
    row = next(r for r in e.profiles_overview()["profiles"] if r["name"] == prof.name)
    assert tool in row["stale_tools"] and row["stale_count"] >= 1
    # the permission list marks it as running on a default rather than a stored choice
    row2 = next(r for r in e.permissions_list() if r["key"] == tool)
    assert row2["state"] == "ask" and row2["is_default"] is True


# --------------------------------------------------------------- 3. activation is visible


async def test_activation_emits_an_event_naming_every_widened_tool(tmp_path):
    e = _engine(tmp_path)
    seen = []
    orig = e.events.publish

    async def capture(ev):
        seen.append(ev)
        await orig(ev)

    e.events.publish = capture

    narrow = e.profile_for("")
    narrow.tools["calculator"] = "deny"
    narrow.tools["get_current_time"] = "ask"
    e.profiles.save_profile(narrow)
    wide = Profile.from_json(narrow.to_json())
    wide.name = "Wide"
    wide.tools["calculator"] = "allow"          # deny -> allow  (WIDER)
    wide.tools["get_current_time"] = "deny"     # ask  -> deny   (narrower)
    e.profiles.create(wide)

    res = await e.activate_profile("Wide", session_id="s1")
    ev = next(x for x in seen if x.kind == "profile")
    assert ev.data["profile"] == "Wide" and ev.data["previous"] == narrow.name
    widened = {w["tool"]: (w["from"], w["to"]) for w in ev.data["widened"]}
    assert widened["calculator"] == ("deny", "allow")
    assert "get_current_time" not in widened     # narrowing needs no announcement
    assert res["widened"] == ev.data["widened"]


async def test_activating_a_narrower_profile_reports_nothing_widened(tmp_path):
    e = _engine(tmp_path)
    wide = e.profile_for("")
    wide.tools["calculator"] = "allow"
    e.profiles.save_profile(wide)
    narrow = Profile.from_json(wide.to_json())
    narrow.name = "Narrow"
    narrow.tools["calculator"] = "deny"
    e.profiles.create(narrow)
    res = await e.activate_profile("Narrow", session_id="s1")
    assert res["widened"] == []


def test_widening_counts_a_stale_profiles_ask_default(tmp_path):
    """An unconfigured tool is `ask`, so moving to a profile that allows it IS a widening."""
    prev = Profile(name="A", tools={})                     # never heard of it -> ask
    new = Profile(name="B", tools={"web_search": "allow"})
    assert widened_tools(prev, new, ["web_search"]) == [
        {"tool": "web_search", "from": "ask", "to": "allow"}]


# ------------------------------------------------------------------- 4. migration


def test_migration_creates_default_and_leaves_the_resolved_config_identical(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                 enable_observer=False, skill_selection_mode="explicit", max_steps=9)
    before = cfg.model_dump()
    assert not (tmp_path / "profiles.json").exists()
    e = Engine(cfg, data_dir=str(tmp_path))
    assert (tmp_path / "profiles.json").exists()
    assert e.profiles.active_profile == "Default"
    assert e.profile_for("brand-new-session").name == "Default"
    # every field, not just the governed ones
    assert e.config_for("any-session").model_dump() == before
    assert e.config_for("").model_dump() == e._config.model_dump()
    # the snapshot pinned today's permission states for every tool that exists today
    prof = e.profile_for("")
    for name in e.registry.names():
        assert prof.permission(name) == e.permissions.get(name)
    # ...and the persona/system prompt came across unchanged
    assert prof.soul == e.soul and prof.system_prompt == e.system_prompt
    # there is no "no profile" state after migration
    assert e.profiles.list() and e.profiles.default() is not None


def test_migration_is_idempotent_across_restarts(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="")
    e1 = Engine(cfg, data_dir=str(tmp_path))
    prof = e1.profile_for("")
    prof.description = "edited by hand"
    prof.tools["calculator"] = "deny"
    e1.profiles.save_profile(prof)
    e2 = Engine(cfg, data_dir=str(tmp_path))              # "restart"
    assert e2.profiles.names() == ["Default"]
    assert e2.profile_for("").description == "edited by hand"
    assert e2.profile_for("").permission("calculator") == "deny"


# --------------------------------------------------------------- 5. delete refusals


def test_deleting_the_active_or_the_last_profile_is_refused(tmp_path):
    e = _engine(tmp_path)
    with pytest.raises(ValueError):                      # last remaining
        e.profile_delete("Default")
    e.profile_create("Second", source="Default")
    with pytest.raises(ValueError):                      # active (the global default)
        e.profile_delete("Default")
    assert "Default" in e.profiles.names()
    # a profile in use by a session is refused too
    e.profiles.bind("s1", "Second")
    with pytest.raises(ValueError):
        e.profile_delete("Second")
    e.profiles.unbind("s1")
    assert e.profile_delete("Second") == {"deleted": "Second"}
    assert e.profiles.names() == ["Default"]


def test_duplicate_rename_and_create(tmp_path):
    e = _engine(tmp_path)
    e.profile_create("Coding", source="Default", description="terse")
    assert e.profile_for("").to_json()["tools"] == e.profiles.get("Coding").tools
    assert e.profiles.get("Coding").description == "terse"
    with pytest.raises(ValueError):
        e.profile_create("Coding", source="Default")     # name collision
    e.profile_rename("Coding", "Code")
    assert "Code" in e.profiles.names() and "Coding" not in e.profiles.names()


# ------------------------------------------------------- 6. memory is NOT scoped


async def test_memory_is_global_and_shared_across_profiles(tmp_path):
    """Memory is explicitly NOT part of a profile."""
    e = _engine(tmp_path)
    e.profile_create("B", source="Default")
    await e.activate_profile("B", session_id="s2")
    assert e.profile_for("s1").name == "Default" and e.profile_for("s2").name == "B"

    await e.memory.remember(e._memory_key("s1"), "The owner's cat is called Mango.")
    got = await e.memory.recall(e._memory_key("s2"), "cat")
    assert any("Mango" in m["text"] for m in got)
    # no profile field carries memory
    assert "memory" not in e.profile_for("s2").to_json()


# ---------------------------------------- 7. a swap changes the NEXT turn, not history


async def test_switching_profiles_changes_the_next_turns_config_without_touching_history(tmp_path):
    e = _engine(tmp_path, enable_observer=True)
    _capture(e)
    await e.run_task("s1", "hello")
    history_before = list(e.store.conversation("s1"))
    assert e.config_for("s1").enable_observer is True

    other = Profile.from_json(e.profile_for("").to_json())
    other.name = "Quiet"
    other.flags["enable_observer"] = False
    other.flags["skill_selection_mode"] = "explicit"
    e.profiles.create(other)
    await e.activate_profile("Quiet", session_id="s1")

    assert e.config_for("s1").enable_observer is False          # next turn
    assert e.config_for("s1").skill_selection_mode == "explicit"
    assert e.config_for("s2").enable_observer is True           # other sessions unaffected
    assert list(e.store.conversation("s1")) == history_before   # history untouched
    await e.run_task("s1", "hello again")
    assert list(e.store.conversation("s1"))[:len(history_before)] == history_before


# --------------------------------------------------- 8 + 9. skill scoping


@pytest.mark.parametrize("mode", ["explicit", "model_driven", "hybrid"])
async def test_a_hidden_skill_is_not_offered_in_any_selection_mode(tmp_path, mode):
    """Including an explicit BY-NAME request — the one that could resurrect it."""
    e = _engine(tmp_path, skill_selection_mode=mode)
    mc = _capture(e)
    e.skill_registry.register(_skill("banana_protocol"))

    # control: visible, and an explicit request activates it
    await e.run_task("s1", "run the banana protocol", requested_skill="banana_protocol")
    assert "PROCEDURE-MARKER-BANANA_PROTOCOL" in mc.last_system or \
        "banana_protocol" in mc.last_system

    prof = e.profile_for("")
    prof.skills["banana_protocol"] = False
    e.profiles.save_profile(prof)

    await e.run_task("s2", "run the banana protocol", requested_skill="banana_protocol")
    assert "PROCEDURE-MARKER-BANANA_PROTOCOL" not in mc.last_system
    assert "banana_protocol" not in mc.last_system
    # trigger-phrase activation can't resurrect it either
    await e.run_task("s3", "please do the banana protocol now")
    assert "PROCEDURE-MARKER-BANANA_PROTOCOL" not in mc.last_system


def test_a_hidden_skill_is_absent_from_the_scoped_registry_every_selector_sees(tmp_path):
    e = _engine(tmp_path)
    e.skill_registry.register(_skill("banana_protocol"))
    prof = e.profile_for("")
    prof.skills["banana_protocol"] = False
    e.profiles.save_profile(prof)
    scoped = e.skill_registry_for(prof)
    assert scoped.get("banana_protocol") is None
    assert "banana_protocol" not in [s.name for s in scoped.list()]
    assert e.skill_registry.get("banana_protocol") is not None   # still INSTALLED, just not offered


async def test_a_skill_added_after_the_profile_was_written_is_visible(tmp_path):
    """Unknown-skill default, mirroring the unknown-tool rule but one notch weaker: a skill cannot
    do anything on its own, so an unknown one must not be undiscoverable."""
    e = _engine(tmp_path, skill_selection_mode="explicit")
    mc = _capture(e)
    prof = e.profile_for("")
    prof.skills = {"some_old_skill": True, "another": False}     # written before ours existed
    e.profiles.save_profile(prof)

    e.skill_registry.register(_skill("brand_new_skill"))
    assert e.profile_for("").skill_visible("brand_new_skill") is True
    assert e.skill_registry_for(e.profile_for("")).get("brand_new_skill") is not None
    await e.run_task("s1", "do it", requested_skill="brand_new_skill")
    assert "PROCEDURE-MARKER-BRAND_NEW_SKILL" in mc.last_system


# ------------------------------------------------- profile-governed prompt + permissions in a turn


async def test_a_turn_runs_under_the_profiles_persona_and_permission_matrix(tmp_path):
    e = _engine(tmp_path)
    mc = _capture(e)
    other = Profile.from_json(e.profile_for("").to_json())
    other.name = "Pirate"
    other.soul = "SOUL-MARKER-PIRATE"
    other.system_prompt = "SYSPROMPT-MARKER-PIRATE"
    other.tools["calculator"] = "deny"
    e.profiles.create(other)
    await e.activate_profile("Pirate", session_id="s1")

    await e.run_task("s1", "hello")
    assert "SOUL-MARKER-PIRATE" in mc.last_system
    assert "SYSPROMPT-MARKER-PIRATE" in mc.last_system

    await e.run_task("s2", "hello")                    # still on Default
    assert "SOUL-MARKER-PIRATE" not in mc.last_system


async def test_a_denied_tool_is_not_advertised_for_the_session_bound_to_that_profile(tmp_path):
    """The profile's matrix reaches the per-run catalog — and only that session's."""
    seen: dict = {}

    e = _engine(tmp_path)

    class _ToolSpy:
        async def chat(self, messages, tools=None, **kw):
            seen[kw.get("_", "last")] = [t["function"]["name"] for t in (tools or [])]
            return ModelResponse(content="ok", finish_reason="stop")

    e._model_client = lambda: _ToolSpy()
    other = Profile.from_json(e.profile_for("").to_json())
    other.name = "NoMath"
    other.tools["calculator"] = "deny"
    e.profiles.create(other)
    await e.activate_profile("NoMath", session_id="s1")

    await e.run_task("s1", "hello")
    assert "calculator" not in seen["last"]
    await e.run_task("s2", "hello")
    assert "calculator" in seen["last"]


def test_rules_are_scoped_by_the_profile_but_a_new_rule_still_applies(tmp_path):
    e = _engine(tmp_path)
    r1 = e.rules_add("Never use emoji.")
    prof = e.profile_for("")
    prof.rules[r1["id"]] = False
    e.profiles.save_profile(prof)
    block = e._compose_rules_block(config=e._config, profile=prof)
    assert "Never use emoji." not in block
    r2 = e.rules_add("Always answer in metric units.")     # created AFTER the profile
    block = e._compose_rules_block(config=e._config, profile=prof)
    assert "Always answer in metric units." in block
    assert r2["id"] not in prof.rules
