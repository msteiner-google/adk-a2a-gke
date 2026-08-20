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

"""Deliver a human's decision to the agent whose tool is suspended.

A grant is sent **directly to the agent that owns the gated tool**, not down
the chain of callers that relayed the request. That is not a shortcut; it is
forced by how ADK models confirmations.

**Why not cascade.** A2A spec 7.6.2 describes a chain of Tasks in
``TASK_STATE_AUTH_REQUIRED``, and the obvious reading is that resolving the
top one resolves the rest. ADK cannot do that. When a peer suspends a tool,
the confirmation reaches the caller as an ordinary function call in the
caller's own session, so the caller believes the confirmation is its to
resolve. On a grant it looks for the tool among its own::

    # google/adk/flows/llm_flows/request_confirmation.py
    if not tools_to_resume_with_confirmation:
      return

The tool lives one hop away, nothing matches, and the grant is dropped in
silence. Measured on this repo, twice: after granting at the orchestrator the
specialist received no traffic at all, and the orchestrator's task sat in
``working`` indefinitely. Suppressing the caller's duplicate confirmation was
tried and did not help -- the duplicate is a function-call part, not the
``requested_tool_confirmations`` action.

So the request bubbles **up** through the callers (which works, and is what
tells the human something is pending), while the decision goes **straight
down** to the owner (which works, and is where the tool actually is). The two
paths are asymmetric on purpose.

**What arrives at the owner** is a function response addressed to the
suspended ``adk_request_confirmation`` call. ADK's request processor then
re-executes the original tool with the decision attached, so the effect is
performed by code rather than narrated by a model -- see
``app/agents/gating.py``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import httpx
from loguru import logger

if TYPE_CHECKING:
    from app.cluster.config import PeerSpec

# a2a-sdk serves the JSON-RPC route with `enable_v0_3_compat=True`, so a request
# arriving with no version header is treated as protocol 0.3 -- and a 1.0 method
# name on an unversioned request comes back as HTTP 200 carrying a JSON-RPC
# -32009 body, which reads as "the agent had nothing to say". Always send it.
A2A_HEADERS = {"A2A-Version": "1.0", "Content-Type": "application/json"}

# How long to wait for the owner to run the approved action. A gated action can
# be a BigQuery job, so this is generous; it is a ceiling, not an expectation.
GRANT_TIMEOUT_SECONDS = 300.0


class GrantDeliveryError(RuntimeError):
    """The decision could not be delivered to the agent that owns the tool."""


def _rpc_url(peer: PeerSpec, rpc_path: str) -> str:
    """Return the JSON-RPC endpoint for a peer.

    Args:
        peer: The peer that owns the suspended tool.
        rpc_path: The configured A2A RPC path (``/a2a/app`` by default).

    Returns:
        The peer's JSON-RPC URL.
    """
    return f"{peer.base_url}{rpc_path}"


def confirmation_message(
    *,
    task_id: str,
    context_id: str,
    confirmation_id: str,
    confirmed: bool,
    approved_by: str,
    note: str,
) -> dict[str, Any]:
    """Build the A2A message that answers a suspended confirmation.

    The shape is dictated by ADK on the receiving side: a data part whose
    ``adk_type`` metadata marks it as a function response, carrying a
    serialized ``FunctionResponse`` whose ``id`` matches the suspended
    ``adk_request_confirmation`` call. ``ToolConfirmation`` forbids unknown
    fields, so ``confirmed`` and ``payload`` are the only two keys allowed --
    the approver's identity travels inside ``payload``.

    Args:
        task_id: The owner's suspended task.
        context_id: That task's context.
        confirmation_id: The id of the ``adk_request_confirmation`` call.
        confirmed: Whether the human approved.
        approved_by: Who decided.
        note: Any feedback to record alongside the effect.

    Returns:
        The ``message`` parameter for a ``SendMessage`` call.
    """
    return {
        "messageId": uuid.uuid4().hex,
        "role": "ROLE_USER",
        "taskId": task_id,
        "contextId": context_id,
        "parts": [
            {
                "data": {
                    "id": confirmation_id,
                    "name": "adk_request_confirmation",
                    "response": {
                        "confirmed": confirmed,
                        "payload": {"approved_by": approved_by, "note": note},
                    },
                },
                "metadata": {"adk_type": "function_response"},
            }
        ],
    }


async def deliver(
    peer: PeerSpec,
    *,
    rpc_path: str,
    task_id: str,
    context_id: str,
    confirmation_id: str,
    confirmed: bool,
    approved_by: str,
    note: str = "",
) -> dict[str, Any]:
    """Send a decision to the agent that owns the suspended tool.

    Args:
        peer: The peer that owns the tool.
        rpc_path: The configured A2A RPC path.
        task_id: The owner's suspended task.
        context_id: That task's context.
        confirmation_id: The id of the ``adk_request_confirmation`` call.
        confirmed: Whether the human approved.
        approved_by: Who decided.
        note: Any feedback to record alongside the effect.

    Returns:
        The owner's task as it stood when the call returned.

    Raises:
        GrantDeliveryError: If the owner could not be reached, or answered with
            a JSON-RPC error. The caller must treat this as "the decision is
            recorded but not carried out" and leave the case re-drivable.
    """
    url = _rpc_url(peer, rpc_path)
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {
            "message": confirmation_message(
                task_id=task_id,
                context_id=context_id,
                confirmation_id=confirmation_id,
                confirmed=confirmed,
                approved_by=approved_by,
                note=note,
            )
        },
    }
    try:
        async with httpx.AsyncClient(timeout=GRANT_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=A2A_HEADERS, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPError as exc:
        raise GrantDeliveryError(
            f"Could not reach {peer.name!r} at {url}: {exc}"
        ) from exc

    if "error" in body:
        raise GrantDeliveryError(
            f"Agent {peer.name!r} refused the decision: {body['error']}"
        )

    result = body.get("result", {})
    task = result.get("task", result)
    logger.info(
        "Delivered decision to {peer} task {task_id}: state={state}",
        peer=peer.name,
        task_id=task_id,
        state=task.get("status", {}).get("state"),
    )
    return task if isinstance(task, dict) else {}
