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

**The fix is to stop asking a model to be a serializer.** If the turn produced
any audit-relevant tool result — a proposal awaiting approval, the confirmation
that an approved action ran, or a question only the user can answer — that result
is restated as verbatim JSON, so the caller receives the structured payload
deterministically whatever prose the model produced.

**Two callbacks, and the split is what stops the reply appearing twice.**

* :func:`attach_structured_results` is an *after-model* callback. It appends the
  JSON to the model's own final reply **before** that reply becomes an event, so
  a turn produces exactly one message carrying both the prose and the structure.
  This is the normal path.
* :func:`restate_structured_results` is an *after-agent* callback and a pure
  fallback. ADK appends whatever it returns as an **extra** event
  (``base_agent.py:_handle_after_agent_callback``), so it emits only the results
  that are *not already* in what this agent said this invocation — nothing, in
  the normal path. It exists for the turn that ends without the model speaking
  at all (a skipped summarisation, an empty final response), where the after-
  model hook never fires and the structure would otherwise be lost.

An earlier version had the after-agent callback repeat the model's own wording
above the JSON, on the reasoning that ``AgentTool`` returns only the *last*
content and the prose would otherwise not cross A2A. It does cross — it is in the
same event as the JSON now — and the repetition was visible in the ADK web UI as
the same answer twice, once per event, on exactly the HITL turns this module
exists to protect.

Both are attached to every agent by ``build_agent``. Both are no-ops for a turn
that proposed nothing, which is nearly all of them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from google.genai import types

from app.agents.statuses import (
    AWAITING_APPROVAL,
    EFFECT_PERFORMED,
    NEEDS_USER,
    REFUSED,
)

#: Tool-result statuses that must reach the caller intact rather than as prose:
#: an action awaiting sign-off, the confirmation that an approved one ran, and a
#: question only the user can answer.
#:
#: Derived from the contract vocabulary rather than written out, which is not
#: tidiness. This set said `{AWAITING_APPROVAL, "published"}` when the trades
#: agent landed, so a gated *read* reporting `executed` was silently not
#: restated -- it survived only because the model chose to repeat it, which is
#: exactly the thing this callback exists to stop relying on.
AUDITED_STATUSES = (
    frozenset({AWAITING_APPROVAL, REFUSED}) | EFFECT_PERFORMED | NEEDS_USER
)

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events.event import Event
    from google.adk.models.llm_response import LlmResponse

# Marks the JSON block this callback appends, so a reader (and a future
# maintainer staring at a transcript) can tell it apart from model prose.
RESULT_HEADER = "Structured result(s):"


