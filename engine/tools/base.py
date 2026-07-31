"""Uniform tool contract + registry + argument validation.

This validation layer is central to the small-model thesis: malformed tool calls
are expected, and how gracefully we catch/repair them is a big part of what we
measure. Validation happens BEFORE execution and returns a clear structured error
the loop hands back to the model.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

from pydantic import BaseModel, ValidationError

log = logging.getLogger("argus.tools")


class Tool(ABC):
    name: str
    description: str
    Params: type[BaseModel]
    terminal: bool = False  # if True, running this tool ends the turn (its result is the answer)
    echo_result: bool = False  # if True, the tool's output IS the deliverable (e.g. a text chart) and
    # must reach the user — the loop appends it to the final answer if the model didn't include it

    @abstractmethod
    async def run(self, args: BaseModel) -> str:
        """Execute and return a string result (text the model can read)."""


@dataclass
class ValidationResult:
    ok: bool
    args: Optional[BaseModel] = None
    error: Optional[str] = None


def _compact_pydantic_error(exc: ValidationError) -> str:
    parts = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e["loc"]) or "(root)"
        parts.append(f"{loc}: {e['msg']}")
    return "; ".join(parts)


class ToolRegistry:
    """Every registered tool, plus the two catalogs the model is shown.

    `permissions` is an OPTIONAL resolver — a callable `name -> "allow"|"ask"|"deny"` — and is the
    registry's entire knowledge of the approvals system (no import, no hard dependency, so a
    benchmark or eval harness constructing a bare registry is unaffected). A tool whose effective
    state is `deny` is NOT ADVERTISED: it is absent from openai_schema() and text_schema(), so the
    model neither pays for its schema every turn nor is told it has a capability it does not have.

    VISIBILITY IS NOT THE SECURITY BOUNDARY. Filtering the catalog is an optimization and a hint;
    enforcement stays at ApprovalBroker.gate() on every call (engine/loop.py). Dispatch —
    get/validate/names/list — is deliberately NOT filtered, exactly as with progressive disclosure:
    a denied tool named out of conversation history is still a real, resolvable tool, and the gate
    refuses it there with "Blocked by your policy".
    """

    def __init__(self, permissions: Optional[Callable[[str], str]] = None):
        self._tools: dict[str, Tool] = {}
        self.permissions = permissions

    # ---- permission-aware catalog (advertising only; dispatch is never filtered) ----
    def is_denied(self, name: str) -> bool:
        """True when `name`'s effective permission is deny. No resolver -> nothing is denied.

        A resolver that raises must never take down a turn: the gate is the real boundary, so a
        broken resolver degrades to today's behaviour (advertise it) rather than to a hard error."""
        if self.permissions is None:
            return False
        try:
            return self.permissions(name) == "deny"
        except Exception:
            log.debug("permission resolver failed for %r; advertising it", name, exc_info=True)
            return False

    def denied_names(self) -> set[str]:
        """Registered tools whose permission is deny — the ones no catalog may advertise."""
        if self.permissions is None:
            return set()
        return {n for n in self._tools if self.is_denied(n)}

    def advertised(self) -> list[Tool]:
        """The tools both schema builders offer the model, in registry insertion order."""
        return [t for n, t in self._tools.items() if not self.is_denied(n)]

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def validate(self, name: str, raw_args: dict) -> ValidationResult:
        tool = self.get(name)
        if tool is None:
            known = ", ".join(self.names()) or "(none)"
            return ValidationResult(ok=False, error=f"unknown tool '{name}'. Known tools: {known}")
        if not isinstance(raw_args, dict):
            return ValidationResult(ok=False, error=f"args must be a JSON object, got {type(raw_args).__name__}")
        try:
            parsed = tool.Params(**raw_args)
        except ValidationError as e:
            return ValidationResult(ok=False, error=f"invalid args for '{name}': {_compact_pydantic_error(e)}")
        return ValidationResult(ok=True, args=parsed)

    def openai_schema(self) -> list[dict]:
        """OpenAI-compatible `tools` array for native mode. Denied tools are not offered."""
        out = []
        for t in self.advertised():
            out.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.Params.model_json_schema(),
                },
            })
        return out

    def text_schema(self) -> str:
        """Human/model-readable tool catalog for manual-mode system-prompt injection.

        Filtered too: manual mode injects this INTO the system prompt, so skipping it would leave
        denied tools fully advertised in the one mode that is immune to native parse failures."""
        lines = []
        for t in self.advertised():
            schema = t.Params.model_json_schema()
            props = schema.get("properties", {})
            required = set(schema.get("required", []))
            arg_descs = []
            for pname, pinfo in props.items():
                ptype = pinfo.get("type", "any")
                req = "required" if pname in required else "optional"
                default = f", default={pinfo['default']!r}" if "default" in pinfo else ""
                desc = f" — {pinfo['description']}" if pinfo.get("description") else ""
                arg_descs.append(f"    - {pname} ({ptype}, {req}{default}){desc}")
            args_block = "\n".join(arg_descs) if arg_descs else "    (no arguments)"
            lines.append(f"- {t.name}: {t.description}\n  args:\n{args_block}")
        return "\n".join(lines)
