"""HITL approvals: the durable pending-approval table (D4 + D4.2).

Unlike revisions 0001 and 0002, this table is **ours** -- no library defines it,
so there is nothing to drift from. What carries over is the operating rule: no
``create_all()`` at runtime, the migration Job owns the DDL, and the agent roles
keep no ``CREATE`` privilege. Revision 0003 already issued
``ALTER DEFAULT PRIVILEGES``, so the agent role picks up DML on this table
automatically and no ``GRANT`` belongs here.

Why the lease columns exist (D4.2)
----------------------------------
Persisting the approval record alone does **not** make a resume restart-safe,
and a table without these columns would be actively worse than the in-memory
store it replaces.

``resume()`` appends the human's ``FunctionResponse`` to the session and then
drives the model in-process. A pod killed between those two steps leaves a row
mid-flight. The endpoint's rollback runs in ``except`` handlers, which a SIGKILL
never reaches, and any row that is not ``pending`` reads as "already decided" --
so a stranded row would be **permanently unanswerable**. In memory that
self-heals because the dict dies with the pod; writing it to a table is exactly
what removes the self-healing.

``deciding_since`` / ``deciding_by`` turn the claim into a reclaimable **lease**:

- ``deciding_by`` carries the owning pod *and* process, so a row claimed by any
  identity other than the live one is provably abandoned -- reclaimable at once
  on startup, with no waiting.
- ``deciding_since`` bounds the residual case (a claim held by this same
  process) with a timeout.

``decision`` is written in the *same* statement that sets ``status='deciding'``,
not after the resume succeeds. That is what lets a sweeper re-drive an
interrupted resume without asking the human again -- with the outcome only
flipping to ``approved``/``rejected`` once the continuation actually completes.

``resumed_at`` is audit and latency data. It is deliberately **not** the signal
that separates a crashed resume from a finished one: ``status`` already does
that (``deciding`` = in flight or abandoned, ``approved``/``rejected`` = done).

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "hitl_approvals"

# Kept in sync with app/cluster/approvals.py's STATUSES; the CHECK constraint is
# what stops a typo in application code from silently creating a fifth state
# that no query filters on.
_STATUSES = ("pending", "deciding", "approved", "rejected", "expired")


def _json_type() -> sa.types.TypeEngine[object]:
    """Return JSONB on PostgreSQL and generic JSON elsewhere.

    The hermetic tests render this migration for PostgreSQL, but the same
    revision has to remain runnable against SQLite for local experiments, and
    SQLite has no JSONB.

    Returns:
        The dialect-appropriate JSON column type.
    """
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    """Create the approvals table, its CHECK constraint, and its indexes."""
    op.create_table(
        TABLE,
        # Short opaque handle; this is what the HTTP API exposes.
        sa.Column("approval_id", sa.String(32), nullable=False),
        # --- what paused, and where -------------------------------------
        sa.Column("app_name", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("invocation_id", sa.String(128), nullable=False),
        sa.Column("function_call_id", sa.String(128), nullable=False),
        # ADK's synthesised call (adk_request_confirmation / adk_request_input).
        # Needed verbatim to rebuild the FunctionResponse on resume, so it is
        # stored rather than re-derived.
        sa.Column("call_name", sa.String(128), nullable=False),
        # confirmation | input | other -- selects which response shape to build.
        sa.Column("kind", sa.String(32), nullable=False),
        # The tool the human is being asked about ("" for a plain question).
        sa.Column("tool_name", sa.String(128), nullable=False, server_default=""),
        # Which agent paused: the local one, or a peer reached over A2A.
        sa.Column("author", sa.String(128), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("args", _json_type(), nullable=False),
        # --- lifecycle ---------------------------------------------------
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("decision", _json_type(), nullable=True),
        sa.Column("decided_by", sa.String(256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        # --- D4.2 lease ----------------------------------------------------
        sa.Column("deciding_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deciding_by", sa.String(256), nullable=True),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("approval_id"),
        # ADK resumption is at-least-once, so the plugin can observe the same
        # pause twice. This is the key that makes capture idempotent.
        sa.UniqueConstraint(
            "session_id", "function_call_id", name="uq_hitl_approvals_call"
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _STATUSES) + ")",
            name="ck_hitl_approvals_status",
        ),
    )

    # The global queue view: filter by status, order by age.
    op.create_index(
        "idx_hitl_approvals_status_created", TABLE, ["status", "created_at"]
    )

    # The per-user list (D4.1: user_id is also the approver). Partial, because
    # pending is a small and shrinking fraction of the table.
    op.create_index(
        "idx_hitl_approvals_user_pending",
        TABLE,
        ["user_id", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # The D4.2 reclaim sweep: find leases that outlived their owner.
    op.create_index(
        "idx_hitl_approvals_deciding",
        TABLE,
        ["deciding_since"],
        postgresql_where=sa.text("status = 'deciding'"),
    )


def downgrade() -> None:
    """Drop the approvals table and its indexes."""
    op.drop_index("idx_hitl_approvals_deciding", table_name=TABLE)
    op.drop_index("idx_hitl_approvals_user_pending", table_name=TABLE)
    op.drop_index("idx_hitl_approvals_status_created", table_name=TABLE)
    op.drop_table(TABLE)
