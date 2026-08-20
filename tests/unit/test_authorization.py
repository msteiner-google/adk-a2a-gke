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

"""In-task authorization: the A2A half of the human-approval flow.

``test_two_phase_approval.py`` and ``test_trades.py`` cover the gate itself --
that a tool does nothing until a human decides. These cover what a *client*
sees, which is the part A2A specifies (spec 7.6) and the part a non-ADK squad
would integrate against:

* a suspended gated tool is reported as ``TASK_STATE_AUTH_REQUIRED``, not as
  the ``input_required`` ADK derives for any long-running call;
* an ordinary clarifying question is left alone;
* the proposal a reviewer reads is generated from the pending call;
* a decision is addressed to the agent that owns the tool.

The events here are built with ADK's own converter rather than hand-rolled.
That is deliberate: ``app/cluster/authorization.py`` matches on private ADK
metadata keys (``adk_is_long_running``, ``adk_type``), so a rename upstream
must fail here rather than silently stop matching and quietly downgrade every
authorization request to a question.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from a2a.types import TaskStatusUpdateEvent
from a2a.types.a2a_pb2 import TaskState
from google.adk.a2a.converters.event_converter import convert_event_to_a2a_events
from google.adk.a2a.converters.part_converter import convert_genai_part_to_a2a_part
from google.adk.a2a.executor.executor_context import ExecutorContext
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService, Session
from google.genai import types as genai_types

from app.cluster import grants
from app.cluster.authorization import (
    build_interceptor,
    pending_authorization,
    relaying_peer,
)

pytestmark = pytest.mark.asyncio

CONFIRMATION = "adk_request_confirmation"

# What ADK puts in an adk_request_confirmation call: the pending call and the
# hint the tool composed for a human.
CONFIRMATION_ARGS: dict[str, Any] = {
    "originalFunctionCall": {
        "id": "adk-original",
        "name": "publish_result",
        "args": {"value": "391000000.0", "label": "q3-revenue"},
    },
    "toolConfirmation": {
        "hint": "Publish '391000000' under label 'q3-revenue'.",
        "confirmed": False,
        "payload": {
            "action": "publish_result",
            "value": "391000000",
            "label": "q3-revenue",
        },
    },
}


async def _status_event(
    name: str, args: dict[str, Any], *, long_running: bool = True
) -> TaskStatusUpdateEvent:
    """Convert a function-call ADK event through ADK's own A2A converter.

    The converter is typed as returning a2a's ``Event`` union; for a
    function-call event it is always a ``TaskStatusUpdateEvent``, which is what
    the interceptor is handed in production.
    """
    call_id = str(uuid.uuid4())
    event = Event(
        invocation_id="inv-1",
        author="math",
        content=genai_types.Content(
            role="model",
            parts=[
                genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        id=call_id, name=name, args=args
                    )
                )
            ],
        ),
        long_running_tool_ids={call_id} if long_running else set(),
    )
    service = InMemorySessionService()
    session = await service.create_session(
        app_name="app", user_id="u1", session_id="s1"
    )
    ctx = InvocationContext(
        session_service=service,
        invocation_id="inv-1",
        agent=LlmAgent(name="math", model="gemini-2.5-flash"),
        session=session,
    )
    events = convert_event_to_a2a_events(
        event, ctx, "task-1", "ctx-1", convert_genai_part_to_a2a_part
    )
    return cast(TaskStatusUpdateEvent, events[0])


async def _upgrade(event: TaskStatusUpdateEvent) -> TaskStatusUpdateEvent:
    """Run the interceptor's after_agent hook over one terminal status event.

    The hook is Optional on ``ExecuteInterceptor`` and takes an
    ``ExecutorContext`` this test has no use for -- it is only read when a
    recorder is wired, and none is here.
    """
    hook = build_interceptor().after_agent
    assert hook is not None
    return await hook(cast(ExecutorContext, None), event)


# --- What a client is told ----------------------------------------------------


async def test_a_suspended_gated_tool_is_reported_as_auth_required():
    # The whole point. Without the upgrade a client cannot tell "a human must
    # authorise this" from "the agent asked a question", and A2A spec 7.6 has
    # a state for exactly this case.
    event = await _status_event(CONFIRMATION, CONFIRMATION_ARGS)
    assert event.status.state == TaskState.TASK_STATE_INPUT_REQUIRED

    upgraded = await _upgrade(event)
    assert upgraded.status.state == TaskState.TASK_STATE_AUTH_REQUIRED


async def test_an_ordinary_question_is_left_alone():
    # request_input and friends are long-running too. Upgrading those would
    # tell a client a human must sign something off when the agent only wants
    # a missing detail.
    event = await _status_event("request_input", {"question": "Which currency?"})
    upgraded = await _upgrade(event)
    assert upgraded.status.state == TaskState.TASK_STATE_INPUT_REQUIRED


async def test_a_finished_task_is_never_rewritten():
    # The interceptor must not be able to mask a completed or failed task as a
    # request for approval.
    event = await _status_event("calculate", {"expression": "1+1"}, long_running=False)
    before = event.status.state
    upgraded = await _upgrade(event)
    assert upgraded.status.state == before
    assert before != TaskState.TASK_STATE_AUTH_REQUIRED


async def test_reading_an_authorization_request_is_idempotent():
    # The same rule has to work on the server (deciding whether to upgrade) and
    # on a client (reading a request that already bubbled up), so it accepts a
    # task in either interrupted state.
    event = await _status_event(CONFIRMATION, CONFIRMATION_ARGS)
    assert pending_authorization(event) is not None
    event.status.state = TaskState.TASK_STATE_AUTH_REQUIRED
    again = pending_authorization(event)
    assert again is not None
    assert again["name"] == CONFIRMATION


async def test_the_request_carries_the_pending_call_and_the_hint():
    # What a reviewer is shown is generated from the call that is actually
    # suspended, so it cannot describe something other than what will run.
    event = await _status_event(CONFIRMATION, CONFIRMATION_ARGS)
    call = pending_authorization(event)
    assert call is not None
    args = call["args"]
    assert args["originalFunctionCall"]["name"] == "publish_result"
    assert "q3-revenue" in args["toolConfirmation"]["hint"]


# --- Finding the agent a decision has to reach --------------------------------


def _peer_event(author: str, task_id: str) -> Event:
    return Event(
        invocation_id="inv-1",
        author=author,
        content=genai_types.Content(role="model", parts=[genai_types.Part(text="ok")]),
        custom_metadata={"a2a:task_id": task_id, "a2a:context_id": f"ctx-{task_id}"},
    )


async def test_the_relaying_peer_is_the_most_recent_one():
    # A caller with several peers must send the decision to the one whose task
    # is actually suspended, which is the last one it heard from.
    session = Session(id="s1", app_name="app", user_id="u1")
    session.events = [_peer_event("research", "task-r"), _peer_event("math", "task-m")]
    assert relaying_peer(session) == ("math", "task-m", "ctx-task-m")


async def test_a_local_tool_has_no_relaying_peer():
    # An agent whose own tool suspended owns the request; there is no further
    # hop, and recording one would send the decision to the wrong process.
    session = Session(id="s1", app_name="app", user_id="u1")
    session.events = [
        Event(
            invocation_id="inv-1",
            author="math",
            content=genai_types.Content(
                role="model", parts=[genai_types.Part(text="hi")]
            ),
        )
    ]
    assert relaying_peer(session) is None


# --- Delivering the decision --------------------------------------------------


async def test_a_decision_is_addressed_to_the_suspended_call():
    # ADK matches the answer to the pending tool call by id, and rejects a
    # resume that carries anything else with "not provided a function response
    # for the function call".
    message = grants.confirmation_message(
        task_id="task-m",
        context_id="ctx-m",
        confirmation_id="adk-confirm",
        confirmed=True,
        approved_by="alice@bnpp.com",
        note="Reviewed.",
    )
    assert message["taskId"] == "task-m"
    assert message["contextId"] == "ctx-m"

    (part,) = message["parts"]
    # The data part MUST be marked as a function response, or ADK treats the
    # resume as an ordinary message and re-emits the same interrupted state.
    assert part["metadata"] == {"adk_type": "function_response"}
    assert part["data"]["id"] == "adk-confirm"
    assert part["data"]["name"] == CONFIRMATION


async def test_the_approver_travels_in_the_payload():
    # ToolConfirmation forbids unknown fields, so `confirmed` and `payload` are
    # the only two keys allowed -- an approver put anywhere else is silently
    # dropped, and the effect is recorded with nobody's name on it.
    message = grants.confirmation_message(
        task_id="t",
        context_id="c",
        confirmation_id="adk-confirm",
        confirmed=True,
        approved_by="alice@bnpp.com",
        note="Reviewed.",
    )
    response = message["parts"][0]["data"]["response"]
    assert set(response) == {"confirmed", "payload"}
    assert response["confirmed"] is True
    assert response["payload"]["approved_by"] == "alice@bnpp.com"
    assert response["payload"]["note"] == "Reviewed."


async def test_a_refusal_is_delivered_too():
    # A rejected case still has a suspended task on the other side. Not telling
    # it leaks the task for as long as the process lives.
    message = grants.confirmation_message(
        task_id="t",
        context_id="c",
        confirmation_id="adk-confirm",
        confirmed=False,
        approved_by="bob@bnpp.com",
        note="Not authorised.",
    )
    assert message["parts"][0]["data"]["response"]["confirmed"] is False
