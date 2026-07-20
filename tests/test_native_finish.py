"""native_finish mode — forced structured tool-or-finish decision each turn."""
import json

import pytest

from engine.modes.base import get_mode
from engine.modes.native_finish import FINAL_ANSWER_TOOL, NativeFinishMode
from engine.protocol import FinalAnswer, ModelResponse, ParseFailure, ToolCall
from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool


def _registry():
    r = ToolRegistry()
    r.register(CalculatorTool())
    return r


def _tc(name, args):
    return ModelResponse(content="", tool_calls=[
        {"id": "call_1", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}])


def test_get_mode_returns_native_finish():
    assert isinstance(get_mode("native_finish"), NativeFinishMode)


def test_build_request_injects_final_answer_and_required():
    req = NativeFinishMode().build_request("", [{"role": "user", "content": "hi"}], _registry())
    assert req["tool_choice"] == "required"
    names = [t["function"]["name"] for t in req["tools"]]
    assert "final_answer" in names and "calculator" in names
    assert "final_answer" in req["messages"][0]["content"]        # the finish directive is in the system prompt


def test_custom_system_prompt_keeps_finish_directive():
    req = NativeFinishMode().build_request("You are Argus.", [], _registry())
    sys = req["messages"][0]["content"]
    assert "You are Argus." in sys and "final_answer" in sys      # appended, not lost


def test_final_answer_call_becomes_final_answer():
    parsed = NativeFinishMode().parse_response(_tc("final_answer", {"answer": "The result is 42."}))
    assert isinstance(parsed, FinalAnswer)
    assert parsed.text == "The result is 42."


def test_real_tool_call_stays_a_tool_call():
    parsed = NativeFinishMode().parse_response(_tc("calculator", {"expression": "6*7"}))
    assert isinstance(parsed, ToolCall)
    assert parsed.tool == "calculator" and parsed.args == {"expression": "6*7"}


def test_final_answer_null_or_missing_is_parse_failure():
    # a null/omitted answer must NOT become the literal string "None" — reprompt instead
    assert isinstance(NativeFinishMode().parse_response(_tc("final_answer", {"answer": None})), ParseFailure)
    assert isinstance(NativeFinishMode().parse_response(_tc("final_answer", {})), ParseFailure)


def test_empty_tool_calls_falls_back_to_native():
    # under required this shouldn't happen, but if it does, inherit native's content->FinalAnswer path
    m = NativeFinishMode()
    got = m.parse_response(ModelResponse(content="here is the answer", tool_calls=[]))
    assert isinstance(got, FinalAnswer) and got.text == "here is the answer"
    nothing = m.parse_response(ModelResponse(content="", tool_calls=[]))
    assert isinstance(nothing, ParseFailure)


def test_malformed_args_is_parse_failure():
    resp = ModelResponse(content="", tool_calls=[
        {"id": "c", "type": "function", "function": {"name": "calculator", "arguments": "{bad json"}}])
    assert isinstance(NativeFinishMode().parse_response(resp), ParseFailure)


def test_final_answer_tool_schema_shape():
    fn = FINAL_ANSWER_TOOL["function"]
    assert fn["name"] == "final_answer"
    assert fn["parameters"]["required"] == ["answer"]


def test_chat_forwards_tool_choice_to_payload(monkeypatch):
    # a passed tool_choice must land in the request payload (default stays "auto")
    import asyncio

    from engine.model_client import ModelClient, ModelResponse as MR

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok", "tool_calls": []},
                                 "finish_reason": "stop"}], "usage": {}}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            captured.update(json)
            return _Resp()

    monkeypatch.setattr("engine.model_client.httpx.AsyncClient", _Client)
    c = ModelClient("http://x/v1", "m", "k")
    tools = [{"type": "function", "function": {"name": "calculator", "parameters": {}}}]
    asyncio.run(c.chat([{"role": "user", "content": "hi"}], tools=tools, tool_choice="required"))
    assert captured["tool_choice"] == "required"
    # and default when not passed
    captured.clear()
    asyncio.run(c.chat([{"role": "user", "content": "hi"}], tools=tools))
    assert captured["tool_choice"] == "auto"
