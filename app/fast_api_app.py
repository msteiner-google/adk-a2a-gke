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

# The serving app. This looks like boilerplate, but three things here are
# load-bearing and easy to "simplify" away by mistake:
#
#   1. `instrument_fastapi_app(app)` after the app is built (see below).
#   2. The Runner is built with the injector's session service instead of
#      services.get_session_service() (see the note at the call site).
#   3. The Runner is built with the injector's artifact service instead of
#      services.get_artifact_service(), and `shared://artifact` is re-registered
#      to the same instance when a storage URI is configured.
#
# On (1): that call gives the serving app standard OpenTelemetry
# FastAPI instrumentation, which EXTRACTS the W3C `traceparent` header from
# inbound A2A requests so each agent continues the caller's trace instead of
# starting a new one. Combined with the httpx client instrumentation set up in
# app/shared (which INJECTS `traceparent` on outbound A2A calls), a single trace
# spans every hop of the multi-agent system. ADK's built-in context propagation
# only handles Google-Agent-Engine headers, not the standard `traceparent`, so
# this explicit instrumentation is what makes cross-pod A2A tracing work.

import contextlib
import dataclasses
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import google.auth
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from google.adk.artifacts.base_artifact_service import BaseArtifactService
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.service_registry import get_service_registry
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.cloud import logging as google_cloud_logging
from google.genai import types as genai_types
from loguru import logger as log
from pydantic import BaseModel

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.typing import Feedback
from app.cluster import cases, grants
from app.cluster.artifacts import ARTIFACT_STORAGE_URI_ENV
from app.cluster.authorization import build_executor_config
from app.cluster.db import DatabaseConfig
from app.shared.telemetry import instrument_fastapi_app

load_dotenv()
_, project_id = google.auth.default()
logging_client = google_cloud_logging.Client()
logger = logging_client.logger(__name__)
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Whether a durable database is configured. A pure config read, deliberately not
# a Database instance: this runs at import, and the only question being asked is
# yes/no. Reading it from `app.agent` instead would import the injector here, and
# that resolves the live Vertex model catalog over the network -- see the
# "Importing app hits the network" gotcha in AGENTS.md. The instance itself comes
# from the injector, inside lifespan, like every other service.
_DATABASE_CONFIGURED = DatabaseConfig.from_env().enabled


# When a durable database is configured, override app_utils/services.py's
# "shared://session" registration so the ADK web routes resolve to the SAME
# DB-backed session service the Runner and the A2A path use. app/__init__.py
# imports app.agent, so the injector is already built by the time the factory
# below is called; the import stays inside the function only to keep this
# module importable in isolation.
#
# This is intentionally conditional: with no database configured (the default,
# and every test run) the original registration is left completely untouched,
# so local `adk web` behaviour is unchanged.
if _DATABASE_CONFIGURED:

    def _shared_session_service(uri: str, **kwargs: Any) -> BaseSessionService:
        """Resolve `shared://session` to the injector's session service."""
        del uri, kwargs  # The scheme carries no configuration of its own.
        from app.agent import session_service

        return session_service

    get_service_registry().register_session_service("shared", _shared_session_service)


# Same treatment for artifacts: when a storage location is configured, override
# the "shared://artifact" registration so the ADK web routes (upload/download)
# hit the SAME cloudpathlib-backed service the Runner and the A2A path use,
# instead of services.get_artifact_service() -- which only knows about a GCS
# bucket in LOGS_BUCKET_NAME and otherwise silently hands back a per-pod
# in-memory store.
#
# Conditional for the same reason as above: with ARTIFACT_STORAGE_URI unset (the
# default, and every test run) the original registration is left untouched.
if os.environ.get(ARTIFACT_STORAGE_URI_ENV, "").strip():

    def _shared_artifact_service(uri: str, **kwargs: Any) -> BaseArtifactService:
        """Resolve `shared://artifact` to the injector's artifact service."""
        del uri, kwargs  # The scheme carries no configuration of its own.
        from app.agent import artifact_service

        return artifact_service

    get_service_registry().register_artifact_service("shared", _shared_artifact_service)


@dataclasses.dataclass
class TurnResult:
    """What one conversational turn produced.

    Attributes:
        trace: One log-friendly line per event.
        final_text: The agent's final answer, if it produced one.
        texts: Every text body the turn emitted — tool results included. A
            specialist reached over A2A arrives as a tool result, so this is
            where a reported proposal is found.
    """

    trace: list[str] = dataclasses.field(default_factory=list)
    final_text: str | None = None
    texts: list[str] = dataclasses.field(default_factory=list)


