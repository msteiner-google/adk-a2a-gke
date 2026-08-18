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

"""Unit tests for PeerTool — the explicit-payload delegation boundary.

These are the guards for D1. The property under test is *what a specialist can
see*: only the payload the caller composed, never the caller's conversation.
Because that difference is invisible at runtime until something leaks, it is
asserted directly on the message a peer would receive.
"""

import json
from typing import cast

import pytest
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService, Session
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app.agents.contracts import MathRequest, ResearchRequest
from app.cluster.peer_tool import PeerTool


def _peer() -> RemoteA2aAgent:
    return RemoteA2aAgent(name="research", agent_card="http://research/card.json")


def _tool() -> PeerTool:
    return PeerTool(_peer(), payload_schema=ResearchRequest)


def _text_event(author: str, text: str) -> Event:
    return Event(
        author=author,
        invocation_id="inv-1",
        content=types.Content(
            role="user" if author == "user" else "model",
            parts=[types.Part(text=text)],
        ),
    )


def _outbound(session: Session) -> list[str]:
    """The text parts RemoteA2aAgent would put on the wire for this session."""
    peer = _peer()
    ctx = InvocationContext(
        session_service=InMemorySessionService(),
        invocation_id="inv-1",
        agent=peer,
        session=session,
    )
    parts, _ = peer._construct_message_parts_from_session(ctx)
    out = []
    for part in parts:
        root = getattr(part, "root", part)
        if getattr(root, "text", None) is not None:
            out.append(root.text)
    return out


def test_declaration_is_the_contract():
    declaration = _tool()._get_declaration()
    schema = declaration.parameters_json_schema
    assert isinstance(schema, dict)
    assert declaration.name == "research"
    assert set(schema["properties"]) == {
        "case_id",
        "document_refs",
        "question",
        "constraints",
    }


def test_declaration_documents_every_field():
    # The field descriptions are the only instructions the calling model gets
    # about a peer's contract, so an undocumented field is a silent mis-call.
    schema = MathRequest.model_json_schema()
    for name, prop in schema["properties"].items():
        assert prop.get("description"), f"{name} has no description"


@pytest.mark.asyncio
async def test_payload_is_validated_before_leaving_the_pod():
    # `question` is required. A caller that omits it must fail here, in-process,
    # rather than as a remote error several hops away. Validation happens before
    # the tool context is touched, so a null one is safe to pass.
    tool = _tool()
    with pytest.raises(ValueError, match="Invalid payload for peer 'research'"):
        await tool.run_async(
            args={"case_id": "c1"}, tool_context=cast(ToolContext, None)
        )


def test_specialist_sees_only_the_payload():
    # What AgentTool builds: a fresh session whose sole event is the arguments.
    payload = {"case_id": "case-1", "question": "Is BNP Paribas registered in IE?"}
    session = Session(
        id="child",
        app_name="app",
        user_id="u1",
        events=[_text_event("user", json.dumps(payload, sort_keys=True))],
    )
    assert _outbound(session) == [json.dumps(payload, sort_keys=True)]


def test_transcript_wiring_would_leak_and_payload_wiring_does_not():
    # The measurement from docs/design-decisions.md (D1), pinned as a guard.
    # A peer attached as a SUB-AGENT runs inside the caller's session, so the
    # caller's transcript is what reaches it.
    transcript = Session(
        id="caller",
        app_name="app",
        user_id="u1",
        events=[
            _text_event("user", "my mobile is +353 87 555 0101"),
            _text_event("orchestrator", "noted"),
            _text_event("math", "17 * 23 = 391"),
            _text_event("user", "now check BNP Paribas in IE"),
        ],
    )
    leaked = " ".join(_outbound(transcript))
    assert "555 0101" in leaked
    assert "391" in leaked

    # A peer attached as a TOOL runs in a fresh session holding only the payload.
    payload = {"case_id": "case-1", "question": "Is BNP Paribas registered in IE?"}
    clean = Session(
        id="child",
        app_name="app",
        user_id="u1",
        events=[_text_event("user", json.dumps(payload, sort_keys=True))],
    )
    sent = " ".join(_outbound(clean))
    assert "555 0101" not in sent
    assert "391" not in sent


def test_session_state_never_reaches_the_wire():
    # D3: `shared:` state was documented as propagating across A2A hops. It does
    # not -- only event content is serialized. This guards the claim so the old
    # docstring cannot creep back.
    session = Session(
        id="s1",
        app_name="app",
        user_id="u1",
        state={"shared:client": "SENTINEL-STATE", "published_value": "SENTINEL-2"},
        events=[_text_event("user", "look up the entity")],
    )
    sent = " ".join(_outbound(session))
    assert "SENTINEL" not in sent
    assert sent == "look up the entity"
