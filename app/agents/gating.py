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

"""Suspend a tool until a human authorises it, then run it for real.

A gated tool asks :func:`require_approval` for a decision. On the first call
there is none, so the tool suspends; once a human answers, **ADK re-executes
the same tool with the same arguments** and the call returns a decision the
tool can act on::

    def publish_result(value: str, label: str, tool_context: ToolContext):
        value = canonical_value(value)
        decision = require_approval(
            tool_context, summary=f"Publish {value!r} under label {label!r}."
        )
        if decision.pending:
            return AWAITING_APPROVAL          # suspended; nothing happened
        if not decision.granted:
            return {"status": REFUSED, ...}   # a human said no
        PUBLICATIONS.append(...)              # only reachable once granted
        return {"status": PUBLISHED, "approved_by": decision.approved_by, ...}

**Why re-execution matters, and what it replaces.** The obvious design is a
long-running tool that returns ``None`` to pause, with the human's answer
arriving as the function response. It was built here first and it does not
work. ADK hands that response to the *model* as the tool's result and never
calls the tool again, so the effect is never performed -- while the model,
reading a response that says ``approved_by: alice``, cheerfully reports that
it published. Measured on this repo::

    [3] function_call  publish_result {"approved_by": "", "value": "391000000.0"}
    [5] function_response publish_result {"approved_by": "alice@bnpp.com"}
    [6] text  "The result 391000000.0 has been published under the label
               q3-revenue, approved by alice@bnpp.com"

Nothing published. Three separate failures in this codebase have had that
shape, which is why the rule here is to assert on an effect the code emits.

ADK's tool-confirmation flow does not have the gap. The request processor at
``google/adk/flows/llm_flows/request_confirmation.py`` explicitly re-runs the
tool once the decision arrives::

    # Step 4: Re-execute the confirmed tools.
    if function_response_event := await functions.handle_function_call_list_async(
        invocation_context,
        list(tools_to_resume_with_args.values()),
        tools_dict,
        set(tools_to_resume_with_confirmation.keys()),
        tools_to_resume_with_confirmation,
    ):

so the effect is performed by the tool, in code, with the arguments a human
actually saw -- not by a model deciding to call it again.

**What the reviewer sees.** Requesting a confirmation makes ADK emit a
synthetic long-running function call named ``adk_request_confirmation`` whose
arguments carry both the original call and the hint::

    {"originalFunctionCall": {"name": "publish_result",
                              "args": {"value": "391000000", "label": "q3-revenue"}},
     "toolConfirmation": {"hint": "Publish '391000000' under label 'q3-revenue'."}}

That is the proposal. It is generated from the real pending call rather than
restated by a model, so it cannot drift from what would actually run. It is
also self-identifying, which is what lets
``app/cluster/authorization.py`` report the suspension as
``TASK_STATE_AUTH_REQUIRED`` without maintaining a list of gated tool names.

**The gate is the ``granted`` check, not the task state.** A2A spec 7.6.4 is
explicit that ``TASK_STATE_AUTH_REQUIRED`` authorises nothing by itself, and
that the implementation must define "how the authorized operation is
identified and how that authorization is checked before the operation is
performed". Suspending is how the human is asked; the branch on
:attr:`Approval.granted` is what keeps the effect unreachable until one
answers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ToolContext must be imported at RUNTIME: ADK builds each tool's declaration
# with typing.get_type_hints(), which evaluates the annotation. Under
# `from __future__ import annotations` a TYPE_CHECKING-only import raises
# NameError at request time and breaks every tool in the module.
from google.adk.tools.tool_context import ToolContext

from app.agents.statuses import AWAITING_APPROVAL, REFUSED

__all__ = [
    "AWAITING_APPROVAL",
    "REFUSED",
    "Approval",
    "gated",
    "is_gated",
    "require_approval",
]

#: Attribute set by :func:`gated`. Read through :func:`is_gated`.
_GATED = "__suspends_for_authorization__"


def gated[F: Callable[..., Any]](func: F) -> F:
    """Mark a tool as one that can suspend pending human authorization.

    This is not decoration for its own sake: it decides **how the agent that
    owns this tool is wired into its callers**. A peer that can suspend has to
    be reached as a sub-agent, because an ``AgentTool`` runs the peer to
    exhaustion and drops ``long_running_tool_ids`` -- a suspended tool then
    looks to the caller like one that answered with an empty string, and the
    authorization request never reaches a human.

    A peer that cannot suspend is better off as a tool, because
    ``transfer_to_agent`` hands control over one way: the caller's invocation
    ends, so it can never use what the peer returned. Marking every peer as
    gated would therefore break ordinary composition, and marking none would
    break authorization.

    ``tests/unit/test_agents.py`` cross-checks this against the source: a tool
    that calls :func:`require_approval` without this marker fails there rather
    than silently having its suspensions swallowed at runtime.

    Args:
        func: The gated tool function.

    Returns:
        ``func``, marked.
    """
    setattr(func, _GATED, True)
    return func


def is_gated(tool: object) -> bool:
    """Return whether ``tool`` can suspend pending human authorization.

    Args:
        tool: A plain function or a ``BaseTool``.

    Returns:
        True when the tool (or the function a ``BaseTool`` wraps) is marked by
        :func:`gated`.
    """
    if getattr(tool, _GATED, False):
        return True
    return bool(getattr(getattr(tool, "func", None), _GATED, False))


@dataclass(frozen=True)
class Approval:
    """The authorization state of the call currently being executed.

    Attributes:
        pending: True when no human has answered yet. The tool has just asked
            for authorization and must return without acting.
        granted: True when a human authorised this exact call.
        approved_by: Who answered, taken from the decision payload. Empty when
            the decision carried no identity.
        note: Any feedback the approver attached, for the audit record.
    """

    pending: bool
    granted: bool
    approved_by: str = ""
    note: str = ""


def require_approval(
    tool_context: ToolContext,
    *,
    summary: str,
    proposal: dict[str, Any] | None = None,
) -> Approval:
    """Return the authorization decision for the call in progress.

    On the first invocation this records a request for confirmation, which
    suspends the invocation and surfaces ``summary`` to whoever must decide.
    ADK then re-executes the tool with the same arguments once an answer
    arrives, and this returns that answer.

    Args:
        tool_context: The calling tool's context. Required -- the confirmation
            is keyed on ``tool_context.function_call_id``.
        summary: One line telling a human exactly what is about to happen.
            Compose it from the tool's own arguments so it cannot describe
            something other than what would run.
        proposal: The exact values the tool will act on, once normalised. Pass
            this whenever the tool canonicalises its inputs, or the caller ends
            up comparing the model's raw arguments against a normalised result
            and correctly refuses to confirm a perfectly good execution.
            Measured: a proposal recorded as ``88000.0`` against a published
            ``88000`` was reported as ``approved_not_confirmed``. Canonicalise
            at the source rather than loosening the comparison — that check is
            what catches a specialist doing something *else*.

    Returns:
        An :class:`Approval`. Check ``pending`` first and return without acting
        if it is set; then check ``granted``.
    """
    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        tool_context.request_confirmation(hint=summary, payload=proposal)
        # Without this the model narrates the placeholder return value as
        # though it were an outcome, which is how a suspended action gets
        # reported to the user as a completed one.
        tool_context.actions.skip_summarization = True
        return Approval(pending=True, granted=False)

    payload: Any = confirmation.payload or {}
    if not isinstance(payload, dict):
        payload = {}
    return Approval(
        pending=False,
        granted=bool(confirmation.confirmed),
        approved_by=str(payload.get("approved_by", "")),
        note=str(payload.get("note", "")),
    )
