"""Cluster configuration parsed from the environment.

This module is intentionally **pure** (standard library only, no ADK/genai
imports) so it is trivially unit-testable and can be reasoned about without a
running cluster. It models two things:

- which agent this process *is* (``AGENT_NAME``), and
- the *peers* it can reach over A2A, addressed by Kubernetes DNS.

The same container image runs every agent; ``AGENT_NAME`` selects which one this
process becomes at startup (see ``app/agent.py``). Every agent is treated the
same — a "coordinating" agent is simply one whose configured peers are non-empty
(its defaults come from its ``AgentSpec.peers``; see ``app/agents``). Peers are
resolved to their A2A **agent card** URLs by ``app/cluster/resolver.py`` and
wired in as ``RemoteA2aAgent`` children.

Environment variables
---------------------
- ``AGENT_NAME``         Which agent this instance is. Default: ``orchestrator``.
- ``A2A_NAMESPACE``      Kubernetes namespace peers live in. Default: ``agents``.
- ``A2A_CLUSTER_DOMAIN`` Cluster DNS domain. Default: ``svc.cluster.local``.
- ``A2A_PEERS``          Comma-separated peers this agent can reach. Each item is
                         either ``name`` (URL derived from cluster DNS) or
                         ``name=https://host[:port]`` (explicit URL). When unset,
                         the agent falls back to its declared default peers.
- ``A2A_PEER_SCHEME``    Scheme for DNS-derived peer URLs. Default: ``http``.
- ``A2A_PEER_PORT``      Port for DNS-derived peer URLs. Default: ``80``.
- ``A2A_RPC_PATH``       Path segment the A2A serving layer mounts the JSON-RPC
                         endpoint + agent card under. It uses
                         ``/a2a/<app_name>`` and the app name is ``app`` (see
                         ``app/agent.py``: ``App(name="app")``), so the default
                         is ``/a2a/app``. A bare peer name therefore resolves to
                         ``http://<name>.<ns>.<domain>/a2a/app/.well-known/...``.
                         Override only if you change the app name or mount path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# The agent a process becomes when AGENT_NAME is unset. Kept as a plain string
# here (this module must not import ``app.agents``, to avoid an import cycle);
# it matches ``app.agents.DEFAULT_AGENT``.
DEFAULT_AGENT_NAME = "orchestrator"

DEFAULT_NAMESPACE = "agents"
DEFAULT_CLUSTER_DOMAIN = "svc.cluster.local"
DEFAULT_PEER_SCHEME = "http"
DEFAULT_PEER_PORT = 80
# The base A2A serving mounts the JSON-RPC endpoint + agent card under
# ``/a2a/<app_name>`` and the app name is ``app`` (App(name="app")), so peers
# publish their card at ``<base_url>/a2a/app/.well-known/agent-card.json``.
DEFAULT_RPC_PATH = "/a2a/app"

AGENT_NAME_ENV = "AGENT_NAME"
NAMESPACE_ENV = "A2A_NAMESPACE"
CLUSTER_DOMAIN_ENV = "A2A_CLUSTER_DOMAIN"
PEERS_ENV = "A2A_PEERS"
PEER_SCHEME_ENV = "A2A_PEER_SCHEME"
PEER_PORT_ENV = "A2A_PEER_PORT"
RPC_PATH_ENV = "A2A_RPC_PATH"


@dataclass(frozen=True)
class PeerSpec:
    """A single reachable peer agent.

    Attributes:
        name: The peer's logical name (matches its Kubernetes Service name and
            the ``AGENT_NAME`` it runs under).
        base_url: The peer's base URL (scheme + host [+ port]), without the
            agent-card path. The resolver appends the well-known card path.
    """

    name: str
    base_url: str


def service_dns_url(
    name: str,
    *,
    namespace: str = DEFAULT_NAMESPACE,
    cluster_domain: str = DEFAULT_CLUSTER_DOMAIN,
    scheme: str = DEFAULT_PEER_SCHEME,
    port: int = DEFAULT_PEER_PORT,
) -> str:
    """Build the in-cluster base URL for a peer from Kubernetes DNS conventions.

    A ``Service`` named ``name`` in ``namespace`` is reachable in-cluster at
    ``<name>.<namespace>.<cluster_domain>``. The default port for the scheme
    (80 for http, 443 for https) is omitted for a cleaner URL.

    Args:
        name: The peer's Service name.
        namespace: The Kubernetes namespace the Service lives in.
        cluster_domain: The cluster DNS domain (usually ``svc.cluster.local``).
        scheme: URL scheme (``http`` or ``https``).
        port: TCP port the Service exposes.

    Returns:
        The peer's base URL, e.g. ``http://research.agents.svc.cluster.local``.
    """
    host = f"{name}.{namespace}.{cluster_domain}"
    default_port = 443 if scheme == "https" else 80
    if port == default_port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _parse_peers(
    raw: str | None,
    *,
    namespace: str,
    cluster_domain: str,
    scheme: str,
    port: int,
    defaults: Sequence[str],
) -> tuple[PeerSpec, ...]:
    """Parse the ``A2A_PEERS`` value into ``PeerSpec`` entries.

    Args:
        raw: The raw ``A2A_PEERS`` string, or ``None`` when unset.
        namespace: Namespace used to derive DNS URLs for bare names.
        cluster_domain: Cluster DNS domain used for derived URLs.
        scheme: Scheme used for derived URLs.
        port: Port used for derived URLs.
        defaults: Peer names to use when ``raw`` is empty/unset.

    Returns:
        A tuple of unique ``PeerSpec`` entries, order preserved.
    """
    items = [item.strip() for item in raw.split(",")] if raw else list(defaults)

    peers: dict[str, PeerSpec] = {}
    for item in items:
        if not item:
            continue
        name, _, url = item.partition("=")
        name = name.strip()
        if not name:
            continue
        explicit = url.strip()
        if explicit:
            base_url = explicit
        else:
            base_url = service_dns_url(
                name,
                namespace=namespace,
                cluster_domain=cluster_domain,
                scheme=scheme,
                port=port,
            )
        peers[name] = PeerSpec(name=name, base_url=base_url.rstrip("/"))
    return tuple(peers.values())


@dataclass(frozen=True)
class ClusterConfig:
    """Resolved cluster configuration for this agent instance.

    Attributes:
        name: Which agent this instance is (matches a key of ``app.agents``).
        namespace: Kubernetes namespace peers are addressed in.
        cluster_domain: Cluster DNS domain used to derive peer URLs.
        peer_scheme: Scheme used for DNS-derived peer URLs.
        peer_port: Port used for DNS-derived peer URLs.
        peers: The peers this instance can reach over A2A.
        rpc_path: Path segment the base A2A serving mounts the JSON-RPC endpoint
            + agent card under (``/a2a/app`` by default). The resolver appends it
            (plus the well-known card path) to each peer's base URL.
    """

    name: str
    namespace: str
    cluster_domain: str
    peer_scheme: str
    peer_port: int
    peers: tuple[PeerSpec, ...]
    rpc_path: str = DEFAULT_RPC_PATH

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        name: str | None = None,
        default_peers: Sequence[str] = (),
    ) -> ClusterConfig:
        """Build a ``ClusterConfig`` from environment variables.

        Args:
            env: Mapping to read from (defaults to ``os.environ``). Accepting it
                as an argument keeps this pure and unit-testable.
            name: The selected agent name. When ``None`` it is read from
                ``AGENT_NAME`` (falling back to ``DEFAULT_AGENT_NAME``). Passing
                it lets the caller resolve the name once and reuse it.
            default_peers: Peer names this agent falls back to when ``A2A_PEERS``
                is unset (typically the selected agent's ``AgentSpec.peers``).

        Returns:
            The resolved ``ClusterConfig``.
        """
        source = os.environ if env is None else env

        resolved_name = (
            name if name is not None else source.get(AGENT_NAME_ENV, DEFAULT_AGENT_NAME)
        ).strip() or DEFAULT_AGENT_NAME
        namespace = source.get(NAMESPACE_ENV, DEFAULT_NAMESPACE).strip()
        cluster_domain = source.get(CLUSTER_DOMAIN_ENV, DEFAULT_CLUSTER_DOMAIN).strip()
        scheme = source.get(PEER_SCHEME_ENV, DEFAULT_PEER_SCHEME).strip()
        port = _parse_port(source.get(PEER_PORT_ENV))
        rpc_path = _normalize_rpc_path(source.get(RPC_PATH_ENV))

        # A2A_PEERS overrides the agent's declared default peers when set.
        peers = _parse_peers(
            source.get(PEERS_ENV),
            namespace=namespace,
            cluster_domain=cluster_domain,
            scheme=scheme,
            port=port,
            defaults=default_peers,
        )

        return cls(
            name=resolved_name,
            namespace=namespace,
            cluster_domain=cluster_domain,
            peer_scheme=scheme,
            peer_port=port,
            peers=peers,
            rpc_path=rpc_path,
        )


def _parse_port(raw: str | None) -> int:
    """Parse a port env value, falling back to the default on empty/invalid.

    Args:
        raw: The raw port string, or ``None``.

    Returns:
        The parsed port, or ``DEFAULT_PEER_PORT`` if missing/malformed.
    """
    if not raw or not raw.strip():
        return DEFAULT_PEER_PORT
    try:
        return int(raw.strip())
    except ValueError:
        return DEFAULT_PEER_PORT


def _normalize_rpc_path(raw: str | None) -> str:
    """Normalize the A2A RPC path segment, falling back to the default.

    Ensures a single leading slash and no trailing slash so it composes cleanly
    with a peer base URL and the well-known card path
    (``<base_url><rpc_path>/.well-known/agent-card.json``).

    Args:
        raw: The raw ``A2A_RPC_PATH`` value, or ``None``.

    Returns:
        The normalized path (e.g. ``/a2a/app``), or ``DEFAULT_RPC_PATH`` if
        missing/empty.
    """
    if not raw or not raw.strip():
        return DEFAULT_RPC_PATH
    return "/" + raw.strip().strip("/")
