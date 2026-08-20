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

"""Record where a decision has to be delivered, on the approval case.

In-task authorization (A2A spec 7.6) suspends the *specialist's* task, not the
caller's copy of it. A decision is therefore addressed to three things that
nothing in ``approval_cases`` previously held:

- ``owner_task_id`` — the suspended task on the agent that owns the gated tool.
- ``owner_context_id`` — that task's context. A2A requires the pair to agree
  when both are supplied, so storing the task id alone is not enough.
- ``confirmation_id`` — the id of the suspended ``adk_request_confirmation``
  call. ADK matches the answer to the pending tool call by this id; without it
  the resume is rejected as "not provided a function response for the function
  call".

Why the owner and not the caller
--------------------------------
The obvious reading of spec 7.6.2 is that a chain of tasks in
``TASK_STATE_AUTH_REQUIRED`` unwinds from the top. ADK cannot do that: a peer's
confirmation arrives in the caller's session as an ordinary function call, so
the caller tries to resolve it against its own tools, finds nothing (the tool
is one hop away), and drops the grant silently
(``request_confirmation.py``: ``if not tools_to_resume_with_confirmation:
return``). Measured twice on this repo — the specialist received no traffic
at all after a grant. So the request bubbles up and the decision goes straight
down. These columns are what make the second direction addressable.

All three are ``NOT NULL DEFAULT ''`` rather than nullable, so the table keeps
matching ``app.cluster.cases.ApprovalCase`` field-for-field — a property
``tests/unit/test_cases.py::test_table_columns_match_the_dataclass`` asserts.
Existing rows get the empty string, which is correct: a case opened before this
revision was driven by the old re-send flow and has no suspended task to
resume. Such a case is still decidable, and ``/cases/{id}`` reports it as
un-routable rather than pretending it was delivered.

Purely additive ``ALTER TABLE``; no rewrite on PostgreSQL, safe against a live
table.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "approval_cases"

# Column name -> length. All three are opaque identifiers minted elsewhere
# (a2a-sdk task/context uuids, ADK's "adk-<uuid4>" call ids), so the widths are
# generous rather than exact.
COLUMNS: dict[str, int] = {
    "owner_task_id": 128,
    "owner_context_id": 128,
    "confirmation_id": 128,
}


def upgrade() -> None:
    """Add the grant-routing columns to ``approval_cases``."""
    for name, length in COLUMNS.items():
        op.add_column(
            TABLE,
            sa.Column(
                name,
                sa.String(length),
                nullable=False,
                # Inline literal, not a bound parameter: `alembic upgrade --sql`
                # renders offline without binding, so a :param placeholder would
                # end up verbatim in the generated script.
                server_default="",
            ),
        )


def downgrade() -> None:
    """Drop the grant-routing columns."""
    for name in reversed(list(COLUMNS)):
        op.drop_column(TABLE, name)
