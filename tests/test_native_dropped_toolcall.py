"""Native mode must never show a dropped tool-call attempt to the user as the answer.

When a provider can't parse the model's tool-call attempt it returns `tool_calls: []` and leaves
the attempt in the content channel. The old code returned FinalAnswer(text=<that markup>), so the
user was shown `<tool_call><tool_call>…` as their answer, silently.

The samples are REAL captured model output (tests/fixtures/native_toolcall_debris.txt), not
hand-written approximations. Add new debris to the fixture; no new test code needed.
"""
from pathlib import Path

import pytest

from engine.events import EventBus
from engine.loop import LoopDeps, run_loop
from engine.modes.native import DROPPED_TOOL_CALL_REASON, NativeMode
from engine.modes.native_finish import NativeFinishMode
from engine.protocol import FinalAnswer, ModelResponse, ParseFailure, ToolCall
from engine.state import SessionStore
from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool

FIXTURE = Path(__file__).parent / "fixtures" / "native_toolcall_debris.txt"


def _load_samples(path: Path) -> list[tuple[str, str]]:
    """Parse the corpus into (label, content) pairs. `--- task: … ---` starts a sample;
    `####` section banners and `====` rules end one."""
    samples: list[tuple[str, str]] = []
    label: str | None = None
    buf: list[str] = []

    def flush():
        nonlocal label, buf
        if label is not None and "\n".join(buf).strip():
            samples.append((label, "\n".join(buf).strip()))
        label, buf = None, []

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("--- task:"):
            flush()
            label, buf = line.strip("- ").strip(), []
        elif line.startswith("####") or line.startswith("===="):
            flush()
        elif label is not None:
            buf.append(line)
    flush()
    return samples


SAMPLES = _load_samples(FIXTURE)


def test_fixture_parses():
    # guards the parser itself: the corpus shipped with 24 samples, all non-empty
    assert len(SAMPLES) >= 24
    assert all(text.strip() for _, text in SAMPLES)


@pytest.mark.parametrize("label,content", SAMPLES, ids=[f"{i}-{lbl.split()[1]}"
                                                        for i, (lbl, _) in enumerate(SAMPLES)])
def test_every_captured_sample_is_a_parse_failure(label, content):
    parsed = NativeMode().parse_response(ModelResponse(content=content, tool_calls=[],
                                                       finish_reason="stop"))
    assert isinstance(parsed, ParseFailure), f"{label}: markup reached the user as an answer"
    assert parsed.reason == DROPPED_TOOL_CALL_REASON
    assert parsed.raw == content.strip()          # the drop is observable in the error event


def test_normal_prose_answer_is_unaffected():
    parsed = NativeMode().parse_response(
        ModelResponse(content="Your Thursday standup is at 9:30am in the Ada room.",
                      finish_reason="stop"))
    assert isinstance(parsed, FinalAnswer)
    assert parsed.text == "Your Thursday standup is at 9:30am in the Ada room."


def test_answer_that_discusses_tools_is_not_a_false_positive():
    # A legitimate answer naming tools, their parameters, and example invocations. No structural
    # markup — this must reach the user untouched.
    text = (
        "You have three file tools. The read_file tool takes a name, so read_file(\"notes.txt\") "
        "returns the file's text. list_files takes no arguments. To convert money, "
        "currency_convert wants an amount plus from_currency and to_currency, like this:\n\n"
        "```\ncurrency_convert(amount=200, from_currency=\"USD\", to_currency=\"EUR\")\n```\n\n"
        "I'd call read_file first, then decide."
    )
    parsed = NativeMode().parse_response(ModelResponse(content=text, finish_reason="stop"))
    assert isinstance(parsed, FinalAnswer) and parsed.text == text


def test_single_think_close_tag_alone_does_not_trigger():
    # </think> is too weak a marker on its own — a stray one must not cost the user their answer.
    text = "</think>\n\nThe capital of Kenya is Nairobi."
    parsed = NativeMode().parse_response(ModelResponse(content=text, finish_reason="stop"))
    assert isinstance(parsed, FinalAnswer) and parsed.text == text.strip()


# ---------------------------------------------------------------------------
# False positives. An orphan `</think>` is the NORMAL artifact of a chat template that pre-fills
# `<think>` with no reasoning parser configured (model_client only strips when `<think>` is also
# present), so every one of these is ordinary product output the user must receive verbatim.
# ---------------------------------------------------------------------------

