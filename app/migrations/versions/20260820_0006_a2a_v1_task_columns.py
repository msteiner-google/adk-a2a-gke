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

"""A2A v1.0: the three columns a2a-sdk 1.x added to its tasks table.

Revision 0002 created ``tasks`` to match ``a2a.server.models.TaskMixin`` as it
stood in a2a-sdk 0.3.x. The 1.x line adds three columns to that model, and
``DatabaseTaskStore`` names all of them in its INSERTs and SELECTs. Without this
revision the store fails on first use with ``UndefinedColumn`` — at the first
delegation, not at startup, because the table itself still exists and the pod's
health probe never touches it.

The three columns
-----------------
- ``owner`` — who the task belongs to. 1.x scopes task visibility to the
  authenticated caller (``ListTasks``/``GetTask`` MUST only return a caller's own
  tasks), and ``DatabaseTaskStore`` fills this from its ``owner_resolver``,
  which defaults to ``resolve_user_scope``. Nullable, because rows written by
  0.3.x predate the concept.
- ``last_updated`` — the SDK's own mtime, distinct from this repo's
  ``updated_at`` below.
- ``protocol_version`` — which protocol version wrote the row. This is what lets
  a store hold 0.3-shaped and 1.0-shaped tasks side by side during a rollout,
  so it is the column that makes ``enable_v0_3_compat`` on the JSON-RPC route
  actually survive a restart.

All three are nullable in the library's model, so this is a pure additive
``ALTER TABLE`` — it takes no table rewrite on PostgreSQL and is safe to run
against a live ``tasks`` table with rows in it.

``last_updated`` vs this repo's ``updated_at``
----------------------------------------------
They are not duplicates and neither replaces the other. ``updated_at`` (added by
0002, with a trigger) is *server*-maintained: it fires on every UPDATE no matter
who wrote it, which is what makes it trustworthy for retention sweeps.
``last_updated`` is *library*-maintained — the ORM sets it, so it is only as
accurate as the SDK's own write path, and it is NULL for every row 0.3.x wrote.
Retention still keys off ``updated_at``; the trigger from 0002 keeps working
untouched, because it names only its own column.

The index mirrors the library's ``idx_tasks_owner_last_updated`` on
``(owner, last_updated)``, which is what a scoped task list orders by.
``ix_tasks_id`` is still deliberately skipped, for the same reason 0002 skipped
it: the primary key's implicit unique index already serves every lookup.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "tasks"
OWNER_INDEX = "idx_tasks_owner_last_updated"


def upgrade() -> None:
    """Add the a2a-sdk 1.x task columns and the owner/last_updated index."""
    op.add_column(TABLE, sa.Column("owner", sa.String(255), nullable=True))
    # Naive DateTime, matching the library's own column exactly. TaskMixin
    # declares a bare `DateTime` with no timezone, unlike the timezone-aware
    # created_at/updated_at this repo added in 0002. Widening it here would
    # leave the ORM binding a naive value into a timestamptz column and letting
    # the server guess a zone.
    op.add_column(TABLE, sa.Column("last_updated", sa.DateTime(), nullable=True))
    op.add_column(TABLE, sa.Column("protocol_version", sa.String(16), nullable=True))
    op.create_index(OWNER_INDEX, TABLE, ["owner", "last_updated"])


def downgrade() -> None:
    """Drop the a2a-sdk 1.x task columns and their index."""
    op.drop_index(OWNER_INDEX, table_name=TABLE)
    op.drop_column(TABLE, "protocol_version")
    op.drop_column(TABLE, "last_updated")
    op.drop_column(TABLE, "owner")
