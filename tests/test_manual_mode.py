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


# ---- progressive tool disclosure ----
# Manual mode is the mode where hiding a tool is most visible: the catalog IS the system prompt.
# The catalog must narrow, but `_known_tools` must NOT — it is what repairs the small model's
# habit of putting a tool NAME in the "action" field, and losing it for a hidden tool would turn a
# repairable envelope into a ParseFailure and a wasted reprompt.

def _disclosed_reg(visible=("calculator",)):
    from engine.tools.base import ToolRegistry
    from engine.tools.disclosure import DisclosedRegistry
    from engine.tools.time_tool import TimeTool
    full = ToolRegistry()
    full.register(CalculatorTool())
    full.register(TimeTool())
    return DisclosedRegistry(full, visible)


def test_build_request_catalog_narrows_to_visible_tools():
    m = ManualMode()
    req = m.build_request("", [{"role": "user", "content": "hi"}], _disclosed_reg())
    catalog = req["messages"][0]["content"]
    assert "calculator" in catalog
    assert "get_current_time" not in catalog


def test_build_request_keeps_known_tools_full():
    m = ManualMode()
    m.build_request("", [{"role": "user", "content": "hi"}], _disclosed_reg())
    assert m._known_tools == {"calculator", "get_current_time"}


def test_name_as_action_repair_still_fires_for_a_hidden_tool():
    m = ManualMode()
    m.build_request("", [{"role": "user", "content": "hi"}], _disclosed_reg())
    r = m.parse_response(ModelResponse(content='{"action":"get_current_time","args":{}}'))
    assert isinstance(r, ToolCall) and r.tool == "get_current_time" and r.repaired
