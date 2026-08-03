"""Guard the Alembic migrations against drift from the libraries they mirror.

The `sessions` / `events` / state tables belong to ADK's
`DatabaseSessionService`, and `tasks` belongs to the a2a SDK's
`DatabaseTaskStore`. Neither is defined by a model in this repo, so autogenerate
is switched off in `app/migrations/env.py` and the DDL is written by hand. That
buys reviewability, but it means a library upgrade that adds a column would
otherwise be discovered in production, as an SQL error on a live request.

These tests close that gap. They render the migrations to SQL offline (no
database required), compile the DDL those two libraries would generate
themselves, and compare the two column-by-column.
"""

from __future__ import annotations

import argparse
import io
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "app" / "migrations" / "alembic.ini"

TEST_SCHEMA = "testagent"


def _render(schema: str = TEST_SCHEMA, agent_role: str | None = None) -> str:
    """Render every migration to PostgreSQL DDL without touching a database.

    Uses Alembic's offline mode, so this needs no server and stays fast enough
    to run in the hermetic unit suite.
    """
    import os

    keys = ("DB_BACKEND", "DB_URL", "DB_AGENT_ROLE")
    previous = {key: os.environ.get(key) for key in keys}
    # env.py's offline path requires the `url` backend; nothing ever connects.
    os.environ["DB_BACKEND"] = "url"
    os.environ["DB_URL"] = "postgresql://user@localhost/db"
    if agent_role is None:
        os.environ.pop("DB_AGENT_ROLE", None)
    else:
        os.environ["DB_AGENT_ROLE"] = agent_role
    try:
        buffer = io.StringIO()
        config = Config(
            str(ALEMBIC_INI),
            output_buffer=buffer,
            # How `alembic -x schema=...` reaches env.py.
            cmd_opts=argparse.Namespace(x=[f"schema={schema}"]),
        )
        command.upgrade(config, "head", sql=True)
        return buffer.getvalue()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def migration_sql() -> str:
    """The full migration DDL, with the grant revision left inert."""
    return _render()


def _created_tables(sql: str) -> dict[str, list[str]]:
    """Map each CREATE TABLE in `sql` to its normalized body lines."""
    tables: dict[str, list[str]] = {}
    pattern = re.compile(
        r"CREATE TABLE (?P<name>[\w.]+) \((?P<body>.*?)\n\);", re.DOTALL
    )
    for match in pattern.finditer(sql):
        name = match.group("name").split(".")[-1]
        lines = [
            line.strip().rstrip(",")
            for line in match.group("body").strip().splitlines()
            if line.strip()
        ]
        tables[name] = lines
    return tables


def _column_definitions(lines: list[str]) -> set[str]:
    """Keep only column definitions, dropping table-level constraints."""
    constraint_prefixes = ("PRIMARY KEY", "FOREIGN KEY", "CONSTRAINT", "UNIQUE")
    return {
        " ".join(line.split())
        for line in lines
        if not line.upper().startswith(constraint_prefixes)
    }


def _library_columns(table) -> set[str]:
    """Compile a library's own CREATE TABLE and return its column definitions."""
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    body = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    lines = [line.strip().rstrip(",") for line in body.splitlines() if line.strip()]
    return _column_definitions(lines)


# --- ADK session schema ------------------------------------------------------


def test_migration_creates_every_adk_v1_table(migration_sql: str) -> None:
    from google.adk.sessions.schemas.v1 import Base as BaseV1

    created = set(_created_tables(migration_sql))
    expected = set(BaseV1.metadata.tables)

    missing = expected - created
    assert not missing, (
        f"ADK's v1 schema declares tables the migrations do not create: {missing}. "
        "ADK likely added a table; add it in a new Alembic revision."
    )


@pytest.mark.parametrize(
    "table_name",
    ["sessions", "events", "app_states", "user_states", "adk_internal_metadata"],
)
def test_adk_table_columns_match_library(migration_sql: str, table_name: str) -> None:
    """Every ADK table must match ADK's own DDL exactly, column for column."""
    from google.adk.sessions.schemas.v1 import Base as BaseV1

    ours = _column_definitions(_created_tables(migration_sql)[table_name])
    theirs = _library_columns(BaseV1.metadata.tables[table_name])

    assert ours == theirs, (
        f"Migration DDL for {table_name!r} has drifted from "
        f"google.adk.sessions.schemas.v1.\n"
        f"  only in migration: {sorted(ours - theirs)}\n"
        f"  only in ADK:       {sorted(theirs - ours)}"
    )


