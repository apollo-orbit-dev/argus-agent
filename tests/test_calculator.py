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


def test_sqrt_and_functions():
    assert run("sqrt(16)") == "4"
    assert run("sqrt(34)").startswith("5.83")
    assert run("pow(2, 10)") == "1024"
    assert run("abs(-5)") == "5"
    assert run("round(3.14159, 2)") == "3.14"
    assert run("max(3, 7, 2)") == "7"
    assert run("min(3, 7, 2)") == "2"
    assert run("floor(2.9)") == "2"
    assert run("hypot(3, 4)") == "5"


def test_trig_and_log_and_constants():
    assert run("sin(0)") == "0"
    assert run("log(e)") == "1"
    assert run("log10(1000)") == "3"
    assert run("pi").startswith("3.14159")
    assert run("cos(tau)") == "1"       # cos(2*pi) == 1
    assert run("sin(pi/2)") == "1"


def test_pow_function_respects_exponent_guard():
    out = run("pow(2, 5000)")
    assert "error" in out.lower() and "exponent" in out.lower()


def test_rejects_non_whitelisted_function():
    assert "error" in run("factorial(5)").lower()      # deliberately not whitelisted (runaway risk)
    assert "error" in run("eval('1')").lower()
    assert "not allowed" in run("open('x')").lower()


def test_rejects_unknown_name():
    assert "error" in run("x + 1").lower()
    assert "unknown name" in run("foo").lower()
