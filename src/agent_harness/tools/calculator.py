"""用 AST 白名单计算有限范围内的四则运算。"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, TypeAlias

from agent_harness.tools.registry import ToolDefinition, ToolError

_MAX_LENGTH = 256
_MAX_MAGNITUDE = 10**15
_EXPRESSION_CHARS = re.compile(r"[0-9.eE+*/()\s-]+", re.ASCII)
_Number: TypeAlias = int | Fraction


def _divide(left: _Number, right: _Number) -> Fraction:
    return Fraction(left) / right


_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: _divide,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _check_range(value: _Number | Decimal) -> None:
    if not -_MAX_MAGNITUDE <= value <= _MAX_MAGNITUDE:
        raise ToolError(
            "out_of_range", f"数值或中间结果的绝对值不能超过 {_MAX_MAGNITUDE}。"
        )
    if value and float(value) == 0.0:
        raise ToolError("out_of_range", "非零数值或中间结果过小，转换为浮点数会变成零。")


def _evaluate(node: ast.expr, expression: str) -> _Number:
    if isinstance(node, ast.Constant):
        if type(node.value) is int:
            _check_range(node.value)
            return node.value
        if type(node.value) is float:
            # AST 中的浮点值可能已舍入。按原文检查范围，再转换为精确分数。
            literal = ast.get_source_segment(expression, node)
            assert literal is not None
            exact = Decimal(literal)
            _check_range(exact)
            return Fraction(exact)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_evaluate(node.operand, expression))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        left = _evaluate(node.left, expression)
        right = _evaluate(node.right, expression)
        value = _BINARY_OPS[type(node.op)](left, right)
        _check_range(value)
        return value
    raise ToolError("invalid_expression", "仅支持实数、加减乘除和括号。")


def _calculate(arguments: Mapping[str, Any]) -> dict[str, int | float]:
    expression = arguments["expression"].strip()
    if not _EXPRESSION_CHARS.fullmatch(expression):
        raise ToolError("invalid_expression", "仅支持十进制数、加减乘除和括号。")
    try:
        tree = ast.parse(expression, mode="eval")
        value = _evaluate(tree.body, expression)
        return {"value": float(value) if isinstance(value, Fraction) else value}
    except SyntaxError:
        raise ToolError("invalid_expression", "表达式语法不合法。") from None
    except ZeroDivisionError:
        raise ToolError("division_by_zero", "除数不能为零。") from None
    except (InvalidOperation, OverflowError, RecursionError):
        raise ToolError("out_of_range", "表达式超出可计算范围。") from None


CALCULATOR = ToolDefinition(
    name="calculator",
    description="计算十进制数的四则运算表达式，支持括号和正负号。",
    parameters_schema={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "minLength": 1,
                "maxLength": _MAX_LENGTH,
                "pattern": r"\S",
            }
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
    handler=_calculate,
)
