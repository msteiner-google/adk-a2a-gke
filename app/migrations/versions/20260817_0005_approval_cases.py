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

"""Approval cases replace the HITL approvals table.

Human sign-off stopped being an infrastructure concern and became business
state: a specialist proposes and finishes, the caller records a case, and the
approved action is an ordinary later call. See
``docs/design-decisions.md``.

What that removes from the schema
---------------------------------
Every column ``hitl_approvals`` carried to make a *suspended invocation*
resumable is gone, because nothing is suspended any more:

- ``invocation_id`` / ``function_call_id`` / ``call_name`` / ``kind`` addressed
  a specific paused ADK function call. There is no paused call to answer.
- ``deciding_since`` / ``deciding_by`` were the reclaimable lease (0004's D4.2).
  A lease exists to stop two pods re-driving one frozen coroutine; with no
  coroutine to drive, the single conditional UPDATE on ``status`` is the whole
  concurrency story.
- ``resumed_at`` recorded whether a replay reached the user. Its absence was the
  decisive failure: one that took effect while its answer never arrived.

What it adds
------------
- ``proposal`` / ``summary`` — the action in full, and one line describing it, so
  a reviewer sees what they are signing, an auditor can reconstruct it later, and
  the caller can check that what ran is what was approved. The old table had no
  equivalent: 0004 recorded consent to a *call*, not to its contents.
- ``executed_at`` / ``result`` — that the approved action was actually carried
  out, and what came back.

Dropping 0004's table rather than migrating it
----------------------------------------------
No row in ``hitl_approvals`` can be migrated into this shape: a pending row there
points at a paused invocation that this release can no longer resume, and it
records consent to a function *call* rather than to a described action, so there
is nothing to put in ``proposal``. Deploy this only once outstanding approvals
under the old mechanism are drained or abandoned.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "approval_cases"
OLD_TABLE = "hitl_approvals"

# Kept in sync with app/cluster/cases.py's STATUSES; the CHECK constraint is what
# stops a typo in application code from silently creating a sixth state that no
# query filters on.
_STATUSES = ("pending", "approved", "rejected", "executed", "failed")

# Mirrors 0004, so its table can be recreated on downgrade.
_OLD_STATUSES = ("pending", "deciding", "approved", "rejected", "expired")


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
    """Create the cases table and drop the superseded approvals table."""
    op.create_table(
        TABLE,
        # Short opaque handle; this is what the HTTP API addresses.
        sa.Column("proposal_id", sa.String(32), nullable=False),
        # The wider unit of work. NOT unique: one case may raise several
        # approvals, which is why it is not the primary key.
        sa.Column("case_id", sa.String(128), nullable=False),
        # --- what is being approved ---------------------------------------
        sa.Column("agent", sa.String(128), nullable=False, server_default=""),
        sa.Column("action", sa.String(128), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        # The audit anchor: exactly what the human was shown and signed off.
        sa.Column("proposal", _json_type(), nullable=False),
        # --- lifecycle -------------------------------------------------------
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("decided_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("result", _json_type(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("proposal_id"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _STATUSES) + ")",
            name="ck_approval_cases_status",
        ),
    )

    # The queue view: filter by status, order by age.
    op.create_index(
        "idx_approval_cases_status_created", TABLE, ["status", "created_at"]
    )

    # Everything raised for one unit of work, which is how an operator asks
    # "what is outstanding on this case?".
    op.create_index("idx_approval_cases_case", TABLE, ["case_id", "created_at"])

    # Reconciliation: find an approved case whose action never completed. This
    # is the query that replaces 0004's `resumed_at IS NULL` hunt -- and unlike
    # that one, the answer is actionable, because re-driving is just another
    # call to the decide endpoint.
    op.create_index(
        "idx_approval_cases_unexecuted",
        TABLE,
        ["decided_at"],
        postgresql_where=sa.text("status = 'approved'"),
    )

    op.drop_index("idx_hitl_approvals_deciding", table_name=OLD_TABLE)
    op.drop_index("idx_hitl_approvals_user_pending", table_name=OLD_TABLE)
    op.drop_index("idx_hitl_approvals_status_created", table_name=OLD_TABLE)
    op.drop_table(OLD_TABLE)


def downgrade() -> None:
    """Drop the cases table and recreate 0004's approvals table."""
    op.create_table(
        OLD_TABLE,
        sa.Column("approval_id", sa.String(32), nullable=False),
        sa.Column("app_name", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("invocation_id", sa.String(128), nullable=False),
        sa.Column("function_call_id", sa.String(128), nullable=False),
        sa.Column("call_name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("author", sa.String(128), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("args", _json_type(), nullable=False),
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
        sa.Column("deciding_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deciding_by", sa.String(256), nullable=True),
        sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint(
            "session_id", "function_call_id", name="uq_hitl_approvals_call"
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _OLD_STATUSES) + ")",
            name="ck_hitl_approvals_status",
        ),
    )
    op.create_index(
        "idx_hitl_approvals_status_created", OLD_TABLE, ["status", "created_at"]
    )
    op.create_index(
        "idx_hitl_approvals_user_pending",
        OLD_TABLE,
        ["user_id", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "idx_hitl_approvals_deciding",
        OLD_TABLE,
        ["deciding_since"],
        postgresql_where=sa.text("status = 'deciding'"),
    )

    op.drop_index("idx_approval_cases_unexecuted", table_name=TABLE)
    op.drop_index("idx_approval_cases_case", table_name=TABLE)
    op.drop_index("idx_approval_cases_status_created", table_name=TABLE)
    op.drop_table(TABLE)
