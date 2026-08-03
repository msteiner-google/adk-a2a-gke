"""ADK session schema v1: sessions, events, app/user state.

Creates, inside the target agent's schema, the tables that ADK's
``DatabaseSessionService`` expects. This mirrors
``google.adk.sessions.schemas.v1`` exactly -- that module is the source of
truth, and ``tests/unit/test_migrations.py`` fails if the two drift.

Two details are load-bearing:

* **JSONB, not JSON.** ADK's ``DynamicJSON`` type resolves to ``JSONB`` on
  PostgreSQL (``schemas/shared.py``). Creating these columns as plain ``JSON``
  would still store data but silently lose index/containment support and
  mismatch what the ORM binds.
* **The ``schema_version`` row is mandatory.** On startup ADK calls
  ``get_db_schema_version_from_connection``; if ``adk_internal_metadata``
  exists but holds no ``schema_version`` row it raises "Schema version not
  found ... The database might be malformed." Seeding it here is what lets ADK
  skip its own table creation and treat the schema as v1.

With these tables present, ADK's ``prepare_tables()`` reduces to a reflection
pass: ``create_all`` and index creation both run with ``checkfirst=True`` and
issue no DDL, so the agent roles need no CREATE privileges at runtime.

Revision ID: 0001
Revises:
Create Date: 2026-08-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ADK's DEFAULT_MAX_KEY_LENGTH / DEFAULT_MAX_VARCHAR_LENGTH
# (google.adk.sessions.schemas.shared).
_KEY_LEN = 128
_VARCHAR_LEN = 256

# The value ADK's _schema_check_utils.LATEST_SCHEMA_VERSION expects to read.
_SCHEMA_VERSION = "1"


def upgrade() -> None:
    """Create the ADK v1 session tables and record the schema version."""
    # Tables are created unqualified so they land in the connection's
    # search_path, which env.py pins to this run's agent schema.
    op.create_table(
        "adk_internal_metadata",
        sa.Column("key", sa.String(_KEY_LEN), nullable=False),
        sa.Column("value", sa.String(_VARCHAR_LEN), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "sessions",
        sa.Column("app_name", sa.String(_KEY_LEN), nullable=False),
        sa.Column("user_id", sa.String(_KEY_LEN), nullable=False),
        sa.Column("id", sa.String(_KEY_LEN), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("app_name", "user_id", "id"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.String(_KEY_LEN), nullable=False),
        sa.Column("app_name", sa.String(_KEY_LEN), nullable=False),
        sa.Column("user_id", sa.String(_KEY_LEN), nullable=False),
        sa.Column("session_id", sa.String(_KEY_LEN), nullable=False),
        sa.Column("invocation_id", sa.String(_VARCHAR_LEN), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("event_data", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id", "app_name", "user_id", "session_id"),
        # Deleting a session cascades to its events; ADK relies on this rather
        # than issuing a second DELETE.
        sa.ForeignKeyConstraint(
            ["app_name", "user_id", "session_id"],
            ["sessions.app_name", "sessions.user_id", "sessions.id"],
            ondelete="CASCADE",
        ),
    )

    # Declared by ADK itself. Serves the hot path: fetching a session's most
    # recent events (GetSessionConfig.num_recent_events) and the stale-writer
    # check in append_event, both of which order by timestamp descending.
    op.create_index(
        "idx_events_app_user_session_ts",
        "events",
        ["app_name", "user_id", "session_id", sa.text("timestamp DESC")],
    )

    # NOT declared by ADK. Added for retention: expiring old conversations is a
    # range scan over update_time, which would otherwise be a full table scan
    # that grows without bound. Cheap to maintain, and the only index that makes
    # a TTL sweep viable.
    op.create_index("idx_sessions_update_time", "sessions", ["update_time"])

    op.create_table(
        "app_states",
        sa.Column("app_name", sa.String(_KEY_LEN), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("app_name"),
    )

    op.create_table(
        "user_states",
        sa.Column("app_name", sa.String(_KEY_LEN), nullable=False),
        sa.Column("user_id", sa.String(_KEY_LEN), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("app_name", "user_id"),
    )
    # No extra index for list_sessions(app_name, user_id): the sessions primary
    # key is (app_name, user_id, id), whose leading columns already serve that
    # lookup. Same for user_states.

    # Tell ADK this schema is v1 so it does not fall back to the legacy
    # pickle-based v0 layout or attempt a migration of its own.
    #
    # The version is inlined rather than passed as a bind parameter: Alembic's
    # offline mode (`upgrade --sql`) renders statements without binding
    # parameters, so a `:version` placeholder would appear verbatim in the
    # generated script and fail for anyone applying it by hand. _SCHEMA_VERSION
    # is a module constant, never user input.
    op.execute(
        "INSERT INTO adk_internal_metadata (key, value) "
        f"VALUES ('schema_version', '{_SCHEMA_VERSION}')"
    )


def downgrade() -> None:
    """Drop the ADK v1 session tables."""
    op.drop_table("user_states")
    op.drop_table("app_states")
    op.drop_index("idx_sessions_update_time", table_name="sessions")
    op.drop_index("idx_events_app_user_session_ts", table_name="events")
    # events before sessions: the composite foreign key points this way.
    op.drop_table("events")
    op.drop_table("sessions")
    op.drop_table("adk_internal_metadata")
