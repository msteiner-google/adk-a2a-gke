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

"""Report a paused gated tool as A2A ``TASK_STATE_AUTH_REQUIRED``.

A2A models "I need a human to authorise this" as a task state, not as an
application-level message: spec section 7.6 (In-Task Authorization). An agent
that needs authorisation MUST keep a Task, MUST move it to
``TASK_STATE_AUTH_REQUIRED``, and MUST attach a status message saying what is
being asked. That state is *interrupted*, not terminal, so the task stays
alive and can be resumed once the credential arrives out of band.

**ADK will not emit that state on its own.** It derives the interrupted state
purely from long-running function calls, and picks ``auth_required`` only for
its own OAuth credential flow::

    # google/adk/a2a/converters/event_converter.py
    if any(is_euc_call(part) for part in message.parts):
        status.state = _compat.TS_AUTH_REQUIRED      # name == adk_request_credential
    elif any(is_long_running_call(part) for part in message.parts):
        status.state = _compat.TS_INPUT_REQUIRED     # everything else

ADK's *tool confirmation* flow -- the one that actually re-runs a tool once a
human answers, and therefore the one this repo gates effects with -- is not
that flow. It raises a long-running call named ``adk_request_confirmation``,
which falls into the ``else`` branch above. So an action suspended awaiting
human sign-off is reported as ``input_required``: indistinguishable, to a
client, from an agent asking a clarifying question.

This module closes that gap with the sanctioned hook: an
``ExecuteInterceptor.after_agent`` that inspects the terminal status event and
upgrades ``input_required`` to ``auth_required`` when the pause was a
confirmation request.

Two details make the upgrade safe rather than a blanket rewrite:

* It is keyed on ADK's own ``adk_request_confirmation`` marker, so it needs no
  register of gated tool names and cannot drift from one. A pause caused by
  anything else -- ``request_input``, a clarifying question, ADK's credential
  flow -- is left exactly as ADK reported it.
* It only ever rewrites ``input_required``. A ``failed`` or ``completed`` task
  is never touched, so the interceptor cannot mask an error as a request for
  approval.

The interceptor runs on the legacy executor path as well as the new one
(``a2a_agent_executor.py`` calls ``execute_after_agent_interceptors`` at the
end of both), so this needs no ``force_new_version`` and no protocol
extension negotiated with the caller.

**The state is not the gate.** Spec section 7.6.4 is explicit that
``TASK_STATE_AUTH_REQUIRED`` by itself authorises nothing, and that an
implementation "is responsible for defining how the authorized operation is
identified and how that authorization is checked before the operation is
performed". Here that check stays where it already was: in the tool, which
refuses to act without a non-empty ``approved_by``, and in the content
comparison the caller makes against the approved proposal
(``app/cluster/cases.py``). This module only changes how the *request* to a
human is transported and how the resumed task is woken up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from a2a.types.a2a_pb2 import TaskState
from google.adk.a2a.executor.config import A2aAgentExecutorConfig, ExecuteInterceptor
from google.adk.agents.remote_a2a_agent import A2A_METADATA_PREFIX
from google.adk.flows.llm_flows.functions import (
    REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
)
from google.protobuf.json_format import MessageToDict
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from a2a.types import TaskStatusUpdateEvent
    from google.adk.a2a.executor.executor_context import ExecutorContext
    from google.adk.sessions import Session

# Metadata keys ADK stamps on the data part it emits for a long-running
# function call. `ADK_METADATA_KEY_PREFIX` is "adk_"; the two keys are declared
# in google/adk/a2a/converters/part_converter.py and utils.py. They are spelled
# out here rather than imported because they are private module constants in
# ADK, and a rename there should fail this repo's test rather than silently
# stop matching -- see tests/unit/test_authorization.py.
_IS_LONG_RUNNING_KEY = "adk_is_long_running"
_TYPE_KEY = "adk_type"
_FUNCTION_CALL = "function_call"

# The two states A2A calls "interrupted" rather than terminal (spec 4.1.3): the
# task is suspended and still resumable. `pending_authorization` accepts both so
# that reading an authorisation request is idempotent -- see its docstring.
_INTERRUPTED = frozenset(
    {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}
)


def long_running_call(part: Any) -> dict[str, Any] | None:
    """Return the function-call payload of ``part``, if it is a paused call.

    Args:
        part: One A2A ``Part`` from a task status message.

    Returns:
        The decoded ``{"name": ..., "id": ..., "args": {...}}`` payload when
        ``part`` is the data part ADK emits for a long-running function call,
        otherwise ``None``.
    """
    if not part.HasField("metadata") or not part.HasField("data"):
        return None
    metadata = MessageToDict(part.metadata)
    if metadata.get(_IS_LONG_RUNNING_KEY) is not True:
        return None
    if metadata.get(_TYPE_KEY) != _FUNCTION_CALL:
        return None
    payload = MessageToDict(part.data)
    return payload if isinstance(payload, dict) else None


def pending_authorization(event: TaskStatusUpdateEvent) -> dict[str, Any] | None:
    """Return the confirmation request that paused ``event``, if any.

    Accepts a task in either interrupted state, so it reads the same whether it
    is called before the upgrade below (server side, deciding whether to
    upgrade) or after it (client side, reading a request that has already
    bubbled up). Being idempotent is what lets both callers share one rule for
    what counts as an authorisation request.

    Args:
        event: A terminal status event, or a status read back off a task.

    Returns:
        The ``adk_request_confirmation`` call payload that suspended the task —
        whose ``args`` carry both ``originalFunctionCall`` and the reviewer's
        hint — or ``None`` when the task did not stop to ask for authorisation.
    """
    if event.status.state not in _INTERRUPTED:
        return None
    if not event.status.HasField("message"):
        return None
    for part in event.status.message.parts:
        call = long_running_call(part)
        if (
            call is not None
            and call.get("name") == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
        ):
            return call
    return None


def relaying_peer(session: Session) -> tuple[str, str, str] | None:
    """Return the peer this agent last heard from, if any.

    A confirmation raised by *this* agent's own tool is resolved by resuming
    this agent's task. One relayed from a peer has to travel one hop further,
    and this is what identifies that hop.

    Args:
        session: The ADK session backing the suspended invocation.

    Returns:
        ``(peer name, peer task id, peer context id)`` taken from the most
        recent event a remote peer produced, or ``None`` when nothing in this
        session came from a peer — meaning the gated tool is local.
    """
    for event in reversed(session.events or []):
        metadata = event.custom_metadata or {}
        task_id = metadata.get(f"{A2A_METADATA_PREFIX}task_id")
        if isinstance(task_id, str) and task_id and event.author:
            context_id = metadata.get(f"{A2A_METADATA_PREFIX}context_id", "")
            return event.author, task_id, str(context_id)
    return None


def build_interceptor(
    on_request: Callable[
        [ExecutorContext, dict[str, Any], tuple[str, str, str] | None],
        Awaitable[None],
    ]
    | None = None,
) -> ExecuteInterceptor:
    """Build the interceptor that reports a confirmation pause as ``auth_required``.

    Args:
        on_request: Optional hook invoked when a pause is detected, before the
            state is rewritten. Used to record an approval case. A failure here
            must not swallow the state change, so it is logged and swallowed
            instead — a case nobody recorded is recoverable, a task reported as
            ``completed`` when it is actually suspended is not.

    Returns:
        An ``ExecuteInterceptor`` suitable for ``A2aAgentExecutorConfig``.
    """

    async def after_agent(
        context: ExecutorContext, final_event: TaskStatusUpdateEvent
    ) -> TaskStatusUpdateEvent:
        call = pending_authorization(final_event)
        if call is None:
            return final_event
        if on_request is not None:
            try:
                session = await context.runner.session_service.get_session(
                    app_name=context.app_name,
                    user_id=context.user_id,
                    session_id=context.session_id,
                )
                peer = relaying_peer(session) if session is not None else None
                await on_request(context, call, peer)
            except Exception:
                logger.exception("Could not record the authorization request")
        final_event.status.state = TaskState.TASK_STATE_AUTH_REQUIRED
        return final_event

    return ExecuteInterceptor(after_agent=after_agent)


def build_executor_config(
    on_request: Callable[
        [ExecutorContext, dict[str, Any], tuple[str, str, str] | None],
        Awaitable[None],
    ]
    | None = None,
) -> A2aAgentExecutorConfig:
    """Build the executor config that upgrades a confirmation pause.

    Args:
        on_request: Optional hook that records the request. See
            :func:`build_interceptor`.

    Returns:
        The config to hand to ``A2aAgentExecutor``.
    """
    return A2aAgentExecutorConfig(execute_interceptors=[build_interceptor(on_request)])
