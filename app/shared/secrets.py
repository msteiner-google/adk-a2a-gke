"""Secret Manager access wired through dependency injection.

`SecretModule` provides two injectables for reading Google Cloud Secret Manager
secrets, keeping secret access consistent across variants the same way
`ModelModule` does for models:

- `Secrets` — a small service with `get(name)` / `get_as(type, name)` that
  resolves short names against the injected `GoogleCloudProject` (or accepts a
  fully-qualified resource path) and memoizes each resolved value, so a given
  secret version is fetched at most once per process (pass `refresh=True` to
  re-fetch after rotation).
- `SecretResolver` — a lightweight *callable* handle for code that only needs to
  read a secret into a distinctly-typed value:

      MyToken = NewType("MyToken", str)

      class GithubClient:
          @inject
          def __init__(self, resolve: SecretResolver) -> None:
              self._token = resolve(MyToken, "github-token")  # typed MyToken

Install it alongside `ModelModule`, which binds the shared `GoogleCloudProject`
these providers reuse:

    from injector import Injector

    from .shared.config import ModelModule
    from .shared.secrets import SecretModule, Secrets

    secrets = Injector([ModelModule(), SecretModule()]).get(Secrets)
    token = secrets.get("github-token")
"""

from collections.abc import Callable
from typing import TypeVar

from google.cloud.secretmanager import SecretManagerServiceClient
from injector import Module, inject, provider, singleton

from .project_types import GoogleCloudProject

# The secret *version* read when a caller does not pin one. "latest" always
# resolves to the newest enabled version of the secret.
DEFAULT_SECRET_VERSION = "latest"

# The type a resolved secret value is tagged with — typically a `NewType` over
# `str`, e.g. `NewType("ApiKey", str)`. Bound to `str` so only string-like tags
# are accepted.
SecretT = TypeVar("SecretT", bound=str)


class Secrets:
    """Injectable accessor for Google Cloud Secret Manager.

    Reads secret payloads by short name (expanded against the injected
    `GoogleCloudProject`) or by a fully-qualified resource path, decodes them as
    UTF-8, and memoizes each resolved value so a given secret version is fetched
    at most once per process. Pass `refresh=True` to re-fetch after rotation.
    """

    @inject
    def __init__(
        self,
        client: SecretManagerServiceClient,
        project: GoogleCloudProject,
    ) -> None:
        """Store the Secret Manager client and default project.

        Args:
            client: The Secret Manager client used to access secret versions.
            project: The GCP project short names are resolved against. May be an
                empty string when unset — then only fully-qualified names work.
        """
        self._client = client
        self._project = project
        self._cache: dict[str, str] = {}

    def resource_name(self, name: str, version: str = DEFAULT_SECRET_VERSION) -> str:
        """Return the fully-qualified secret-version resource path for `name`.

        A `name` that already looks like a resource path (contains a "/") is
        returned unchanged; otherwise it is expanded to
        `projects/<project>/secrets/<name>/versions/<version>`.

        Args:
            name: A short secret id (e.g. `github-token`) or a full resource
                path (e.g. `projects/p/secrets/s/versions/1`).
            version: The secret version to read when `name` is a short id.

        Returns:
            The fully-qualified secret-version resource path.

        Raises:
            ValueError: If `name` is a short id but no project is configured.
        """
        if "/" in name:
            return name
        if not self._project:
            raise ValueError(
                f"Cannot resolve secret {name!r} without a project: set "
                "GOOGLE_CLOUD_PROJECT (or install ModelModule with a project), "
                "or pass a fully-qualified resource name."
            )
        return f"projects/{self._project}/secrets/{name}/versions/{version}"

    def get(
        self,
        name: str,
        version: str = DEFAULT_SECRET_VERSION,
        *,
        refresh: bool = False,
    ) -> str:
        """Return the secret value for `name`, fetching and caching on demand.

        Args:
            name: A short secret id or a fully-qualified resource path.
            version: The version to read when `name` is a short id.
            refresh: When True, bypass the cache and re-fetch the value.

        Returns:
            The decoded (UTF-8) secret payload.
        """
        resource = self.resource_name(name, version)
        if refresh or resource not in self._cache:
            response = self._client.access_secret_version(name=resource)
            self._cache[resource] = response.payload.data.decode("utf-8")
        return self._cache[resource]

    def get_as(
        self,
        as_type: Callable[[str], SecretT],
        name: str,
        version: str = DEFAULT_SECRET_VERSION,
        *,
        refresh: bool = False,
    ) -> SecretT:
        """Return the secret value wrapped in `as_type` for a distinct type.

        `as_type` is typically a `NewType` constructor, giving the value a
        distinct static type (so a DB password can't be passed where an API key
        is expected) at zero runtime cost.

        Args:
            as_type: A callable (e.g. a `NewType`) that tags the string value.
            name: A short secret id or a fully-qualified resource path.
            version: The version to read when `name` is a short id.
            refresh: When True, bypass the cache and re-fetch the value.

        Returns:
            The secret value wrapped by `as_type`.
        """
        return as_type(self.get(name, version, refresh=refresh))


