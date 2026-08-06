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

"""A2A task store: the tasks table, plus timestamps for retention.

Creates the table the a2a SDK's ``DatabaseTaskStore`` expects, mirroring
``a2a.server.models.TaskMixin``.

**Plain JSON, not JSONB, here.** Unlike the ADK tables in revision 0001, the
a2a models use SQLAlchemy's generic ``JSON`` (``PydanticType.impl = JSON``),
which renders as ``json`` on PostgreSQL. Matching the library exactly matters:
the ORM binds and result-processes according to its own declared type, and a
mismatch on the wire is how you get intermittent serialization failures rather
than a clean startup error. The types differ between the two revisions because
the two libraries differ -- that is intentional, not an oversight.

**Two columns the library does not declare.** ``TaskMixin`` has no timestamps
at all, so a2a tasks accumulate forever with no way to identify old rows. Since
you asked for durable memory, that is a leak rather than a feature.
``created_at`` / ``updated_at`` are added here with server-side defaults and a
trigger, which keeps them completely invisible to the ORM: ``DatabaseTaskStore``
constructs its model from a fixed column list, so its INSERTs let the defaults
fire, its UPDATEs let the trigger fire, and its ``select()`` never names them.
A trigger is required because PostgreSQL has no ``ON UPDATE`` clause and the
library's ``session.merge()`` only ever writes its own columns.

``push_notification_configs`` is deliberately not created. It belongs to
``DatabasePushNotificationConfigStore``, which this deployment does not
instantiate; add it in a new revision if push notifications are enabled.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOUCH_FUNCTION = "tasks_touch_updated_at"


def upgrade() -> None:
    """Create the a2a tasks table, its indexes, and the updated_at trigger."""
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("context_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=True),
        sa.Column("history", sa.JSON(), nullable=True),
        # Named "metadata" in the database; the ORM maps it to the attribute
        # task_metadata to dodge a clash with Pydantic's own `metadata`.
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # a2a's TaskMixin marks the primary key column index=True as well, which
    # would add a second btree over the same column. The primary key's implicit
    # unique index already serves every lookup DatabaseTaskStore performs
    # (get/delete by id), so that duplicate is deliberately skipped. Safe
    # because the store is constructed with create_table=False and therefore
    # never compares the live schema against its metadata.

    # Tasks are grouped by A2A context: context_id is the correlator that ties
    # a task back to the conversation that produced it, so it is the one column
    # any operational or analytical query filters on.
    op.create_index("idx_tasks_context_id", "tasks", ["context_id"])

    # Retention sweeps, as with sessions in revision 0001.
    op.create_index("idx_tasks_updated_at", "tasks", ["updated_at"])

    # Created unqualified, so it lands in this run's agent schema alongside the
    # table it serves.
    op.execute(
        f"""
        CREATE FUNCTION {_TOUCH_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER tasks_set_updated_at
        BEFORE UPDATE ON tasks
        FOR EACH ROW EXECUTE FUNCTION {_TOUCH_FUNCTION}()
        """
    )


def downgrade() -> None:
    """Drop the tasks table and its trigger."""
    op.execute("DROP TRIGGER IF EXISTS tasks_set_updated_at ON tasks")
    op.execute(f"DROP FUNCTION IF EXISTS {_TOUCH_FUNCTION}()")
    op.drop_index("idx_tasks_updated_at", table_name="tasks")
    op.drop_index("idx_tasks_context_id", table_name="tasks")
    op.drop_table("tasks")
