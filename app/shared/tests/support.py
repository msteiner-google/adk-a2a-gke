"""Shared fixtures/data for the shared-library tests.

Kept tiny and dependency-light so every test module can import the sample
catalogs without duplicating them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from opentelemetry.trace import Tracer

# A throwaway project id so tests never depend on the ambient environment.
TEST_PROJECT = "test-project"


def in_memory_tracer() -> tuple[Tracer, InMemorySpanExporter]:
    """Return a tracer backed by a local in-memory span exporter.

    Uses a fresh `TracerProvider` (not the global one) so tests never mutate
    process-wide OpenTelemetry state or hit the network, and so span export is
    order-independent across tests.

    Returns:
        A ``(tracer, exporter)`` pair; read recorded spans with
        ``exporter.get_finished_spans()``.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("shared.tests"), exporter


class FakeSecretClient:
    """In-memory stand-in for `SecretManagerServiceClient` (no network).

    Maps fully-qualified secret-version resource names to payload strings and
    records every requested name, so tests can assert on caching / fetch counts.
    Cast it to `SecretManagerServiceClient` when constructing `Secrets` (the real
    type is what the code expects).
    """

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.requested: list[str] = []

    def access_secret_version(self, *, name: str) -> SimpleNamespace:
        """Return a response whose `.payload.data` holds the encoded secret."""
        self.requested.append(name)
        data = self.values[name].encode("utf-8")
        return SimpleNamespace(payload=SimpleNamespace(data=data))


# Representative generative catalog sample from a live
# `models.list(query_base=True)` — covers stable, preview, versioned, and
# specialized (tts/image/live) variants across families.
SAMPLE_CATALOG = [
    "gemini-1.5-pro-002",
    "gemini-2.0-flash-001",
    "gemini-2.0-flash-lite-001",
    "gemini-2.5-flash-preview-04-17",
    "gemini-2.5-pro-exp-03-25",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro-tts",
    "gemini-2.5-flash-tts",
    "gemini-live-2.5-flash-native-audio",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-image-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.5-flash",
    "gemini-3.1-flash-image",
    "gemini-3-pro-image",
    "gemini-embedding-2",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-image",
]

# Embedding models seen across regions in a live `models.list(query_base=True)`.
EMBEDDING_CATALOG = [
    "embeddinggemma",
    "gemini-embedding-001",
    "gemini-embedding-2",
    "multimodalembedding",
    "text-embedding-005",
    "text-embedding-large-exp-03-07",
    "text-multilingual-embedding-002",
    "textembedding-gecko",
]
