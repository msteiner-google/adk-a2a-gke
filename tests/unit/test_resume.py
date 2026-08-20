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

"""A human's decision reaches the peer that is paused on it.

The property under test is the one an approval gate cannot do without: a
decision forwarded to a peer as *text* leaves the suspended tool un-re-executed,
so the effect never happens while the peer's model reports that it did. See
``app/cluster/resume.py`` for the ADK behaviour this corrects
(google/adk-python#6721).
"""

from types import SimpleNamespace
from typing import Any, cast

from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.genai import types

from app.cluster.resume import ResumingA2aAgent

CALL_ID = "adk-call-1"
PEER = "trades"


def _pause(author: str = PEER, name: str = "adk_request_confirmation") -> Event:
    """The peer's long-running pause, as ADK relays it into the caller."""
    return Event(
        author=author,
        invocation_id="inv-1",
        long_running_tool_ids={CALL_ID},
        custom_metadata={
            "a2a:task_id": "task-1",
            "a2a:context_id": "ctx-1",
            "a2a:response": True,
        },
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id=CALL_ID,
                        name=name,
                        args={
                            "originalFunctionCall": {
                                "id": "p1",
                                "name": "run_trade_query",
                                "args": {"sql": "SELECT 1"},
                            },
                            "toolConfirmation": {"hint": "Run it?"},
                        },
                    )
                )
            ],
        ),
    )


def _answer(*parts: types.Part) -> Event:
    return Event(
        author="user",
        invocation_id="inv-1",
        content=types.Content(role="user", parts=list(parts)),
    )


def _decision(name: str = "adk_request_confirmation", **response: Any) -> types.Part:
    return types.Part(
        function_response=types.FunctionResponse(
            id=CALL_ID,
            name=name,
            response=response or {"confirmed": True, "payload": {"sql": "SELECT 1"}},
        )
    )


def _message(*events: Event):
    agent = ResumingA2aAgent(name=PEER, agent_card="http://127.0.0.1:8094/card.json")
    ctx = cast(
        InvocationContext,
        SimpleNamespace(
            session=SimpleNamespace(
                id="s1", app_name="app", user_id="u", events=list(events)
            ),
            app_name="app",
            user_id="u",
            invocation_id="inv-1",
            branch=None,
        ),
    )
    return agent._create_a2a_request_for_user_function_response(ctx)


def _kinds(message: Any) -> list[str]:
    # a2a v1.0 parts are protobuf: the payload is a `content` oneof, so
    # `WhichOneof` names the populated field ("text" or "data").
    return [part.WhichOneof("content") for part in message.parts]


def test_the_peer_node_is_re_entered_on_resume():
    # Without this the workflow scheduler -- used by any agent that has
    # sub-agents -- replays the peer node as "completed, with the human's own
    # answer as its output" and never calls it again. The peer receives no
    # request at all, the gated tool never re-executes, and the turn produces
    # zero events. Measured: `node orchestrator@1/trades@1 schedule:
    # Fast-forwarding completed execution`. See ADK's
    # workflow/utils/_replay_interceptor.py, case 4.
    agent = ResumingA2aAgent(name=PEER, agent_card="http://127.0.0.1:8094/card.json")
    assert agent.rerun_on_resume is True


def test_a_peer_is_reached_as_a_task_delegation():
    # `transfer_to_agent` ends the caller's invocation, so a turn that needed
    # two specialists stopped after the first -- the trades query ran and the
    # currency conversion never happened. Task mode makes ADK wrap the peer in
    # a _TaskAgentTool, which propagates the pause AND returns to the caller.
    agent = ResumingA2aAgent(name=PEER, agent_card="http://127.0.0.1:8094/card.json")
    assert agent.mode == "task"


def test_a_decision_on_a_relayed_pause_stays_a_function_response():
    # The whole point. Flattened to text, the peer never re-executes the gated
    # tool -- and answers as though it had.
    message = _message(_pause(), _answer(_decision()))
    assert message is not None
    assert _kinds(message) == ["data"]
    assert message.task_id == "task-1"
    assert message.context_id == "ctx-1"


def test_a_pause_this_agent_raised_itself_still_flattens():
    # ADK's own behaviour, deliberately untouched: the peer never saw that call,
    # so a function response addressed to it would be meaningless.
    message = _message(_pause(author="orchestrator"), _answer(_decision()))
    assert message is not None
    assert _kinds(message) == ["text"]


def test_text_alongside_a_decision_is_dropped_not_mixed():
    # A peer's runner rejects a message carrying both ("Message cannot contain
    # both function responses and text"), which fails the whole resume.
    message = _message(_pause(), _answer(_decision(), types.Part(text="continue")))
    assert message is not None
    assert _kinds(message) == ["data"]


def test_a_relayed_input_request_resumes_the_same_way():
    message = _message(
        _pause(name="adk_request_input"),
        _answer(_decision(name="adk_request_input", result="EUR")),
    )
    assert message is not None
    assert _kinds(message) == ["data"]


def test_credential_material_is_never_forwarded_as_data():
    # aec7aa3's security intent: an AuthConfig envelope carries access tokens.
    # This class declines the response and ADK drops it.
    message = _message(
        _pause(name="adk_request_credential"),
        _answer(
            _decision(
                name="adk_request_credential",
                authScheme={"type": "oauth2"},
                exchangedAuthCredential={"oauth2": {"access_token": "secret"}},
            )
        ),
    )
    assert message is None or "data" not in _kinds(message)


def test_history_rebuild_renders_function_calls_as_text():
    # Succeeding at one delegation broke every later one in the same turn: ADK
    # synthesizes a `user`-authored function response for a completed task
    # delegation, and replaying it verbatim gives the next peer a message
    # carrying both a function response and text, which its runner rejects.
    from a2a.types import Part as A2APart

    from app.cluster.resume import _as_plain_text

    part = A2APart(text="hello")
    assert _as_plain_text(part) is part

    call = _a2a_data_part({"name": "trades", "args": {"q": 1}}, "function_call")
    rendered = _as_plain_text(call)
    assert rendered is not None
    assert rendered.WhichOneof("content") == "text"
    assert "trades" in rendered.text

    pause = _a2a_data_part(
        {"name": "adk_request_confirmation", "args": {}}, "function_call"
    )
    # HITL bookkeeping is a conversation with the human, not with a peer.
    assert _as_plain_text(pause) is None


def _a2a_data_part(payload: dict[str, Any], adk_type: str):
    from a2a.types import Part as A2APart
    from google.adk.a2a import _compat
    from google.protobuf.json_format import ParseDict
    from google.protobuf.struct_pb2 import Value

    part = A2APart()
    # a2a v1.0 `Part.data` is a Value, not a Struct.
    part.data.CopyFrom(ParseDict(payload, Value()))
    _compat.set_part_metadata(part, {"adk_type": adk_type})
    return part
