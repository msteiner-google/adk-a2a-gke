"""Database connectivity for durable session and A2A task storage.

This module owns the single ``AsyncEngine`` each agent process uses to reach
AlloyDB. Everything above it (the ADK session service, the A2A task store) is
handed that engine rather than a URL, so there is exactly one connection pool
per pod no matter how many storage-backed services are wired up.

Backends (``DB_BACKEND``)
------------------------
- ``none`` (default): no database at all. Keeps unit tests and local runs
  hermetic — nothing here imports a cloud SDK or opens a socket until a durable
  backend is explicitly selected.
- ``alloydb``: AlloyDB over the official Python connector using **IAM database
  authentication**. The pod authenticates as its Workload Identity service
  account; there is no password anywhere. Requires ``ALLOYDB_INSTANCE_URI`` and
  ``ALLOYDB_IAM_USER``.
- ``url``: a plain SQLAlchemy async URL (``DB_URL``). Used for local Postgres,
  for the Alembic migration job when it runs outside the cluster, and for
  SQLite in tests.

Per-agent schema isolation
--------------------------
Every agent runs ``App(name="app")`` (see the invariants in AGENTS.md), so in a
single flat schema every ``sessions`` row would carry ``app_name='app'`` and be
unattributable. Instead each agent gets its own PostgreSQL schema, selected by
``DB_SCHEMA`` (defaulting to ``AGENT_NAME``) and applied as the connection's
``search_path``.

Both storage libraries emit **unqualified** table names — ADK's
``DatabaseSessionService`` and a2a's ``DatabaseTaskStore`` alike — so
``search_path`` routes each agent to its own tables with no patching of either
library. The isolation is then enforced by ``GRANT``, which maps one-to-one onto
the per-agent IAM identity (see ``infra/terraform/alloydb.tf``).

``search_path`` is deliberately set per *connection* rather than with
``ALTER ROLE``, so the value is visible in this repo instead of being invisible
server state, and so the migration job can target any schema it likes.

Connection budget
-----------------
The prototype instance is ``c4a-highmem-1`` — a single vCPU. Pool defaults are
therefore small on purpose: ``DB_POOL_SIZE`` connections plus
``DB_MAX_OVERFLOW`` burst per pod. Raise them only alongside the machine type.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.cluster.config import AGENT_NAME_ENV, DEFAULT_AGENT_NAME

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

DB_BACKEND_ENV = "DB_BACKEND"
DB_NAME_ENV = "DB_NAME"
DB_SCHEMA_ENV = "DB_SCHEMA"
DB_URL_ENV = "DB_URL"
DB_POOL_SIZE_ENV = "DB_POOL_SIZE"
DB_MAX_OVERFLOW_ENV = "DB_MAX_OVERFLOW"
DB_POOL_RECYCLE_ENV = "DB_POOL_RECYCLE"
ALLOYDB_INSTANCE_URI_ENV = "ALLOYDB_INSTANCE_URI"
ALLOYDB_IAM_USER_ENV = "ALLOYDB_IAM_USER"
ALLOYDB_IP_TYPE_ENV = "ALLOYDB_IP_TYPE"

NONE = "none"
ALLOYDB = "alloydb"
URL = "url"

BACKENDS = (NONE, ALLOYDB, URL)

DEFAULT_DB_NAME = "agents"
DEFAULT_IP_TYPE = "PRIVATE"
DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 2
# Recycle connections well inside any middlebox idle timeout.
DEFAULT_POOL_RECYCLE = 1800


def _int_env(raw: str | None, default: int) -> int:
    """Parse an integer env value, falling back on empty/invalid input.

    Args:
        raw: The raw value, or ``None`` when unset.
        default: Value to use when missing or malformed.

    Returns:
        The parsed integer, or ``default``.
    """
    if not raw or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class DatabaseConfig:
    """Resolved database configuration for this agent instance.

    Attributes:
        backend: One of ``none``, ``alloydb``, or ``url``.
        instance_uri: AlloyDB instance URI
            (``projects/P/locations/L/clusters/C/instances/I``).
        iam_user: The PostgreSQL role to authenticate as. For a service account
            this is its email **without** the ``.gserviceaccount.com`` suffix.
        name: The database (catalog) name to connect to.
        db_schema: PostgreSQL schema applied as this connection's
            ``search_path``. Empty leaves the server default alone.
        url: SQLAlchemy async URL, used only by the ``url`` backend.
        ip_type: AlloyDB IP type (``PRIVATE``, ``PUBLIC``, or ``PSC``).
        pool_size: Persistent connections held per pod.
        max_overflow: Extra connections allowed above ``pool_size`` under load.
        pool_recycle: Seconds after which a pooled connection is replaced.
    """

    backend: str
    instance_uri: str
    iam_user: str
    name: str
    db_schema: str
    url: str
    ip_type: str
    pool_size: int
    max_overflow: int
    pool_recycle: int

    @property
    def enabled(self) -> bool:
        """Whether a durable database backend is configured.

        Returns:
            ``True`` unless the backend is ``none``.
        """
        return self.backend != NONE

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        default_schema: str = "",
    ) -> DatabaseConfig:
        """Build a ``DatabaseConfig`` from environment variables.

        Args:
            env: Mapping to read from (defaults to ``os.environ``). Accepting it
                as an argument keeps this pure and unit-testable.
            default_schema: Schema to use when ``DB_SCHEMA`` is unset —
                typically the selected agent's name, so each agent lands in its
                own schema without per-agent configuration.

        Returns:
            The resolved ``DatabaseConfig``.

        Raises:
            ValueError: If the backend name is unknown, or a setting the chosen
                backend requires is missing.
        """
        source = os.environ if env is None else env

        backend = source.get(DB_BACKEND_ENV, NONE).strip().lower() or NONE
        if backend not in BACKENDS:
            raise ValueError(
                f"Unknown {DB_BACKEND_ENV}={backend!r}; expected one of "
                f"{', '.join(repr(item) for item in BACKENDS)}."
            )

        instance_uri = source.get(ALLOYDB_INSTANCE_URI_ENV, "").strip()
        iam_user = source.get(ALLOYDB_IAM_USER_ENV, "").strip()
        url = source.get(DB_URL_ENV, "").strip()

        if backend == ALLOYDB:
            missing = [
                name
                for name, value in (
                    (ALLOYDB_INSTANCE_URI_ENV, instance_uri),
                    (ALLOYDB_IAM_USER_ENV, iam_user),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"{DB_BACKEND_ENV}={ALLOYDB} requires {' and '.join(missing)} "
                    "to be set."
                )
        elif backend == URL and not url:
            raise ValueError(f"{DB_BACKEND_ENV}={URL} requires {DB_URL_ENV} to be set.")

        return cls(
            backend=backend,
            instance_uri=instance_uri,
            iam_user=iam_user,
            name=source.get(DB_NAME_ENV, DEFAULT_DB_NAME).strip() or DEFAULT_DB_NAME,
            db_schema=source.get(DB_SCHEMA_ENV, default_schema).strip(),
            url=url,
            ip_type=(
                source.get(ALLOYDB_IP_TYPE_ENV, DEFAULT_IP_TYPE).strip().upper()
                or DEFAULT_IP_TYPE
            ),
            pool_size=_int_env(source.get(DB_POOL_SIZE_ENV), DEFAULT_POOL_SIZE),
            max_overflow=_int_env(
                source.get(DB_MAX_OVERFLOW_ENV), DEFAULT_MAX_OVERFLOW
            ),
            pool_recycle=_int_env(
                source.get(DB_POOL_RECYCLE_ENV), DEFAULT_POOL_RECYCLE
            ),
        )


def _wants_search_path(config: DatabaseConfig) -> bool:
    """Whether a PostgreSQL ``search_path`` should be pinned for this backend.

    SQLite (used by the hermetic tests) has no schema concept and asyncpg's
    ``server_settings`` is meaningless there, so the setting is skipped unless
    the target really is PostgreSQL.

    Args:
        config: The resolved configuration.

    Returns:
        ``True`` when a schema is configured and the driver is PostgreSQL.
    """
    if not config.db_schema:
        return False
    if config.backend == ALLOYDB:
        return True
    return config.url.startswith("postgresql")


class Database:
    """Owns this process's ``AsyncEngine`` and the AlloyDB connector behind it.

    The engine is built **lazily**, on first ``engine()`` call, because the
    AlloyDB connector resolves Application Default Credentials in its
    constructor — doing that at import time would break hermetic tests and slow
    startup for deployments that use no database at all. Constructing this
    object itself is cheap and side-effect free, so it is safe to provide from
    the injector.

    ``engine()`` is deliberately **synchronous**. Neither
    ``create_async_engine`` nor ``AsyncConnector.__init__`` needs a running
    event loop: the connector explicitly defers both its RSA keypair generation
    and its API client to the first ``connect()`` call, which does run inside
    the loop. Keeping this sync is what lets the engine be wired up from
    ordinary synchronous construction paths — the injector providers and ADK's
    service registry — instead of forcing every consumer to be async.
    """

    def __init__(self, config: DatabaseConfig) -> None:
        """Initialize the holder without touching the network.

        Args:
            config: The resolved database configuration.
        """
        self._config = config
        self._engine: AsyncEngine | None = None
        self._connector: Any | None = None
        self._lock = threading.Lock()

    @property
    def config(self) -> DatabaseConfig:
        """The configuration this database was built from.

        Returns:
            The resolved :class:`DatabaseConfig`.
        """
        return self._config

    @property
    def enabled(self) -> bool:
        """Whether a durable backend is configured.

        Returns:
            ``True`` unless the backend is ``none``.
        """
        return self._config.enabled

    def engine(self) -> AsyncEngine:
        """Return the process-wide engine, creating it on first use.

        Returns:
            The shared :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.

        Raises:
            ValueError: If no durable backend is configured.
        """
        if not self._config.enabled:
            raise ValueError(
                f"No database configured ({DB_BACKEND_ENV}={NONE}). Set "
                f"{DB_BACKEND_ENV} to {ALLOYDB!r} or {URL!r} before requesting "
                "an engine."
            )
        if self._engine is not None:
            return self._engine

        with self._lock:
            # Double-check: a concurrent caller may have built it while we
            # waited for the lock.
            if self._engine is None:
                self._engine = (
                    self._build_alloydb_engine()
                    if self._config.backend == ALLOYDB
                    else self._build_url_engine()
                )
            return self._engine

    def _pool_kwargs(self) -> dict[str, Any]:
        """Pool tuning shared by both backends.

        Returns:
            Keyword arguments for ``create_async_engine``.
        """
        return {
            "pool_size": self._config.pool_size,
            "max_overflow": self._config.max_overflow,
            "pool_recycle": self._config.pool_recycle,
            # Cheap liveness check; AlloyDB maintenance can drop idle
            # connections and a stale one otherwise surfaces as a request error.
            "pool_pre_ping": True,
        }

    def _server_settings(self) -> dict[str, str]:
        """PostgreSQL session settings applied to every new connection.

        Returns:
            A mapping suitable for asyncpg's ``server_settings``; empty when no
            schema pinning applies.
        """
        if not _wants_search_path(self._config):
            return {}
        return {"search_path": self._config.db_schema}

    def _build_alloydb_engine(self) -> AsyncEngine:
        """Build an engine that reaches AlloyDB via IAM database auth.

        No password is involved: the connector mints a short-lived OAuth token
        from the pod's Workload Identity credentials for every connection, and
        wraps the socket in mTLS using a client certificate it fetches from the
        AlloyDB Admin API.

        Returns:
            A configured :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.
        """
        from google.cloud.alloydbconnector import AsyncConnector
        from sqlalchemy.ext.asyncio import create_async_engine

        connector = AsyncConnector(ip_type=self._config.ip_type)
        self._connector = connector

        connect_kwargs: dict[str, Any] = {
            "user": self._config.iam_user,
            "db": self._config.name,
            "enable_iam_auth": True,
        }
        server_settings = self._server_settings()
        if server_settings:
            connect_kwargs["server_settings"] = server_settings

        async def _connect() -> Any:
            return await connector.connect(
                self._config.instance_uri, "asyncpg", **connect_kwargs
            )

        # The connector supplies the DBAPI connection, so the URL carries only
        # the dialect; host/credentials are intentionally absent.
        return create_async_engine(
            "postgresql+asyncpg://",
            async_creator=_connect,
            **self._pool_kwargs(),
        )

    def _build_url_engine(self) -> AsyncEngine:
        """Build an engine from a plain SQLAlchemy async URL.

        Returns:
            A configured :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.
        """
        from sqlalchemy.ext.asyncio import create_async_engine

        # SQLite (tests) rejects the pool tuning that Postgres wants.
        if self._config.url.startswith("sqlite"):
            return create_async_engine(self._config.url)

        connect_args: dict[str, Any] = {}
        server_settings = self._server_settings()
        if server_settings:
            connect_args["server_settings"] = server_settings

        return create_async_engine(
            self._config.url,
            connect_args=connect_args,
            **self._pool_kwargs(),
        )

    async def aclose(self) -> None:
        """Dispose of the engine and close the AlloyDB connector.

        Safe to call when nothing was ever created. Call it from the serving
        app's shutdown path so the connector's background refresh tasks do not
        outlive the process.
        """
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        if self._connector is not None:
            await self._connector.close()
            self._connector = None


def build_database() -> Database:
    """Construct the :class:`Database` described by the environment.

    A plain factory, matching ``build_session_service`` / ``build_task_store`` /
    ``build_artifact_service``: sharing is the injector's job, not this
    function's. ``SessionModule`` provides it as a singleton so the session
    store and the task store hand out one connection pool between them rather
    than opening one each against a one-vCPU instance.

    It used to be ``functools.cache``d, on the belief that consumers outside the
    injector needed to land on the same instance. They do not: ADK's service
    registry resolves ``shared://session`` to ``app.agent.session_service``,
    which is the injector's, and the one remaining caller outside the injector
    is ``app/cluster/bootstrap.py`` -- a separate one-shot process where sharing
    is meaningless. Caching a value the injector already owns just gives it two
    homes.

    The schema defaults to this process's ``AGENT_NAME``, which is what gives
    each agent its own tables without any per-agent configuration.

    Returns:
        A new :class:`Database` (disabled when ``DB_BACKEND`` is unset).
    """
    default_schema = (
        os.environ.get(AGENT_NAME_ENV, DEFAULT_AGENT_NAME).strip() or DEFAULT_AGENT_NAME
    )
    return Database(DatabaseConfig.from_env(default_schema=default_schema))
