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

"""Pluggable session and memory backends, selected by environment.

Session state and long-term memory are wired through dependency injection (see
``app/cluster/di.py``) so agent code depends only on the ADK base classes
(``BaseSessionService`` / ``BaseMemoryService``) and the concrete backend is a
deployment concern.

The default backend is **in-memory**, which keeps unit tests and local runs
hermetic (no external services, no GCP). A cluster deployment selects a durable
backend via environment variables, so state survives pod restarts and is shared
across replicas:

Session backends (``SESSION_BACKEND``)
    - ``in_memory`` (default): ``InMemorySessionService`` — ephemeral.
    - ``alloydb``: ``DatabaseSessionService`` bound to the shared AlloyDB engine
      from ``app/cluster/db.py``. This is the durable backend for the cluster:
      it authenticates with IAM (no password), writes into this agent's own
      PostgreSQL schema, and shares one connection pool with the A2A task
      store. Schema creation is Alembic's job, not the service's.
    - ``database``: ``DatabaseSessionService`` from ``SESSION_DB_URL`` (e.g. a
      Cloud SQL / Postgres URL). Requires the ``google-adk[db]`` extra. Kept for
      a plain DSN; prefer ``alloydb`` in the cluster so the pool is shared.
    - ``vertex_ai``: ``VertexAiSessionService`` (managed Agent Engine sessions);
      uses the injected project/location and ``AGENT_ENGINE_ID``.

Why ADK's ``DatabaseSessionService`` is reused rather than reimplemented: it
already carries optimistic concurrency on ``update_time``, per-session in-process
locking, ``SELECT ... FOR UPDATE`` row locking on PostgreSQL, and app/user/session
state merging. Re-deriving that correctly would be a large amount of subtle code
for no gain. What this module adds is the *engine* — the connector, the pool,
and the per-agent schema — which is exactly the part ADK leaves to the caller.

Memory backends (``MEMORY_BACKEND``)
    - ``in_memory`` (default): ``InMemoryMemoryService`` — ephemeral.
    - ``vertex_ai``: ``VertexAiMemoryBankService`` (managed Memory Bank); uses
      the injected project/location and ``AGENT_ENGINE_ID``.

Keep this consistent with the serving layer: ADK's ``get_fast_api_app`` also
honors ``SESSION_SERVICE_URI`` for serving-side persistence — set both from the
same source of truth in your manifests.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from google.adk.memory import BaseMemoryService, InMemoryMemoryService
from google.adk.sessions import BaseSessionService, InMemorySessionService

if TYPE_CHECKING:
    from app.cluster.db import Database

SESSION_BACKEND_ENV = "SESSION_BACKEND"
MEMORY_BACKEND_ENV = "MEMORY_BACKEND"
SESSION_DB_URL_ENV = "SESSION_DB_URL"
AGENT_ENGINE_ID_ENV = "AGENT_ENGINE_ID"

IN_MEMORY = "in_memory"
ALLOYDB = "alloydb"
DATABASE = "database"
VERTEX_AI = "vertex_ai"


def build_session_service(
    *,
    project: str = "",
    location: str = "",
    database: Database | None = None,
) -> BaseSessionService:
    """Construct the session service selected by ``SESSION_BACKEND``.

    Args:
        project: GCP project for the ``vertex_ai`` backend (empty -> resolved
            from ADC by the underlying client).
        location: GCP location for the ``vertex_ai`` backend.
        database: Shared engine holder, required by the ``alloydb`` backend.

    Returns:
        A concrete ``BaseSessionService`` implementation.

    Raises:
        ValueError: If the backend name is unknown, or a required setting (e.g.
            ``SESSION_DB_URL`` for ``database``) is missing.
    """
    backend = os.environ.get(SESSION_BACKEND_ENV, IN_MEMORY).strip().lower()

    if backend == IN_MEMORY:
        return InMemorySessionService()

    if backend == ALLOYDB:
        if database is None or not database.enabled:
            raise ValueError(
                f"{SESSION_BACKEND_ENV}={ALLOYDB} requires a configured "
                "database; set DB_BACKEND=alloydb (see app/cluster/db.py)."
            )
        from google.adk.sessions import DatabaseSessionService

        # The tables are owned by Alembic, so nothing here creates them. ADK's
        # prepare_tables() still runs on first use, but with the schema already
        # present it degrades to a reflection pass that issues no DDL -- which
        # is why the agent roles need no CREATE privilege.
        return DatabaseSessionService(db_engine=database.engine())

    if backend == DATABASE:
        db_url = os.environ.get(SESSION_DB_URL_ENV, "").strip()
        if not db_url:
            raise ValueError(
                f"{SESSION_BACKEND_ENV}={DATABASE} requires {SESSION_DB_URL_ENV} "
                "to be set (e.g. a Cloud SQL connection URL)."
            )
        # Imported lazily: pulls in the google-adk[db] extra (SQLAlchemy).
        from google.adk.sessions import DatabaseSessionService

        return DatabaseSessionService(db_url=db_url)

    if backend == VERTEX_AI:
        from google.adk.sessions import VertexAiSessionService

        return VertexAiSessionService(
            project=project or None,
            location=location or None,
            agent_engine_id=os.environ.get(AGENT_ENGINE_ID_ENV) or None,
        )

    raise ValueError(
        f"Unknown {SESSION_BACKEND_ENV}={backend!r}; expected one of "
        f"{IN_MEMORY!r}, {ALLOYDB!r}, {DATABASE!r}, {VERTEX_AI!r}."
    )


def build_memory_service(*, project: str = "", location: str = "") -> BaseMemoryService:
    """Construct the memory service selected by ``MEMORY_BACKEND``.

    Args:
        project: GCP project for the ``vertex_ai`` backend (empty -> resolved
            from ADC by the underlying client).
        location: GCP location for the ``vertex_ai`` backend.

    Returns:
        A concrete ``BaseMemoryService`` implementation.

    Raises:
        ValueError: If the backend name is unknown.
    """
    backend = os.environ.get(MEMORY_BACKEND_ENV, IN_MEMORY).strip().lower()

    if backend == IN_MEMORY:
        return InMemoryMemoryService()

    if backend == VERTEX_AI:
        from google.adk.memory import VertexAiMemoryBankService

        return VertexAiMemoryBankService(
            project=project or None,
            location=location or None,
            agent_engine_id=os.environ.get(AGENT_ENGINE_ID_ENV) or None,
        )

    raise ValueError(
        f"Unknown {MEMORY_BACKEND_ENV}={backend!r}; expected one of "
        f"{IN_MEMORY!r}, {VERTEX_AI!r}."
    )
