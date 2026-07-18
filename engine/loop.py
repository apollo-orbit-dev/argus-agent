"""The mode-agnostic agent loop — the thing under test.

The loop body is IDENTICAL across tool-calling modes: it depends only on the
ToolCallingMode interface and never branches on `mode`. Every step emits one
structured StepEvent (the experimental data). Tool errors and validation errors
are fed back to the model as results so it can recover; MAX_STEPS bounds it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

OBSERVER_NUDGE = (
    "[note] You have now run this exact tool call more than once and gotten the same "
    "result. Do not repeat it — try a different tool or different arguments, or give the "
    "user your best answer with the information you already have.")

CREATE_VERIFY_NUDGE = (
    "[note] You've created several tools in a row without running one. create_tool only "
    "test-runs with sample arguments — to know whether your fix actually works, CALL the tool "
    "you're fixing NOW and read its REAL output before rewriting it again. If it already returns "
    "the right values, you're done: stop and report. Don't keep rewriting blind, and don't leave "
    "throwaway probe tools behind — delete_tool any scratch probes once you've learned what you need.")

from engine.events import EventBus, StepEvent
from engine.model_client import ModelError
from engine.modes.base import ToolCallingMode
from engine.protocol import FinalAnswer, ParseFailure, ToolCall
from engine.state import SessionStore
from engine.tools.base import ToolRegistry


def _with_unechoed(answer: str, echoed: list[str]) -> str:
    """Append any echo_result tool output (e.g. a text chart) the final answer doesn't already
    contain, so it reaches the user even when the model only described it. Dedupes on the chart's
    most distinctive (longest) line so a verbatim paste isn't duplicated."""
    answer = answer or ""
    extra = []
    for out in echoed:
        body = out.split("```")[1] if out.count("```") >= 2 else out   # fenced chart body
        lines = [l for l in body.splitlines() if l.strip()]
        if not lines:
            continue
        probe = max(lines, key=len).strip()
        if probe and probe not in answer:
            extra.append(out.strip())
    if not extra:
        return answer
    return (answer.rstrip() + "\n\n" + "\n\n".join(extra)).strip()


@dataclass
class LoopDeps:
    mode: ToolCallingMode
    registry: ToolRegistry
    model_client: object          # anything with async chat(messages, tools=, max_tokens=, temperature=)
    store: SessionStore
    events: EventBus
    max_steps: int = 6
    max_tokens: int = 1536
    temperature: float = 0.0
    system_prompt: str = ""
    enable_observer: bool = True
    observer_threshold: int = 2
    think: Optional[bool] = None   # None = model default (reasoning on); adaptive router may set it
    reasoning: Optional[str] = None  # per-turn reasoning LEVEL (off|low|medium|high) from the adaptive router; wins over think


