"""A tool set to Deny is NOT ADVERTISED — it is absent from openai_schema() and text_schema().

The rule: effective permission `deny` => the tool is not in either catalog. `ask` and `allow` are
both visible (ask is the "propose it and I'll approve at call time" state — the whole point).

VISIBILITY IS NOT THE SECURITY BOUNDARY, and these tests are written so that a change which
"fixes" the catalog by weakening the gate fails: ApprovalBroker.gate() still refuses a denied tool
called out of conversation-history residue, with the same "Blocked by your policy" tool result.

Two composition traps with progressive tool disclosure are pinned below, because both fail
SILENTLY: DisclosedRegistry.openai_schema() builds a brand-new ToolRegistry, and find_tool /
register() exist specifically to widen the view.
"""
import json

from pydantic import BaseModel

from engine.approvals.broker import ApprovalBroker
from engine.approvals.policy import PermissionStore
from engine.approvals.store import ApprovalStore
from engine.events import EventBus
from engine.loop import LoopDeps, run_loop
from engine.modes.manual import ManualMode
from engine.modes.native import NativeMode
from engine.protocol import ModelResponse
from engine.state import SessionStore
from engine.tools.base import Tool, ToolRegistry
from engine.tools.disclosure import DisclosedRegistry, FindToolTool, select_visible

from tests.test_loop import FakeModel


def _tool(nm, desc="does a thing", param="x"):
    ns = {"name": nm, "description": desc,
          "Params": type("P", (BaseModel,), {"__annotations__": {param: str}, param: ""}),
          "run": lambda self, args: None}
    return type(nm.title().replace("_", ""), (Tool,), ns)()


def _policy(tmp_path, **states) -> PermissionStore:
    p = PermissionStore(str(tmp_path / "permissions.json"))
    for k, v in states.items():
        p.set(k, v)
    return p


def _reg(policy=None, *names) -> ToolRegistry:
    r = ToolRegistry(permissions=(policy.get if policy is not None else None))
    for n in names:
        r.register(_tool(n))
    return r


def _schema_names(r) -> set[str]:
    return {f["function"]["name"] for f in r.openai_schema()}


# --------------------------------------------------------------------- 1 + 2: the rule


def test_denied_tool_is_absent_from_both_schemas(tmp_path):
    p = _policy(tmp_path, web_search="deny")
    r = _reg(p, "web_search", "calculator")
    assert _schema_names(r) == {"calculator"}
    assert "web_search" not in r.text_schema()
    assert "calculator" in r.text_schema()
    # ...but it is still a real, dispatchable tool: only ADVERTISING is filtered.
    assert r.get("web_search") is not None
    assert "web_search" in r.names()
    assert r.validate("web_search", {"x": "1"}).ok


def test_ask_and_allow_are_both_advertised(tmp_path):
    p = _policy(tmp_path, exec_python="ask", calculator="allow", web_search="deny")
    r = _reg(p, "exec_python", "calculator", "web_search")
    assert _schema_names(r) == {"exec_python", "calculator"}
    txt = r.text_schema()
    assert "exec_python" in txt and "calculator" in txt and "web_search" not in txt


# ------------------------------------------------- 3: the gate is still the enforcement layer


class SideEffectTool(Tool):
    name = "side_effect"
    description = "records that it ran"

    class Params(BaseModel):
        pass

    def __init__(self, calls: list):
        self.calls = calls

    async def run(self, args: "SideEffectTool.Params") -> str:
        self.calls.append("ran")
        return "did the thing"


async def test_denied_tool_called_from_history_residue_is_still_blocked_by_the_gate(tmp_path):
    """The model names a tool that is NOT in its array this turn (it remembers it from earlier, or
    hallucinates it). Hiding the schema must not have removed the refusal."""
    calls: list = []
    policy = _policy(tmp_path, side_effect="deny")
    broker = ApprovalBroker(ApprovalStore(str(tmp_path / "approvals.json")), policy)
    reg = ToolRegistry(permissions=policy.get)
    reg.register(SideEffectTool(calls))

    assert reg.openai_schema() == []            # not offered...
    model = FakeModel([
        ModelResponse(content=None,
                      tool_calls=[{"id": "c1",
                                   "function": {"name": "side_effect", "arguments": "{}"}}]),
        ModelResponse(content="done")])
    deps = LoopDeps(mode=NativeMode(), registry=reg, model_client=model,
                    store=SessionStore(), events=EventBus(),
                    approvals=broker, run_id="r1", origin="api")

    out = await run_loop(deps, "s", "r1", "run side_effect anyway")

    assert calls == []                          # ...and calling it anyway still does not run it
    assert out == "done"
    tr = next(e for e in deps.events.recent("s") if e.kind == "tool_result")
    assert tr.data["result"] == "Blocked by your policy: you declined running 'side_effect'."
    convo = deps.store.conversation("s")
    assert any("Blocked by your policy" in str(m.get("content")) for m in convo)


