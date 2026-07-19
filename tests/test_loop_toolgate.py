"""Task 4: the loop gates every tool BY NAME before running it (Deny/Allow/no-broker parity).

Modeled on the shipped tests/test_loop_turnpaused.py harness. Uses a real ApprovalBroker +
PermissionStore (isolated to tmp_path) so Deny/Allow are driven by actual policy, not a mock.
"""
from engine.approvals.broker import ApprovalBroker
from engine.approvals.policy import PermissionStore
from engine.approvals.store import ApprovalStore
from engine.events import EventBus
from engine.loop import LoopDeps, run_loop
from engine.modes.native import NativeMode
from engine.protocol import ModelResponse
from engine.state import SessionStore
from engine.tools.base import Tool, ToolRegistry
from pydantic import BaseModel

from tests.test_loop import FakeModel


class SideEffectTool(Tool):
    """A tool whose `run` has an observable side effect, so we can prove it was (or wasn't) run."""
    name = "side_effect"
    description = "records that it ran"

    class Params(BaseModel):
        pass

    def __init__(self, calls: list):
        self.calls = calls

    async def run(self, args: "SideEffectTool.Params") -> str:
        self.calls.append("ran")
        return "did the thing"


def _tool_call(cid="c1"):
    return ModelResponse(content=None, tool_calls=[
        {"id": cid, "function": {"name": "side_effect", "arguments": "{}"}}])


def _broker(tmp_path, state: str) -> ApprovalBroker:
    store = ApprovalStore(str(tmp_path / "approvals.json"))
    policy = PermissionStore(str(tmp_path / "permissions.json"))
    policy.set("side_effect", state)
    return ApprovalBroker(store, policy)


def _deps(model, calls, approvals=None) -> LoopDeps:
    reg = ToolRegistry()
    reg.register(SideEffectTool(calls))
    return LoopDeps(mode=NativeMode(), registry=reg, model_client=model,
                    store=SessionStore(), events=EventBus(),
                    approvals=approvals, run_id="r1", origin="api")


async def test_deny_policy_blocks_tool_and_loop_continues(tmp_path):
    calls: list = []
    model = FakeModel([_tool_call(), ModelResponse(content="done")])
    deps = _deps(model, calls, approvals=_broker(tmp_path, "deny"))

    out = await run_loop(deps, "s", "r1", "please run side_effect")

    assert calls == []                      # the tool's run() NEVER executed
    assert out == "done"                    # the loop kept going to a final answer
    tr = next(e for e in deps.events.recent("s") if e.kind == "tool_result")
    assert tr.data["ok"] is False
    assert "blocked" in tr.data["result"].lower()
    assert "side_effect" in tr.data["result"]


async def test_allow_policy_runs_tool_normally(tmp_path):
    calls: list = []
    model = FakeModel([_tool_call(), ModelResponse(content="done")])
    deps = _deps(model, calls, approvals=_broker(tmp_path, "allow"))

    out = await run_loop(deps, "s", "r1", "please run side_effect")

    assert calls == ["ran"]
    assert out == "done"
    tr = next(e for e in deps.events.recent("s") if e.kind == "tool_result")
    assert tr.data["ok"] is True


async def test_no_approvals_broker_means_no_gating(tmp_path):
    calls: list = []
    model = FakeModel([_tool_call(), ModelResponse(content="done")])
    deps = _deps(model, calls, approvals=None)   # master flag off / not wired

    out = await run_loop(deps, "s", "r1", "please run side_effect")

    assert calls == ["ran"]                 # tool ran despite no explicit policy set
    assert out == "done"
