"""Unit tests for human-in-the-loop capture and resume.

Hermetic: no model, no network. They exercise the pause-capture logic and the
shapes of the responses sent back to ADK, plus a guard on the ADK routing bug
the resume helper works around.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.genai import types

from app.agents import AGENTS
from app.cluster import approvals, hitl
from app.cluster.resolver import AgentResolver
from app.shared.config import Models

_FAKE_MODELS = cast(
    Models,
    SimpleNamespace(
        fast="gemini-2.5-flash-lite",
        balanced="gemini-2.5-flash",
        capable="gemini-2.5-pro",
    ),
)


def _call_event(
    *, name: str, call_id: str, args: dict, author: str = "math", long_running: bool
) -> Event:
    """Build an event carrying a (possibly long-running) function call."""
    return Event(
        author=author,
        invocation_id="e-test",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(id=call_id, name=name, args=args)
                )
            ],
        ),
        long_running_tool_ids={call_id} if long_running else set(),
    )


#: A fresh store per test, installed by setup_function. Explicit rather than
#: process-global: the store is injected everywhere in app/ (see
#: app/cluster/di.py), so the tests hand it over the same way the injector does.
STORE: approvals.ApprovalStore


def _capture(event: Event) -> list[hitl.PendingApproval]:
    return asyncio.run(
        hitl.capture(event, app_name="app", user_id="u", session_id="s", store=STORE)
    )


def _pending() -> list[hitl.PendingApproval]:
    return asyncio.run(STORE.list_by_status(approvals.PENDING))


def setup_function() -> None:
    # The durable backend is covered separately, in test_approvals.py and
    # against real Postgres.
    global STORE
    STORE = approvals.InMemoryApprovalStore()


def test_confirmation_pause_is_captured_with_tool_and_hint():
    event = _call_event(
        name=hitl.CONFIRMATION_CALL,
        call_id="adk-1",
        args={
            "originalFunctionCall": {"name": "publish_result", "args": {"value": "42"}},
            "toolConfirmation": {"hint": "Approve publishing 42?", "confirmed": False},
        },
        long_running=True,
    )
    (pending,) = _capture(event)
    assert pending.kind == "confirmation"
    assert pending.tool_name == "publish_result"
    assert pending.message == "Approve publishing 42?"
    assert pending.author == "math"
    assert pending.status == "pending"


def test_input_pause_is_captured_with_the_question():
    event = _call_event(
        name=hitl.INPUT_CALL,
        call_id="adk-2",
        args={"message": "Which Cambridge did you mean?"},
        author="research",
        long_running=True,
    )
    (pending,) = _capture(event)
    assert pending.kind == "input"
    assert pending.message == "Which Cambridge did you mean?"
    assert pending.tool_name == ""


def test_ordinary_function_calls_are_not_captured():
    event = _call_event(
        name="calculate",
        call_id="adk-3",
        args={"expression": "1+1"},
        long_running=False,
    )
    assert _capture(event) == []


def test_capture_is_idempotent_for_the_same_call_id():
    event = _call_event(
        name=hitl.INPUT_CALL, call_id="adk-4", args={"message": "?"}, long_running=True
    )
    assert len(_capture(event)) == 1
    # ADK resumption is at-least-once, so the same pause can be replayed.
    assert _capture(event) == []
    assert len(_pending()) == 1


def test_confirmation_response_carries_confirmed_flag_and_note():
    pending = hitl.PendingApproval(
        approval_id="a",
        kind="confirmation",
        call_id="adk-5",
        call_name=hitl.CONFIRMATION_CALL,
        tool_name="publish_result",
        message="Approve?",
    )
    content = hitl.confirmation_response(pending, approved=True, note="looks fine")
    (part,) = content.parts or []
    assert part.function_response is not None
    response = part.function_response.response or {}
    # ADK reads `confirmed` to decide whether the tool body runs at all; the
    # payload is free-form data the tool can read.
    assert response["confirmed"] is True
    assert response["payload"] == {"note": "looks fine"}
    assert part.function_response.id == "adk-5"


def test_input_response_carries_free_text():
    pending = hitl.PendingApproval(
        approval_id="b",
        kind="input",
        call_id="adk-6",
        call_name=hitl.INPUT_CALL,
        tool_name="",
        message="Which one?",
    )
    content = hitl.input_response(pending, text="the one in Massachusetts")
    (part,) = content.parts or []
    assert part.function_response is not None
    # MUST be exactly {"result": value}: ADK unwraps that single key before
    # validating against the pause's response_schema. Any other key reaches the
    # validator still wrapped and kills the invocation.
    assert part.function_response.response == {"result": "the one in Massachusetts"}


def test_plugin_captures_from_any_serving_surface():
    """The plugin is the single capture point, whatever drove the Runner."""
    event = _call_event(
        name=hitl.INPUT_CALL, call_id="adk-7", args={"message": "?"}, long_running=True
    )
    ctx = cast(
        InvocationContext,
        type(
            "Ctx",
            (),
            {
                "session": type(
                    "S", (), {"app_name": "app", "user_id": "u", "id": "s"}
                )()
            },
        )(),
    )
    # asyncio.run rather than an async test: the suite has no async plugin, and a
    # single coroutine does not justify adding one.
    plugin = hitl.HitlPlugin(STORE)
    returned = asyncio.run(
        plugin.on_event_callback(invocation_context=ctx, event=event)
    )
    assert returned is None
    assert [p.call_id for p in _pending()] == ["adk-7"]


def test_agents_declare_the_expected_strategies():
    """Each HITL strategy sits on exactly the agent that demonstrates it."""

    def names(agent_name: str) -> set[str]:
        return {
            getattr(t, "name", None) or getattr(t, "__name__", "?")
            for t in AGENTS[agent_name].tools
        }

    assert "publish_result" in names("math")  # A: require_confirmation
    assert "adk_request_input" in names("research")  # B: free-form question
    # C: a graph-rooted agent, no tools at all -- the pause is a graph node.
    assert AGENTS["planner"].root_node is not None
    assert AGENTS["planner"].tools == ()


def test_resume_appends_the_response_before_running():
    """Guard for the ADK routing bug the resume helper works around.

    ``Runner.run_async`` chooses the agent that continues an invocation
    (runners.py:1089) *before* appending the incoming message (runners.py:554),
    so a resume that passes the FunctionResponse as ``new_message`` is routed to
    the root agent and yields nothing. ``hitl.resume`` therefore appends the
    event itself and resumes with ``new_message=None``.

    If a future ADK release fixes the ordering, this test still passes -- it
    pins OUR contract. The behavioural guard lives in the integration test,
    which asserts the bug is still present.
    """
    import inspect

    source = inspect.getsource(hitl.resume)
    assert "_append_user_event" in source
    assert "new_message=None" in source
    # And the Runner API we depend on still takes those arguments.
    params = inspect.signature(Runner.run_async).parameters
    assert {"invocation_id", "new_message"} <= set(params)


def test_decide_route_claims_then_resumes_then_completes():
    """Ordering guard: claim, resume, and only then record the outcome.

    Recording the outcome before resuming is how a crashed resume made an
    approval permanently un-retryable in the cluster: every retry answered
    "already_decided" while the invocation stayed paused forever. The behaviour
    itself is covered in test_approvals.py; this pins the route's ordering,
    which is the part a refactor could silently invert.
    """
    import inspect

    from app import fast_api_app

    source = inspect.getsource(fast_api_app.hitl_decide)
    claim = source.index("await store.claim(")
    resume_call = source.index("await hitl.resume(")
    complete = source.index("await store.complete(")
    assert claim < resume_call < complete
    # Every failure path releases the claim so the human can retry.
    assert source.count("await store.release(approval_id)") == 3


def test_graph_agent_is_served_as_its_workflow():
    """A spec carrying a root_node is served as the graph, not an LlmAgent."""
    from google.adk.workflow import Workflow

    from app.agents import build_agent
    from app.agents.planner.workflow import planner_workflow

    resolver = cast("AgentResolver", SimpleNamespace(resolve_all=lambda: []))
    built = build_agent(AGENTS["planner"], _FAKE_MODELS, resolver)
    assert isinstance(built, Workflow)
    assert built is planner_workflow


def test_graph_agent_with_peers_is_rejected():
    """Peers on a graph agent would be silently dropped, so refuse them."""
    from app.agents import build_agent

    resolver = cast("AgentResolver", SimpleNamespace(resolve_all=lambda: [object()]))
    with pytest.raises(ValueError, match="cannot delegate over A2A"):
        build_agent(AGENTS["planner"], _FAKE_MODELS, resolver)


def test_planner_graph_marks_the_post_pause_node():
    """The node after the human pause tags its output.

    That marker is the only reliable evidence the graph continued past the
    pause: the rejected `run_node` shortcut produced a plausible answer while
    skipping this node entirely (docs/plans/hitl/results.md R3).
    """
    from app.agents.planner.workflow import APPLIED_MARKER, apply_feedback

    result = apply_feedback("add a rollback step")
    # Content, not str: only Event(content=...) reaches an A2A caller (R8).
    text = "".join(p.text or "" for p in result.parts or [])
    assert APPLIED_MARKER in text
    assert "add a rollback step" in text


# --- the startup sweep's honesty (R10) ---------------------------------------


class _FakeSessionService:
    """Just enough session service for resume()'s append step."""

    def __init__(self, session) -> None:
        self._session = session
        self.appended: list[Event] = []

    async def get_session(self, *, app_name, user_id, session_id):
        return self._session

    async def append_event(self, *, session, event) -> None:
        self.appended.append(event)
        session.events.append(event)


