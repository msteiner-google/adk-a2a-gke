"""Human-in-the-loop: capturing pauses and resuming them with a decision.

An agent pauses whenever it emits a **long-running function call** — ADK
synthesises one for a tool that declares ``require_confirmation``, the built-in
``request_input`` tool is one, and a graph ``RequestInput`` node surfaces as one
too. The invocation then stops with no final answer until someone sends the
matching ``FunctionResponse``.

This module supplies the three pieces that turns that mechanism into a usable
feature (see ``docs/plans/hitl/``):

- :class:`HitlPlugin` — captures every pause, whichever serving surface drove the
  run (ADK web, the A2A executor, or the ``/hitl`` routes). A plugin sees events
  on the *Runner*, so there is exactly one capture point.
- The store — what is waiting, and who holds it. Lives in
  ``app/cluster/approvals.py``: durable on the ``hitl_approvals`` table when a
  database is configured, per-pod in memory otherwise.
- :func:`resume` — sends the human's response back into the paused invocation,
  working around an ADK routing bug (see ``_RESUME_NOTE``), and skipping the
  append when a previous attempt already made it (see :func:`response_in_session`).

Nothing here is agent-specific: any agent that pauses is captured the same way,
and a pause raised inside a peer reached over A2A is captured on the caller,
because the peer's long-running call is replayed into the caller's event stream.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from google.adk.events.event import Event
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from app.cluster.approvals import (
    APPROVED,
    REJECTED,
    ApprovalStore,
    PendingApproval,
    heartbeat,
)

if TYPE_CHECKING:
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.runners import Runner
    from google.adk.sessions import BaseSessionService, Session

__all__ = [
    "CONFIRMATION_CALL",
    "INPUT_CALL",
    "HitlPlugin",
    "PendingApproval",
    "capture",
    "confirmation_response",
    "content_for",
    "content_json",
    "heartbeat",
    "input_response",
    "redrive_abandoned",
    "response_in_session",
    "resume",
    "summarise",
]

# ADK's own names for the two synthesised pause calls. A tool that declares
# `require_confirmation` pauses as `adk_request_confirmation`; the built-in
# free-form input tool pauses as `adk_request_input`.
CONFIRMATION_CALL = "adk_request_confirmation"
INPUT_CALL = "adk_request_input"

_RESUME_NOTE = """\
Why resume() appends the response itself instead of passing new_message:

For an LlmAgent root, ADK 2.6.1 picks the agent that continues the invocation in
`Runner.run_async` (runners.py:1089) BEFORE `_run_node_async` appends the
incoming message (runners.py:554). After a `transfer_to_agent` to an A2A peer,
the last session event at routing time is still the peer's function CALL, so
`find_matching_function_call` misses, routing falls back to the root agent -- and
the root is already `end_of_agent`, so the run yields nothing at all. The pause
stays pending and the caller sees an empty stream.

