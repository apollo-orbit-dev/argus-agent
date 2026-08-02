"""The profile EDITING surface (argus-m9n): the chip switcher, skill visibility, role bindings and
the Developer page's "which profile am I editing".

Three of the eight cases below are about a browser control, which no test here can click. They are
split the way the risk is: the BEHAVIOUR each control triggers is tested against the real HTTP
surface (that is where a bug would actually bite), and the wiring — that the control exists, is fed
from the same payload, and routes through the same call — is asserted statically against
dashboard/app.js + index.html. What is NOT covered anywhere: rendering, layout, focus and anything
that needs a DOM. Those are unverified.

The load-bearing one is `test_2_*`: switching from the chip must produce the SAME widened-permission
announcement Settings activation produces. Activation is allowed not to block only because it is
announced; a second, quieter path to activation would defeat that, so the chip is required to reach
activation through the one function that announces.
"""
import re
from pathlib import Path

import httpx
import pytest

from backend.app import create_app
from config import Config
from engine.engine import Engine
from engine.profiles.store import PROFILE_BOUND_ROLES
from engine.protocol import ModelResponse
from engine.skills.base import Skill

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
APP_JS = (DASHBOARD / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (DASHBOARD / "index.html").read_text(encoding="utf-8")


def _engine(tmp_path, **ov):
    ov.setdefault("model_base_url", "http://x/v1")
    ov.setdefault("model_name", "m")
    ov.setdefault("telegram_bot_token", "")
    return Engine(Config(**ov), data_dir=str(tmp_path), env_path=str(tmp_path / ".env"))


@pytest.fixture
def client(tmp_path):
    eng = _engine(tmp_path)
    app = create_app(eng)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), eng


class _CaptureModel:
    def __init__(self):
        self.last_system = ""

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None,
                   reasoning=None):
        self.last_system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        return ModelResponse(content="ok", finish_reason="stop")


def _skill(name, trigger="banana protocol"):
    return Skill(name=name, description=f"{name} description", tools=[],
                 procedure=f"PROCEDURE-MARKER-{name.upper()}: do the thing.", triggers=[trigger])


# --------------------------------------------------------------- 1. the chip is a switcher


async def test_1_chip_lists_profiles_marks_this_session_and_rebinds_only_it(client):
    """The chip menu is built from GET /profiles: it lists every profile, `session_profile` is the
    one it ticks, and choosing another rebinds THIS session — `active_profile` (the default for new
    sessions) and every other session's binding are untouched."""
    c, eng = client
    async with c:
        await c.post("/profiles", json={"name": "Research", "source": "Default"})
        await c.post("/profiles", json={"name": "Coding", "source": "Default"})
        await c.post("/profiles/Coding/activate", json={"session_id": "other"})

        d = (await c.get("/profiles?session_id=s1")).json()
        assert sorted(p["name"] for p in d["profiles"]) == ["Coding", "Default", "Research"]
        assert d["session_profile"] == "Default"          # what the chip ticks for s1
        assert d["active_profile"] == "Default"

        r = await c.post("/profiles/Research/activate", json={"session_id": "s1"})
        assert r.status_code == 200 and r.json()["scope"] == "session"

        assert (await c.get("/profiles?session_id=s1")).json()["session_profile"] == "Research"
        assert (await c.get("/profiles?session_id=other")).json()["session_profile"] == "Coding"
        assert (await c.get("/profiles")).json()["active_profile"] == "Default"
        assert eng.profiles.active_profile == "Default"   # the global default never moved
        assert eng.profiles.sessions == {"other": "Coding", "s1": "Research"}


def test_1_chip_markup_and_handler_bind_the_session_only():
    """Statically: the chip is a real control, its menu is rendered from the /profiles rows with the
    session's marked, and selecting one posts a session_id — never the global-default activation
    (which is `{}` and stays a Settings action)."""
    assert 'id="chipProfileBtn"' in INDEX_HTML and 'id="chipProfileMenu"' in INDEX_HTML
    assert 'aria-haspopup="menu"' in INDEX_HTML
    menu = re.search(r"function renderProfileMenu\(\)\{(.*?)\n  \}", APP_JS, re.S).group(1)
    assert "data-chip-profile=" in menu
    assert "d.session_profile" in menu and 'aria-checked=' in menu
    # the chip's activation goes through activateProfileHere, which always sends session_id
    assert "activateProfileHere(name)" in APP_JS
    here = re.search(r"function activateProfileHere\(name\)\{(.*?)\n  \}", APP_JS, re.S).group(1)
    assert "session_id: SESSION" in here
    assert "/activate" in here
    # ...and Settings' "Use here" now calls the SAME function, so there is one code path, not two
    assert "data-profile-activate'))){\n      activateProfileHere(name);" in APP_JS


