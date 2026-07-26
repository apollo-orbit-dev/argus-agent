"""Native mode — use the model's built-in OpenAI-compatible tool-calling.

Passes tools via the `tools` param, reads the structured `tool_calls` field, and
appends tool results as proper `tool` role messages.
"""
from __future__ import annotations

import json
import re

from engine.modes.base import ToolCallingMode
from engine.protocol import FinalAnswer, ModelResponse, ParseFailure, ParseResult, ToolCall
from engine.tools.base import ToolRegistry

NATIVE_SYSTEM = (
    "You are a helpful assistant with access to tools. Use a tool when it helps "
    "answer the user's request; otherwise answer directly. When you have the final "
    "answer, reply normally with that answer."
)

# Reason used when the provider dropped a tool call: tool_calls is empty but the content is
# visibly a tool-call attempt. Identifies the failure so reprompt_messages can avoid echoing
# the degenerate content back at the model.
DROPPED_TOOL_CALL_REASON = (
    "the provider returned no tool calls, but the reply is raw tool-call markup — "
    "the tool call was not understood"
)

# STRUCTURAL markers only: tag-shaped debris a provider's tool-call parser left behind. Prose that
# merely *discusses* tools ("the read_file tool takes a name") contains none of these, which is the
# point — a false positive costs the user a reprompt and can replace their real answer with an error.
_MARKUP_RE = re.compile(
    r"</?tool_call\b"          # <tool_call> … </tool_call>   (Qwen/Hermes)
    r"|</?tool_code\b"         # <tool_code>
    r"|<parameter[ =>]"        # <parameter name="x">  /  <parameter=x>
    r"|</parameter>"
    r"|</?invoke\b"            # <invoke name="geocode">
    r"|</?function[ =>]"       # <function=…> / </function>
    r"|<model_thinking\b",
    re.IGNORECASE,
)
# A bare keyword-argument call sitting on its own line — `currency_convert(amount=200, ...)`.
# Too weak alone (real answers show example code), so it only counts alongside a stray </think>.
_BARE_CALL_RE = re.compile(r"^[ \t]*[A-Za-z_][A-Za-z0-9_]*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*=", re.M)
_THINK_CLOSE_RE = re.compile(r"</think\s*>", re.IGNORECASE)


def looks_like_dropped_tool_call(text: str) -> bool:
    """True when `text` is a tool-call attempt the provider failed to parse.

    Deliberately conservative. A lone `</think>` does NOT qualify (too plausible in ordinary
    prose); it only counts when repeated — degenerate output no answer produces — or when it
    sits next to a bare call blob.
    """
    if not text:
        return False
    if _MARKUP_RE.search(text):
        return True
    thinks = len(_THINK_CLOSE_RE.findall(text))
    if thinks >= 2:
        return True
    return thinks >= 1 and bool(_BARE_CALL_RE.search(text))


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
            text = resp.content.strip()
            # The provider couldn't parse the model's tool-call attempt, so it came back as
            # content with tool_calls=[]. Never hand that to the user as their answer — fail the
            # parse and let the loop do what it already does for parse failures (observable
            # error event, one reprompt, honest error if it recurs).
            if looks_like_dropped_tool_call(text):
                return ParseFailure(reason=DROPPED_TOOL_CALL_REASON, raw=text)
            return FinalAnswer(text=text)
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
        # Dropped tool call: the content is degenerate markup. Echoing `</think></think></think>`
        # back into the conversation just invites more of it — say what went wrong instead.
        if failure.reason == DROPPED_TOOL_CALL_REASON:
            return [{"role": "user", "content":
                     "[note] Your last message contained tool-call markup as plain text, so no "
                     "tool ran and nothing was shown to the user. Do not write tool syntax in "
                     "your message. Either issue a real tool call through the tool-calling "
                     "interface, or reply with the answer in plain prose."}]
        # Do NOT echo tool_calls here (would require a matching tool response).
        return [
            {"role": "assistant", "content": resp.content or ""},
            {"role": "user", "content": f"That didn't work: {failure.reason}. Please try again."},
        ]
