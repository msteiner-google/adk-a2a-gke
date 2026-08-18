# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Resolve cluster peers into callable tools.

The ``AgentResolver`` is the injectable "service discovery" layer for the
cluster: given the ``ClusterConfig`` (which peers exist and where), it produces
the tools a coordinating agent uses to delegate over A2A.

Discovery is **agent-card based**: for each peer we point ``RemoteA2aAgent`` at
the peer's well-known agent-card URL. The A2A serving layer mounts the JSON-RPC
endpoint and the agent card under ``/a2a/<app_name>`` (``/a2a/app`` by default,
see ``ClusterConfig.rpc_path``), so the card lives at
``<base_url>/a2a/app/.well-known/agent-card.json`` — NOT at the service root.
A peer's ``base_url`` is therefore the service root (scheme + host [+ port]); the
resolver appends the RPC path and the well-known card path to it. ADK fetches and
validates that card lazily on first use, so constructing the remote agents here
does no network I/O.

**Peers resolve to tools, not sub-agents** (D1). Each remote agent is wrapped in
a :class:`~app.cluster.peer_tool.PeerTool` carrying that peer's declared payload
contract, so the caller sends an explicit typed request rather than its
conversation history. See ``app/cluster/peer_tool.py`` for why, and
``docs/design-decisions.md`` for the measurement.

**A declared contract is optional.** A peer with no entry in the payload mapping
— typically an agent another squad owns, configured by URL through ``A2A_PEERS``
— resolves to :class:`UnknownPeerRequest`, a correlation id plus one free-text
task. That is a supported way to run, not a broken state: the peer's real schema
is published in its own agent card, and this repo simply has no local model for
it. ``app/agents/contracts.py`` describes the trade-off between the two tiers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents.remote_a2a_agent import (
    AGENT_CARD_WELL_KNOWN_PATH,
    RemoteA2aAgent,
)
from pydantic import BaseModel, Field

from app.cluster.peer_tool import PeerTool

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.cluster.config import ClusterConfig, PeerSpec


class UnknownPeerRequest(BaseModel):
    """Default contract for a peer with no declared model — free text, typed.

    This is what delegation looks like when nobody declares anything: one
    self-contained task in prose. Deliberately minimal rather than free-form —
    the caller must still supply a correlation key and state the task in full,
    so even an undeclared peer is delegated to explicitly instead of being
    handed a transcript. Declare a model in ``app/agents/contracts.py`` to
    replace this with named, validated fields.
    """

    case_id: str = Field(
        description="Identifier correlating all work for one case or request."
    )
    task: str = Field(
        description=(
            "The complete, self-contained task for this agent. It cannot see "
            "the conversation this request came from."
        )
    )


class AgentResolver:
    """Resolves configured peers into callable ``PeerTool`` instances.

    Construct it with a ``ClusterConfig`` and the payload contracts (usually via
    dependency injection — see ``app/cluster/di.py``), then call
    :meth:`resolve_all` to get every peer as a tool.
    """

    def __init__(
        self,
        config: ClusterConfig,
        payload_schemas: Mapping[str, type[BaseModel]] | None = None,
    ) -> None:
        """Initialize the resolver.

        Args:
            config: The resolved cluster configuration listing reachable peers.
            payload_schemas: Contract per peer name (``app/agents/contracts.py``
                supplies these). A peer absent from the mapping falls back to
                :class:`UnknownPeerRequest`. Passed in rather than imported so
                this module stays free of any dependency on ``app.agents``.
        """
        self._config = config
        self._payload_schemas: Mapping[str, type[BaseModel]] = payload_schemas or {}

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

    def payload_schema(self, name: str) -> type[BaseModel]:
        """Return the contract a named peer accepts.

        Args:
            name: The peer's name.

        Returns:
            The peer's declared payload model, or :class:`UnknownPeerRequest`
            when this repo has no local contract for it.
        """
        return self._payload_schemas.get(name, UnknownPeerRequest)

    def resolve_peer(self, peer: PeerSpec) -> PeerTool:
        """Build a typed ``PeerTool`` for a single peer spec.

        Args:
            peer: The peer to wrap.

        Returns:
            A ``PeerTool`` addressing the peer's agent card. The card (and the
            peer's real description/capabilities) is resolved lazily by ADK on
            first invocation, so this call does no network I/O.
        """
        remote = RemoteA2aAgent(
            name=peer.name,
            agent_card=self.card_url(peer),
            description=(
                f"Remote specialist agent '{peer.name}' reachable over A2A at "
                f"{peer.base_url}."
            ),
        )
        return PeerTool(
            remote,
            payload_schema=self.payload_schema(peer.name),
            description=(
                f"Delegate a self-contained task to the '{peer.name}' specialist "
                f"agent over A2A. It receives ONLY the fields below — it cannot "
                f"see this conversation — so state everything it needs."
            ),
        )

    def resolve(self, name: str) -> PeerTool:
        """Resolve a single peer by name.

        Args:
            name: The peer name to look up in the configuration.

        Returns:
            The peer as a ``PeerTool``.

        Raises:
            KeyError: If no peer with that name is configured.
        """
        for peer in self._config.peers:
            if peer.name == name:
                return self.resolve_peer(peer)
        raise KeyError(f"No peer named {name!r} in cluster configuration")

    def resolve_all(self) -> list[PeerTool]:
        """Resolve every configured peer into a callable tool.

        Returns:
            A list of ``PeerTool`` instances, one per configured peer (empty
            when no peers are configured).
        """
        return [self.resolve_peer(peer) for peer in self._config.peers]
