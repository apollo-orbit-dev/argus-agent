"""Safe arithmetic calculator — no eval(). Whitelisted AST only. Network-free."""
from __future__ import annotations

import ast
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


class _CalcError(Exception):
    pass


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _CalcError("only numbers are allowed")
        return node.value
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
    raise _CalcError(f"{type(node).__name__} is not allowed")


class CalculatorTool(Tool):
    name = "calculator"
    description = ("Evaluate a basic arithmetic expression (+, -, *, /, //, %, **, parentheses). "
                  "Numbers only, no variables or functions. Example: '47 * (89 + 3)'.")

    class Params(BaseModel):
        expression: str = Field(..., description="Arithmetic expression, e.g. '47*89'")

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
