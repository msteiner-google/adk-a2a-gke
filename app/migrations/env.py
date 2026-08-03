"""Alembic environment for the multi-agent system's per-agent schemas.

This environment differs from the stock async template in three ways.

**It reuses the application's connection code.** Rather than reading
``sqlalchemy.url`` from ``alembic.ini``, it builds the engine with
:class:`app.cluster.db.Database`, the same class the agents use. Migrations
therefore reach AlloyDB through the AlloyDB connector with IAM authentication --
there is no password and no second copy of the connection settings to drift.

**It targets one schema per invocation.** Every agent owns a PostgreSQL schema
(see the module docstring in ``app/cluster/db.py`` for why). The schema is
chosen, in order of precedence, by ``-x schema=<name>``, then ``DB_SCHEMA``,
then ``AGENT_NAME``. The schema is created if absent, and Alembic's own
``alembic_version`` bookkeeping table is placed **inside** that schema -- so
each agent is versioned independently and one agent's migration cannot silently
mark another's as applied.

**There is no ``target_metadata``.** Autogenerate is deliberately disabled. The
tables here are owned by two third-party libraries (ADK's session schema and
the a2a SDK's task schema), not by models in this repo. Pointing autogenerate at
their metadata would let a library upgrade rewrite production DDL without
review; the migrations are written by hand instead, and
``tests/unit/test_migrations.py`` asserts they still match what those libraries
expect.

Run it with::

    uv run alembic -c app/migrations/alembic.ini -x schema=research upgrade head
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
from typing import TYPE_CHECKING

from alembic import context

from app.cluster.db import URL, Database, DatabaseConfig

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config

# Autogenerate is off by design (see the module docstring).
target_metadata = None

# PostgreSQL identifiers we are willing to interpolate into DDL. The schema name
# reaches us from an -x argument or the environment, and CREATE SCHEMA cannot be
# parameterized, so it is validated rather than trusted.
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _resolve_schema() -> str:
    """Determine which PostgreSQL schema this run should migrate.

    Returns:
        The validated schema name.

    Raises:
        ValueError: If no schema could be resolved, or the resolved name is not
            a safe lowercase PostgreSQL identifier.
    """
    schema = (
        context.get_x_argument(as_dictionary=True).get("schema")
        or os.environ.get("DB_SCHEMA", "")
        or os.environ.get("AGENT_NAME", "")
    ).strip()

    if not schema:
        raise ValueError(
            "No target schema. Pass -x schema=<name>, or set DB_SCHEMA or "
            "AGENT_NAME. Each agent owns its own schema; see app/cluster/db.py."
        )
    if not _SCHEMA_RE.match(schema):
        raise ValueError(
            f"Refusing to use schema {schema!r}: expected a lowercase "
            "PostgreSQL identifier matching [a-z_][a-z0-9_]*."
        )
    return schema


def _resolve_config(schema: str) -> DatabaseConfig:
    """Build the database configuration for this migration run.

    Args:
        schema: The schema this run targets.

    Returns:
        A :class:`DatabaseConfig` pinned to ``schema``.
    """
    # default_schema is only a fallback inside from_env; the replace() below is
    # what actually pins the connection's search_path to the resolved schema.
    return dataclasses.replace(
        DatabaseConfig.from_env(default_schema=schema), db_schema=schema
    )


def _configure(connection: Connection | None, schema: str, url: str = "") -> None:
    """Apply the shared migration-context settings.

    Args:
        connection: The live connection (online mode), or ``None`` offline.
        schema: The schema being migrated.
        url: The database URL (offline mode only).
    """
    context.configure(
        connection=connection,
        url=url or None,
        target_metadata=target_metadata,
        # Keep each agent's Alembic history inside that agent's own schema.
        version_table_schema=schema,
        include_schemas=True,
        # Render server defaults so a column's default is visible in the diff
        # rather than silently applied.
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )


def run_migrations_offline() -> None:
    """Emit migration SQL to stdout without connecting to a database.

    Useful for review, or for handing DDL to a DBA. Only the ``url`` backend is
    supported: the AlloyDB connector needs a live connection to authenticate, so
    there is no meaningful URL to render for it.

    Raises:
        ValueError: If the configured backend cannot produce a static URL.
    """
    schema = _resolve_schema()
    db_config = _resolve_config(schema)

    if db_config.backend != URL:
        raise ValueError(
            "Offline mode requires DB_BACKEND=url with DB_URL set; the AlloyDB "
            "connector authenticates per connection and has no static URL."
        )

    _configure(None, schema, url=db_config.url)
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection, schema: str) -> None:
    """Create the target schema if needed, then run the migrations.

    The schema must exist before Alembic writes its ``alembic_version`` table
    into it, so this cannot be deferred to the first revision.

    Args:
        connection: A live synchronous connection (via ``run_sync``).
        schema: The schema being migrated.
    """
    from sqlalchemy import schema as sa_schema

    # Validated against _SCHEMA_RE above; CreateSchema quotes it regardless.
    connection.execute(sa_schema.CreateSchema(schema, if_not_exists=True))

    _configure(connection, schema)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect with the application's engine and run the migrations."""
    schema = _resolve_schema()
    db_config = _resolve_config(schema)
    database = Database(db_config)

    try:
        async with database.engine().begin() as connection:
            await connection.run_sync(_run_migrations, schema)
    finally:
        # Releases the pool and stops the connector's background refresh tasks,
        # so a migration Job's pod can actually exit.
        await database.aclose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
