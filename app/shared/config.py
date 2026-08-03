"""Dependency-injection wiring for the shared models.

The `ModelModule` here is the single place that decides *which* Gemini models
back the injectable `FastModel` / `BalancedModel` / `CapableModel` /
`EmbeddingModel` types and *how* they reach Vertex AI. Rather than hardcoding
model names, it discovers the **latest** model in each family from the live
Vertex AI catalog (unless pinned via an env var or a constructor override):

- `FastModel`     -> latest in the `flash-lite` family (env: `GEMINI_FAST_MODEL`)
- `BalancedModel` -> latest in the `flash` family      (env: `GEMINI_BALANCED_MODEL`)
- `CapableModel`  -> latest in the `pro` family        (env: `GEMINI_CAPABLE_MODEL`)
- `EmbeddingModel`-> best embedding model name         (env: `GEMINI_EMBEDDING_MODEL`)

It constructs one shared `google.genai.Client` (pinned to an injectable project +
location, e.g. for geofencing) and patches it onto every generative model, so
every consumer that installs the module gets consistent models, retry policy,
and region.

The heavy lifting lives in focused modules: `model_selection` (pure id parsing /
"latest" selection), `model_catalog` (`ModelCatalog` live listing), and
`model_factory` (`build_model`).
"""

import os

from google.genai import Client
from injector import Module, inject, provider, singleton

from .model_catalog import ModelCatalog
from .model_factory import build_model
from .model_selection import FLASH_FAMILY, FLASH_LITE_FAMILY, PRO_FAMILY
from .project_types import (
    BalancedModel,
    CapableModel,
    EmbeddingModel,
    FastModel,
    GoogleCloudLocation,
    GoogleCloudProject,
)

# Vertex AI location used when none is configured. "global" targets the global
# endpoint (models are discovered / served region-agnostically); set an explicit
# region (constructor or GOOGLE_CLOUD_LOCATION) for geofencing / data residency.
DEFAULT_LOCATION = "global"

# Env vars that pin a specific model id per tier, overriding catalog discovery.
FAST_MODEL_ENV = "GEMINI_FAST_MODEL"
BALANCED_MODEL_ENV = "GEMINI_BALANCED_MODEL"
CAPABLE_MODEL_ENV = "GEMINI_CAPABLE_MODEL"
EMBEDDING_MODEL_ENV = "GEMINI_EMBEDDING_MODEL"


def _resolve_model_id(
    override: str | None, env_var: str, family: str, catalog: ModelCatalog
) -> str:
    """Resolve a model id: constructor override, then env var, then catalog.

    Args:
        override: The constructor-supplied model id, or None.
        env_var: The env var that can pin the model id.
        family: The catalog family key to fall back to.
        catalog: The model catalog used for discovery.

    Returns:
        The resolved model id.
    """
    return override or os.environ.get(env_var) or catalog.latest(family)


class Models:
    """Bundle of the three generative models, for type-safe injection.

    Resolving this real class avoids `injector.get(SomeNewType)` (which does not
    type-check), while still injecting each model by its distinct `NewType` key:

        from injector import Injector
        from .shared.config import ModelModule, Models

        models = Injector([ModelModule()]).get(Models)
        agent = Agent(model=models.balanced, ...)
    """

    @inject
    def __init__(
        self,
        fast: FastModel,
        balanced: BalancedModel,
        capable: CapableModel,
    ) -> None:
        """Receive all three generative models via constructor injection.

        Args:
            fast: The `flash-lite`-family model.
            balanced: The `flash`-family model.
            capable: The `pro`-family model.
        """
        self.fast = fast
        self.balanced = balanced
        self.capable = capable


