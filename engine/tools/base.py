"""Uniform tool contract + registry + argument validation.

This validation layer is central to the small-model thesis: malformed tool calls
are expected, and how gracefully we catch/repair them is a big part of what we
measure. Validation happens BEFORE execution and returns a clear structured error
the loop hands back to the model.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, ValidationError


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
    def __init__(self):
        self._tools: dict[str, Tool] = {}

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
        """OpenAI-compatible `tools` array for native mode."""
        out = []
        for t in self._tools.values():
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
        """Human/model-readable tool catalog for manual-mode system-prompt injection."""
        lines = []
        for t in self._tools.values():
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
