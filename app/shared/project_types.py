"""Distinct model types for dependency injection.

Both a "capable" and a "fast" model are `Gemini` instances at runtime, so the
injector cannot tell them apart from the type alone. Wrapping each in a
`NewType` gives the injector two distinct binding keys, letting a `Module`
provide both and letting consumers ask for exactly the one they want:

    from injector import inject

    class Summarizer:
        @inject
        def __init__(self, model: FastModel) -> None:
            self._model = model

`NewType` is a zero-cost, type-checker-only construct: `CapableModel(gemini)`
returns the `gemini` object unchanged at runtime, but static analysis (and the
injector) treat `CapableModel` and `FastModel` as separate types.
"""

from typing import NewType

from google.adk.models import Gemini

# The high-capability model (Gemini `pro` family) — use for hard reasoning,
# planning, or final answers.
CapableModel = NewType("CapableModel", Gemini)

# The balanced model (Gemini `flash` family) — the general-purpose default that
# trades a little quality for lower latency/cost.
BalancedModel = NewType("BalancedModel", Gemini)

# The fastest / cheapest model (Gemini `flash-lite` family) — use for cheap,
# high-volume, or simple calls.
FastModel = NewType("FastModel", Gemini)

# The text-embedding model *name* (e.g. `gemini-embedding-001`). Unlike the
# generative tiers this is a plain id, not an ADK `Gemini` object, because
# embeddings are called by name via `client.models.embed_content(model=...)`.
EmbeddingModel = NewType("EmbeddingModel", str)

# The GCP project the Vertex AI genai client bills / runs against. Injectable so
# consumers (and tests) can override it without reaching for env vars directly.
GoogleCloudProject = NewType("GoogleCloudProject", str)

# The GCP location (region) the Vertex AI genai client is pinned to — e.g. for
# data-residency / geofencing (`europe-west4`, `us-central1`, ...).
GoogleCloudLocation = NewType("GoogleCloudLocation", str)
