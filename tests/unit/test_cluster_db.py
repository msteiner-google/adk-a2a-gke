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

"""Tests for the database configuration and engine factory."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.cluster.db import (
    ALLOYDB,
    DEFAULT_DB_NAME,
    NONE,
    URL,
    Database,
    DatabaseConfig,
)


def test_defaults_to_no_database() -> None:
    """Nothing durable unless asked for, so tests and local runs stay hermetic."""
    config = DatabaseConfig.from_env({})

    assert config.backend == NONE
    assert not config.enabled
    assert config.name == DEFAULT_DB_NAME


def test_schema_defaults_to_the_agent_name() -> None:
    """Each agent lands in its own schema with no per-agent configuration."""
    config = DatabaseConfig.from_env({"DB_BACKEND": NONE}, default_schema="research")

    assert config.db_schema == "research"


def test_explicit_schema_overrides_the_agent_name() -> None:
    config = DatabaseConfig.from_env(
        {"DB_BACKEND": NONE, "DB_SCHEMA": "shared"}, default_schema="research"
    )

    assert config.db_schema == "shared"


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown DB_BACKEND"):
        DatabaseConfig.from_env({"DB_BACKEND": "mysql"})


def test_alloydb_backend_requires_instance_and_user() -> None:
    with pytest.raises(ValueError, match="ALLOYDB_INSTANCE_URI and ALLOYDB_IAM_USER"):
        DatabaseConfig.from_env({"DB_BACKEND": ALLOYDB})


def test_alloydb_backend_reports_only_the_missing_setting() -> None:
    with pytest.raises(ValueError, match="requires ALLOYDB_IAM_USER to be set"):
        DatabaseConfig.from_env(
            {"DB_BACKEND": ALLOYDB, "ALLOYDB_INSTANCE_URI": "projects/p/..."}
        )


def test_url_backend_requires_a_url() -> None:
    with pytest.raises(ValueError, match="requires DB_URL"):
        DatabaseConfig.from_env({"DB_BACKEND": URL})


def test_pool_settings_fall_back_on_malformed_values() -> None:
    """A typo in a ConfigMap must not take the pod down at startup."""
    config = DatabaseConfig.from_env(
        {"DB_BACKEND": NONE, "DB_POOL_SIZE": "many", "DB_MAX_OVERFLOW": ""}
    )

    assert config.pool_size == 5
    assert config.max_overflow == 2


def test_engine_is_refused_when_no_backend_is_configured() -> None:
    database = Database(DatabaseConfig.from_env({}))

    assert not database.enabled
    with pytest.raises(ValueError, match="No database configured"):
        database.engine()


def test_url_backend_builds_a_cached_engine() -> None:
    """One engine per process: consumers must share a single pool."""
    database = Database(
        DatabaseConfig.from_env(
            {"DB_BACKEND": URL, "DB_URL": "sqlite+aiosqlite:///:memory:"}
        )
    )

    engine = database.engine()

    assert isinstance(engine, AsyncEngine)
    assert database.engine() is engine


def test_sqlite_skips_the_postgres_search_path() -> None:
    """SQLite has no schemas; asyncpg server_settings would be meaningless."""
    database = Database(
        DatabaseConfig.from_env(
            {"DB_BACKEND": URL, "DB_URL": "sqlite+aiosqlite:///:memory:"},
            default_schema="research",
        )
    )

    assert database._server_settings() == {}


def test_postgres_url_pins_the_search_path_to_the_agent_schema() -> None:
    """search_path is what routes each agent to its own tables."""
    database = Database(
        DatabaseConfig.from_env(
            {"DB_BACKEND": URL, "DB_URL": "postgresql+asyncpg://u@h/d"},
            default_schema="research",
        )
    )

    assert database._server_settings() == {"search_path": "research"}


def test_alloydb_pins_the_search_path() -> None:
    database = Database(
        DatabaseConfig.from_env(
            {
                "DB_BACKEND": ALLOYDB,
                "ALLOYDB_INSTANCE_URI": "projects/p/locations/l/clusters/c/instances/i",
                "ALLOYDB_IAM_USER": "agent-math@p.iam",
            },
            default_schema="math",
        )
    )

    assert database._server_settings() == {"search_path": "math"}


def test_the_injector_shares_one_database() -> None:
    """Session store, approval store and task store share a single pool.

    build_database() is a plain factory -- calling it twice gives two holders,
    and two pools against a one-vCPU instance. Sharing is the injector's job,
    so that is what this pins.
    """
    from injector import Injector

    from app.cluster.di import SessionModule

    injector = Injector([SessionModule()])
    assert injector.get(Database) is injector.get(Database)


@pytest.mark.asyncio
async def test_aclose_is_safe_when_nothing_was_built() -> None:
    await Database(DatabaseConfig.from_env({})).aclose()