_CHART_SCRIPT = (
    "</think>\n\n"
    "Here is the Q3 revenue chart script:\n\n"
    "```python\n"
    'bar_chart(labels=months, values=sales, title="Q3 revenue")\n'
    "```\n"
)
_REPORT_CALL = (
    "</think>\n\n"
    "Run this to get July as CSV:\n\n"
    "```python\n"
    'build_report(month="July", fmt="csv")\n'
    "```\n"
)
_GGPLOT = (
    "</think>\n\n"
    "In R you'd write:\n\n"
    "```r\n"
    "ggplot(data = df, aes(x = month, y = revenue)) + geom_col()\n"
    "```\n"
)
_TS_GENERICS = (
    "</think>\n\n"
    "Type the ref with the built-in `Function` type:\n\n"
    "```ts\n"
    "const t = useRef<Function | null>(null);\n"
    "type Bus = Record<string, Array<Function>>;\n"
    "const handler: Ref<Function> = ref(() => {});\n"
    "```\n"
)
# The repo's own false-positive guard string, with an orphan </think> prepended.
_TOOL_TALK = (
    "You have three file tools. The read_file tool takes a name, so read_file(\"notes.txt\") "
    "returns the file's text. list_files takes no arguments. To convert money, "
    "currency_convert wants an amount plus from_currency and to_currency, like this:\n\n"
    "```\ncurrency_convert(amount=200, from_currency=\"USD\", to_currency=\"EUR\")\n```\n\n"
    "I'd call read_file first, then decide."
)
_TOOL_TALK_WITH_THINK = "</think>\n\n" + _TOOL_TALK
# A trailing bare call with NO </think> at all: rule 3 needs both ingredients.
_TRAILING_CALL_NO_THINK = (
    "To convert the money, call:\n\n"
    'currency_convert(amount=200, from_currency="USD", to_currency="EUR")'
)
# An inline call on the trailing line: pinned so the `^` anchor in _BARE_CALL_RE cannot be dropped.
_INLINE_CALL = (
    "</think>\n\n"
    'The helper is currency_convert(amount=200, from_currency="USD") — pass it the amount first.'
)
# Two orphan </think> is still ordinary: a support answer that quotes the tag while explaining it.
_TWO_THINK_PROSE = (
    "</think>\n\n"
    "That stray `</think>` comes from the chat template pre-filling `<think>` for you. "
    "Configure a reasoning parser and the tag disappears."
)

_FALSE_POSITIVES = {
    "chart_script": _CHART_SCRIPT,
    "report_call": _REPORT_CALL,
    "ggplot": _GGPLOT,
    "typescript_function_generics": _TS_GENERICS,
    "tool_talk_with_orphan_think": _TOOL_TALK_WITH_THINK,
    "trailing_call_without_think": _TRAILING_CALL_NO_THINK,
    "inline_call_on_last_line": _INLINE_CALL,
    "two_orphan_thinks_in_prose": _TWO_THINK_PROSE,
}


@pytest.mark.parametrize("text", list(_FALSE_POSITIVES.values()), ids=list(_FALSE_POSITIVES))
def test_ordinary_product_output_is_never_a_dropped_tool_call(text):
    parsed = NativeMode().parse_response(ModelResponse(content=text, finish_reason="stop"))
    assert isinstance(parsed, FinalAnswer), "a real answer was replaced by a parse failure"
    assert parsed.text == text.strip()


async def test_loop_delivers_the_chart_script_answer_untouched():
    # end-to-end: the FP costs the user their answer, so pin it through run_loop, not just the parse
    model = _FakeModel([ModelResponse(content=_CHART_SCRIPT, tool_calls=[], finish_reason="stop")])
    d = _deps(model)
    out = await run_loop(d, "s", "r", "make me a chart of Q3 revenue")
    assert out == _CHART_SCRIPT.strip()
    assert not any(e.kind == "reprompt" for e in d.events.recent("s"))


# Non-Qwen tool-call delimiters. Not captured output — these are the documented wire formats — so
# they live here rather than in the fixture, which holds real captures only.
_OTHER_PROVIDER_DEBRIS = {
    "mistral": '[TOOL_CALLS] [{"name": "read_file", "arguments": {"name": "notes.txt"}}]',
    "llama_python_tag": '<|python_tag|>read_file.call(name="notes.txt")',
    "kimi": '<|tool_calls_section_begin|><|tool_call_begin|>read_file<|tool_call_end|>',
    "deepseek": '<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>read_file',
    "gemma_fenced": 'Let me read it.\n\n```tool_code\nread_file(name="notes.txt")\n```',
}


@pytest.mark.parametrize("text", list(_OTHER_PROVIDER_DEBRIS.values()),
                         ids=list(_OTHER_PROVIDER_DEBRIS))
