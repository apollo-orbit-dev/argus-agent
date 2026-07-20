"""native_finish mode — native tool-calling, but every turn is a forced structured decision.

Adds a synthetic `final_answer` tool and sends `tool_choice="required"`, so the model must emit a tool
call each turn and "answer the user" is itself a structured action (calling final_answer). This makes
prose-slip impossible (the dominant native failure on small models) and lets vLLM guided-decode valid
tool-call JSON (killing the malformed-JSON tail) — while keeping native's server-side tool parsing.
The trade-off to measure: it can tempt a weak model to finish early. See
docs/superpowers/specs/2026-07-20-native-finish-mode-design.md.
"""
from __future__ import annotations

from engine.modes.native import NativeMode
from engine.protocol import FinalAnswer, ModelResponse, ParseResult, ToolCall
from engine.tools.base import ToolRegistry

FINAL_ANSWER_TOOL = {
    "type": "function",
    "function": {
        "name": "final_answer",
        "description": "Give your final answer to the user. Call this when you are done — it ends the turn.",
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string", "description": "your complete answer to the user"}},
            "required": ["answer"],
        },
    },
}

NATIVE_FINISH_SYSTEM = (
    "You are an agent with tools. On EVERY turn you MUST call exactly one tool. Use a tool to take an "
    "action or gather information; when you are finished and ready to reply to the user, call "
    "`final_answer` with your complete answer. Never reply in plain text — always via a tool call."
)


class NativeFinishMode(NativeMode):
    name = "native_finish"

    def build_request(self, system_prompt: str, conversation: list[dict],
                      registry: ToolRegistry) -> dict:
        base = system_prompt.strip()
        # Always carry the finish directive; append it to a custom system prompt rather than lose it.
        system = (base + "\n\n" + NATIVE_FINISH_SYSTEM) if base else NATIVE_FINISH_SYSTEM
        messages = [{"role": "system", "content": system}] + conversation
        tools = list(registry.openai_schema()) + [FINAL_ANSWER_TOOL]
        return {"messages": messages, "tools": tools, "tool_choice": "required"}

    def parse_response(self, resp: ModelResponse) -> ParseResult:
        parsed = super().parse_response(resp)
        # A `final_answer` tool call is the terminal turn — unwrap it to a FinalAnswer.
        if isinstance(parsed, ToolCall) and parsed.tool == "final_answer":
            return FinalAnswer(text=str(parsed.args.get("answer", "")))
        return parsed
