"""Grant read-only access on every agent schema to human IAM principals.

Terraform creates the AlloyDB cluster user and the project-level IAM roles (see
``database_readers`` in infra/terraform), but the in-database ``GRANT`` cannot be
expressed as a Terraform resource -- it needs a SQL connection. This script is
that step.

It is deliberately **not** an Alembic revision. Revision 0003 grants the agent
role on its own schema because that is tied to schema creation and runs once per
schema. Who may *read* the data is a people-lifecycle concern: readers come and
go, and adding one should not require bumping a migration.

Run it as the migrator -- the only identity that owns every schema::

    kubectl -n agents run grant-readers --restart=Never \
      --image="$REPO/agent:latest" --overrides="$(cat overrides.json)"

with ``serviceAccountName: agent-migrator``, ``ALLOYDB_IAM_USER`` set to the
migrator role, and ``DB_READERS`` set to a comma-separated list of principals.

Grants applied per schema, per principal:
  - ``USAGE`` on the schema (name resolution only; conveys no table access)
  - ``SELECT`` on all existing tables
  - ``ALTER DEFAULT PRIVILEGES ... SELECT`` so tables created by later
    migrations are covered without re-running this

Read-only by construction: no INSERT/UPDATE/DELETE, and no CREATE, so a reader
cannot mutate agent state or add objects. Idempotent -- re-running is a no-op.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import sys

from sqlalchemy import text

from app.cluster.db import Database, build_database

logger = logging.getLogger(__name__)

READERS_ENV = "DB_READERS"
SCHEMAS_ENV = "DB_READER_SCHEMAS"

# A principal is an email address; it cannot be a bind parameter in GRANT, so it
# is validated before being quoted as an identifier.
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9._%+@-]+$")
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_READ_PRIVILEGES = "SELECT"


def _validate(value: str, pattern: re.Pattern[str], kind: str) -> str:
    """Check an identifier before it is interpolated into DDL.

    Args:
        value: The identifier to check.
        pattern: The pattern it must match.
        kind: Human-readable noun for the error message.

    Returns:
        The validated value.

    Raises:
        ValueError: If the value is malformed or too long for PostgreSQL.
    """
    if not pattern.match(value):
        raise ValueError(f"Refusing to use {kind} {value!r}: unexpected characters.")
    if len(value.encode("utf-8")) > 63:
        raise ValueError(
            f"{kind.capitalize()} {value!r} exceeds PostgreSQL's 63-byte "
            "identifier limit."
        )
    return value


async def grant_readers() -> None:
    """Apply read-only grants for every configured principal and schema.

    Raises:
        ValueError: If no database is configured, or an identifier is invalid.
    """
    readers = [
        _validate(item.strip(), _PRINCIPAL_RE, "principal")
        for item in os.environ.get(READERS_ENV, "").split(",")
        if item.strip()
    ]
    if not readers:
        logger.info("%s is empty; nothing to grant.", READERS_ENV)
        return

    schemas = [
        _validate(item.strip(), _SCHEMA_RE, "schema")
        for item in os.environ.get(SCHEMAS_ENV, "").replace(",", " ").split()
        if item.strip()
    ]
    if not schemas:
        raise ValueError(f"{SCHEMAS_ENV} must list the agent schemas to grant on.")

    # Standalone admin script, like app/cluster/bootstrap.py: its own process
    # and its own short-lived pool, so it constructs a Database directly
    # rather than resolving one through the injector.
    database = build_database()
    if not database.enabled:
        raise ValueError("No database configured; set DB_BACKEND=alloydb.")

    # Clear the search_path pin: this connection works across every schema.
    admin = Database(dataclasses.replace(database.config, db_schema=""))

    try:
        engine = admin.engine()
        preparer = engine.dialect.identifier_preparer
        async with engine.begin() as conn:
            for schema in schemas:
                quoted_schema = preparer.quote(schema)
                for reader in readers:
                    quoted_reader = preparer.quote(reader)
                    await conn.execute(
                        text(
                            f"GRANT USAGE ON SCHEMA {quoted_schema} TO {quoted_reader}"
                        )
                    )
                    await conn.execute(
                        text(
                            f"GRANT {_READ_PRIVILEGES} ON ALL TABLES IN SCHEMA "
                            f"{quoted_schema} TO {quoted_reader}"
                        )
                    )
                    await conn.execute(
                        text(
                            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted_schema} "
                            f"GRANT {_READ_PRIVILEGES} ON TABLES TO {quoted_reader}"
                        )
                    )
                    logger.info("granted read-only on %s to %s", schema, reader)

        # Report the effective privileges rather than assuming the grants stuck.
        async with engine.connect() as conn:
            for reader in readers:
                for schema in schemas:
                    usage = await conn.scalar(
                        text("SELECT has_schema_privilege(:r, :s, 'USAGE')"),
                        {"r": reader, "s": schema},
                    )
                    create = await conn.scalar(
                        text("SELECT has_schema_privilege(:r, :s, 'CREATE')"),
                        {"r": reader, "s": schema},
                    )
                    select = await conn.scalar(
                        text("SELECT has_table_privilege(:r, :t, 'SELECT')"),
                        {"r": reader, "t": f"{schema}.sessions"},
                    )
                    insert = await conn.scalar(
                        text("SELECT has_table_privilege(:r, :t, 'INSERT')"),
                        {"r": reader, "t": f"{schema}.sessions"},
                    )
                    print(
                        f"{reader} on {schema:<13} usage={usage} select={select} "
                        f"insert={insert} create={create}"
                    )
    finally:
        await admin.aclose()


def main() -> int:
    """Entry point.

    Returns:
        A process exit code: 0 on success, 1 on failure.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(grant_readers())
    except Exception:
        logger.exception("Granting reader access failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