def test_adk_schema_version_row_is_seeded(migration_sql: str) -> None:
    """ADK raises on startup if adk_internal_metadata has no schema_version."""
    from google.adk.sessions.migration import _schema_check_utils

    assert "INSERT INTO adk_internal_metadata" in migration_sql
    assert _schema_check_utils.SCHEMA_VERSION_KEY in migration_sql
    # The seeded value must be the version ADK considers current, or it falls
    # back to the legacy pickle-based v0 layout.
    assert f"'{_schema_check_utils.LATEST_SCHEMA_VERSION}'" in migration_sql


def test_adk_declared_index_is_created(migration_sql: str) -> None:
    """ADK declares this index itself; it serves the recent-events hot path."""
    assert "idx_events_app_user_session_ts" in migration_sql


# --- a2a task store ----------------------------------------------------------


def test_tasks_table_is_a_superset_of_a2a_columns(migration_sql: str) -> None:
    """`tasks` must carry every a2a column; extras (timestamps) are ours."""
    from a2a.server.models import TaskModel

    ours = _column_definitions(_created_tables(migration_sql)["tasks"])
    theirs = _library_columns(TaskModel.__table__)

    missing = theirs - ours
    assert not missing, (
        "Migration DDL for 'tasks' is missing columns a2a's TaskModel declares: "
        f"{sorted(missing)}. The SDK likely added a column; add it in a new "
        "Alembic revision."
    )


def test_tasks_table_adds_retention_timestamps(migration_sql: str) -> None:
    """a2a's model has no timestamps, so tasks could never be expired."""
    from a2a.server.models import TaskModel

    ours = _column_definitions(_created_tables(migration_sql)["tasks"])
    extra = {line.split()[0] for line in ours - _library_columns(TaskModel.__table__)}

    assert extra == {"created_at", "updated_at"}
    # updated_at only advances via the trigger: DatabaseTaskStore writes through
    # session.merge(), which touches only its own mapped columns, and PostgreSQL
    # has no ON UPDATE clause.
    assert "CREATE TRIGGER tasks_set_updated_at" in migration_sql


def test_task_retention_indexes_exist(migration_sql: str) -> None:
    assert "idx_tasks_context_id" in migration_sql
    assert "idx_tasks_updated_at" in migration_sql


# --- Alembic bookkeeping -----------------------------------------------------


def test_version_table_lives_in_the_agent_schema(migration_sql: str) -> None:
    """Each agent is versioned independently inside its own schema."""
    assert f"CREATE TABLE {TEST_SCHEMA}.alembic_version" in migration_sql


# --- Per-agent grants --------------------------------------------------------


def test_grants_are_skipped_without_an_agent_role(migration_sql: str) -> None:
    """No DB_AGENT_ROLE means no grant, so local Postgres runs still work."""
    assert "GRANT" not in migration_sql


def test_grant_scopes_the_agent_to_its_own_schema_only() -> None:
    """The agent gets USAGE + DML on its schema, and deliberately no CREATE."""
    sql = _render(agent_role="agent-research@my-project.iam")

    assert (
        f'GRANT USAGE ON SCHEMA {TEST_SCHEMA} TO "agent-research@my-project.iam"' in sql
    )
    assert f"ON ALL TABLES IN SCHEMA {TEST_SCHEMA}" in sql
    # Future revisions' tables are covered without re-granting.
    assert f"ALTER DEFAULT PRIVILEGES IN SCHEMA {TEST_SCHEMA}" in sql

    # CREATE is withheld: a compromised agent must not be able to add or drop
    # tables in its own schema, let alone anyone else's.
    grant_lines = [line for line in sql.splitlines() if line.startswith("GRANT")]
    assert grant_lines, "expected at least one GRANT"
    assert not any("CREATE" in line for line in grant_lines)


def test_grant_rejects_an_over_long_role() -> None:
    """PostgreSQL truncates at 63 bytes, which would grant to the wrong role."""
    with pytest.raises(ValueError, match="63-byte identifier limit"):
        _render(agent_role="a" * 64)


def test_grant_rejects_an_injection_shaped_role() -> None:
    """The role name cannot be a bind parameter, so it is validated instead."""
    with pytest.raises(ValueError, match="Refusing to grant"):
        _render(agent_role='evil"; DROP SCHEMA public; --')
