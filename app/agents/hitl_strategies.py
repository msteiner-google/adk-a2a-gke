"""Human-in-the-loop tools an agent can declare.

Two of the three HITL strategies are *tools*, and they live here. The third is
not a tool at all — it is an agent whose root is a graph
(``app/agents/planner/``) — so it has no place in this module. See
``docs/human-in-the-loop.md`` for which to reach for.

A. :data:`publish_result_tool` — ``require_confirmation``. A yes/no gate on a
   specific action. ADK stops the call before the tool body runs and synthesises
   an ``adk_request_confirmation`` long-running call. Strongest guarantee: the
   effect cannot happen early, whatever the model says.
B. :data:`request_input` (re-exported) — ADK's built-in free-form question. The
   model calls it when it needs a human; the reply is an ordinary
   ``FunctionResponse``, so arbitrary text/data reaches the model. No action is
   gated -- this asks, it does not guard.

A fourth shape was tried and rejected: running a graph from inside a tool via
``tool_context.run_node``. It pauses, but every node after the pause is skipped
while the caller still gets a plausible-looking answer. The code has been removed
so nobody wires it up by accident; the evidence is in
``docs/plans/hitl/results.md`` (R3).
"""

from __future__ import annotations

# `google.adk.tools.__all__` is built at runtime from a lazy-import map, so this
# IS the public path -- but a static checker cannot see through it and reads the
# name as a private re-export. Importing `._request_input_tool` directly would
# silence it by reaching into a private module instead; prefer the public name.
from google.adk.tools import request_input  # pyright: ignore[reportPrivateImportUsage]
from google.adk.tools.function_tool import FunctionTool

# Import at runtime, not under TYPE_CHECKING: ADK evaluates the annotation with
# typing.get_type_hints() when it builds each tool's declaration. See the note in
# app/agents/common.py.
from google.adk.tools.tool_context import ToolContext

__all__ = ["publish_result_tool", "request_input"]


# --- Strategy A: require_confirmation ---------------------------------------


def publish_result(value: str, tool_context: ToolContext) -> dict[str, str]:
    """Publish a computed result to the shared record. Requires approval.

    Args:
        value: The result to publish.
        tool_context: Injected by ADK.

    Returns:
        A mapping describing what was published.
    """
    # Reached ONLY after a human confirmed: ADK returns early otherwise, so this
    # write is the proof that the gate held (or didn't).
    note = ""
    confirmation = tool_context.tool_confirmation
    if confirmation is not None and isinstance(confirmation.payload, dict):
        note = str(confirmation.payload.get("note", ""))
    tool_context.state["published_value"] = value
    return {"status": "published", "value": value, "reviewer_note": note}


publish_result_tool = FunctionTool(func=publish_result, require_confirmation=True)


# --- Strategy B: request_input ------------------------------------------------
# Re-exported above as-is: ADK's `request_input` needs no wrapping. It is listed
# here so an agent imports both strategies from one place.
