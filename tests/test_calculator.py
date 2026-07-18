import asyncio

from engine.tools.calculator import CalculatorTool


def run(expr):
    t = CalculatorTool()
    return asyncio.run(t.run(t.Params(expression=expr)))


def test_basic_multiply():
    assert run("47*89") == "4183"


def test_precedence_and_parens():
    assert run("2 + 3 * 4") == "14"
    assert run("(2 + 3) * 4") == "20"


def test_power_and_float():
    assert run("2**10") == "1024"
    assert run("10 / 4") == "2.5"


def test_unary_and_modulo():
    assert run("-5 + 2") == "-3"
    assert run("17 % 5") == "2"


def test_divide_by_zero_is_clean_error():
    out = run("1/0")
    assert "error" in out.lower() and "zero" in out.lower()


def test_rejects_names_and_imports():
    out = run("__import__('os').system('echo hi')")
    assert "error" in out.lower()


def test_rejects_attribute_access():
    out = run("(1).__class__")
    assert "error" in out.lower()


def test_rejects_bad_syntax():
    out = run("2 +")
    assert "error" in out.lower()