# ------------------------------------------- 2. the chip makes the SAME announcement


async def test_2_switching_announces_widened_permissions_and_says_so_when_nothing_widened(client):
    """The response the chip acts on carries `widened` in both directions — a populated list when a
    tool got wider, and an EMPTY list (not an absent field) when nothing did, which is what lets the
    dashboard say "nothing widened" out loud instead of staying silent."""
    c, eng = client
    async with c:
        narrow = eng.profile_for("")
        narrow.tools["calculator"] = "deny"
        narrow.tools["get_current_time"] = "ask"
        eng.profiles.save_profile(narrow)
        await c.post("/profiles", json={"name": "Wide", "source": "Default"})
        await c.put("/profiles/Wide", json={"tools": dict(narrow.tools,
                                                          calculator="allow",
                                                          get_current_time="deny")})

        d = (await c.post("/profiles/Wide/activate", json={"session_id": "s1"})).json()
        widened = {w["tool"]: (w["from"], w["to"]) for w in d["widened"]}
        assert widened["calculator"] == ("deny", "allow")
        assert "get_current_time" not in widened            # narrowing needs no announcement
        assert d["previous"] == "Default"

        # a PURE narrowing: the field is present and EMPTY, so the dashboard has something to say
        # "nothing widened" about rather than an absent field it could only stay silent on.
        await c.post("/profiles", json={"name": "Narrower", "source": "Wide"})
        await c.put("/profiles/Narrower", json={"tools": dict(narrow.tools,
                                                              calculator="deny",
                                                              get_current_time="deny")})
        back = (await c.post("/profiles/Narrower/activate", json={"session_id": "s1"})).json()
        assert isinstance(back["widened"], list) and back["widened"] == []


def test_2_one_announcement_path_covers_both_the_chip_and_settings():
    """The announcement lives in profileAction(), which both surfaces call, and it fires on the
    PRESENCE of `widened` rather than its length — so the empty case is announced, not swallowed."""
    fn = re.search(r"async function profileAction\(url, body, okMsg\)\{(.*?)\n  \}", APP_JS, re.S).group(1)
    assert "Array.isArray(d.widened)" in fn
    assert "Wider permissions:" in fn and "Nothing widened" in fn
    # the chip does not roll its own activation fetch
    chip = re.search(r"\$\('chipProfileMenu'\)\.addEventListener\('click'.*?\n  \}\);", APP_JS, re.S).group(0)
    assert "fetch(" not in chip
    assert "activateProfileHere(name)" in chip


# ----------------------------------------------------- 3 + 4. skill visibility, editable


async def test_3_hiding_a_skill_in_the_editor_persists_and_hides_it_from_every_mode(client, tmp_path):
    """The editor writes only the HIDDEN skills (absent = visible). After that PUT the skill is
    unavailable to a session bound to that profile in explicit, model_driven and hybrid alike —
    including an explicit by-name request, the one that could resurrect it."""
    c, eng = client
    async with c:
        eng.skill_registry.register(_skill("banana_protocol"))
        d = (await c.get("/profiles/Default")).json()
        assert "banana_protocol" in d["all_skills"]        # the editor renders this list
        assert d["skills"] == {}                           # nothing hidden yet

        r = await c.put("/profiles/Default", json={"skills": {"banana_protocol": False}})
        assert r.status_code == 200 and r.json()["skills"] == {"banana_protocol": False}
        # persisted, not just in memory
        from engine.profiles.store import ProfileStore
        assert ProfileStore(str(tmp_path / "profiles.json")).get("Default").skills == \
            {"banana_protocol": False}
        # and reported back to the profile row
        row = next(p for p in (await c.get("/profiles")).json()["profiles"] if p["name"] == "Default")
        assert row["hidden_skills"] == ["banana_protocol"]

    for mode in ("explicit", "model_driven", "hybrid"):
        eng._config = eng._config.patch({"skill_selection_mode": mode})
        prof = eng.profile_for("")
        prof.flags["skill_selection_mode"] = mode
        eng.profiles.save_profile(prof)
        mc = _CaptureModel()
        eng._model_client = lambda: mc
        await eng.run_task(f"s-{mode}", "run the banana protocol",
                           requested_skill="banana_protocol")
        assert "PROCEDURE-MARKER-BANANA_PROTOCOL" not in mc.last_system, mode
        assert "banana_protocol" not in mc.last_system, mode


