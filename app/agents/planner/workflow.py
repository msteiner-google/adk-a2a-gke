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

"""The planner graph: draft a plan, ask the human, then apply their feedback.

This is a deterministic ADK ``Workflow`` — three nodes wired by edges, no model
deciding the control flow:

``START -> draft_plan -> collect_feedback -> apply_feedback``

``collect_feedback`` yields a :class:`RequestInput`, which pauses the whole graph
until a human replies. Everything after it is ordinary graph execution, which is
the property being tested: an ``LlmAgent`` that pauses resumes by handing the
reply to a *model*, whereas here the reply must flow into the next **node**.

``apply_feedback`` writes :data:`APPLIED_MARKER` into its output. That marker is
the evidence that the post-pause node actually ran: the rejected shortcut (this
same graph executed via ``tool_context.run_node`` from inside a tool) pauses and
returns a plausible answer while silently skipping this node — see
``docs/plans/hitl/results.md`` (R3). Grep the logs for the marker rather than
trusting the prose.
"""

from __future__ import annotations

from typing import Any

from google.adk.events import RequestInput
from google.adk.workflow import Workflow
from google.genai import types
from loguru import logger

APPLIED_MARKER = "PLAN-APPLIED-BY-GRAPH"
"""Emitted only by the node *after* the human pause. Absence proves a skip."""


def _as_text(node_input: Any) -> str:
    """Flatten a node input to text.

    The first node of a graph receives the raw user ``Content``, not a string,
    so ``str()`` on it yields a Part repr. Later nodes receive whatever the
    previous node returned.
    """
    parts = getattr(node_input, "parts", None)
    if parts:
        return "".join(getattr(p, "text", "") or "" for p in parts).strip()
    return str(node_input or "").strip()


def draft_plan(node_input: Any) -> str:
    """Draft an initial plan from the request.

    Args:
        node_input: Whatever the caller sent (the request text).

    Returns:
        The draft plan text.
    """
    logger.info("planner: draft_plan ran")
    topic = _as_text(node_input) or "an unspecified topic"
    return (
        f"Draft plan for {topic}:\n"
        "  1. Assess the current state.\n"
        "  2. Make the change.\n"
        "  3. Verify the outcome."
    )


def collect_feedback(node_input: Any):
    """Pause the graph and show the draft to a human.

    Args:
        node_input: The draft produced by :func:`draft_plan`.

    Yields:
        A :class:`RequestInput` describing what the human should reply with.
    """
    logger.info("planner: collect_feedback ran, pausing for a human")
    yield RequestInput(
        message=(
            f"{node_input}\n\n"
            "Reply with the changes you want; the plan is revised with them."
        ),
        payload={"draft": str(node_input)},
        response_schema=str,
    )


def apply_feedback(node_input: Any) -> types.Content:
    """Fold the human's reply into the final plan.

    Returns ``Content``, not a plain string, and that distinction is
    load-bearing. A node returning any other value is wrapped in
    ``Event(output=...)`` (``workflow/_function_node.py:394``), and **nothing
    converts ``event.output`` into an A2A message** -- the graph would finish
    correctly while the caller received an empty answer. Returning ``Content``
    produces ``Event(content=...)``, which is what the A2A converter, the ADK
    web UI and any text-reading caller actually look at. See
    ``docs/plans/hitl/results.md`` (R8).

    Args:
        node_input: The human's reply, routed here by the graph.

    Returns:
        The revised plan as model content, tagged with :data:`APPLIED_MARKER`.
    """
    logger.info("planner: apply_feedback ran ({})", APPLIED_MARKER)
    text = (
        f"{APPLIED_MARKER}\n"
        f"Revised plan, incorporating the reviewer's instruction "
        f"({_as_text(node_input)!r}):\n"
        "  1. Assess the current state.\n"
        "  2. Apply the reviewer's change.\n"
        "  3. Make the change.\n"
        "  4. Verify the outcome, with the reviewer's condition checked."
    )
    return types.Content(role="model", parts=[types.Part(text=text)])


planner_workflow = Workflow(
    name="planner",
    edges=[("START", draft_plan, collect_feedback), (collect_feedback, apply_feedback)],
)
