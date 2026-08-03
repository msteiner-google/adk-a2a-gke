"""Tests for the ModelModule injector wiring.

Model-resolution tests pin model ids (via constructor/env) so they never make
network calls; one guarded live smoke test hits the real Vertex AI catalog and
is skipped when credentials are unavailable.
"""

import pytest
from google.genai import Client
from injector import Injector, inject

from ..config import DEFAULT_LOCATION, ModelModule, Models
from ..project_types import (
    EmbeddingModel,
    GoogleCloudLocation,
    GoogleCloudProject,
)
from .support import TEST_PROJECT

# Pinned model ids so provider tests don't hit the catalog.
_FAST_PIN = "gemini-2.5-flash-lite"
_BALANCED_PIN = "gemini-2.5-flash"
_CAPABLE_PIN = "gemini-2.5-pro"
_EMBEDDING_PIN = "gemini-embedding-001"


class _ModelConsumer:
    """Receives the three generative models via the `Models` bundle.

    Resolving a real class (rather than calling `injector.get(FastModel)` with a
    `NewType`) both type-checks cleanly and mirrors how application code depends
    on these models.
    """

    @inject
    def __init__(self, models: Models) -> None:
        self.models = models


class _EnvConsumer:
    """Receives the injected project/location/client values."""

    @inject
    def __init__(
        self,
        project: GoogleCloudProject,
        location: GoogleCloudLocation,
        client: Client,
    ) -> None:
        self.project = project
        self.location = location
        self.client = client


class _EmbeddingConsumer:
    """Receives the injected embedding model name."""

    @inject
    def __init__(self, embedding: EmbeddingModel) -> None:
        self.embedding = embedding


def _module(**kwargs) -> ModelModule:
    """Build a ModelModule with a fixed project unless a test overrides it."""
    kwargs.setdefault("project", TEST_PROJECT)
    return ModelModule(**kwargs)


def _pinned_module(**kwargs) -> ModelModule:
    """Build a ModelModule with all generative model ids pinned (no network)."""
    kwargs.setdefault("fast_model", _FAST_PIN)
    kwargs.setdefault("balanced_model", _BALANCED_PIN)
    kwargs.setdefault("capable_model", _CAPABLE_PIN)
    return _module(**kwargs)


# --- generative models ------------------------------------------------------


def test_provides_all_three_models():
    models = Injector([_pinned_module()]).get(_ModelConsumer).models
    assert models.fast.model == _FAST_PIN
    assert models.balanced.model == _BALANCED_PIN
    assert models.capable.model == _CAPABLE_PIN


def test_models_are_distinct():
    models = Injector([_pinned_module()]).get(_ModelConsumer).models
    assert len({id(models.fast), id(models.balanced), id(models.capable)}) == 3


def test_provides_singletons():
    injector = Injector([_pinned_module()])
    first = injector.get(_ModelConsumer).models
    second = injector.get(_ModelConsumer).models
    assert first.capable is second.capable
    assert first.fast is second.fast


def test_applies_shared_retry_policy():
    models = Injector([_pinned_module()]).get(_ModelConsumer).models
    assert models.capable.retry_options is not None
    assert models.capable.retry_options.attempts == 3


def test_patches_shared_client_onto_models():
    injector = Injector([_pinned_module()])
    models = injector.get(_ModelConsumer).models
    client = injector.get(_EnvConsumer).client
    assert models.fast.api_client is client
    assert models.balanced.api_client is client
    assert models.capable.api_client is client


def test_reads_model_env_vars(monkeypatch):
    monkeypatch.setenv("GEMINI_FAST_MODEL", "env-fast")
    monkeypatch.setenv("GEMINI_BALANCED_MODEL", "env-balanced")
    monkeypatch.setenv("GEMINI_CAPABLE_MODEL", "env-capable")
    # Only the project is pinned; models resolve from env (no catalog call).
    models = Injector([_module()]).get(_ModelConsumer).models
    assert models.fast.model == "env-fast"
    assert models.balanced.model == "env-balanced"
    assert models.capable.model == "env-capable"


def test_constructor_override_beats_env(monkeypatch):
    monkeypatch.setenv("GEMINI_FAST_MODEL", "env-fast")
    models = Injector([_pinned_module()]).get(_ModelConsumer).models
    assert models.fast.model == _FAST_PIN


# --- embedding model --------------------------------------------------------


def test_provides_embedding_from_constructor():
    consumer = Injector([_module(embedding_model=_EMBEDDING_PIN)]).get(
        _EmbeddingConsumer
    )
    assert consumer.embedding == _EMBEDDING_PIN


def test_reads_embedding_env_var(monkeypatch):
    monkeypatch.setenv("GEMINI_EMBEDDING_MODEL", "env-embedding")
    consumer = Injector([_module()]).get(_EmbeddingConsumer)
    assert consumer.embedding == "env-embedding"


def test_embedding_constructor_beats_env(monkeypatch):
    monkeypatch.setenv("GEMINI_EMBEDDING_MODEL", "env-embedding")
    consumer = Injector([_module(embedding_model=_EMBEDDING_PIN)]).get(
        _EmbeddingConsumer
    )
    assert consumer.embedding == _EMBEDDING_PIN


# --- project / location -----------------------------------------------------


def test_uses_injected_project_and_location():
    consumer = Injector(
        [_module(project="explicit-proj", location="europe-west4")]
    ).get(_EnvConsumer)
    assert consumer.project == "explicit-proj"
    assert consumer.location == "europe-west4"
    assert consumer.client.vertexai is True


def test_location_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    consumer = Injector([_module()]).get(_EnvConsumer)
    assert consumer.location == DEFAULT_LOCATION


def test_reads_project_from_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-project")
    consumer = Injector([ModelModule()]).get(_EnvConsumer)
    assert consumer.project == "env-project"


def test_falls_back_to_adc_when_no_project(monkeypatch):
    # Unset project -> empty string injected, and the client still builds
    # (project resolution is deferred to ADC).
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    consumer = Injector([ModelModule()]).get(_EnvConsumer)
    assert consumer.project == ""
    assert consumer.client.vertexai is True


# --- live smoke test --------------------------------------------------------


def test_live_catalog_resolves_latest_per_family():
    # Guarded live smoke test against the real Vertex AI catalog; skipped when
    # credentials / network are unavailable.
    try:
        injector = Injector([ModelModule()])
        models = injector.get(_ModelConsumer).models
        fast, balanced, capable = (
            models.fast.model,
            models.balanced.model,
            models.capable.model,
        )
        embedding = injector.get(_EmbeddingConsumer).embedding
    except Exception as exc:
        pytest.skip(f"Vertex AI catalog unavailable: {exc}")
    # These hold by construction of the family filters.
    assert "flash-lite" in fast
    assert "flash" in balanced and "lite" not in balanced
    assert "pro" in capable
    assert "embedding" in embedding