async def test_4_a_skill_registered_after_the_save_is_visible_to_that_profile(client):
    """The editor stores only hidden skills, so a skill that did not exist when the profile was
    saved has no entry — and an absent entry is VISIBLE."""
    c, eng = client
    async with c:
        eng.skill_registry.register(_skill("banana_protocol"))
        await c.put("/profiles/Default", json={"skills": {"banana_protocol": False}})

        eng.skill_registry.register(_skill("brand_new_skill", trigger="new thing"))
        prof = eng.profile_for("")
        assert "brand_new_skill" not in prof.skills
        assert prof.skill_visible("brand_new_skill") is True
        assert eng.skill_registry_for(prof).get("brand_new_skill") is not None

        eng._config = eng._config.patch({"skill_selection_mode": "explicit"})
        mc = _CaptureModel()
        eng._model_client = lambda: mc
        await eng.run_task("s1", "do it", requested_skill="brand_new_skill")
        assert "PROCEDURE-MARKER-BRAND_NEW_SKILL" in mc.last_system


def test_3_the_editor_renders_a_control_per_skill_and_saves_only_the_hidden_ones():
    """Statically: the skill list is in the PROFILE editor (which works without activating the
    profile), and the save writes `false` entries only — the shape the unknown-skill rule depends
    on. A save that wrote `true` for every visible skill would pin today's list into the profile and
    make tomorrow's skill's visibility a matter of luck."""
    fn = re.search(r"function profileSkillsHtml\(p\)\{(.*?)\n  \}", APP_JS, re.S).group(1)
    assert "p.all_skills" in fn and "data-pf-skill=" in fn and "visible" in fn
    save = re.search(r"async function saveProfile\(name\)\{(.*?)\n  \}", APP_JS, re.S).group(1)
    assert "data-pf-skill" in save and "body.skills = skills" in save
    assert "if (!cb.checked) skills[cb.getAttribute('data-pf-skill')] = false;" in save
    # never writes a positive visibility entry — that is what keeps "absent = visible" true
    assert re.search(r"skills\[[^\]]*\]\s*=\s*true", save) is None


# ------------------------------------------- 5 + 6. model role bindings follow the profile


def _add_conn(eng, label, model_name):
    return eng.model_preset_add(model_name, base_url="http://conn/v1", label=label)


class _RecordingClient:
    """Stands in for ModelClient so a turn records WHICH model it was built against."""
    used: list = []

    def __init__(self, base_url, model, api_key="dummy", **kw):
        self.base_url = base_url
        self.model = model

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None,
                   reasoning=None):
        _RecordingClient.used.append(self.model)
        return ModelResponse(content="ok", finish_reason="stop")


async def test_5_a_profiles_chat_binding_drives_that_sessions_turn_only(tmp_path, monkeypatch):
    """A profile selects a role BINDING (a connection label), never a key. A session bound to that
    profile runs on it; a session on another profile is unaffected."""
    eng = _engine(tmp_path)
    _add_conn(eng, "big", "big-model")
    _add_conn(eng, "small", "small-model")
    eng.set_role("chat", "small", persist=False)          # the GLOBAL binding

    eng.profile_create("Heavy", source="Default")
    eng.profile_save("Heavy", {"model_roles": {"chat": "big"}})
    await eng.activate_profile("Heavy", session_id="s1")

    monkeypatch.setattr("engine.engine.ModelClient", _RecordingClient)
    _RecordingClient.used = []
    await eng.run_task("s1", "hello")
    assert _RecordingClient.used[-1] == "big-model"       # the profile's binding

    _RecordingClient.used = []
    await eng.run_task("s2", "hello")                     # still on Default
    assert _RecordingClient.used[-1] == "small-model"     # the global binding

    # ...and only a LABEL was ever stored — no credential crossed into the profile
    assert eng.profiles.get("Heavy").model_roles == {"chat": "big"}
    assert "api_key" not in str(eng.profiles.get("Heavy").to_json())


