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

"""Make a specialist's structured results survive the A2A text boundary.

**The problem this solves, measured rather than assumed.** A specialist's tool
returns a dict, but ADK's ``AgentTool`` reduces a peer's A2A response to its
merged *text* parts, so the caller receives whatever the specialist's model chose
to write. Asked to report a proposal verbatim, `math` instead replied:

    "I can propose to publish '391.0' under the label 'q3-revenue'. The proposal
     digest is '2700779a219514e5'. Nothing has been published yet."

Accurate, readable — and structurally useless. The orchestrator found no
proposal to record, so no approval case was opened, while the model went on to
tell the user it had published the result. Exactly the failure this repo keeps
rediscovering: a plausible answer that is not proof the flow ran.

**The fix is to stop asking a model to be a serializer.** This callback runs
after the agent finishes and, if the turn produced any audit-relevant tool result
— a proposal awaiting approval, or the confirmation that an approved action ran —
emits one final event restating those results as verbatim JSON. ADK appends that
event after the agent's own (``base_agent.py:_handle_after_agent_callback``), and
``AgentTool`` returns the *last* content — so the caller receives the structured
payload deterministically, whatever prose the model produced along the way.

The agent's own wording is preserved above the JSON, so a human reading the
transcript still gets the readable version.

This is attached to every agent by ``build_agent``. It is a no-op for a turn that
proposed nothing, which is nearly all of them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from google.genai import types

from app.agents.contracts import APPROVAL_REQUIRED, EFFECT_PERFORMED, NEEDS_USER

#: Tool-result statuses that must reach the caller intact rather than as prose:
#: an action awaiting sign-off, the confirmation that an approved one ran, and a
#: question only the user can answer.
#:
#: Derived from the contract vocabulary rather than written out, which is not
#: tidiness. This set said `{APPROVAL_REQUIRED, "published"}` when the trades
#: agent landed, so a gated *read* reporting `executed` was silently not
#: restated -- it survived only because the model chose to repeat it, which is
#: exactly the thing this callback exists to stop relying on.
AUDITED_STATUSES = frozenset({APPROVAL_REQUIRED}) | EFFECT_PERFORMED | NEEDS_USER

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.events.event import Event

# Marks the JSON block this callback appends, so a reader (and a future
# maintainer staring at a transcript) can tell it apart from model prose.
RESULT_HEADER = "Structured result(s):"


def _embedded(text: str) -> list[dict[str, Any]]:
    """Return JSON objects embedded anywhere in a string.

    This is what makes the callback work more than one hop deep. A peer reached
    through ``PeerTool`` answers as TEXT, so on the *caller* the reply is a
    string tool result, not a dict -- and a proposal or a question raised two
    levels down (``orchestrator -> math -> currency``) would be invisible to the
    dict-only scan below, leaving it to the middle agent's model to relay
    faithfully. That is precisely the dependency this module exists to remove.

    Args:
        text: A string tool result to scan.

    Returns:
        Every top-level JSON object found in it, in order.
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            found.append(value)
            index = text.find("{", end)
        else:
            index = text.find("{", index + 1)
    return found


def _payloads(event: Event) -> list[dict[str, Any]]:
    """Return the tool-result payloads an event carries.

    Two shapes, because ADK produces both: the tool's dict as-is, and the
    ``{"result": ...}`` wrapper. The wrapper is where a remote peer's reply
    lands, as a STRING -- ``FunctionResponse.response`` is typed ``dict | None``,
    so the peer's own JSON is a value inside it rather than the payload itself,
    and it has to be scanned out.

    Args:
        event: The event to inspect.

    Returns:
        Every candidate payload dict on the event.
    """
    found: list[dict[str, Any]] = []
    for response in event.get_function_responses():
        payload = response.response
        if not isinstance(payload, dict):
            continue
        inner = payload.get("result")
        if isinstance(inner, dict):
            found.append(inner)
        elif isinstance(inner, str):
            found.extend(_embedded(inner))
        else:
            found.append(payload)
    return found


def restate_structured_results(
    callback_context: CallbackContext,
) -> types.Content | None:
    """Restate this turn's audit-relevant tool results as verbatim JSON.

    Args:
        callback_context: Injected by ADK after the agent finishes.

    Returns:
        Content carrying the agent's own words plus the structured results, or
        ``None`` when the turn produced none (the common case, in which the
        agent's reply is left exactly as it was).
    """
    # ADK exposes no public accessor for the invocation's events from a
    # callback; this is the same private reach the framework's own callbacks
    # use. If it breaks on an ADK upgrade, tests/unit/test_reporting.py fails.
    ctx = callback_context._invocation_context
    invocation_id = ctx.invocation_id

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    spoken = ""
    for event in ctx.session.events:
        if event.invocation_id != invocation_id:
            continue
        for payload in _payloads(event):
            if payload.get("status") not in AUDITED_STATUSES:
                continue
            key = json.dumps(payload, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                results.append(payload)
        # `ctx.agent` is Optional on InvocationContext; a callback always has
        # one, but read it defensively rather than asserting inside a hook whose
        # whole job is to never break the turn.
        agent_name = getattr(ctx.agent, "name", "")
        if event.author == agent_name and event.content and event.content.parts:
            text = "".join(part.text or "" for part in event.content.parts).strip()
            if text:
                spoken = text

    if not results:
        return None

    blocks = "\n".join(json.dumps(result, sort_keys=True) for result in results)
    body = f"{spoken}\n\n{RESULT_HEADER}\n{blocks}" if spoken else blocks
    return types.Content(role="model", parts=[types.Part(text=body)])
