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