async def test_6_inherit_global_clears_the_binding_and_falls_back(tmp_path, monkeypatch):
    """"Inherit global" is expressed by REMOVING the key, which is what the editor's empty select
    sends. The turn then resolves through the global role again."""
    eng = _engine(tmp_path)
    _add_conn(eng, "big", "big-model")
    _add_conn(eng, "small", "small-model")
    eng.set_role("chat", "small", persist=False)
    eng.profile_create("Heavy", source="Default")
    eng.profile_save("Heavy", {"model_roles": {"chat": "big"}})
    await eng.activate_profile("Heavy", session_id="s1")

    monkeypatch.setattr("engine.engine.ModelClient", _RecordingClient)
    _RecordingClient.used = []
    await eng.run_task("s1", "hello")
    assert _RecordingClient.used[-1] == "big-model"

    eng.profile_save("Heavy", {"model_roles": {}})        # "inherit global"
    assert eng.profiles.get("Heavy").model_roles == {}
    assert eng._profile_chat_client(eng.profile_for("s1")) is None
    _RecordingClient.used = []
    await eng.run_task("s1", "hello")
    assert _RecordingClient.used[-1] == "small-model"

    # a later global change reaches the inheriting profile, which is the point of inheriting
    eng.set_role("chat", "big", persist=False)
    _RecordingClient.used = []
    await eng.run_task("s1", "hello")
    assert _RecordingClient.used[-1] == "big-model"


async def test_7_a_connection_is_global_and_visible_from_every_profile(client):
    """Editing a CONNECTION (url / key / model) is infrastructure: it is global, and every profile
    sees the edit — including one that pins that connection for its chat role."""
    c, eng = client
    async with c:
        r = await c.post("/model/presets", json={"label": "shared", "model_name": "v1",
                                                 "base_url": "http://a/v1", "api_key": "sekret"})
        assert r.status_code == 200
        await c.post("/profiles", json={"name": "A", "source": "Default"})
        await c.post("/profiles", json={"name": "B", "source": "Default"})
        await c.put("/profiles/A", json={"model_roles": {"chat": "shared"}})

        # one edit, through the global surface
        await c.post("/model/presets", json={"label": "shared", "model_name": "v2",
                                             "base_url": "http://b/v1"})
        conns = {p["label"]: p for p in (await c.get("/model/roles")).json()["connections"]}
        assert conns["shared"]["model_name"] == "v2" and conns["shared"]["base_url"] == "http://b/v1"

        # both profiles read the SAME connection record; a profile stores only a LABEL, never a key
        for name in ("A", "B"):
            d = (await c.get(f"/profiles/{name}")).json()
            assert "sekret" not in str(d)
            assert all(isinstance(v, str) for v in d["model_roles"].values())
            assert "base_url" not in str(d["model_roles"])
        assert eng.model_presets_store.resolve("shared")["model_name"] == "v2"
        # the profile that pins it picks up the edit without being touched itself
        assert eng.profiles.get("A").model_roles["chat"] == "shared"
        assert eng._profile_chat_client(eng.profiles.get("A")).model == "v2"
        assert eng._profile_chat_client(eng.profiles.get("A")).base_url == "http://b/v1"

        # ...and no per-profile connection surface exists to contradict that
        assert "/model/presets" not in re.search(
            r"function profileRolesHtml\(p\)\{(.*?)\n  \}", APP_JS, re.S).group(1)


def test_5_the_editor_only_offers_the_roles_a_profile_actually_overrides():
    """`chat` is the only capability a turn resolves through the profile. utility and embedding are
    global by design (background work is not session-scoped; memory/knowledge vectors are shared
    across profiles, so one embedding model has to write them all) — the editor shows them read-only
    rather than offering a binding that would silently do nothing."""
    assert PROFILE_BOUND_ROLES == ("chat",)
    fn = re.search(r"function profileRolesHtml\(p\)\{(.*?)\n  \}", APP_JS, re.S).group(1)
    assert "profile_bound_roles" in fn
    assert "data-pf-role=" in fn
    assert "inherit global" in fn
    assert "global — " in fn                       # the read-only row for the unbound capabilities
    save = re.search(r"async function saveProfile\(name\)\{(.*?)\n  \}", APP_JS, re.S).group(1)
    assert "if (s.value) roles[cap] = s.value; else delete roles[cap];" in save   # empty = inherit


