"""The tool-calling abstraction. One interface, two implementations. The agent
loop depends ONLY on this interface and never branches on mode.

Responsibilities that differ between native and manual are isolated here:
  - build_request:   how tools are presented (native `tools` param vs text prompt)
  - parse_response:  how a model reply becomes ToolCall | FinalAnswer | ParseFailure
  - tool_result_messages: how a tool result is appended (tool role vs plain text)
  - reprompt_messages:    how a parse failure is corrected

Everything else (the loop, validation, execution, MAX_STEPS, events) is shared.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from engine.protocol import FinalAnswer, ModelResponse, ParseFailure, ParseResult, ToolCall
from engine.tools.base import ToolRegistry


class ToolCallingMode(ABC):
    name: str

    @abstractmethod
    def build_request(self, system_prompt: str, conversation: list[dict],
                      registry: ToolRegistry) -> dict:
        """Return kwargs for ModelClient.chat (at least 'messages'; native adds 'tools')."""

    @abstractmethod
    def parse_response(self, resp: ModelResponse) -> ParseResult:
        ...

    @abstractmethod
    def tool_result_messages(self, resp: ModelResponse, call: ToolCall,
                             result: str) -> list[dict]:
        """Messages to append after executing a tool (assistant echo + result)."""

    @abstractmethod
    def reprompt_messages(self, resp: ModelResponse, failure: ParseFailure) -> list[dict]:
        """Messages to append to nudge the model to retry after a parse failure."""


def get_mode(name: str, registry: "ToolRegistry | None" = None) -> ToolCallingMode:
    from engine.modes.native import NativeMode
    from engine.modes.manual import ManualMode
    if name == "native":
        return NativeMode()
    if name == "manual":
        known = set(registry.names()) if registry is not None else None
        return ManualMode(known_tools=known)
    raise ValueError(f"unknown tool_calling_mode: {name!r} (expected 'native' or 'manual')")
