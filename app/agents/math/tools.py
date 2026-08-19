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

"""Tools specific to the math agent.

Besides ``calculate``, this module carries the repo's example of an action that
needs a human's sign-off (``docs/design-decisions.md``, D5).

It is deliberately **one tool, not two**. :func:`publish_result` looks at whether
an approval is present: without one it describes what it *would* do and performs
nothing; with one it goes ahead. There is no long-running call, no resumability
config, no lease and no sweeper — which is what lets an approval take a week, and
lets a specialist on another framework implement the same contract.

**The specialist holds nothing between the two calls.** It does not remember what
it proposed; the caller owns the case record and re-sends the original request
with the approval attached. The value is *recomputed* from ``expression`` rather
than passed back, so the same request necessarily produces the same result and
there is nothing for a caller to retype incorrectly.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any

from app.agents.contracts import APPROVAL_REQUIRED, PUBLISHED

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


# --- The gated action -----------------------------------------------------

PUBLISH_ACTION = "publish_result"
"""Names the effect in a proposal, so a reviewer sees what they are approving."""

#: Where a published value lands. A module-level list stands in for whatever the
#: real effect would be (a write to a system of record). It is here so tests can
#: assert the effect happened exactly once, and only after approval.
PUBLICATIONS: list[dict[str, str]] = []


def canonical_value(value: str) -> str:
    """Normalise a numeric result to one stable spelling.

    The caller confirms an execution by comparing the published values against
    the approved proposal, so the same number reached twice has to produce the
    same string. Left to a model it does not: a live run proposed
    ``'391000000'`` and then published ``'391000000.0'`` -- arithmetically
    identical, textually different, and the case was correctly refused as
    unconfirmed.

    Canonicalising at the source rather than loosening the comparison keeps the
    check strict: a genuinely different number still fails it.

    Args:
        value: The result as the model supplied it.

    Returns:
        The canonical spelling, or the input unchanged when it is not numeric.
    """
    try:
        number = float(value)
    except TypeError, ValueError:
        return value
    return str(int(number)) if number.is_integer() else repr(number)


def publish_result(
    value: str, label: str, approved_by: str = "", note: str = ""
) -> dict[str, Any]:
    """Publish a computed result, or propose doing so if nobody has approved yet.

    One function, two behaviours, chosen by whether an approval is present. That
    is the whole gate: with no ``approved_by`` the effect below is unreachable,
    so a model that decides to publish on its own simply cannot.

    Args:
        value: The result to publish.
        label: The label to publish it under.
        approved_by: Who approved it. Empty means "not approved yet", and the
            call returns a proposal instead of publishing.
        note: Optional feedback the approver attached, recorded for audit.

    Returns:
        Either ``status='approval_required'`` describing what would happen, or
        ``status='published'`` confirming what did.
    """
    value = canonical_value(value)
    if not approved_by.strip():
        return {
            "status": APPROVAL_REQUIRED,
            "action": PUBLISH_ACTION,
            "proposal": {"action": PUBLISH_ACTION, "value": value, "label": label},
            "summary": f"Publish {value!r} under label {label!r}.",
        }

    record = {"value": value, "label": label, "approved_by": approved_by}
    PUBLICATIONS.append(record)
    # `status` must stay a member of contracts.EFFECT_PERFORMED: it is what the
    # caller scans for to confirm the approved action actually ran.
    return {"status": PUBLISHED, "action": PUBLISH_ACTION, "note": note, **record}
