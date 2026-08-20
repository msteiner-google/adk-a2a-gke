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

"""Send a human's decision to the peer that is actually paused on it.

Two independent ADK behaviours stand between a human answering an approval
widget and the peer's gated tool running. Both had to be corrected, and each
alone leaves the flow looking like "I confirmed and nothing happened".

**1. The node is never re-entered** (``rerun_on_resume``). An agent with
sub-agents is driven by ADK's workflow scheduler, and on resume the scheduler
replays each node from the session rather than re-running it. A node whose only
interrupt has just been answered lands in
``google/adk/workflow/utils/_replay_interceptor.py`` case 4::

    elif recovered.interrupt_ids:
      # all prior interrupts are resolved, but no output yet
      if not node.rerun_on_resume:
        output = ...the human's own answer...   # node "completes"
      else:
        should_run = True                        # node re-runs

The default is ``False``, which means the node completes *using the decision as
its own output* — sensible for a local ``request_input`` node, and wrong for a
remote peer, which is a live A2A task waiting to be re-driven. Measured on this
repo: the scheduler logged ``node orchestrator@1/trades@1 schedule:
Fast-forwarding completed execution``, the peer received no request at all, and
the turn produced zero events. Hence ``rerun_on_resume=True`` below.

Note this only bites an agent that *has* sub-agents: without them the runner
skips the scheduler entirely, which is why a gated tool resumes fine when the
decision is posted straight at the agent that owns it
(``app/cluster/grants.py``, the ``curl`` path) and fails through the caller.

**2. The decision is flattened to text.** Once the node does re-run,
``RemoteA2aAgent`` rewrites the answer to a human-input pause into
plain **text** before forwarding it, and it decides what to rewrite by function
*call name*::

    # google/adk/agents/remote_a2a_agent.py
    _HUMAN_INPUT_FUNCTION_CALL_NAMES = frozenset({
        MOCK_FUNCTION_CALL_FOR_REQUIRED_USER_INPUT,
        MOCK_FUNCTION_CALL_FOR_REQUIRED_USER_AUTH,
        REQUEST_INPUT_FUNCTION_CALL_NAME,
        REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
        REQUEST_EUC_FUNCTION_CALL_NAME,
    })

That is right for a pause the **caller** raised: the peer never saw that call,
so a ``FunctionResponse`` addressed to it would mean nothing. It is wrong for a
pause raised **inside the peer** and relayed up, which carries the same name —
ADK manufactures exactly that shape itself, re-emitting the peer's long-running
call into the caller's session so a nested approval reaches the human at the
top (``google/adk/a2a/converters/to_adk_event.py``).

Flattened, the decision arrives at the peer as an ordinary user turn. The
suspended tool is never re-executed, so **the gated effect never happens** while
the peer's model reads ``{"confirmed": true, ...}`` and narrates a success. That
is the failure this repo exists to make impossible; see
``app/agents/gating.py`` for why re-execution, not the model, must perform the
effect. Upstream: google/adk-python#6721, fix pending in #6759.

The symptom is not always silent, and neither shape is obvious:

* Answer the widget in the ADK dev UI and *nothing happens* — the peer got text.
* Type anything alongside it and the resume falls off ADK's function-response
  path entirely (``find_matching_function_call`` looks at the **last** event),
  so the request is rebuilt from raw history and carries the function response
  *and* the text. The peer's runner then rejects the whole message with
  "Message cannot contain both function responses and text."

:class:`ResumingA2aAgent` fixes both: it sets ``rerun_on_resume`` so the node is
re-entered, and it classifies the answer by **origin** instead of by name — when
the paused call came from this peer, the answer is forwarded as a function
response so the peer can resume. A pause this agent raised locally still
flattens exactly as ADK intends, and credential payloads are still dropped, so
the security intent of the upstream rewrite is preserved.

Verified end to end against the six local agents: ``orchestrator -> trades``,
approve, and the gated BigQuery read executes and returns rows with
``approved_by`` set; refuse, and it reports ``refused`` having run nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, override

from a2a.types import Part as A2APartMessage
from google.adk.a2a import _compat
from google.adk.a2a.converters.event_converter import convert_event_to_a2a_message
from google.adk.agents.remote_a2a_agent import (
    A2A_METADATA_PREFIX,
    RemoteA2aAgent,
    _is_credential_function_response,
)
from google.adk.flows.llm_flows.functions import (
    REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
    REQUEST_INPUT_FUNCTION_CALL_NAME,
    find_matching_function_call,
)
from google.protobuf.json_format import MessageToDict

if TYPE_CHECKING:
    from a2a.types import AgentCard
    from a2a.types import Message as A2AMessage
    from a2a.types import Part as A2APart
    from google.adk.agents.invocation_context import InvocationContext
    from google.genai import types as genai_types

__all__ = ["RELAYED_PAUSE_CALL_NAMES", "ResumingA2aAgent"]

#: The pauses a peer can be resumed from. Both are answered by a decision the
#: peer's own flow consumes -- ADK re-executes the suspended tool for a
#: confirmation, and feeds the answer back into the paused call for an input
#: request. The credential names in ADK's set are deliberately absent: an
#: ``AuthConfig`` envelope carries access tokens and is never forwarded.
RELAYED_PAUSE_CALL_NAMES = frozenset(
    {REQUEST_CONFIRMATION_FUNCTION_CALL_NAME, REQUEST_INPUT_FUNCTION_CALL_NAME}
)


#: Metadata keys ADK stamps on the data parts it emits for function calls and
#: their responses (``google/adk/a2a/converters/part_converter.py``). Spelled
#: out because they are private constants there; a rename should fail this
#: repo's tests rather than silently stop matching.
_ADK_TYPE = "adk_type"
_FUNCTION_CALL = "function_call"
_FUNCTION_RESPONSE = "function_response"


def _as_plain_text(part: A2APart) -> A2APart | None:
    """Render a function call/response part as text, or drop it.

    Args:
        part: One outbound A2A part, already converted by ADK.

    Returns:
        ``part`` unchanged when it is not function-call machinery, ``None`` for
        human-in-the-loop bookkeeping that no peer should see, and otherwise an
        equivalent text part.
    """
    if not part.HasField("data"):
        return part
    metadata = _compat.part_metadata(part) or {}
    kind = metadata.get(_ADK_TYPE)
    if kind not in {_FUNCTION_CALL, _FUNCTION_RESPONSE}:
        return part
    payload = MessageToDict(part.data)
    name = payload.get("name") if isinstance(payload, dict) else None
    if name in RELAYED_PAUSE_CALL_NAMES:
        return None
    if kind == _FUNCTION_CALL:
        body = json.dumps(payload.get("args", {}), default=str)
        text = f"[{name}] was called with: {body}"
    else:
        body = json.dumps(payload.get("response", {}), default=str)
        text = f"[{name}] returned: {body}"
    rendered = A2APartMessage(text=text)
    # Keep `is_user_input` (the receiver uses it to attribute the turn) but not
    # `adk_type`, which would tell the receiver to parse this back into a call.
    carried = {k: v for k, v in metadata.items() if k != _ADK_TYPE}
    if carried:
        _compat.set_part_metadata(rendered, carried)
    return rendered


class ResumingA2aAgent(RemoteA2aAgent):
    """A ``RemoteA2aAgent`` that can resume a pause the peer itself raised.

    Behaves exactly like its base class except when the function call being
    answered was authored by this peer, in which case the answer is forwarded
    as a function response instead of being rewritten as text.
    """

    mode: Literal["chat", "task", "single_turn"] | None = "task"
    """Reach this peer as a task delegation rather than ``transfer_to_agent``.

    ``transfer_to_agent`` is one-way: the caller's invocation *ends* at the
    handoff, so a turn that needed two specialists stopped after the first.
    A task-mode sub-agent is wrapped by ADK in a ``_TaskAgentTool``, which the
    workflow layer drives as a node -- so the pause still propagates (unlike the
    classic ``AgentTool``, which runs the peer to exhaustion and drops
    ``long_running_tool_ids``) *and* the caller gets a function response back
    and carries on. Measured: with this, one turn ran
    ``orchestrator -> trades`` (approve, query executes) and then went on to
    the currency conversion; without it the turn ended inside ``trades``.
    """

    def __init__(self, name: str, agent_card: AgentCard | str, **kwargs: Any) -> None:
        """Initialize the agent.

        Declared only so the base class's constructor keeps type-checking:
        ``RemoteA2aAgent`` is a pydantic model with a hand-written ``__init__``,
        and a subclass that does not restate it gets one synthesized from the
        model's fields -- which do not include ``agent_card``.

        Args:
            name: The peer's agent name.
            agent_card: The peer's agent card, or a URL or path to it.
            **kwargs: Forwarded to ``RemoteA2aAgent``.
        """
        # See the module docstring: without this the workflow scheduler
        # fast-forwards this node on resume and the peer is never re-driven.
        kwargs.setdefault("rerun_on_resume", True)
        super().__init__(name=name, agent_card=agent_card, **kwargs)

    @override
    def _create_a2a_request_for_user_function_response(
        self, ctx: InvocationContext
    ) -> A2AMessage | None:
        """Build the outbound message for a user's function response.

        Args:
            ctx: The invocation context whose session holds the paused call and
                the user's answer to it.

        Returns:
            A message carrying the answer as a function response when the pause
            was raised by this peer, otherwise whatever ADK would have built.
        """
        events = ctx.session.events
        if not events or events[-1].author != "user":
            return None
        call_event = find_matching_function_call(events)
        answer = events[-1]
        # `author == self.name` is what makes this a *relayed* pause: the call
        # came back over A2A from this peer, so the peer is the one blocked on
        # it. A pause this agent raised itself has a different author and stays
        # on ADK's flattening path.
        if (
            call_event is None
            or call_event.author != self.name
            or answer.content is None
            or not answer.content.parts
        ):
            return super()._create_a2a_request_for_user_function_response(ctx)

        names_by_id = {
            fc.id: fc.name for fc in call_event.get_function_calls() if fc.name
        }
        parts = self._resume_parts(answer.content.parts, names_by_id)
        if parts is None:
            return super()._create_a2a_request_for_user_function_response(ctx)

        event = answer.model_copy(deep=True)
        content = event.content
        assert content is not None
        content.parts = parts
        message = convert_event_to_a2a_message(
            event, ctx, _compat.ROLE_USER, self._genai_part_converter
        )
        if message is None:
            return None
        metadata = call_event.custom_metadata or {}
        task_id = metadata.get(f"{A2A_METADATA_PREFIX}task_id")
        if isinstance(task_id, str):
            message.task_id = task_id
        context_id = metadata.get(f"{A2A_METADATA_PREFIX}context_id")
        if isinstance(context_id, str):
            message.context_id = context_id
        return message

    @override
    def _construct_message_parts_from_session(
        self, ctx: InvocationContext
    ) -> tuple[list[A2APart], str | None]:
        """Rebuild a fresh request from session history, minus HITL bookkeeping.

        ADK's version re-serializes every part it finds as-is, including
        function calls and their responses. Sent to a peer that is starting a
        fresh turn, those are fatal rather than merely noisy: a message may not
        carry a function response *and* text ("Message cannot contain both
        function responses and text"), and a user message may not carry a
        function call at all. Measured on this repo once task delegation
        landed — the very act of delegating successfully to ``trades``
        appends a synthesized ``trades`` function response authored by
        ``user``, and every later delegation in that turn then failed.

        So the machinery is rendered as text instead: the peer still learns
        what was called and what came back, in the form ADK already uses for
        another agent's reply. Human-in-the-loop bookkeeping is dropped
        outright, being a conversation with the human rather than with a peer.
        A resume never reaches here (it is built by
        :meth:`_create_a2a_request_for_user_function_response`), so none of
        this touches the path that carries a decision.

        Args:
            ctx: The invocation context whose session is being replayed.

        Returns:
            The A2A parts to send and the peer's context id, as ADK's own
            version returns them.
        """
        parts, context_id = super()._construct_message_parts_from_session(ctx)
        rendered = (_as_plain_text(p) for p in parts)
        return [p for p in rendered if p is not None], context_id

    @staticmethod
    def _resume_parts(
        parts: list[genai_types.Part], names_by_id: dict[str | None, str]
    ) -> list[genai_types.Part] | None:
        """Keep the parts that resume the peer, or decline to handle them.

        Text is dropped rather than sent alongside: a message carrying both is
        rejected outright by the peer's runner ("Message cannot contain both
        function responses and text"), and the function response is the part
        that actually resumes the invocation.

        Args:
            parts: The parts of the user's answering message.
            names_by_id: Function-call id to name, taken from the paused call
                event, so a response is classified by the call it answers
                rather than by a name a client chose.

        Returns:
            The parts to forward, or ``None`` when this is not a relayed pause
            this class handles — a credential exchange, or a response to some
            call other than a confirmation or input request. The caller then
            falls back to ADK's own handling rather than guessing.
        """
        kept: list[genai_types.Part] = []
        resumes = False
        for part in parts:
            response = part.function_response
            if response is None:
                if part.text is None:
                    kept.append(part)
                continue
            name = names_by_id.get(response.id)
            if (
                _is_credential_function_response(response, {name} if name else None)
                or (name or response.name) not in RELAYED_PAUSE_CALL_NAMES
            ):
                return None
            kept.append(part)
            resumes = True
        return kept if resumes else None