class SecretResolver:
    """Callable handle that resolves a named secret into a typed value.

    Inject this (rather than the whole `Secrets` service) in code that only
    needs to read secrets into distinctly-typed values:

        ApiKey = NewType("ApiKey", str)

        class GithubClient:
            @inject
            def __init__(self, resolve: SecretResolver) -> None:
                self._token = resolve(ApiKey, "github-token")
    """

    @inject
    def __init__(self, secrets: Secrets) -> None:
        """Wrap a `Secrets` service.

        Args:
            secrets: The backing secret accessor.
        """
        self._secrets = secrets

    def __call__(
        self,
        as_type: Callable[[str], SecretT],
        name: str,
        version: str = DEFAULT_SECRET_VERSION,
        *,
        refresh: bool = False,
    ) -> SecretT:
        """Resolve `name` and wrap it in `as_type` (see `Secrets.get_as`).

        Args:
            as_type: A callable (e.g. a `NewType`) that tags the string value.
            name: A short secret id or a fully-qualified resource path.
            version: The version to read when `name` is a short id.
            refresh: When True, bypass the cache and re-fetch the value.

        Returns:
            The secret value wrapped by `as_type`.
        """
        return self._secrets.get_as(as_type, name, version, refresh=refresh)


class SecretModule(Module):
    """Injector module providing Secret Manager access.

    Provides, as singletons, a Secret Manager client, the `Secrets` service, and
    the `SecretResolver` callable. It reuses the shared `GoogleCloudProject`
    binding, so install it alongside `ModelModule` (which provides that binding):

        Injector([ModelModule(project="my-proj"), SecretModule()])
    """

    @singleton
    @provider
    def provide_client(self) -> SecretManagerServiceClient:
        """Provide the (singleton) Secret Manager client.

        Returns:
            A `SecretManagerServiceClient` using Application Default Credentials.
        """
        return SecretManagerServiceClient()

    @singleton
    @provider
    def provide_secrets(
        self,
        client: SecretManagerServiceClient,
        project: GoogleCloudProject,
    ) -> Secrets:
        """Provide the (singleton) `Secrets` accessor.

        Args:
            client: The injected Secret Manager client.
            project: The injected GCP project short names resolve against.

        Returns:
            The shared `Secrets` service.
        """
        return Secrets(client, project)

    @singleton
    @provider
    def provide_resolver(self, secrets: Secrets) -> SecretResolver:
        """Provide the (singleton) `SecretResolver` callable.

        Args:
            secrets: The injected `Secrets` service.

        Returns:
            A `SecretResolver` wrapping `secrets`.
        """
        return SecretResolver(secrets)