async def _run_turn(
    runner: Runner, *, user_id: str, session_id: str, text: str
) -> TurnResult:
    """Drive one conversational turn and collect what it produced.

    Args:
        runner: The serving Runner.
        user_id: The session's user.
        session_id: The session to run on.
        text: The user (or system) message to send.

    Returns:
        The turn's trace, final answer and every text it emitted.
    """
    result = TurnResult()
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part(text=text)]
        ),
    ):
        result.trace.append(cases.summarise(event))
        result.texts.extend(cases.reply_texts(event))
        if (
            event.is_final_response()
            and event.content
            and event.content.parts
            and (body := "".join(p.text or "" for p in event.content.parts).strip())
        ):
            result.final_text = body
    return result


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import (
        artifact_service,
        case_store,
        database,
        resolver,
        root_agent,
        session_service,
        task_store,
    )

    runner = Runner(
        app=adk_app,
        # Use the injector's session service rather than
        # services.get_session_service(): that helper only understands a plain
        # DSN, and the AlloyDB path authenticates per connection with IAM. When
        # no database is configured both resolve to the same in-memory service.
        session_service=session_service,
        # Likewise the injector's artifact service rather than
        # services.get_artifact_service(): the cloudpathlib-backed store follows
        # ARTIFACT_STORAGE_URI (gs:// / s3:// / az:// / local path), while that
        # helper only understands a GCS bucket in LOGS_BUCKET_NAME. With no URI
        # configured both resolve to an in-memory service.
        artifact_service=artifact_service,
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    # The one store the injector built. Only this agent writes it: a case
    # belongs to whoever asked the human, not to the specialist that proposed.
    app.state.case_store = case_store
    app.state.cluster_config = resolver.config

    async def record_request(
        context: Any,
        call: dict[str, Any],
        peer: tuple[str, str, str] | None,
    ) -> None:
        """Open an approval case for a tool this agent just suspended."""
        if peer is None:
            # The gated tool is this agent's own. Nothing to record here: the
            # agent that relayed the request to a human is the one that owns
            # the case, and it records the hop to us.
            return
        name, owner_task_id, owner_context_id = peer
        case = cases.case_from_confirmation(
            call,
            session_id=context.session_id,
            case_id=context.session_id,
            agent=name,
            owner_task_id=owner_task_id,
            owner_context_id=owner_context_id,
        )
        if await case_store.open(case):
            log.info(
                "case {}: {} on {} awaiting authorization",
                case.proposal_id,
                case.action,
                case.agent,
            )

    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        # Durable when TASK_STORE_BACKEND=database; otherwise per-pod
        # in-memory, exactly as before.
        task_store=task_store,
        rpc_path=f"/a2a/{adk_app.name}",
        # Report a suspended confirmation as TASK_STATE_AUTH_REQUIRED instead
        # of the input_required ADK derives for any long-running call, so a
        # caller can tell "a human must authorise this" apart from "the agent
        # asked a question". No-op for an agent that never asks.
        executor_config=build_executor_config(record_request),
    )

    # No recovery task. Nothing is held open between a proposal and its
    # approval, so there is no in-flight work a crash could strand -- which is
    # the operational point of modelling approval as business state rather than
    # a suspended invocation (docs/design-decisions.md).

    yield

    # Dispose the pool and stop the AlloyDB connector's background certificate
    # refresh, which would otherwise keep the event loop alive on shutdown.
    await database.aclose()


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=True,
    lifespan=lifespan,
)
app.title = "Multi-agent system (GKE)"
app.description = "API for interacting with the GKE multi-agent system"

# Extract W3C trace context from inbound A2A requests (see the module docstring).
# Importing app.agent above (via app.app_utils -> app package) already ran
# configure_observability(), so the global tracer provider and W3C propagator are
# in place before this instruments the app.
instrument_fastapi_app(app)


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}


# --- Approval cases -----------------------------------------------------------
# The human's window onto work that needs sign-off. A specialist never blocks:
# it returns a proposal and finishes, this agent records a case, and the
# approved action is carried out later by an ordinary new call. Nothing is held
# open in between, so an approval may take a week.
#
# See docs/design-decisions.md (D5) for why this replaced the previous
# pause-and-resume machinery, and docs/human-in-the-loop.md for the guide.


class CaseRun(BaseModel):
    """Start (or continue) a conversation that may raise an approval."""

    text: str
    session_id: str | None = None
    user_id: str = "case-user"


class CaseDecision(BaseModel):
    """A human's decision on a pending case."""

    approved: bool = True
    note: str = ""
    """Free-form feedback recorded with the decision."""
    decided_by: str = ""
    """Who decided, for audit. NOT verified -- see docs/human-in-the-loop.md."""


@app.post("/cases/run")
async def cases_run(req: CaseRun) -> dict[str, Any]:
    """Run a turn and report any approvals it raised."""
    runner = app.state.runner
    store: cases.CaseStore = app.state.case_store
    session_id = req.session_id or f"case-{uuid.uuid4().hex[:8]}"

    turn = await _run_turn(
        runner, user_id=req.user_id, session_id=session_id, text=req.text
    )

    opened: list[dict[str, Any]] = []
    for found in cases.find_proposals(turn.texts):
        case = cases.case_from_proposal(
            found, session_id=session_id, case_id=session_id
        )
        if await store.open(case):
            opened.append(case.public())

    return {
        "status": "awaiting_approval" if opened else "completed",
        "session_id": session_id,
        "user_id": req.user_id,
        "final_text": turn.final_text,
        "pending": opened,
        "trace": turn.trace,
    }


