"""Pluggable A2A task store, selected by environment.

An A2A ``Task`` is the unit of work one agent hands another: it carries the
status, the message history, and any artifacts produced. The serving layer keeps
them in a ``TaskStore``.

The default store is **in-memory and per-pod**, which is fine for a single
replica but wrong for anything durable. A task created by the pod that answered
``message/send`` is invisible to the pod that later receives ``tasks/get`` for
it, so scaling an agent past one replica silently breaks task polling and
resubscription, and a restart loses in-flight work. Selecting the ``database``
backend fixes both.

Backends (``TASK_STORE_BACKEND``)
    - ``in_memory`` (default): ``InMemoryTaskStore`` — ephemeral, per-pod.
    - ``database``: ``DatabaseTaskStore`` on the shared engine from
      ``app/cluster/db.py``, writing into this agent's own schema.

As with the session service, the a2a SDK's ``DatabaseTaskStore`` is reused
rather than reimplemented — it already maps the Pydantic ``Task`` to its ORM
model and back, and ``save`` is an upsert via ``session.merge()``. What this
module supplies is the engine and the decision not to let it create tables.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from a2a.server.tasks import InMemoryTaskStore, TaskStore

if TYPE_CHECKING:
    from app.cluster.db import Database

TASK_STORE_BACKEND_ENV = "TASK_STORE_BACKEND"

IN_MEMORY = "in_memory"
DATABASE = "database"


def build_task_store(*, database: Database | None = None) -> TaskStore:
    """Construct the task store selected by ``TASK_STORE_BACKEND``.

    Args:
        database: Shared engine holder, required by the ``database`` backend.

    Returns:
        A concrete ``TaskStore`` implementation.

    Raises:
        ValueError: If the backend name is unknown, or the ``database`` backend
            was selected without a configured database.
    """
    backend = os.environ.get(TASK_STORE_BACKEND_ENV, IN_MEMORY).strip().lower()

    if backend == IN_MEMORY:
        return InMemoryTaskStore()

    if backend == DATABASE:
        if database is None or not database.enabled:
            raise ValueError(
                f"{TASK_STORE_BACKEND_ENV}={DATABASE} requires a configured "
                "database; set DB_BACKEND=alloydb (see app/cluster/db.py)."
            )
        # Imported from the concrete module rather than the `a2a.server.tasks`
        # re-export: that package resolves this name lazily, which `ty` widens
        # into a union it cannot match against TaskStore. Same class of problem
        # as the ToolContext import note in app/agents/common.py.
        from a2a.server.tasks.database_task_store import DatabaseTaskStore

        # create_table=False: the `tasks` table is owned by Alembic (see
        # app/migrations). Leaving the default True would have every pod race
        # to CREATE TABLE at startup, require DDL privileges the agent roles
        # deliberately lack, and quietly bypass the migration history.
        return DatabaseTaskStore(engine=database.engine(), create_table=False)

    raise ValueError(
        f"Unknown {TASK_STORE_BACKEND_ENV}={backend!r}; expected one of "
        f"{IN_MEMORY!r}, {DATABASE!r}."
    )