def _embedded(text: str) -> list[dict[str, Any]]:
    """Return JSON objects embedded anywhere in a string.

    This is what makes the callback work more than one hop deep. A peer's
    reply crosses A2A as TEXT, so on the *caller* a structured result is a
    string rather than a dict -- and a question or a pending authorization
    raised two levels down (``orchestrator -> math -> currency``) would be
    invisible to the dict-only scan below, leaving it to the middle agent's
    model to relay faithfully. That is precisely the dependency this module
    exists to remove.

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


def _line(payload: dict[str, Any]) -> str:
    """Render one payload in the canonical form used everywhere in this module.

    Canonical because the same string is both what gets emitted and what the
    fallback searches the transcript for; if the two forms drifted, the fallback
    would restate a result the model's reply already carries.

    Args:
        payload: The tool-result payload.

    Returns:
        Its JSON, keys sorted, with unserialisable values coerced rather than
        raising inside a callback whose job is to never break the turn.
    """
    return json.dumps(payload, sort_keys=True, default=str)


def _block(results: list[dict[str, Any]]) -> str:
    """Render the labelled JSON block that crosses the A2A boundary.

    Args:
        results: The payloads to restate, in order.

    Returns:
        The header followed by one JSON document per line.
    """
    return f"{RESULT_HEADER}\n" + "\n".join(_line(result) for result in results)


def _audited(ctx: InvocationContext) -> list[dict[str, Any]]:
    """Return this invocation's audit-relevant tool results, deduplicated.

    Args:
        ctx: The invocation context to scan.

    Returns:
        Every distinct payload whose ``status`` is in :data:`AUDITED_STATUSES`,
        in the order the tools produced them.
    """
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in ctx.session.events:
        if event.invocation_id != ctx.invocation_id:
            continue
        for payload in _payloads(event):
            if payload.get("status") not in AUDITED_STATUSES:
                continue
            key = _line(payload)
            if key not in seen:
                seen.add(key)
                results.append(payload)
    return results


def _spoken(ctx: InvocationContext) -> str:
    """Return everything this agent has said so far in this invocation.

    Args:
        ctx: The invocation context to scan.

    Returns:
        The concatenated text of this agent's own events, including any block
        :func:`attach_structured_results` already folded into its reply.
    """
    # `ctx.agent` is Optional on InvocationContext; a callback always has one,
    # but read it defensively rather than asserting inside a hook whose whole
    # job is to never break the turn.
    agent_name = getattr(ctx.agent, "name", "")
    chunks: list[str] = []
    for event in ctx.session.events:
        if event.invocation_id != ctx.invocation_id:
            continue
        if event.author != agent_name or not event.content or not event.content.parts:
            continue
        chunks.extend(part.text for part in event.content.parts if part.text)
    return "\n".join(chunks)


def attach_structured_results(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse | None:
    """Fold this turn's structured results into the model's own reply.

    Running here rather than after the agent is what keeps the answer to a
    single message: the JSON is appended to the response *before* ADK turns it
    into an event, so the user sees one reply and the caller's ``AgentTool``
    still finds the structure in the last content it receives.

    Args:
        callback_context: Injected by ADK.
        llm_response: The response the model just produced.

    Returns:
        The response with a JSON block appended, or ``None`` to leave it exactly
        as it was — which covers streamed chunks, tool-call responses, and the
        overwhelming majority of turns, which restate nothing.
    """
    # A streamed chunk is re-delivered in the aggregated response that follows,
    # so appending here would emit the block once per chunk.
    if llm_response.partial or llm_response.error_code:
        return None
    content = llm_response.content
    if content is None or not content.parts:
        return None
    # A response that calls a tool is mid-turn: the results are not all in yet,
    # and the text alongside a function call is not the agent's answer.
    if any(part.function_call for part in content.parts):
        return None
    if not any(part.text and not part.thought for part in content.parts):
        return None

    # ADK exposes no public accessor for the invocation's events from a
    # callback; this is the same private reach the framework's own callbacks
    # use. If it breaks on an ADK upgrade, tests/unit/test_reporting.py fails.
    results = _audited(callback_context._invocation_context)
    if not results:
        return None

    return llm_response.model_copy(
        update={
            "content": types.Content(
                role=content.role or "model",
                parts=[*content.parts, types.Part(text=f"\n{_block(results)}")],
            )
        }
    )


def restate_structured_results(
    callback_context: CallbackContext,
) -> types.Content | None:
    """Emit any structured result the agent's reply does not already carry.

    The fallback half of this module. In the normal path
    :func:`attach_structured_results` has already folded the block into the
    model's own reply, every result is found in the transcript, and this returns
    ``None`` — which is what stops the same answer being shown twice. It emits
    only when the model never spoke, or spoke without the block.

    Args:
        callback_context: Injected by ADK after the agent finishes.

    Returns:
        Content carrying the missing structured results, or ``None`` when there
        are none (the common case, in which the reply is left as it was).
    """
    ctx = callback_context._invocation_context
    spoken = _spoken(ctx)
    missing = [result for result in _audited(ctx) if _line(result) not in spoken]
    if not missing:
        return None
    return types.Content(role="model", parts=[types.Part(text=_block(missing))])
