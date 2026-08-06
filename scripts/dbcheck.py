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

"""Ad-hoc inspection of the agents' AlloyDB state.

Run inside the cluster as the migrator identity -- it is the only one that can
see every agent's schema; an agent's own role is granted USAGE on its schema
alone.

The image copies only ``./app`` (see the Dockerfile), so this file is not in it.
Inject it into a throwaway pod instead::

    kubectl -n agents run dbcheck --rm -i --restart=Never \
      --image="$REPO/agent:latest" --overrides="$(cat overrides.json)" \
      -- sh -c "echo $(base64 < scripts/dbcheck.py) | base64 -d > /tmp/q.py \
                && uv run python /tmp/q.py"

The overrides must set ``serviceAccountName: agent-migrator``, pull in the
``agent-config`` ConfigMap, and set ``ALLOYDB_IAM_USER`` to the migrator's role.

Prints, per agent schema, what the session service and the A2A task store have
actually persisted. Read-only.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text

from app.cluster.db import Database, DatabaseConfig

SCHEMAS = os.environ.get("CHECK_SCHEMAS", "orchestrator research math").split()


async def main() -> None:
    """Print a summary of every agent schema's session and task tables."""
    # Empty db_schema: queries below qualify their tables explicitly so one
    # connection can see across every agent.
    config = DatabaseConfig.from_env(default_schema="")
    database = Database(config)
    engine = database.engine()

    try:
        async with engine.connect() as conn:
            found = await conn.scalars(
                text(
                    "SELECT nspname FROM pg_namespace "
                    "WHERE nspname NOT LIKE 'pg_%' "
                    "AND nspname NOT IN ('information_schema','public') "
                    "ORDER BY nspname"
                )
            )
            print(f"schemas: {list(found)}\n")

            for schema in SCHEMAS:
                print(f"=== {schema} ===")

                version = await conn.scalar(
                    text(f'SELECT version_num FROM "{schema}".alembic_version')
                )
                adk_version = await conn.scalar(
                    text(
                        f'SELECT value FROM "{schema}".adk_internal_metadata '
                        "WHERE key = 'schema_version'"
                    )
                )
                print(f"  alembic={version}  adk_schema_version={adk_version}")

                for table in ("sessions", "events", "tasks"):
                    count = await conn.scalar(
                        text(f'SELECT count(*) FROM "{schema}".{table}')
                    )
                    print(f"  {table:<9} rows={count}")

                rows = await conn.execute(
                    text(
                        "SELECT id, user_id, state, create_time, update_time "
                        f'FROM "{schema}".sessions ORDER BY update_time DESC LIMIT 3'
                    )
                )
                for row in rows:
                    events = await conn.scalar(
                        text(
                            f'SELECT count(*) FROM "{schema}".events '
                            "WHERE session_id = :sid"
                        ),
                        {"sid": row.id},
                    )
                    print(
                        f"    session {row.id[:18]}… user={row.user_id} "
                        f"events={events} state={row.state}"
                    )

                rows = await conn.execute(
                    text(
                        "SELECT id, context_id, status->>'state' AS state, "
                        "created_at, updated_at, "
                        "jsonb_array_length(history::jsonb) AS msgs "
                        f'FROM "{schema}".tasks ORDER BY updated_at DESC LIMIT 3'
                    )
                )
                for row in rows:
                    touched = row.updated_at > row.created_at
                    print(
                        f"    task {row.id[:18]}… state={row.state} "
                        f"msgs={row.msgs} ctx={row.context_id[:12]}… "
                        f"updated_at>created_at={touched}"
                    )
                print()
    finally:
        await database.aclose()


if __name__ == "__main__":
    asyncio.run(main())
