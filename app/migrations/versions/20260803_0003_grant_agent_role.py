"""Grant the agent's IAM role DML access to its own schema.

This is the step that turns schema-per-agent from a naming convention into an
enforced boundary. The migration job runs as a privileged migrator identity and
owns every schema; each agent gets only ``USAGE`` on its own schema plus
``SELECT/INSERT/UPDATE/DELETE`` on the tables in it. Notably it does **not** get
``CREATE``, so a compromised agent cannot add tables, and it has no grant at all
on any other agent's schema.

The role name is the agent's Google service account email with the
``.gserviceaccount.com`` suffix stripped -- the form AlloyDB uses for IAM
service-account database users (see ``infra/terraform/alloydb.tf``). It is read
from ``DB_AGENT_ROLE``; when that is unset this revision is a no-op, which keeps
local Postgres and SQLite runs working without inventing a role.

Because every agent owns a separate schema, each has its own
``alembic_version`` table and therefore runs this revision itself, with its own
``DB_AGENT_ROLE``. Adding a new agent needs no change here.
``ALTER DEFAULT PRIVILEGES`` covers tables created by any later revision, so
future migrations do not need to re-grant.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-03

"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENT_ROLE_ENV = "DB_AGENT_ROLE"

# An AlloyDB IAM service-account role looks like
# "agents-research@my-project.iam" -- letters, digits, and . _ - @ only.
# Identifiers are quoted before use as well; this is belt-and-braces, because a
# role name cannot be passed as a bind parameter to GRANT.
_ROLE_RE = re.compile(r"^[A-Za-z0-9._@-]+$")

_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"


def _resolve_role() -> str:
    """Read and validate the agent role to grant to.

    Returns:
        The role name, or an empty string when ``DB_AGENT_ROLE`` is unset.

    Raises:
        ValueError: If the role name contains unexpected characters, or exceeds
            PostgreSQL's 63-byte identifier limit.
    """
    role = os.environ.get(AGENT_ROLE_ENV, "").strip()
    if not role:
        return ""
    if not _ROLE_RE.match(role):
        raise ValueError(
            f"Refusing to grant to role {role!r}: expected only letters, "
            "digits, and the characters . _ - @"
        )
    if len(role.encode("utf-8")) > 63:
        # PostgreSQL truncates identifiers at NAMEDATALEN-1 silently, which
        # would grant to a role that is not the one intended.
        raise ValueError(
            f"Role {role!r} exceeds PostgreSQL's 63-byte identifier limit; "
            "shorten the service account name."
        )
    return role


def _schema_name() -> str:
    """Return the schema this migration run is targeting.

    Returns:
        The schema name Alembic was configured with in ``env.py``.
    """
    # env.py pins version_table_schema to the run's schema, so this is the
    # authoritative value rather than re-reading the environment.
    return op.get_context().version_table_schema or "public"


def upgrade() -> None:
    """Grant the agent role USAGE plus DML on its schema."""
    role = _resolve_role()
    if not role:
        return

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (hermetic tests) has neither schemas nor roles.
        return

    preparer = bind.dialect.identifier_preparer
    quoted_role = preparer.quote(role)
    quoted_schema = preparer.quote(_schema_name())

    # USAGE alone lets the role resolve names in the schema; it conveys no
    # access to the tables themselves. CREATE is deliberately withheld.
    op.execute(f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_role}")
    op.execute(
        f"GRANT {_TABLE_PRIVILEGES} ON ALL TABLES IN SCHEMA {quoted_schema} "
        f"TO {quoted_role}"
    )
    # Applies to tables created by later revisions, so this never needs redoing.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} "
        f"GRANT {_TABLE_PRIVILEGES} ON TABLES TO {quoted_role}"
    )


def downgrade() -> None:
    """Revoke the agent role's access to its schema."""
    role = _resolve_role()
    if not role:
        return

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    preparer = bind.dialect.identifier_preparer
    quoted_role = preparer.quote(role)
    quoted_schema = preparer.quote(_schema_name())

    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} "
        f"REVOKE {_TABLE_PRIVILEGES} ON TABLES FROM {quoted_role}"
    )
    op.execute(
        f"REVOKE {_TABLE_PRIVILEGES} ON ALL TABLES IN SCHEMA {quoted_schema} "
        f"FROM {quoted_role}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA {quoted_schema} FROM {quoted_role}")