class ModelModule(Module):
    """Injector module providing the shared genai client and Gemini models.

    Install it when building an `Injector` to make the project/location, the
    genai `Client`, the `ModelCatalog`, and all models available:

        from injector import Injector
        from .shared.config import ModelModule, Models

        models = Injector([ModelModule()]).get(Models)

    Each model defaults to the latest in its family (discovered from the Vertex
    AI catalog), but can be pinned per-tier via `GEMINI_FAST_MODEL` /
    `GEMINI_BALANCED_MODEL` / `GEMINI_CAPABLE_MODEL` / `GEMINI_EMBEDDING_MODEL`,
    or via constructor overrides. Project and location default to the
    `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` env vars (location falling
    back to the global endpoint), and can also be overridden per-injector:

        Injector([ModelModule(project="my-proj", location="europe-west4")])
    """

    def __init__(
        self,
        project: str | None = None,
        location: str | None = None,
        fast_model: str | None = None,
        balanced_model: str | None = None,
        capable_model: str | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """Configure the Vertex AI target and optional per-tier model pins.

        Args:
            project: GCP project for the genai client. Defaults to the
                `GOOGLE_CLOUD_PROJECT` env var when omitted; if that is also
                unset the client falls back to ADC project discovery.
            location: Vertex AI region for the genai client. Defaults to the
                `GOOGLE_CLOUD_LOCATION` env var, then the global endpoint.
            fast_model: Pin the `FastModel` id (skips catalog discovery).
            balanced_model: Pin the `BalancedModel` id (skips catalog discovery).
            capable_model: Pin the `CapableModel` id (skips catalog discovery).
            embedding_model: Pin the `EmbeddingModel` id (skips catalog
                discovery).
        """
        self._project = project
        self._location = location
        self._fast_model = fast_model
        self._balanced_model = balanced_model
        self._capable_model = capable_model
        self._embedding_model = embedding_model

    @singleton
    @provider
    def provide_project(self) -> GoogleCloudProject:
        """Resolve the GCP project (constructor override, then env var).

        An empty string means "not configured" — the genai client then falls
        back to Application Default Credentials (ADC) to discover the project.
        (A `NewType` base must be a real class, so we can't type this as
        `str | None`; the empty string is the "absent" sentinel instead.)

        Returns:
            The resolved project, or an empty `GoogleCloudProject` if unset.
        """
        project = self._project or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""
        return GoogleCloudProject(project)

    @singleton
    @provider
    def provide_location(self) -> GoogleCloudLocation:
        """Resolve the Vertex AI location (override, env var, then global).

        Returns:
            The resolved region as a `GoogleCloudLocation`.
        """
        location = (
            self._location
            or os.environ.get("GOOGLE_CLOUD_LOCATION")
            or DEFAULT_LOCATION
        )
        return GoogleCloudLocation(location)

    @singleton
    @provider
    def provide_genai_client(
        self,
        project: GoogleCloudProject,
        location: GoogleCloudLocation,
    ) -> Client:
        """Build the shared Vertex AI genai client, pinned to project/location.

        Args:
            project: Injected GCP project. An empty string is treated as unset
                and passed as `None`, letting the client fall back to ADC.
            location: Injected Vertex AI region.

        Returns:
            A `google.genai.Client` configured for Vertex AI.
        """
        # Empty project -> None so genai resolves it from ADC / the environment.
        return Client(vertexai=True, project=project or None, location=location)

    @singleton
    @provider
    def provide_catalog(self, client: Client) -> ModelCatalog:
        """Provide the (singleton) Vertex AI model catalog.

        Args:
            client: The injected, geofenced genai client.

        Returns:
            A `ModelCatalog` that lazily lists models on first use.
        """
        return ModelCatalog(client)

    @singleton
    @provider
    def provide_fast_model(self, client: Client, catalog: ModelCatalog) -> FastModel:
        """Provide the fastest model (`flash-lite` family) as a singleton.

        Args:
            client: The injected, geofenced genai client.
            catalog: The injected model catalog (used only if not pinned).

        Returns:
            The `FastModel`-tagged Gemini instance.
        """
        model_id = _resolve_model_id(
            self._fast_model, FAST_MODEL_ENV, FLASH_LITE_FAMILY, catalog
        )
        return FastModel(build_model(model_id, client))

    @singleton
    @provider
    def provide_balanced_model(
        self, client: Client, catalog: ModelCatalog
    ) -> BalancedModel:
        """Provide the balanced model (`flash` family) as a singleton.

        Args:
            client: The injected, geofenced genai client.
            catalog: The injected model catalog (used only if not pinned).

        Returns:
            The `BalancedModel`-tagged Gemini instance.
        """
        model_id = _resolve_model_id(
            self._balanced_model, BALANCED_MODEL_ENV, FLASH_FAMILY, catalog
        )
        return BalancedModel(build_model(model_id, client))

    @singleton
    @provider
    def provide_capable_model(
        self, client: Client, catalog: ModelCatalog
    ) -> CapableModel:
        """Provide the high-capability model (`pro` family) as a singleton.

        Args:
            client: The injected, geofenced genai client.
            catalog: The injected model catalog (used only if not pinned).

        Returns:
            The `CapableModel`-tagged Gemini instance.
        """
        model_id = _resolve_model_id(
            self._capable_model, CAPABLE_MODEL_ENV, PRO_FAMILY, catalog
        )
        return CapableModel(build_model(model_id, client))

    @singleton
    @provider
    def provide_embedding_model(self, catalog: ModelCatalog) -> EmbeddingModel:
        """Provide the embedding model *name* as a singleton.

        Unlike the generative tiers this returns a model id string (not a
        `Gemini`), for use with `client.models.embed_content(model=...)`. The
        injected `Client` carries the project/location.

        Args:
            catalog: The injected model catalog (used only if not pinned).

        Returns:
            The `EmbeddingModel`-tagged model id.
        """
        model_id = (
            self._embedding_model
            or os.environ.get(EMBEDDING_MODEL_ENV)
            or catalog.latest_embedding()
        )
        return EmbeddingModel(model_id)