@app.get("/cases")
async def cases_list(status: str = cases.PENDING) -> dict[str, Any]:
    """List approval cases in a status, oldest first."""
    items = [c.public() for c in await app.state.case_store.list_by_status(status)]
    return {"count": len(items), "cases": items}


@app.get("/cases/{proposal_id}")
async def case_detail(proposal_id: str) -> dict[str, Any]:
    """Read one approval case."""
    case = await app.state.case_store.get(proposal_id)
    if case is None:
        raise HTTPException(status_code=404, detail="unknown proposal_id")
    return case.public()


@app.post("/cases/{proposal_id}")
async def case_decide(proposal_id: str, decision: CaseDecision) -> dict[str, Any]:
    """Decide a pending case and, if approved, deliver it to the owning agent.

    The decision is written FIRST and separately from the delivery. That
    ordering is what makes this recoverable with no lease and no sweeper: if the
    pod dies before or during delivery, the decision still stands on the row,
    and calling this endpoint again re-drives it. A retry is safe because a
    closed case short-circuits below rather than acting twice.

    The grant goes **straight to the agent that owns the suspended tool**, not
    back through this agent's own invocation. ADK cannot resolve a peer's
    confirmation locally -- it looks for the tool among its own and silently
    drops the grant when it is not there. See ``app/cluster/grants.py``.
    """
    store: cases.CaseStore = app.state.case_store

    outcome, case = await store.decide(
        proposal_id,
        approved=decision.approved,
        decided_by=decision.decided_by,
        note=decision.note,
    )
    if outcome is cases.DecisionOutcome.NOT_FOUND or case is None:
        raise HTTPException(status_code=404, detail="unknown proposal_id")
    if case.status == cases.REJECTED:
        # Still delivered: the specialist is suspended and has to be told, or
        # its task leaks for as long as the process lives.
        await _deliver(case, confirmed=False)
        return {"status": "rejected", "case": case.public()}
    if case.status in (cases.EXECUTED, cases.FAILED):
        # Idempotent: the action already ran, so report it instead of repeating.
        return {"status": "already_executed", "case": case.public()}

    if not case.owner_task_id or not case.confirmation_id:
        # A case opened before grant routing existed, or one whose request was
        # recorded without a suspended task. The decision stands; there is
        # simply nowhere to send it.
        log.warning("case {}: approved, but no suspended task to resume", proposal_id)
        return {"status": "approved_not_routable", "case": case.public()}

    try:
        task = await _deliver(case, confirmed=True)
    except grants.GrantDeliveryError as exc:
        log.warning("case {}: approved, but delivery failed: {}", proposal_id, exc)
        return {
            "status": "approved_not_delivered",
            "case": case.public(),
            "detail": str(exc),
        }

    performed = cases.find_execution(cases.task_texts(task), case.proposal)
    if performed is None:
        # "No exception" is not proof the effect happened -- the trap this repo
        # has hit before (docs/design-decisions.md). Say so plainly and leave
        # the case re-drivable rather than recording it as done.
        log.warning(
            "case {}: delivered, but the owner reported no confirmed execution",
            proposal_id,
        )
        return {
            "status": "approved_not_confirmed",
            "case": (await store.get(proposal_id) or case).public(),
            "owner_state": task.get("status", {}).get("state"),
        }

    updated = await store.record_execution(
        proposal_id, succeeded=True, result=performed
    )
    return {"status": "executed", "case": (updated or case).public()}


async def _deliver(case: cases.ApprovalCase, *, confirmed: bool) -> dict[str, Any]:
    """Send a decision to the agent whose tool is suspended.

    Args:
        case: The decided case, carrying the owner's task and confirmation ids.
        confirmed: Whether the human approved.

    Returns:
        The owner's task as it stood when the call returned, or ``{}`` when
        there was nothing to deliver to.

    Raises:
        grants.GrantDeliveryError: If the owner could not be reached.
    """
    if not case.owner_task_id or not case.confirmation_id:
        return {}
    config = app.state.cluster_config
    peer = next((p for p in config.peers if p.name == case.agent), None)
    if peer is None:
        raise grants.GrantDeliveryError(
            f"Case names agent {case.agent!r}, which is not a configured peer"
        )
    return await grants.deliver(
        peer,
        rpc_path=config.rpc_path,
        task_id=case.owner_task_id,
        context_id=case.owner_context_id,
        confirmation_id=case.confirmation_id,
        confirmed=confirmed,
        approved_by=case.decided_by,
        note=case.note,
    )


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
