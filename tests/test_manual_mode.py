from engine.modes.manual import ManualMode, _extract_first_json_object
from engine.protocol import FinalAnswer, ModelResponse, ParseFailure, ToolCall
from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool


def reg():
    r = ToolRegistry()
    r.register(CalculatorTool())
    return r


def parse(content):
    return ManualMode().parse_response(ModelResponse(content=content))


def test_build_request_has_no_tools_param_but_lists_tools():
    m = ManualMode()
    req = m.build_request("", [{"role": "user", "content": "hi"}], reg())
    assert "tools" not in req
    assert "calculator" in req["messages"][0]["content"]
    assert '"action"' in req["messages"][0]["content"]


def test_parses_leading_whitespace():
    # observed real output: '\n\n{...}'
    r = parse('\n\n{"action":"tool","tool":"calculator","args":{"expression":"47 * 89"}}')
    assert isinstance(r, ToolCall) and r.tool == "calculator" and r.args["expression"] == "47 * 89"


def test_parses_fenced_json():
    r = parse('Sure!\n```json\n{"action":"final","answer":"hi"}\n```')
    assert isinstance(r, FinalAnswer) and r.text == "hi"


def test_parses_prose_wrapped_object():
    r = parse('I think the answer is {"action":"final","answer":"42"} ok')
    assert isinstance(r, FinalAnswer) and r.text == "42"


def test_nested_braces_in_args():
    r = parse('{"action":"tool","tool":"calculator","args":{"expression":"(1+2)*3"}}')
    assert isinstance(r, ToolCall) and r.args["expression"] == "(1+2)*3"


def test_unparseable_is_failure():
    assert isinstance(parse("I cannot produce JSON"), ParseFailure)


def test_missing_action_is_failure():
    assert isinstance(parse('{"tool":"calculator"}'), ParseFailure)


def test_unknown_action_is_failure():
    assert isinstance(parse('{"action":"sing","tune":"la"}'), ParseFailure)


def test_repairs_action_as_toolname():
    # observed real small-model output: {"action":"calculator", ...} (tool name in action)
    m = ManualMode(known_tools={"calculator"})
    r = m.parse_response(ModelResponse(
        content='{"action": "calculator", "args": {"expression": "47 * 89"}}'))
    assert isinstance(r, ToolCall) and r.tool == "calculator" and r.repaired is True
    assert r.args["expression"] == "47 * 89"


def test_no_repair_when_toolname_unknown():
    m = ManualMode(known_tools={"calculator"})
    assert isinstance(m.parse_response(ModelResponse(content='{"action":"sing"}')), ParseFailure)


def test_final_missing_answer_is_failure():
    assert isinstance(parse('{"action":"final"}'), ParseFailure)


def test_reprompt_messages_restate_format():
    m = ManualMode()
    msgs = m.reprompt_messages(ModelResponse(content="junk"),
                               ParseFailure(reason="no JSON", raw="junk"))
    assert msgs[-1]["role"] == "user" and '"action"' in msgs[-1]["content"]


def test_extract_handles_string_with_braces():
    # a brace inside a JSON string must not confuse the extractor
    s = '{"action":"final","answer":"use {curly} carefully"}'
    assert _extract_first_json_object("prefix " + s + " suffix") == s
