"""Shared result types. The agent loop depends only on these — both tool-calling
modes return exactly one of ToolCall | FinalAnswer | ParseFailure, so the loop
treats native and manual identically and never branches on mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]
    call_id: Optional[str] = None   # native tool_call id, echoed back in the tool result
    raw: Optional[str] = None       # raw text the call was parsed from (manual mode)
    repaired: bool = False          # True if the harness repaired a malformed call (measured)


@dataclass
class FinalAnswer:
    text: str


@dataclass
class ParseFailure:
    reason: str                     # human-readable; fed back to the model on reprompt
    raw: str = ""                   # the raw model output that failed to parse


ParseResult = Union[ToolCall, FinalAnswer, ParseFailure]


@dataclass
class ModelResponse:
    content: Optional[str]                       # assistant text (may be None)
    tool_calls: list[dict] = field(default_factory=list)  # native tool_calls, raw dicts
    finish_reason: Optional[str] = None
    usage: dict = field(default_factory=dict)
    reasoning: Optional[str] = None              # thinking/reasoning trace, when the model exposes one
