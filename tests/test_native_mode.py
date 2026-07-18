from engine.modes.native import NativeMode
from engine.protocol import FinalAnswer, ModelResponse, ParseFailure, ToolCall
from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool


def reg():
    r = ToolRegistry()
    r.register(CalculatorTool())
    return r


def test_build_request_has_tools():
    m = NativeMode()
    req = m.build_request("", [{"role": "user", "content": "hi"}], reg())
    assert req["messages"][0]["role"] == "system"
    assert req["messages"][1]["content"] == "hi"
    assert any(f["function"]["name"] == "calculator" for f in req["tools"])


def test_parse_tool_call():
    m = NativeMode()
    resp = ModelResponse(content=None, tool_calls=[
        {"id": "c1", "type": "function",
         "function": {"name": "calculator", "arguments": '{"expression": "47*89"}'}}],
        finish_reason="tool_calls")
    r = m.parse_response(resp)
    assert isinstance(r, ToolCall)
    assert r.tool == "calculator" and r.args["expression"] == "47*89" and r.call_id == "c1"


def test_parse_final():
    m = NativeMode()
    r = m.parse_response(ModelResponse(content="the answer is 42", finish_reason="stop"))
    assert isinstance(r, FinalAnswer) and r.text == "the answer is 42"


def test_parse_bad_args_json_is_failure():
    m = NativeMode()
    resp = ModelResponse(content=None, tool_calls=[
        {"id": "c1", "function": {"name": "calculator", "arguments": "{not json}"}}])
    assert isinstance(m.parse_response(resp), ParseFailure)


def test_parse_empty_is_failure():
    m = NativeMode()
    assert isinstance(m.parse_response(ModelResponse(content=None)), ParseFailure)


def test_tool_result_messages_shape():
    m = NativeMode()
    resp = ModelResponse(content=None, tool_calls=[
        {"id": "c1", "function": {"name": "calculator", "arguments": '{"expression":"1+1"}'}}])
    call = ToolCall(tool="calculator", args={"expression": "1+1"}, call_id="c1")
    msgs = m.tool_result_messages(resp, call, "2")
    assert msgs[0]["role"] == "assistant" and msgs[0]["tool_calls"][0]["id"] == "c1"
    assert msgs[1] == {"role": "tool", "tool_call_id": "c1", "content": "2"}
