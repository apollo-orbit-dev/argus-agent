"""Manual mode — ignore native tool-calling; instruct the model to emit a strict
JSON envelope in normal text, then parse and validate that ourselves.

Tolerant parsing is essential on a small model: observed output includes leading
whitespace/newlines, prose, and ```json fences around the envelope.
"""
from __future__ import annotations

import json
from typing import Optional

from engine.modes.base import ToolCallingMode
from engine.protocol import FinalAnswer, ModelResponse, ParseFailure, ParseResult, ToolCall
from engine.tools.base import ToolRegistry

MANUAL_INSTRUCTIONS = """You are a tool-using agent. You have access to these tools:

{tools}

Respond with EXACTLY ONE JSON object and nothing else. It must have this shape.

To use a tool:
{{"action": "tool", "tool": "<tool_name>", "args": {{ ...arguments... }}}}

To give your final answer to the user:
{{"action": "final", "answer": "<your answer>"}}

IMPORTANT: the "action" field is ALWAYS the literal word "tool" or "final" — never
a tool name. The tool name goes in the separate "tool" field.

Examples:
- Multiply two numbers:
  {{"action": "tool", "tool": "calculator", "args": {{"expression": "6 * 7"}}}}
- Give the final answer:
  {{"action": "final", "answer": "The result is 42."}}

Rules:
- Output ONLY the single JSON object. No prose before or after. No markdown code fences.
- Use one tool at a time. You will receive the tool's result, then decide the next step.
- Use only the tools listed above, with the exact argument names shown.
- When you have enough information, respond with the "final" action."""


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # drop first fence line (``` or ```json) and a trailing fence
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.strip()


def _extract_first_json_object(text: str) -> Optional[str]:
    """Return the first balanced top-level {...} substring, string/escape aware."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


class ManualMode(ToolCallingMode):
    name = "manual"

    def __init__(self, known_tools: Optional[set[str]] = None):
        # Registry-aware repair: small models frequently put the tool NAME in the
        # "action" field. Knowing the tool names lets us repair that in-place
        # instead of paying a reprompt round-trip.
        self._known_tools = set(known_tools or ())

    def build_request(self, system_prompt: str, conversation: list[dict],
                      registry: ToolRegistry) -> dict:
        self._known_tools = set(registry.names())
        instructions = MANUAL_INSTRUCTIONS.format(tools=registry.text_schema())
        system = (system_prompt.strip() + "\n\n" + instructions) if system_prompt.strip() else instructions
        messages = [{"role": "system", "content": system}] + conversation
        return {"messages": messages}  # deliberately NO tools param

    def parse_response(self, resp: ModelResponse) -> ParseResult:
        raw = resp.content or ""
        candidate = _extract_first_json_object(_strip_fences(raw))
        if candidate is None:
            return ParseFailure(reason="no JSON object found in the response", raw=raw)
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError as e:
            return ParseFailure(reason=f"JSON object could not be parsed: {e}", raw=raw)
        if not isinstance(obj, dict) or "action" not in obj:
            return ParseFailure(reason='JSON must include an "action" field ("tool" or "final")', raw=raw)
        action = obj["action"]
        if action == "tool":
            name = obj.get("tool")
            if not name:
                return ParseFailure(reason='tool action requires a "tool" name', raw=raw)
            args = obj.get("args", {})
            if not isinstance(args, dict):
                return ParseFailure(reason='"args" must be a JSON object', raw=raw)
            return ToolCall(tool=name, args=args, raw=candidate)
        if action == "final":
            if "answer" not in obj:
                return ParseFailure(reason='final action requires an "answer" field', raw=raw)
            return FinalAnswer(text=str(obj["answer"]))
        # Repair: the model used the tool name as the action, e.g.
        # {"action": "calculator", "args": {...}} instead of action="tool".
        if action in self._known_tools:
            args = obj.get("args", {})
            if not isinstance(args, dict):
                return ParseFailure(reason='"args" must be a JSON object', raw=raw)
            return ToolCall(tool=action, args=args, raw=candidate, repaired=True)
        return ParseFailure(reason=f'unknown action {action!r} (expected "tool" or "final")', raw=raw)

    def tool_result_messages(self, resp: ModelResponse, call: ToolCall,
                             result: str) -> list[dict]:
        return [
            {"role": "assistant", "content": resp.content or call.raw or ""},
            {"role": "user", "content": f"TOOL RESULT ({call.tool}): {result}\n\n"
                                        f"Now respond with the next JSON object "
                                        f'(another "tool" action, or "final").'},
        ]

    def reprompt_messages(self, resp: ModelResponse, failure: ParseFailure) -> list[dict]:
        return [
            {"role": "assistant", "content": resp.content or ""},
            {"role": "user", "content":
                f"Your previous message was not valid: {failure.reason}. "
                'Respond with ONLY one JSON object: either '
                '{"action":"tool","tool":"<name>","args":{...}} or '
                '{"action":"final","answer":"<text>"}. No other text.'},
        ]