def test_non_qwen_provider_delimiters_are_detected(text):
    parsed = NativeMode().parse_response(ModelResponse(content=text, tool_calls=[],
                                                       finish_reason="stop"))
    assert isinstance(parsed, ParseFailure) and parsed.reason == DROPPED_TOOL_CALL_REASON


def test_llama_bare_json_is_deliberately_not_detected():
    # Documented non-coverage: indistinguishable from a legitimate JSON answer, so adding it would
    # manufacture exactly the false-positive class the rest of this file guards against.
    text = '{"name": "read_file", "parameters": {"name": "notes.txt"}}'
    parsed = NativeMode().parse_response(ModelResponse(content=text, finish_reason="stop"))
    assert isinstance(parsed, FinalAnswer)


def test_response_with_tool_calls_is_unaffected_even_if_content_has_markup():
    resp = ModelResponse(
        content="<tool_call>\ncalculator\n</tool_call>",
        tool_calls=[{"id": "c1", "function": {"name": "calculator",
                                              "arguments": '{"expression": "6*7"}'}}])
    parsed = NativeMode().parse_response(resp)
    assert isinstance(parsed, ToolCall)
    assert parsed.tool == "calculator" and parsed.args == {"expression": "6*7"}


def test_reprompt_for_dropped_tool_call_does_not_echo_the_debris():
    debris = SAMPLES[0][1]
    resp = ModelResponse(content=debris, tool_calls=[], finish_reason="stop")
    failure = NativeMode().parse_response(resp)
    msgs = NativeMode().reprompt_messages(resp, failure)
    blob = "".join(m.get("content") or "" for m in msgs)
    assert "</think>" not in blob and "<tool_call" not in blob
    assert debris not in blob
    assert not any(m["role"] == "assistant" for m in msgs)   # nothing echoed back at all
    assert "tool call" in blob.lower()


def test_reprompt_for_other_failures_still_echoes_content():
    # regression guard: only THIS failure reason changes the reprompt shape
    resp = ModelResponse(content="I really can't do JSON, sorry", tool_calls=[])
    msgs = NativeMode().reprompt_messages(resp, ParseFailure(reason="tool arguments were not "
                                                                    "valid JSON", raw="{"))
    assert msgs[0] == {"role": "assistant", "content": "I really can't do JSON, sorry"}
    assert "not valid JSON" in msgs[1]["content"]


def test_native_finish_inherits_the_guard():
    # NativeFinishMode.parse_response delegates to super() and passes ParseFailure through
    parsed = NativeFinishMode().parse_response(
        ModelResponse(content=SAMPLES[0][1], tool_calls=[], finish_reason="stop"))
    assert isinstance(parsed, ParseFailure) and parsed.reason == DROPPED_TOOL_CALL_REASON


class _FakeModel:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None,
                   think=None, reasoning=None):
        self.requests.append({"messages": messages, "tools": tools})
        if not self._responses:
            raise AssertionError("FakeModel ran out of scripted responses")
        return self._responses.pop(0)


def _deps(model):
    reg = ToolRegistry()
    reg.register(CalculatorTool())
    return LoopDeps(mode=NativeMode(), registry=reg, model_client=model,
                    store=SessionStore(), events=EventBus(), max_steps=6)


async def test_loop_reprompts_and_never_delivers_the_markup():
    debris = SAMPLES[0][1]
    model = _FakeModel([ModelResponse(content=debris, tool_calls=[], finish_reason="stop"),
                        ModelResponse(content="Standup is at 9:30am on Thursday.",
                                      finish_reason="stop")])
    d = _deps(model)
    out = await run_loop(d, "s", "r", "when is the standup?")
    assert out == "Standup is at 9:30am on Thursday."
    assert "</think>" not in out and "<tool_call" not in out
    events = d.events.recent("s")
    assert any(e.kind == "error" and e.data.get("kind") == "parse_failure" for e in events)
    assert any(e.kind == "reprompt" for e in events)
    # and the debris was not fed back to the model on the retry
    second = model.requests[1]["messages"]
    assert not any(debris in (m.get("content") or "") for m in second)


async def test_loop_returns_an_honest_error_when_the_markup_recurs():
    debris = SAMPLES[0][1]
    model = _FakeModel([ModelResponse(content=debris, tool_calls=[], finish_reason="stop"),
                        ModelResponse(content=debris, tool_calls=[], finish_reason="stop")])
    d = _deps(model)
    out = await run_loop(d, "s", "r", "when is the standup?")
    assert "valid response" in out.lower()
    assert "</think>" not in out and "<tool_call" not in out