def _strip_old_images(conversation: list[dict]) -> list[dict]:
    """Keep image parts only in the most recent user message; replace image_url parts in older
    messages with a placeholder, so a conversation doesn't re-send (and re-pay for) base64 images
    on every step and every later turn. The current turn's image survives (it's the last user
    message; tool results are role 'tool'/'assistant')."""
    last_user = max((i for i, m in enumerate(conversation) if m.get("role") == "user"), default=-1)
    out = []
    for i, m in enumerate(conversation):
        c = m.get("content")
        if isinstance(c, list) and i != last_user and any(
                isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            kept = [p for p in c if not (isinstance(p, dict) and p.get("type") == "image_url")]
            kept.append({"type": "text", "text": "[image omitted from history]"})
            m = {**m, "content": kept}
        out.append(m)
    return out


async def run_loop(deps: LoopDeps, session_id: str, run_id: str, user_text: str,
                   user_content=None) -> str:
    async def emit(step: int, kind: str, data: dict):
        await deps.events.publish(StepEvent(run_id, session_id, step, kind, data, time.time()))

    deps.store.append_message(
        session_id, {"role": "user", "content": user_content if user_content is not None else user_text})
    await emit(0, "info", {"message": "received", "text": user_text,
                           "mode": deps.mode.name})

    parse_failures = 0
    pending_echo: list[str] = []       # outputs of echo_result tools (e.g. text charts) to guarantee-deliver
    call_counts: dict[str, int] = {}   # observer: signature -> times seen
    create_names: dict[str, int] = {}  # observer: create_tool/skill NAME -> times (ignores code)
    consecutive_creates = 0            # observer: creates in a row with no tool actually run
    create_verify_nudged = False
    for step in range(1, deps.max_steps + 1):
        conversation = _strip_old_images(deps.store.conversation(session_id))
        req = deps.mode.build_request(deps.system_prompt, conversation, deps.registry)
        await emit(step, "model_request",
                   {"messages": req["messages"], "has_tools": "tools" in req})

        try:
            resp = await deps.model_client.chat(
                **req, max_tokens=deps.max_tokens, temperature=deps.temperature,
                think=deps.think, reasoning=deps.reasoning)
        except ModelError as e:
            await emit(step, "error", {"error": f"model call failed: {e}"})
            return f"Sorry — I couldn't reach the model ({e})."

        await emit(step, "model_response",
                   {"content": resp.content, "tool_calls": resp.tool_calls,
                    "finish_reason": resp.finish_reason, "usage": resp.usage,
                    "reasoning": resp.reasoning})

        parsed = deps.mode.parse_response(resp)

        if isinstance(parsed, FinalAnswer):
            # Guarantee echo_result outputs (text charts) reach the user: small models often
            # DESCRIBE a chart instead of pasting it, so append any the answer doesn't already contain.
            text = _with_unechoed(parsed.text, pending_echo)
            deps.store.append_message(session_id, {"role": "assistant", "content": text})
            await emit(step, "final", {"answer": text})
            return text

        if isinstance(parsed, ParseFailure):
            truncated = getattr(resp, "finish_reason", None) == "length"
            await emit(step, "error", {"kind": "parse_failure", "reason": parsed.reason,
                                       "raw": parsed.raw, "truncated": truncated})
            if parse_failures >= 1:
                await emit(step, "error", {"message": "gave up after reprompt", "reason": parsed.reason})
                if truncated:
                    return ("Sorry — my reply ran past the model's output limit and got cut off "
                            "mid-way. Ask me to write it more concisely, or in parts.")
                return ("Sorry — I couldn't produce a valid response. "
                        f"(last parser error: {parsed.reason})")
            parse_failures += 1
            if truncated:
                # The tool arguments were cut off at the token limit — reprompting for a "valid
                # response" just makes the model repeat the same oversized output. Tell it to shrink.
                deps.store.append_message(session_id, {"role": "user", "content":
                    "[note] Your last response was CUT OFF at the model's output limit — the tool "
                    "arguments were too long to fit in one message. Redo it, but make the content "
                    "much shorter, or write long content in smaller pieces (e.g. append in chunks)."})
            else:
                for m in deps.mode.reprompt_messages(resp, parsed):
                    deps.store.append_message(session_id, m)
            await emit(step, "reprompt", {"reason": parsed.reason})
            continue

        # ToolCall
        parse_failures = 0
        call: ToolCall = parsed
        await emit(step, "tool_call", {"tool": call.tool, "args": call.args,
                                       "call_id": call.call_id, "repaired": call.repaired})

        # Observer: recreating the SAME tool/skill name over and over is a stuck self-repair
        # loop. The args-signature check below can't catch it because the `code` differs each
        # time (the model keeps rewriting the body), so track by NAME, ignoring the code.
        if deps.enable_observer and call.tool in ("create_tool", "create_skill"):
            nm = str((call.args or {}).get("name", ""))
            if nm:
                ckey = call.tool + "#" + nm
                create_names[ckey] = create_names.get(ckey, 0) + 1
                # More headroom than the generic repeat threshold: recreating a tool is genuine
                # iteration (build → fix → fix), each attempt different code. Allow ~4 informed
                # tries, then stop — still catches a real flail (the original bug recreated 7×).
                if create_names[ckey] > max(deps.observer_threshold, 4):
                    await emit(step, "observer", {"issue": "stuck_recreating",
                                                  "tool": call.tool, "name": nm,
                                                  "count": create_names[ckey]})
                    answer = (f"I kept trying to rebuild '{nm}' without it working, so I've "
                              "stopped rather than loop. This likely needs a different approach "
                              "or a closer look — tell me how you'd like to proceed.")
                    deps.store.append_message(session_id, {"role": "assistant", "content": answer})
                    await emit(step, "final", {"answer": answer})
                    return answer

        # Observer: detect the model looping on the same tool call with no progress.
        sig = call.tool + ":" + json.dumps(call.args, sort_keys=True, default=str)
        call_counts[sig] = call_counts.get(sig, 0) + 1
        if deps.enable_observer and call_counts[sig] > deps.observer_threshold:
            await emit(step, "observer", {"issue": "stuck_repeating", "tool": call.tool,
                                          "count": call_counts[sig]})
            answer = ("I wasn't able to make progress — I kept repeating the same step "
                      "without getting new information. Could you rephrase or add detail?")
            deps.store.append_message(session_id, {"role": "assistant", "content": answer})
            await emit(step, "final", {"answer": answer})
            return answer

        v = deps.registry.validate(call.tool, call.args)
        await emit(step, "validation", {"tool": call.tool, "ok": v.ok, "error": v.error})

        if not v.ok:
            result = f"validation error: {v.error}"
            for m in deps.mode.tool_result_messages(resp, call, result):
                deps.store.append_message(session_id, m)
            await emit(step, "tool_result", {"tool": call.tool, "ok": False, "result": result})
            continue

        tool_obj = deps.registry.get(call.tool)
        try:
            result = await tool_obj.run(v.args)
        except Exception as e:  # never crash the loop; feed the error back
            result = f"tool {call.tool} failed: {e}"
            await emit(step, "tool_result", {"tool": call.tool, "ok": False, "result": result})
        else:
            await emit(step, "tool_result", {"tool": call.tool, "ok": True, "result": result})
            if getattr(tool_obj, "echo_result", False) and isinstance(result, str):
                pending_echo.append(result)

        # A terminal tool (e.g. ask_user/clarify) ends the turn: its result IS the answer.
        if getattr(tool_obj, "terminal", False):
            deps.store.append_message(session_id, {"role": "assistant", "content": result})
            await emit(step, "final", {"answer": result})
            return result

        for m in deps.mode.tool_result_messages(resp, call, result):
            deps.store.append_message(session_id, m)

        # Observer: at the repeat threshold, nudge the model to change approach.
        if deps.enable_observer and call_counts[sig] == deps.observer_threshold:
            deps.store.append_message(session_id, {"role": "user", "content": OBSERVER_NUDGE})
            await emit(step, "observer", {"issue": "repeat_nudge", "tool": call.tool,
                                          "count": call_counts[sig]})

        # Observer: building tool after tool without ever RUNNING one to see its real output is
        # a thrash (once made 9 create_tool calls in a row). create_tool only test-runs with
        # sample args — nudge the model to CALL the tool it's fixing and read the real result.
        if call.tool in ("create_tool", "create_skill"):
            consecutive_creates += 1
        else:
            consecutive_creates = 0
        if deps.enable_observer and consecutive_creates >= 3 and not create_verify_nudged:
            create_verify_nudged = True
            deps.store.append_message(session_id, {"role": "user", "content": CREATE_VERIFY_NUDGE})
            await emit(step, "observer", {"issue": "create_without_verify",
                                          "count": consecutive_creates})

    await emit(deps.max_steps, "error",
               {"message": "max steps exceeded", "max_steps": deps.max_steps})
    return (f"Sorry — I couldn't complete this within {deps.max_steps} steps. "
            "Try rephrasing or breaking the request into smaller parts.")
