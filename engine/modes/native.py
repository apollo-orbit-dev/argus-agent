"""Native mode — use the model's built-in OpenAI-compatible tool-calling.

Passes tools via the `tools` param, reads the structured `tool_calls` field, and
appends tool results as proper `tool` role messages.
"""
from __future__ import annotations

import json

from engine.modes.base import ToolCallingMode
from engine.protocol import FinalAnswer, ModelResponse, ParseFailure, ParseResult, ToolCall
from engine.tools.base import ToolRegistry

NATIVE_SYSTEM = (
    "You are a helpful assistant with access to tools. Use a tool when it helps "
    "answer the user's request; otherwise answer directly. When you have the final "
    "answer, reply normally with that answer."
)


class NativeMode(ToolCallingMode):
    name = "native"

    def build_request(self, system_prompt: str, conversation: list[dict],
                      registry: ToolRegistry) -> dict:
        system = system_prompt.strip() or NATIVE_SYSTEM
        messages = [{"role": "system", "content": system}] + conversation
        return {"messages": messages, "tools": registry.openai_schema()}

    def parse_response(self, resp: ModelResponse) -> ParseResult:
        if resp.tool_calls:
            tc = resp.tool_calls[0]  # act on one tool at a time
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError as e:
                return ParseFailure(reason=f"tool arguments were not valid JSON: {e}",
                                    raw=str(raw_args))
            if not isinstance(args, dict):
                return ParseFailure(reason="tool arguments must be a JSON object", raw=str(raw_args))
            return ToolCall(tool=name, args=args, call_id=tc.get("id"), raw=raw_args)
        if resp.content and resp.content.strip():
            return FinalAnswer(text=resp.content.strip())
        return ParseFailure(reason="model returned neither content nor a tool call", raw="")

    def tool_result_messages(self, resp: ModelResponse, call: ToolCall,
                             result: str) -> list[dict]:
        # Echo ONLY the tool_call we acted on, so every tool_call has a matching
        # tool response (OpenAI protocol requirement).
        raw_tc = next((tc for tc in resp.tool_calls if tc.get("id") == call.call_id),
                      resp.tool_calls[0] if resp.tool_calls else None)
        assistant = {"role": "assistant", "content": resp.content or None}
        if raw_tc is not None:
            assistant["tool_calls"] = [raw_tc]
        return [
            assistant,
            {"role": "tool", "tool_call_id": call.call_id, "content": result},
        ]

    def reprompt_messages(self, resp: ModelResponse, failure: ParseFailure) -> list[dict]:
        # Do NOT echo tool_calls here (would require a matching tool response).
        return [
            {"role": "assistant", "content": resp.content or ""},
            {"role": "user", "content": f"That didn't work: {failure.reason}. Please try again."},
        ]