class _FakeRunner:
    """Yields a scripted event stream from run_async."""

    def __init__(self, session_service, events: list[Event]) -> None:
        self.session_service = session_service
        self._events = events

    async def run_async(self, **_kwargs):
        for event in self._events:
            yield event


def _abandoned_store() -> tuple[approvals.InMemoryApprovalStore, str]:
    """A store holding one approval abandoned mid-resume by a dead process."""
    store = approvals.InMemoryApprovalStore()
    pending = hitl.PendingApproval(
        approval_id="r10",
        kind="confirmation",
        call_id="adk-r10",
        call_name=hitl.CONFIRMATION_CALL,
        tool_name="publish_result",
        message="Approve?",
        app_name="app",
        user_id="u",
        session_id="s",
        invocation_id="e-1",
        author="math",
    )
    asyncio.run(store.add(pending))
    asyncio.run(
        store.claim("r10", decision={"approved": True, "text": "ok"}, decided_by="me")
    )
    # The crash: the owner died, so nothing has renewed the lease since. Age is
    # what makes it reclaimable -- the identity is irrelevant now that several
    # replicas may legitimately hold leases at once.
    store._items["r10"].deciding_by = "dead-pod/0000"
    store._items["r10"].deciding_since = datetime.now(UTC) - timedelta(hours=1)
    return store, "r10"


