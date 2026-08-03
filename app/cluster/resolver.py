"""Resolve cluster peers into ADK ``RemoteA2aAgent`` instances.

The ``AgentResolver`` is the injectable "service discovery" layer for the
cluster: given the ``ClusterConfig`` (which peers exist and where), it produces
``RemoteA2aAgent`` children the orchestrator can delegate to over A2A.

Discovery is **agent-card based**: for each peer we point ``RemoteA2aAgent`` at
the peer's well-known agent-card URL. The A2A serving layer mounts the JSON-RPC
endpoint and the agent card under ``/a2a/<app_name>`` (``/a2a/app`` by default,
see ``ClusterConfig.rpc_path``), so the card lives at
``<base_url>/a2a/app/.well-known/agent-card.json`` — NOT at the service root.
A peer's ``base_url`` is therefore the service root (scheme + host [+ port]); the
resolver appends the RPC path and the well-known card path to it. ADK fetches and
validates that card lazily on first use, so constructing the remote agents here
does no network I/O — the orchestrator picks up each peer's real name,
description, and capabilities from its published card at call time.

The card path is imported from ADK (``AGENT_CARD_WELL_KNOWN_PATH``) and the RPC
path comes from the config, so the resolver always agrees with whatever path the
base A2A serving publishes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents.remote_a2a_agent import (
    AGENT_CARD_WELL_KNOWN_PATH,
    RemoteA2aAgent,
)

if TYPE_CHECKING:
    from app.cluster.config import ClusterConfig, PeerSpec


class AgentResolver:
    """Resolves configured peers into ``RemoteA2aAgent`` instances.

    Construct it with a ``ClusterConfig`` (usually via dependency injection —
    see ``app/cluster/di.py``) and call :meth:`resolve_all` to get every peer as
    a remote sub-agent, or :meth:`resolve` for a single named peer.
    """

    def __init__(self, config: ClusterConfig) -> None:
        """Initialize the resolver.

        Args:
            config: The resolved cluster configuration listing reachable peers.
        """
        self._config = config

    @property
    def config(self) -> ClusterConfig:
        """The cluster configuration this resolver was built from."""
        return self._config

    def card_url(self, peer: PeerSpec) -> str:
        """Return the well-known agent-card URL for a peer.

        Composes the peer's service-root base URL with the configured A2A RPC
        path (``/a2a/app`` by default) and the well-known card path, matching
        where the serving layer publishes the card
        (``<base_url>/a2a/app/.well-known/agent-card.json``).

        Args:
            peer: The peer to address.

        Returns:
            The peer's agent-card URL (base URL + RPC path + well-known path).
        """
        return f"{peer.base_url}{self._config.rpc_path}{AGENT_CARD_WELL_KNOWN_PATH}"

    def resolve_peer(self, peer: PeerSpec) -> RemoteA2aAgent:
        """Build a ``RemoteA2aAgent`` for a single peer spec.

        Args:
            peer: The peer to wrap.

        Returns:
            A ``RemoteA2aAgent`` addressing the peer's agent card. The card
            (and the peer's real description/capabilities) is resolved lazily
            by ADK on first invocation, so this call does no network I/O.
        """
        return RemoteA2aAgent(
            name=peer.name,
            agent_card=self.card_url(peer),
            description=(
                f"Remote specialist agent '{peer.name}' reachable over A2A at "
                f"{peer.base_url}. Delegate matching sub-tasks to it."
            ),
        )

    def resolve(self, name: str) -> RemoteA2aAgent:
        """Resolve a single peer by name.

        Args:
            name: The peer name to look up in the configuration.

        Returns:
            The peer as a ``RemoteA2aAgent``.

        Raises:
            KeyError: If no peer with that name is configured.
        """
        for peer in self._config.peers:
            if peer.name == name:
                return self.resolve_peer(peer)
        raise KeyError(f"No peer named {name!r} in cluster configuration")

    def resolve_all(self) -> list[RemoteA2aAgent]:
        """Resolve every configured peer into a remote sub-agent.

        Returns:
            A list of ``RemoteA2aAgent`` instances, one per configured peer
            (empty when no peers are configured).
        """
        return [self.resolve_peer(peer) for peer in self._config.peers]
