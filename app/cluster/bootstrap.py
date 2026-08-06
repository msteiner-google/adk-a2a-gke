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

"""Create the application database if it does not exist yet.

Run once, ahead of Alembic, by the migration Job::

    uv run python -m app.cluster.bootstrap

This step exists because the Terraform Google provider has **no**
``google_alloydb_database`` resource — it can create clusters, instances, and
users, but not databases. The alternatives were to hand the agents the built-in
``postgres`` maintenance database (workable, but a shortcut that is annoying to
undo later) or to shell out to ``psql`` from Terraform, which cannot reach a
private IP from outside the VPC. Doing it from a Job inside the cluster is the
only option that is both automated and correct.

``CREATE DATABASE`` cannot run inside a transaction block, so the connection is
switched to autocommit. The operation is idempotent: it checks ``pg_database``
first and races are caught as ``DuplicateDatabaseError``.

Connects as the migrator identity, which holds ``alloydbsuperuser`` and can
therefore create databases; the agents' own roles cannot.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import sys

from sqlalchemy import text

from app.cluster.db import DatabaseConfig, build_database

logger = logging.getLogger(__name__)

# The maintenance database to connect to while creating the real one. Always
# present on a new AlloyDB cluster.
MAINTENANCE_DATABASE = "postgres"

# CREATE DATABASE cannot be parameterized, so the name is validated instead.
_DATABASE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


async def ensure_database() -> bool:
    """Create the configured database if it is missing.

    Returns:
        ``True`` if the database was created, ``False`` if it already existed.

    Raises:
        ValueError: If no database backend is configured, or the configured
            database name is not a safe lowercase PostgreSQL identifier.
    """
    # A one-shot process of its own: nothing here shares a pool with the
    # agents, so this constructs its Database directly rather than standing
    # up an injector (which would drag in the model catalog and a network
    # call this step has no use for).
    database = build_database()
    if not database.enabled:
        raise ValueError(
            "No database configured. Set DB_BACKEND=alloydb (or url) before "
            "running the bootstrap step; see app/cluster/db.py."
        )

    target = database.config.name
    if not _DATABASE_RE.match(target):
        raise ValueError(
            f"Refusing to create database {target!r}: expected a lowercase "
            "PostgreSQL identifier matching [a-z_][a-z0-9_]*."
        )

    if target == MAINTENANCE_DATABASE:
        logger.info("Target database is %r; nothing to create.", target)
        return False

    # Connect to the maintenance database instead of the target, and drop the
    # search_path pin: the agent schemas do not exist over here.
    admin_config: DatabaseConfig = dataclasses.replace(
        database.config, name=MAINTENANCE_DATABASE, db_schema=""
    )
    admin = type(database)(admin_config)

    try:
        engine = admin.engine()
        # AUTOCOMMIT: PostgreSQL rejects CREATE DATABASE inside a transaction.
        async with engine.connect() as conn:
            autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")

            exists = await autocommit.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target},
            )
            if exists:
                logger.info("Database %r already exists.", target)
                return False

            preparer = engine.dialect.identifier_preparer
            await autocommit.execute(text(f"CREATE DATABASE {preparer.quote(target)}"))
            logger.info("Created database %r.", target)
            return True
    finally:
        await admin.aclose()


def main() -> int:
    """Entry point for ``python -m app.cluster.bootstrap``.

    Returns:
        A process exit code: 0 on success, 1 on failure.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(ensure_database())
    except Exception:
        logger.exception("Database bootstrap failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