def test_sweep_reports_a_replay_that_produced_an_answer():
    store, _ = _abandoned_store()
    session = SimpleNamespace(events=[])
    answer = Event(
        author="math",
        invocation_id="e-1",
        content=types.Content(role="model", parts=[types.Part(text="437")]),
    )
    runner = _FakeRunner(_FakeSessionService(session), [answer])

    (result,) = asyncio.run(hitl.redrive_abandoned(cast(Runner, runner), store))

    assert result["replayed"] is True
    stored = asyncio.run(store.get("r10"))
    assert stored is not None
    assert stored.status == approvals.APPROVED
    assert stored.resumed_at is not None


def test_sweep_does_not_claim_success_when_the_replay_yields_nothing():
    """R10: a failed A2A hop is an error EVENT, not an exception.

    The peer's task is already terminal after a crash, so re-sending is
    rejected and run_async returns normally having produced no answer. Marking
    that "recovered" is the "plausible answer is not proof the flow ran" trap:
    the decision stands and its effect happened, but no continuation completed,
    so resumed_at must stay NULL.
    """
    store, _ = _abandoned_store()
    session = SimpleNamespace(events=[])
    failed = Event(
        author="math",
        invocation_id="e-1",
        error_message="A2A request failed: Task abc is in terminal state: completed",
    )
    runner = _FakeRunner(_FakeSessionService(session), [failed])

    (result,) = asyncio.run(hitl.redrive_abandoned(cast(Runner, runner), store))

    assert result["replayed"] is False
    assert result["errors"], "the error event must be surfaced, not swallowed"
    stored = asyncio.run(store.get("r10"))
    assert stored is not None
    # Finished, not released: retrying cannot help once the task is terminal.
    assert stored.status == approvals.APPROVED
    # But no continuation ran, so this must not claim one did.
    assert stored.resumed_at is None


