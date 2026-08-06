# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tools specific to the math agent."""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable

# Binary/unary operators allowed in the arithmetic evaluator. Anything outside
# this set (calls, names, attributes, ...) is rejected, so `calculate` never
# executes arbitrary code.
_BINARY_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_node(node: ast.AST) -> float:
    """Recursively evaluate a whitelisted arithmetic AST node.

    Args:
        node: The AST node to evaluate.

    Returns:
        The numeric result.

    Raises:
        ValueError: If the node uses an unsupported operation.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        return _BINARY_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("Unsupported expression")


def calculate(expression: str) -> dict[str, str]:
    """Safely evaluate a basic arithmetic expression.

    Supports ``+ - * / // % **``, parentheses, and unary signs only — no names,
    calls, or attribute access — so it never executes arbitrary code.

    Args:
        expression: The arithmetic expression, e.g. ``"(2 + 3) * 4"``.

    Returns:
        A mapping with the result, or an ``error`` status for invalid input.
    """
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree.body)
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError) as exc:
        return {"status": "error", "expression": expression, "error": str(exc)}
    return {"status": "ok", "expression": expression, "result": str(result)}
