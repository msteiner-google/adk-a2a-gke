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
import os
from collections.abc import AsyncIterator
from typing import Any

import google.auth
from dotenv import load_dotenv
from fastapi import FastAPI
from google.adk.artifacts.base_artifact_service import BaseArtifactService
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.service_registry import get_service_registry
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService
from google.cloud import logging as google_cloud_logging

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.typing import Feedback
from app.cluster.artifacts import ARTIFACT_STORAGE_URI_ENV
from app.cluster.db import get_database
from app.shared.telemetry import instrument_fastapi_app

load_dotenv()
_, project_id = google.auth.default()
logging_client = google_cloud_logging.Client()
logger = logging_client.logger(__name__)
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
if get_database().enabled:

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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import (
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
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        # Durable when TASK_STORE_BACKEND=database; otherwise per-pod
        # in-memory, exactly as before.
        task_store=task_store,
        rpc_path=f"/a2a/{adk_app.name}",
    )
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


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