def test_resume_does_not_append_a_second_response_on_redrive():
    """The re-drive guard: the response may already be in the session."""
    existing = Event(
        author="user",
        invocation_id="e-1",
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="adk-r10", name=hitl.CONFIRMATION_CALL, response={}
                    )
                )
            ],
        ),
    )
    session = SimpleNamespace(events=[existing])
    assert hitl.response_in_session(cast(Any, session), "adk-r10") is True

    service = _FakeSessionService(session)
    runner = _FakeRunner(service, [])
    pending = hitl.PendingApproval(
        approval_id="r10",
        kind="confirmation",
        call_id="adk-r10",
        call_name=hitl.CONFIRMATION_CALL,
        tool_name="publish_result",
        message="Approve?",
        app_name="app",
        user_id="u",
        session_id="s",
        invocation_id="e-1",
    )
    asyncio.run(hitl.resume(cast(Runner, runner), pending, hitl.content_for(pending)))
    assert service.appended == [], "appending twice is the F7(a) mistake D5 rejects"


# --- the diagnostic dump's serialisability -----------------------------------


def test_content_json_survives_binary_parts():
    """A Part's thought_signature is raw bytes and used to 500 /hitl/session.

    Gemini attaches an opaque binary thinking blob to function-call parts. A
    python-mode model_dump() returns it as `bytes`, which is not JSON
    serialisable, so the whole diagnostic route failed with
    PydanticSerializationError rather than just omitting one field.
    """
    import json

    event = Event(
        author="orchestrator",
        invocation_id="e-1",
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id="c-1", name="transfer_to_agent", args={"agent_name": "math"}
                    ),
                    # Deliberately not valid UTF-8: that is the failure mode.
                    thought_signature=b"\x01\x8f=k_B\xd5\x821\x85\xe2\xb2",
                )
            ],
        ),
    )

    payload = hitl.content_json(event)
    # The real assertion: this is what FastAPI does, and it used to raise.
    encoded = json.dumps(payload)

    assert payload is not None
    assert payload["parts"][0]["function_call"]["name"] == "transfer_to_agent"
    # Opaque and useless for diagnosing a pause, so it is dropped, not base64ed.
    assert "thought_signature" not in encoded


def test_content_json_keeps_real_binary_payload():
    """Only the thinking blob is dropped; actual inline data is preserved."""
    import json

    event = Event(
        author="user",
        invocation_id="e-1",
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    inline_data=types.Blob(mime_type="image/png", data=b"\x89PNG\r\n")
                )
            ],
        ),
    )

    payload = hitl.content_json(event)
    encoded = json.dumps(payload)

    assert payload is not None
    # base64-encoded rather than discarded: this could be real payload.
    assert payload["parts"][0]["inline_data"]["mime_type"] == "image/png"
    assert payload["parts"][0]["inline_data"]["data"]
    assert isinstance(encoded, str)


def test_content_json_handles_an_event_without_content():
    assert hitl.content_json(Event(author="user", invocation_id="e-1")) is None
