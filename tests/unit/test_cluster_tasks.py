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

"""Tests for the pluggable A2A task store."""

from __future__ import annotations

import pytest
from a2a.server.tasks import InMemoryTaskStore

from app.cluster.db import URL, Database, DatabaseConfig
from app.cluster.tasks import build_task_store


def _sqlite_database() -> Database:
    return Database(
        DatabaseConfig.from_env(
            {"DB_BACKEND": URL, "DB_URL": "sqlite+aiosqlite:///:memory:"}
        )
    )


def test_defaults_to_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TASK_STORE_BACKEND", raising=False)

    assert isinstance(build_task_store(), InMemoryTaskStore)


def test_unknown_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK_STORE_BACKEND", "redis")

    with pytest.raises(ValueError, match="Unknown TASK_STORE_BACKEND"):
        build_task_store()


def test_database_backend_requires_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly at startup rather than silently falling back to per-pod state."""
    monkeypatch.setenv("TASK_STORE_BACKEND", "database")

    with pytest.raises(ValueError, match="requires a configured database"):
        build_task_store(database=None)


def test_database_backend_rejects_a_disabled_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TASK_STORE_BACKEND", "database")
    disabled = Database(DatabaseConfig.from_env({}))

    with pytest.raises(ValueError, match="requires a configured database"):
        build_task_store(database=disabled)


def test_database_backend_never_creates_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema ownership belongs to Alembic, not to a racing pod at startup."""
    from a2a.server.tasks.database_task_store import DatabaseTaskStore

    monkeypatch.setenv("TASK_STORE_BACKEND", "database")

    store = build_task_store(database=_sqlite_database())

    assert isinstance(store, DatabaseTaskStore)
    assert store.create_table is False