async def test_a_global_role_change_reaches_the_default_profile_but_not_a_pinned_one(tmp_path,
                                                                                     monkeypatch):
    """The mismatch this bead was filed about, in its worst form. The migration snapshot pins `chat`
    to whatever connection was live at the time, and a profile's binding wins at turn time — so
    before the write-through, changing the model on the Models page moved the global role AND the
    config while every turn kept running the OLD connection. set_role now mirrors into the DEFAULT
    profile, exactly as the SOUL / system-prompt editors do, and deliberately leaves every other
    profile's pin alone (that is what the Models page's note is for)."""
    eng = _engine(tmp_path)
    assert eng.profile_for("").model_roles.get("chat") == "m"     # the boot snapshot
    _add_conn(eng, "newconn", "new-model")

    eng.set_role("chat", "newconn", persist=False)
    assert eng.model_presets_store.get_role("chat") == "newconn"
    assert eng.profile_for("").model_roles["chat"] == "newconn"   # the mirror
    assert eng._profile_chat_client(eng.profile_for("")) is None  # nothing to override any more

    monkeypatch.setattr("engine.engine.ModelClient", _RecordingClient)
    _RecordingClient.used = []
    await eng.run_task("s1", "hello")
    assert _RecordingClient.used[-1] == "new-model"

    # a non-default profile that pins its own binding is NOT rewritten by a global change
    eng.profile_create("Heavy", source="Default")
    eng.profile_save("Heavy", {"model_roles": {"chat": "m"}})
    eng.set_role("chat", "newconn", persist=False)
    assert eng.profiles.get("Heavy").model_roles == {"chat": "m"}
    await eng.activate_profile("Heavy", session_id="s2")
    _RecordingClient.used = []
    await eng.run_task("s2", "hello")
    assert _RecordingClient.used[-1] == "m"

    # clearing a role clears the mirror too, rather than leaving a stale pin behind
    eng.set_role("chat", None, persist=False)
    assert "chat" not in eng.profile_for("").model_roles


async def test_the_models_page_is_told_which_profile_is_live(client):
    """The judgement call, made non-silent: the Models page edits GLOBAL bindings, so it must name
    the live profile and any role that profile pins (which a change there will not reach)."""
    c, eng = client
    async with c:
        d = (await c.get("/profiles?session_id=s1")).json()
        assert d["profile_bound_roles"] == list(PROFILE_BOUND_ROLES)
        assert "global_roles" in d
        assert all("model_roles" in p for p in d["profiles"])
    assert 'id="rolesProfileNote"' in INDEX_HTML
    fn = re.search(r"function renderRolesProfileNote\(\)\{(.*?)\n  \}", APP_JS, re.S).group(1)
    assert "Live profile" in fn and "pins" in fn
    # a global role change while the live profile pins that role is called out rather than silent
    setrole = re.search(r"async function setRole\(cap, conn\)\{(.*?)\n  \}", APP_JS, re.S).group(1)
    assert "profilePinnedRoles()" in setrole and "pins " in setrole


# ------------------------------------- 8. the Developer page names the profile it edits


async def test_8_the_developer_page_names_the_profile_whose_permissions_it_edits(client):
    """The page reads and writes the matrix of the profile THIS SESSION runs under, and says which
    one that is. Without the session_id both reads and writes silently landed in the global default
    profile even while the header chip named another."""
    c, eng = client
    async with c:
        await c.post("/profiles", json={"name": "Locked", "source": "Default"})
        await c.post("/profiles/Locked/activate", json={"session_id": "dash"})

        # what the note renders from
        assert (await c.get("/profiles?session_id=dash")).json()["session_profile"] == "Locked"

        r = await c.post("/permissions/set",
                         json={"key": "calculator", "state": "deny", "session_id": "dash"})
        assert r.status_code == 200
        assert eng.profiles.get("Locked").permission("calculator") == "deny"
        assert eng.profiles.get("Default").permission("calculator") != "deny"
        rows = {p["key"]: p["state"]
                for p in (await c.get("/permissions?session_id=dash")).json()["permissions"]}
        assert rows["calculator"] == "deny"


def test_8_the_developer_page_markup_and_fetches_are_session_scoped():
    """Statically: the note exists next to the permission controls, and both the read and the write
    carry the session — a note naming one profile while the write lands in another would be worse
    than no note."""
    assert 'id="permProfileNote"' in INDEX_HTML
    # the note sits in the Tools card, above the per-tool controls
    tools_card = INDEX_HTML.split('<span class="card-title">Tools</span>', 1)[1].split("</div>\n          <div class=\"card\">")[0]
    assert 'id="permProfileNote"' in tools_card and 'id="toolsBuiltin"' in tools_card
    fn = re.search(r"function renderPermProfileNote\(\)\{(.*?)\n  \}", APP_JS, re.S).group(1)
    assert "Editing permissions for:" in fn
    assert "d.session_profile" in fn
    lib = re.search(r"async function loadLibrary\(\)\{(.*?)\n  \}\n", APP_JS, re.S).group(1)
    assert "'/permissions?session_id=' + encodeURIComponent(SESSION)" in lib
    assert "renderPermProfileNote()" in lib
    assert "state: state, session_id: SESSION" in APP_JS      # the write is scoped too