Appending the FunctionResponse first makes the routing decision see a user
function response and route to the paused agent. Verified on ADK 2.6.1; see
docs/plans/hitl/spike-findings.md (F6/F7). tests/unit/test_hitl.py guards it, so
this workaround can be deleted when ADK fixes the ordering.
"""


def _describe(call_name: str, args: dict[str, Any]) -> tuple[str, str, str]:
    """Return ``(kind, tool_name, message)`` for a paused function call."""
    if call_name == CONFIRMATION_CALL:
        original = args.get("originalFunctionCall") or {}
        confirmation = args.get("toolConfirmation") or {}
        tool_name = str(original.get("name", ""))
        hint = str(confirmation.get("hint", "")) or f"Approve calling {tool_name}()?"
        return "confirmation", tool_name, hint
    if call_name == INPUT_CALL:
        return "input", "", str(args.get("message", "Input requested."))
    return "other", call_name, f"{call_name}() is waiting for a response."


async def capture(
    event: Event,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    store: ApprovalStore,
) -> list[PendingApproval]:
    """Record any long-running call on ``event`` as a pending approval.

    Args:
        event: The event to inspect.
        app_name: ADK app name owning the session.
        user_id: The session's user.
        session_id: The session that paused.
        store: Where to record.

    Returns:
        The approvals newly created by this call — empty when the event carries
        no pause, or when every pause on it was already known.
    """
    if not event.long_running_tool_ids:
        return []
    created: list[PendingApproval] = []
    for call in event.get_function_calls():
        if call.id not in event.long_running_tool_ids:
            continue
        args = dict(call.args or {})
        kind, tool_name, message = _describe(call.name or "", args)
        approval = PendingApproval(
            approval_id=uuid.uuid4().hex[:8],
            kind=kind,
            call_id=call.id or "",
            call_name=call.name or "",
            tool_name=tool_name,
            message=message,
            args=args,
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            invocation_id=event.invocation_id,
            author=event.author,
        )
        # The store rejects a repeat of the same (session, call): resumption is
        # at-least-once, so the same pause really is observed twice.
        if await store.add(approval):
            created.append(approval)
    return created


class HitlPlugin(BasePlugin):
    """Captures pauses from every serving surface, on the Runner itself."""

    def __init__(self, store: ApprovalStore) -> None:
        """Name the plugin for ADK's registry.

        Args:
            store: Where to record pauses. Passed in rather than resolved
                internally so the plugin shares the one store the injector
                provides -- see the note in :mod:`app.cluster.di`.
        """
        super().__init__(name="hitl")
        self._store = store

    async def on_event_callback(
        self, *, invocation_context: InvocationContext, event: Event
    ) -> Event | None:
        """Record a pause; never modify the event."""
        session = invocation_context.session
        await capture(
            event,
            app_name=session.app_name,
            user_id=session.user_id,
            session_id=session.id,
            store=self._store,
        )
        return None


def confirmation_response(
    pending: PendingApproval, *, approved: bool, note: str
) -> types.Content:
    """Build the response ADK expects for a ``require_confirmation`` pause."""
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=pending.call_id,
                    name=pending.call_name,
                    # ToolConfirmation: `confirmed` gates the tool body, `payload`
                    # is free-form data the tool can read via
                    # tool_context.tool_confirmation.payload.
                    response={
                        "confirmed": approved,
                        "hint": pending.message,
                        "payload": {"note": note},
                    },
                )
            )
        ],
    )


def input_response(pending: PendingApproval, *, text: str) -> types.Content:
    """Build the response for an ``adk_request_input`` pause (free-form).

    The payload MUST be ``{"result": <value>}`` -- a dict with exactly that one
    key. ADK's workflow rehydration unwraps that shape
    (``workflow/utils/_rehydration_utils.py:68``) and then validates the inner
    value against the ``response_schema`` the pause declared. Any other key is
    left wrapped, so a ``str`` schema is handed a dict and the whole invocation
    dies with ``Validation failed for interrupt <id>``. Verified the hard way in
    the cluster; see docs/plans/hitl/results.md (R4).

    Note ADK JSON-parses a string value when it can, so a reply of ``"42"``
    arrives as an int.
    """
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=pending.call_id,
                    name=pending.call_name,
                    response={"result": text},
                )
            )
        ],
    )


def response_in_session(session: Session, call_id: str) -> bool:
    """Whether the session already carries a response to ``call_id``.

    This is the guard that makes a **re-drive** safe. After a crash mid-resume
    the response may already be in the session, because :func:`resume` appends
    it before driving the model. Appending it a second time is exactly the
    F7(a) mistake D5 rejects: two responses to one call.

    Args:
        session: The session to scan.
        call_id: The paused function call's id.

    Returns:
        ``True`` when a matching ``FunctionResponse`` is already present.
    """
    return any(
        response.id == call_id
        for event in session.events
        for response in event.get_function_responses()
    )


async def _append_user_event(
    session_service: BaseSessionService,
    pending: PendingApproval,
    content: types.Content,
) -> bool:
    """Append the human's response to the session before resuming.

    Args:
        session_service: The service owning the session.
        pending: The captured pause.
        content: The ``FunctionResponse`` content answering it.

    Returns:
        ``True`` if the response was appended, ``False`` if an earlier attempt
        had already appended it and this call was a re-drive.

    Raises:
        LookupError: If the session no longer exists.
    """
    session = await session_service.get_session(
        app_name=pending.app_name,
        user_id=pending.user_id,
        session_id=pending.session_id,
    )
    if session is None:
        raise LookupError(f"session {pending.session_id!r} is gone")
    if response_in_session(session, pending.call_id):
        return False
    await session_service.append_event(
        session=session,
        event=Event(
            author="user",
            invocation_id=pending.invocation_id,
            content=content,
        ),
    )
    return True


async def resume(
    runner: Runner, pending: PendingApproval, content: types.Content
) -> tuple[list[str], str | None]:
    """Resume the paused invocation with the human's response.

    Args:
        runner: The serving Runner (``app.state.runner``).
        pending: The captured pause.
        content: The ``FunctionResponse`` content answering it.

    Returns:
        A ``(trace, final_text)`` pair: one line per event, and the agent's final
        answer if it produced one.
    """
    # See _RESUME_NOTE: append first, then resume with no new_message.
    await _append_user_event(runner.session_service, pending, content)

    trace: list[str] = []
    final_text: str | None = None
    async for event in runner.run_async(
        user_id=pending.user_id,
        session_id=pending.session_id,
        invocation_id=pending.invocation_id,
        new_message=None,
    ):
        trace.append(summarise(event))
        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(p.text or "" for p in event.content.parts).strip()
            if text:
                final_text = text
    return trace, final_text


def content_for(pending: PendingApproval) -> types.Content:
    """Rebuild the response content from the approval's persisted decision.

    Purely a function of stored state, which is what lets a sweeper re-drive an
    interrupted resume without the original HTTP request (D4.2). It is the
    reason the claim writes ``decision`` up front.

    Args:
        pending: A claimed approval carrying a decision.

    Returns:
        The ``FunctionResponse`` content answering the pause.
    """
    payload = pending.decision or {}
    approved = bool(payload.get("approved", True))
    text = str(payload.get("text", "") or "")
    if pending.kind == "confirmation":
        return confirmation_response(pending, approved=approved, note=text)
    return input_response(pending, text=text)


async def redrive_abandoned(
    runner: Runner, store: ApprovalStore
) -> list[dict[str, Any]]:
    """Finish resumes that a previous incarnation of this pod left in flight.

    Call once at startup. Each abandoned lease is taken over, re-driven, and
    then either completed or released so a human can retry — a failure here must
    never leave the approval unanswerable, which is the whole point of D4.2.

    Args:
        runner: The serving Runner.
        store: The approval store.

    Returns:
        One result dict per approval recovered, for logging.
    """
    recovered: list[dict[str, Any]] = []
    for pending in await store.sweep_abandoned():
        payload = pending.decision or {}
        outcome = APPROVED if payload.get("approved", True) else REJECTED
        try:
            # Renew the lease while re-driving. A recovery takes as long as the
            # resume it replaces, and without this another replica's sweep would
            # find the row stale and re-drive it a second time alongside us.
            async with heartbeat(store, pending.approval_id):
                trace, final_text = await resume(runner, pending, content_for(pending))
        except Exception as exc:
            await store.release(pending.approval_id)
            recovered.append(
                {
                    "approval_id": pending.approval_id,
                    "status": "released",
                    "error": str(exc),
                }
            )
            continue

        # A replay can "succeed" while producing nothing. ADK folds a failed A2A
        # hop into an error EVENT rather than raising, so a run that never
        # reached the agent still returns normally with no final text -- and
        # calling that recovered is exactly the "a plausible answer is not proof
        # the flow ran" trap this project keeps rediscovering (R3, R7).
        #
        # The usual cause is structural, not transient: when the pause happened
        # inside an A2A peer and the crash came after that peer finished, its
        # task is already terminal ("Task <id> is in terminal state: completed")
        # and re-sending is rejected. The decision stands and its effect already
        # happened exactly once -- only the narration is lost -- so the row is
        # finished rather than released, but resumed_at stays NULL because no
        # continuation completed. Verified in the cluster; see results.md R10.
        errors = [line for line in trace if "error=" in line]
        replayed = final_text is not None and not errors
        await store.complete(pending.approval_id, status=outcome, resumed=replayed)
        recovered.append(
            {
                "approval_id": pending.approval_id,
                "status": outcome,
                "replayed": replayed,
                "final_text": final_text,
                "errors": errors,
            }
        )
    return recovered


def content_json(event: Event) -> dict[str, Any] | None:
    """Return an event's content as JSON-safe data for the diagnostic dump.

    Two things a plain ``model_dump()`` gets wrong here, both of which showed up
    as a 500 on ``/hitl/session/{id}``:

    - **Bytes.** A Gemini ``Part`` can carry ``thought_signature``, an opaque
      binary blob. The default python-mode dump hands back raw ``bytes``, which
      is not JSON-serialisable, so the entire response failed with
      ``PydanticSerializationError: invalid utf-8 sequence``. ``mode="json"``
      encodes bytes as base64, which is correct for *any* binary part --
      inline image or audio data included -- not just this one field.
    - **Noise.** That signature runs to a few hundred bytes, so base64 makes it
      several times larger than the content someone actually opened this
      endpoint to read. It is opaque to us and useless for diagnosing a pause,
      so it is dropped rather than encoded. Other binary parts are kept, since
      those could be real payload.

    Args:
        event: The event whose content to render.

    Returns:
        JSON-safe content, or ``None`` when the event carries none.
    """
    if event.content is None:
        return None
    return event.content.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"parts": {"__all__": {"thought_signature"}}},
    )


def summarise(event: Event) -> str:
    """Return a one-line, log-friendly description of an event."""
    bits = [event.author]
    if event.long_running_tool_ids:
        bits.append(f"lrt={sorted(event.long_running_tool_ids)}")
    if calls := [c.name for c in event.get_function_calls()]:
        bits.append(f"calls={calls}")
    if resps := [r.name for r in event.get_function_responses()]:
        bits.append(f"resp={resps}")
    text = "".join(
        p.text or "" for p in (event.content.parts if event.content else []) or []
    ).strip()
    if text:
        bits.append(f"text={text[:160]!r}")
    if event.error_message:
        bits.append(f"error={event.error_message!r}")
    return " | ".join(bits)