# ------------------------------------------- 4: composition with progressive tool disclosure


def test_deny_survives_the_disclosed_view_for_every_k(tmp_path):
    """TRAP 1: DisclosedRegistry.openai_schema() does not call super() — it builds a BRAND-NEW bare
    ToolRegistry in _visible_view(). A resolver that does not survive that copy means deny silently
    stops working the moment disclosure is switched on. Run through the VIEW, not a plain registry."""
    p = _policy(tmp_path, web_search="deny")
    full = _reg(p, "web_search", "calculator", "get_weather", "read_file")
    for k in range(1, 6):
        visible = select_visible(full, "search the web for weather", k=k)
        view = DisclosedRegistry(full, visible)
        assert "web_search" not in _schema_names(view), f"k={k}"
        assert "web_search" not in view.text_schema(), f"k={k}"
        assert "web_search" not in view.visible_names(), f"k={k}"
        # deny must not even consume a slot of the K budget
        assert "web_search" not in visible, f"k={k}"
    # dispatch through the view is untouched, exactly as with any hidden tool
    assert view.get("web_search") is not None and "web_search" in view.names()


def test_disclosure_without_a_resolver_still_advertises_everything(tmp_path):
    full = _reg(None, "web_search", "calculator")
    view = DisclosedRegistry(full, {"web_search", "calculator"})
    assert _schema_names(view) == {"web_search", "calculator"}


# ------------------------------------------------- 4b: reveal / find_tool / mid-turn register


def test_reveal_cannot_re_admit_a_denied_tool(tmp_path):
    """TRAP 2a: reveal() adds names unconditionally (it knows nothing about policy). The deny filter
    is downstream of it, so revealing a denied tool changes nothing about what is advertised."""
    p = _policy(tmp_path, web_search="deny")
    full = _reg(p, "web_search", "calculator")
    view = DisclosedRegistry(full, {"calculator"})
    view.reveal(["web_search"])
    assert "web_search" not in _schema_names(view)
    assert "web_search" not in view.text_schema()
    assert "web_search" not in view.visible_names()


async def test_find_tool_does_not_advertise_a_denied_tool_and_says_so(tmp_path):
    """TRAP 2b: find_tool exists to reveal hidden tools — the obvious way to resurrect a denied one.
    It must not, and must say something truthful rather than silently returning nothing."""
    p = _policy(tmp_path, web_search="deny")
    full = _reg(p, "web_search", "calculator")
    view = DisclosedRegistry(full, {"calculator"})
    out = await FindToolTool(view).run(FindToolTool.Params(query="web_search"))

    assert "web_search" not in _schema_names(view)
    assert "now available to you" not in out
    assert "web_search" in out and "policy" in out.lower()   # truthful, not silent

    # the full-catalog branch ("all") must not list it as callable either
    allout = await FindToolTool(view).run(FindToolTool.Params(query="all"))
    assert "web_search" not in allout and "calculator" in allout


async def test_find_tool_still_reveals_allowed_matches_alongside_a_denied_one(tmp_path):
    p = _policy(tmp_path, web_search="deny")
    full = _reg(p, "web_search", "web_fetch", "calculator")
    view = DisclosedRegistry(full, {"calculator"})
    out = await FindToolTool(view).run(FindToolTool.Params(query="web"))
    assert "web_fetch" in _schema_names(view)          # the allowed match WAS revealed
    assert "web_search" not in _schema_names(view)
    assert "disabled by your policy" in out


def test_tool_registered_mid_turn_under_a_deny_policy_is_not_advertised(tmp_path):
    """TRAP 2c: DisclosedRegistry.register() auto-reveals ("a tool create_tool builds mid-turn can
    never be unseeable"). A deny policy already recorded for that name still wins."""
    p = _policy(tmp_path, scrape_site="deny")
    full = _reg(p, "calculator")
    view = DisclosedRegistry(full, {"calculator"})
    view.register(_tool("scrape_site"))
    assert "scrape_site" not in _schema_names(view)
    assert "scrape_site" not in view.text_schema()
    assert view.get("scrape_site") is not None          # registered and dispatchable, just unlisted


# --------------------------------------------------------------------- 5 + 6: no surprises


def test_registry_with_no_resolver_advertises_everything(tmp_path):
    """Benchmarks, evals and any other bare-registry caller must be completely unaffected."""
    r = _reg(None, "web_search", "calculator", "exec_python")
    assert _schema_names(r) == {"web_search", "calculator", "exec_python"}
    assert r.denied_names() == set()
    assert r.is_denied("web_search") is False


def test_a_resolver_that_raises_degrades_to_advertising(tmp_path):
    """The gate is the real boundary, so a broken resolver must never take down a turn."""
    def boom(name):
        raise RuntimeError("policy store on fire")

    r = ToolRegistry(permissions=boom)
    r.register(_tool("calculator"))
    assert _schema_names(r) == {"calculator"}


