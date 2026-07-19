"""Safe arithmetic calculator — no eval(). Whitelisted AST only. Network-free."""
from __future__ import annotations

import ast
import math
import operator

from pydantic import BaseModel, Field

from engine.tools.base import Tool

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Whitelisted math functions (numbers in, number out). Deliberately excludes anything that can
# run away (e.g. factorial) — the exponent-size guard also applies to pow().
_FUNCS = {
    "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "abs": abs, "round": round, "pow": pow, "min": min, "max": max,
    "floor": math.floor, "ceil": math.ceil, "trunc": math.trunc,
    "exp": math.exp, "log": math.log, "log2": math.log2, "log10": math.log10,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "degrees": math.degrees, "radians": math.radians, "hypot": math.hypot,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


class _CalcError(Exception):
    pass


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _CalcError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise _CalcError(f"unknown name '{node.id}'")
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise _CalcError(f"operator {type(node.op).__name__} not allowed")
        # guard exponent size to avoid runaway compute
        if isinstance(node.op, ast.Pow):
            exp = _eval(node.right)
            if isinstance(exp, (int, float)) and abs(exp) > 1000:
                raise _CalcError("exponent too large")
        return op(_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARYOPS.get(type(node.op))
        if op is None:
            raise _CalcError(f"unary operator {type(node.op).__name__} not allowed")
        return op(_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise _CalcError("only direct function calls are allowed")
        fn = _FUNCS.get(node.func.id)
        if fn is None:
            raise _CalcError(f"function '{node.func.id}' is not allowed")
        if node.keywords:
            raise _CalcError("keyword arguments are not allowed")
        argvals = [_eval(a) for a in node.args]   # ast.Starred args fall through to a _CalcError
        # same runaway guard as the ** operator, for pow(base, exp)
        if node.func.id == "pow" and len(argvals) >= 2 \
                and isinstance(argvals[1], (int, float)) and abs(argvals[1]) > 1000:
            raise _CalcError("exponent too large")
        return fn(*argvals)
    raise _CalcError(f"{type(node).__name__} is not allowed")


class CalculatorTool(Tool):
    name = "calculator"
    description = (
        "Evaluate a math expression. Supports + - * / // % ** and parentheses, the functions "
        "sqrt, cbrt, pow, abs, round, min, max, floor, ceil, trunc, exp, log, log2, log10, "
        "sin, cos, tan, asin, acos, atan, atan2, hypot, degrees, radians, and the constants "
        "pi, e, tau. Numbers only (no variables). Examples: '47*(89+3)', 'sqrt(34)', 'sin(pi/2)'."
    )

    class Params(BaseModel):
        expression: str = Field(..., description="Math expression, e.g. 'sqrt(34)' or '47*89'")

    async def run(self, args: "CalculatorTool.Params") -> str:
        try:
            tree = ast.parse(args.expression, mode="eval")
        except SyntaxError as e:
            return f"calculator error: could not parse expression ({e.msg})"
        try:
            result = _eval(tree)
        except ZeroDivisionError:
            return "calculator error: division by zero"
        except _CalcError as e:
            return f"calculator error: {e}"
        except Exception as e:  # defensive: never crash the loop
            return f"calculator error: {e}"
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return str(result)
