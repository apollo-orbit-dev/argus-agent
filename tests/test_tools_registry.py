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


# ---- DisclosedRegistry delegation invariants ----
# The view subclasses ToolRegistry and shares its `_tools` dict by reference, so every method the
# loop DISPATCHES through must behave exactly as the base class does. Only openai_schema/text_schema
# may differ. These assert the delegation, method by method, against a hidden tool.

def _disclosed(visible=("calculator",)):
    from engine.tools.disclosure import DisclosedRegistry
    full = reg()
    return full, DisclosedRegistry(full, visible)


def test_disclosed_get_delegates_for_a_hidden_tool():
    _full, view = _disclosed()
    assert view.get("get_current_time") is not None
    assert view.get("nope") is None


def test_disclosed_names_and_list_delegate_in_full():
    full, view = _disclosed()
    assert view.names() == full.names()
    assert [t.name for t in view.list()] == [t.name for t in full.list()]


def test_disclosed_validate_delegates_for_a_hidden_tool():
    _full, view = _disclosed()
    v = view.validate("get_current_time", {})
    assert v.ok is True


def test_disclosed_validate_still_reports_bad_args_for_a_hidden_tool():
    _full, view = _disclosed()
    v = view.validate("calculator", {})
    assert not v.ok and "expression" in v.error


def test_disclosed_unregister_delegates_to_the_shared_dict():
    full, view = _disclosed()
    assert view.unregister("get_current_time") is True
    assert full.get("get_current_time") is None
    assert view.unregister("get_current_time") is False


def test_disclosed_openai_schema_is_the_only_narrowed_surface():
    full, view = _disclosed()
    assert len(full.openai_schema()) == 2
    assert len(view.openai_schema()) == 1
    view.reveal(full.names())
    assert view.openai_schema() == full.openai_schema()
    assert view.text_schema() == full.text_schema()
