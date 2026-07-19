from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Gate:
    label: str
    states: list[str]     # allowed policy states, e.g. ["ask", "deny"]
    default: str          # default state when unset


# The one declarative registry of gated actions.
GATES: dict[str, Gate] = {
    "dep-install": Gate("Install a Python package", ["ask", "deny"], "ask"),
    "soul-edit":   Gate("Edit my persona (SOUL)", ["allow", "ask", "deny"], "ask"),
}


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
