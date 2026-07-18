from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool
from engine.tools.time_tool import TimeTool


def reg():
    r = ToolRegistry()
    r.register(CalculatorTool())
    r.register(TimeTool())
    return r


def test_get_and_list():
    r = reg()
    assert r.get("calculator") is not None
    assert r.get("nope") is None
    assert set(r.names()) == {"calculator", "get_current_time"}


def test_validate_ok():
    r = reg()
    v = r.validate("calculator", {"expression": "1+1"})
    assert v.ok and v.args.expression == "1+1"


def test_validate_missing_required():
    r = reg()
    v = r.validate("calculator", {})
    assert not v.ok and "expression" in v.error


def test_validate_unknown_tool():
    r = reg()
    v = r.validate("frobnicate", {"x": 1})
    assert not v.ok and "unknown tool" in v.error.lower()


def test_validate_non_dict_args():
    r = reg()
    v = r.validate("calculator", ["1+1"])
    assert not v.ok and "object" in v.error.lower()


def test_openai_schema_shape():
    r = reg()
    schema = r.openai_schema()
    names = {f["function"]["name"] for f in schema}
    assert names == {"calculator", "get_current_time"}
    calc = next(f for f in schema if f["function"]["name"] == "calculator")
    assert calc["type"] == "function"
    assert "expression" in calc["function"]["parameters"]["properties"]


def test_text_schema_readable():
    r = reg()
    txt = r.text_schema()
    assert "calculator" in txt and "expression" in txt
    assert "get_current_time" in txt and "timezone" in txt
    assert "required" in txt
