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

import asyncio
import contextlib
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
from app.cluster import approvals, hitl
from app.cluster.artifacts import ARTIFACT_STORAGE_URI_ENV
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


async def _sweep_abandoned_resumes(
    runner: Runner, store: approvals.ApprovalStore
) -> None:
    """Reclaim and finish resumes whose owner stopped renewing their lease.

    Runs for the life of the process, on every replica. A resume in flight keeps
    its own lease fresh, so this only ever picks up work whose owner died.

    Nothing here may escape: a sweep that raises would kill the task and leave
    recovery silently off for the rest of the pod's life, which is worse than a
    failed tick.

    Args:
        runner: The serving Runner, used to drive a recovered resume.
        store: The injector's approval store.
    """
    interval = approvals.lease_ttl_seconds()
    while True:
        try:
            for item in await hitl.redrive_abandoned(runner, store):
                # Separate a genuine replay from a row that was merely closed
                # out. The second means the human's decision took effect but the
                # conversation never got its answer -- someone must know.
                if item.get("replayed"):
                    log.info("HITL: replayed abandoned resume {}", item["approval_id"])
                else:
                    log.warning(
                        "HITL: approval {} finished as {} WITHOUT a replayed answer "
                        "(decision stands, narration lost): {}",
                        item["approval_id"],
                        item.get("status"),
                        item.get("errors") or item.get("error") or "no final response",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("HITL: abandoned-resume sweep failed")
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import (
        approval_store,
        artifact_service,
        database,
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
    # The one store the injector built, shared with the capture plugin that
    # app/agent.py handed the same instance to.
    app.state.approval_store = approval_store
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        # Durable when TASK_STORE_BACKEND=database; otherwise per-pod
        # in-memory, exactly as before.
        task_store=task_store,
        rpc_path=f"/a2a/{adk_app.name}",
    )

    # HITL recovery (D4.2) runs on a timer rather than only at startup: a lease
    # is reclaimed on staleness, not on whose name is against it, so any replica
    # can recover any dead owner's work and a crash no longer waits for a
    # restart to be noticed.
    sweeper = asyncio.create_task(_sweep_abandoned_resumes(runner, approval_store))

    yield

    # Stop the sweeper before the pool goes away, or its next tick queries a
    # disposed engine on the way down.
    sweeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper
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


# --- Human-in-the-loop --------------------------------------------------------
# The approval surface. Capture happens in app/cluster/hitl.py's plugin, which
# runs on the Runner, so a pause is recorded whichever surface drove it -- these
# routes, the ADK web UI, or an inbound A2A call. Resuming goes through the SAME
# Runner (app.state.runner), so it shares the session and artifact services the
# rest of the serving layer uses.


class HitlRun(BaseModel):
    """Start (or continue) a conversation that may pause for a human."""

    text: str
    session_id: str | None = None
    user_id: str = "hitl-user"


class HitlDecision(BaseModel):
    """A human's answer to a pending approval."""

    approved: bool = True
    """Used by confirmation pauses; ignored by free-form input pauses."""
    text: str = ""
    """Free-form reply: the note on a confirmation, the answer to a question."""
    decided_by: str = ""
    """Who decided, for audit. NOT verified -- see docs/human-in-the-loop.md."""


@app.post("/hitl/run")
async def hitl_run(req: HitlRun) -> dict[str, Any]:
    """Run a turn and report whether it completed or paused for a human."""
    runner = app.state.runner
    store = app.state.approval_store
    session_id = req.session_id or f"hitl-{uuid.uuid4().hex[:8]}"

    # Scope the before/after diff to THIS session. The store is shared across
    # replicas, so an unscoped diff would report a pause raised by somebody
    # else's concurrent request as belonging to this one.
    def mine(items: list[approvals.PendingApproval]) -> list[approvals.PendingApproval]:
        return [item for item in items if item.session_id == session_id]

    known = {p.approval_id for p in mine(await store.list_by_status(approvals.PENDING))}
    trace: list[str] = []
    final_text: str | None = None
    async for event in runner.run_async(
        user_id=req.user_id,
        session_id=session_id,
        new_message=genai_types.Content(
            role="user", parts=[genai_types.Part(text=req.text)]
        ),
    ):
        trace.append(hitl.summarise(event))
        if (
            event.is_final_response()
            and event.content
            and event.content.parts
            and (text := "".join(p.text or "" for p in event.content.parts).strip())
        ):
            final_text = text
    new = [
        p.public()
        for p in mine(await store.list_by_status(approvals.PENDING))
        if p.approval_id not in known
    ]
    return {
        "status": "paused" if new else "completed",
        "session_id": session_id,
        "final_text": final_text,
        "pending": new,
        "trace": trace,
    }


@app.get("/hitl/session/{session_id}")
async def hitl_session(session_id: str, user_id: str = "hitl-user") -> dict[str, Any]:
    """Dump a session's events. Diagnostic surface for pauses and resumes."""
    session = await app.state.runner.session_service.get_session(
        app_name=app.state.agent_app_name, user_id=user_id, session_id=session_id
    )
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return {
        "count": len(session.events),
        "events": [
            {
                "author": e.author,
                "summary": hitl.summarise(e),
                # Not a plain model_dump(): a Part's thought_signature is raw
                # bytes and used to 500 the whole route. See hitl.content_json.
                "content": hitl.content_json(e),
                "custom_metadata": e.custom_metadata,
            }
            for e in session.events
        ],
    }


@app.get("/hitl/approvals")
async def hitl_approvals(status: str = approvals.PENDING) -> dict[str, Any]:
    """List captured approvals, oldest first."""
    items = [p.public() for p in await app.state.approval_store.list_by_status(status)]
    return {"count": len(items), "approvals": items}


@app.post("/hitl/approvals/{approval_id}")
async def hitl_decide(approval_id: str, decision: HitlDecision) -> dict[str, Any]:
    """Answer a pending approval and resume the paused invocation."""
    store = app.state.approval_store

    # Claim and record the decision in ONE write, before resuming (D4.2). Two
    # properties come from that ordering. A concurrent retry loses the race
    # rather than driving the same invocation twice; and if this pod dies
    # mid-resume, the row still carries what the human said, so the startup
    # sweep can finish the job without asking again. The claim is reclaimable,
    # which is what stops a crash from making the approval permanently
    # un-retryable -- the R4 failure, see docs/plans/hitl/results.md.
    outcome, pending = await store.claim(
        approval_id,
        decision={"approved": decision.approved, "text": decision.text},
        decided_by=decision.decided_by,
    )
    if outcome is approvals.ClaimOutcome.NOT_FOUND or pending is None:
        raise HTTPException(status_code=404, detail="unknown approval_id")
    if outcome is approvals.ClaimOutcome.ALREADY_DECIDED:
        # Idempotent: report the recorded outcome instead of resuming twice.
        return {"status": "already_decided", "approval": pending.public()}
    if outcome is approvals.ClaimOutcome.IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail="a resume is already running for this approval; retry later",
        )

    try:
        # Hold the lease open for as long as the resume runs. Without this a
        # sweep -- on this pod or any other replica -- would see the lease go
        # stale after the TTL and re-drive an invocation still in flight.
        async with approvals.heartbeat(store, approval_id):
            trace, final_text = await hitl.resume(
                app.state.runner, pending, hitl.content_for(pending)
            )
    except LookupError as exc:
        await store.release(approval_id)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        # ADK rejects a response that does not match the pause's response_schema
        # (R4). Surface it as a client error rather than letting it escape as an
        # unhandled ASGI exception, which drops the connection -- and, through a
        # port-forward, kills the tunnel -- while telling the caller nothing.
        await store.release(approval_id)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        # Any other failure is ours to recover from too: put it back so the
        # human can retry once the cause is fixed.
        await store.release(approval_id)
        raise

    await store.complete(
        approval_id,
        status=approvals.APPROVED if decision.approved else approvals.REJECTED,
    )
    refreshed = await store.get(approval_id)
    return {
        "status": "resumed",
        "approval": (refreshed or pending).public(),
        "final_text": final_text,
        "trace": trace,
    }


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