def test_dep_install_deny_removes_no_tool(tmp_path):
    """`dep-install` is NOT a tool — it is a mid-tool sub-gate inside create_tool. Denying it must
    not touch the catalog (and it is denied by default-ish: its only states are ask/deny)."""
    p = _policy(tmp_path, **{"dep-install": "deny"})
    r = _reg(p, "create_tool", "calculator")
    assert _schema_names(r) == {"create_tool", "calculator"}
    assert r.denied_names() == set()


def test_default_states_advertise_everything(tmp_path):
    """Nothing is denied by default, so on a default install this change is a no-op."""
    p = PermissionStore(str(tmp_path / "permissions.json"))
    r = _reg(p, "exec_python", "update_soul", "forget", "calculator", "delete_row")
    assert _schema_names(r) == {"exec_python", "update_soul", "forget", "calculator", "delete_row"}


# ------------------------------------------------------------------------- 7: the token check


def test_denied_tools_shrink_the_serialized_schema_by_exactly_their_own_size(tmp_path):
    """The stated motivation, as an assertion: a denied tool costs nothing per turn."""
    names = ["web_search", "fetch_page", "exec_python", "calculator", "get_weather"]
    denied = ["web_search", "exec_python"]
    full = _reg(None, *names)
    before = json.dumps(full.openai_schema())

    p = _policy(tmp_path, **{n: "deny" for n in denied})
    after_reg = _reg(p, *names)
    after = json.dumps(after_reg.openai_schema())

    # the two schemas differ by exactly the denied tools' own schemas (+ the ", " that joined
    # each of them to the list)
    per_tool = sum(len(json.dumps(f)) for f in full.openai_schema()
                   if f["function"]["name"] in denied)
    assert len(before) - len(after) == per_tool + len(", ") * len(denied)
    assert len(after) < len(before)
    for n in denied:
        assert n not in after
    # manual mode's catalog shrinks too — it is injected into the system prompt every turn
    assert len(after_reg.text_schema()) < len(full.text_schema())


# ------------------------------------------------------------------- engine-level wiring


def _engine(tmp_path, **ov):
    from config import Config
    from engine.engine import Engine
    return Engine(Config(**ov), data_dir=str(tmp_path))


def test_engine_registry_hides_a_denied_builtin(tmp_path):
    e = _engine(tmp_path)
    name = e.registry.names()[0]
    assert name in _schema_names(e.registry)
    e.permission_set(name, "deny")
    assert name not in _schema_names(e.registry)
    assert name not in e.registry.text_schema()
    assert e.registry.get(name) is not None              # still dispatchable, still gated
    # the Developer page must keep listing it, or you could never un-deny it
    assert any(r["key"] == name and r["state"] == "deny" for r in e.permissions_list())


def test_with_interactive_approvals_off_nothing_is_hidden(tmp_path):
    """The catalog filter and the gate are switched by the SAME flag, so they can never disagree:
    with approvals off nothing is refused at call time, so nothing may be withheld either."""
    e = _engine(tmp_path, enable_interactive_approvals=False)
    name = e.registry.names()[0]
    e.permission_set(name, "deny")
    assert name in _schema_names(e.registry)


async def test_denying_every_tool_omits_the_tools_key_instead_of_sending_an_empty_array(
        tmp_path, monkeypatch):
    """Deny makes an EMPTY catalog reachable for the first time, and an empty `tools` array is not
    universally accepted: a local vLLM answers 400 to `"tools": []` ("`tools` must not be an empty
    array. Either provide at least one tool or omit the field entirely."), while omitting the key
    is fine. ModelClient.chat's `if tools:` already omits it — pinned here because this change is
    what makes that branch reachable in production."""
    import httpx

    from engine.model_client import ModelClient

    seen: dict = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)

    r = _reg(_policy(tmp_path, calculator="deny"), "calculator")     # its only tool is denied
    req = NativeMode().build_request("SYS", [{"role": "user", "content": "hi"}], r)
    assert req["tools"] == []
    await ModelClient("http://x/v1", "main").chat(req["messages"], tools=req["tools"])
    assert "tools" not in seen["body"] and "tool_choice" not in seen["body"]


def test_manual_mode_system_prompt_omits_a_denied_tool(tmp_path):
    """text_schema() is injected INTO the system prompt by manual mode; an unfiltered one would
    leave denied tools fully advertised in the arm that is immune to native parse failures."""
    p = _policy(tmp_path, web_search="deny")
    r = _reg(p, "web_search", "calculator")
    req = ManualMode().build_request("SYSTEM", [{"role": "user", "content": "hi"}], r)
    blob = json.dumps(req)
    assert "web_search" not in blob and "calculator" in blob
