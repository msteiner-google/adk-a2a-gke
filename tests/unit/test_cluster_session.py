"""Unit tests for the pluggable session/memory backend selection."""

import pytest
from google.adk.memory import InMemoryMemoryService
from google.adk.sessions import InMemorySessionService

from app.cluster.session import build_memory_service, build_session_service


def test_session_defaults_to_in_memory(monkeypatch):
    monkeypatch.delenv("SESSION_BACKEND", raising=False)
    assert isinstance(build_session_service(), InMemorySessionService)


def test_session_in_memory_explicit(monkeypatch):
    monkeypatch.setenv("SESSION_BACKEND", "in_memory")
    assert isinstance(build_session_service(), InMemorySessionService)


def test_session_database_requires_url(monkeypatch):
    monkeypatch.setenv("SESSION_BACKEND", "database")
    monkeypatch.delenv("SESSION_DB_URL", raising=False)
    with pytest.raises(ValueError, match="SESSION_DB_URL"):
        build_session_service()


def test_session_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("SESSION_BACKEND", "bogus")
    with pytest.raises(ValueError, match="Unknown"):
        build_session_service()


def test_memory_defaults_to_in_memory(monkeypatch):
    monkeypatch.delenv("MEMORY_BACKEND", raising=False)
    assert isinstance(build_memory_service(), InMemoryMemoryService)


def test_memory_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("MEMORY_BACKEND", "bogus")
    with pytest.raises(ValueError, match="Unknown"):
        build_memory_service()
