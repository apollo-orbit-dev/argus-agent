from __future__ import annotations
from dataclasses import dataclass


# Per-tool Allow/Ask/Deny policy. Any tool name is a valid policy key (default Allow); a fixed
# DEFAULT_ASK set defaults to Ask instead. "dep-install" is the one non-tool key (a mid-tool
# sub-gate inside create_tool, never blanket-allow-able).
DEFAULT_ASK: set[str] = {
    "dep-install", "update_soul", "exec_python", "forget", "delete_row",
    # outbound notifications — CONFIRM the real tool names against the registry:
    "send_telegram", "send_email", "send_ntfy",
}

LABELS: dict[str, str] = {"dep-install": "Install a Python package"}


def states_for(key: str) -> list[str]:
    """Allowed policy states for a key. dep-install can never be blanket-allowed; every other
    key (any tool name) supports the full allow/ask/deny range."""
    return ["ask", "deny"] if key == "dep-install" else ["allow", "ask", "deny"]


def default_for(key: str) -> str:
    """Default policy state for a key when unset: Ask for the fixed DEFAULT_ASK set, else Allow."""
    return "ask" if key in DEFAULT_ASK else "allow"


@dataclass
class Decision:
    approved: bool = False
    denied: bool = False
    auto: bool = False          # resolved by policy without a prompt
    one_shot: bool = False      # resolved by a deferred pre-approval
    actor: str | None = None


class TurnPaused(Exception):
    """Raised by a gate when no decision arrived in time (or origin is non-interactive).
    Caught at the loop's tool-run site to END the turn cleanly; the request stays pending."""
    def __init__(self, req_id: str, kind: str, message: str):
        super().__init__(message)
        self.req_id = req_id
        self.kind = kind
        self.message = message
