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

"""Resolve cluster peers into remote sub-agents.

The ``AgentResolver`` is the injectable "service discovery" layer for the
cluster: given the ``ClusterConfig`` (which peers exist and where), it produces
the ``RemoteA2aAgent`` instances a coordinating agent delegates to over A2A.

Discovery is **agent-card based**: for each peer we point ``RemoteA2aAgent`` at
the peer's well-known agent-card URL. The A2A serving layer mounts the JSON-RPC
endpoint and the agent card under ``/a2a/<app_name>`` (``/a2a/app`` by default,
see ``ClusterConfig.rpc_path``), so the card lives at
``<base_url>/a2a/app/.well-known/agent-card.json`` — NOT at the service root.
A peer's ``base_url`` is therefore the service root (scheme + host [+ port]); the
resolver appends the RPC path and the well-known card path to it. ADK fetches and
validates that card lazily on first use, so constructing the remote agents here
does no network I/O.

**How a peer is wired depends on whether it can suspend.** Neither wiring does
both jobs, so the resolver picks per peer:

* A peer that owns a gated tool becomes a **sub-agent**. In-task authorization
  (A2A spec 7.6) suspends the specialist's Task in
  ``TASK_STATE_AUTH_REQUIRED``, and for that to reach a human every agent in
  between has to suspend too — "a chain of Tasks in
  ``TASK_STATE_AUTH_REQUIRED``" (spec 7.6.2). ``AgentTool`` cannot carry that
  chain: it runs the peer to exhaustion against a throwaway session and keeps
  only ``state_delta``, ``error_message`` and ``content``
  (``google/adk/tools/agent_tool.py``), never ``long_running_tool_ids``. A
  suspended peer is then indistinguishable from one that answered with an empty
  string. As a sub-agent, ``RemoteA2aAgent`` propagates the suspension and
  records the remote ``task_id``, which is what makes the request reach a human
  at all.

* Every other peer stays an **``AgentTool``**, because ``transfer_to_agent`` is
  a one-way handoff: the caller's invocation *ends*. Measured here — ``math``
  transferred to ``currency``, currency answered, and math never resumed to add
  the converted figure or publish it. A peer whose result the caller needs must
  therefore be a tool, and a peer that can suspend must not be.

The split is derived from the peers' own tools rather than declared twice: see
``app/agents/gating.py`` and ``ClusterModule`` in ``app/cluster/di.py``. The
cost of the sub-agent half, accepted deliberately, is that
``transfer_to_agent`` carries no arguments — a specialist is reached with the
caller's recent conversation rather than a typed payload, so it sees context it
has no need for. ``docs/design-decisions.md`` records the original measurement
against that and why authorization overrode it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from google.adk.agents.remote_a2a_agent import (
    AGENT_CARD_WELL_KNOWN_PATH,
    RemoteA2aAgent,
)
from google.adk.tools.agent_tool import AgentTool

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.cluster.config import ClusterConfig, PeerSpec


class AgentResolver:
    """Resolves configured peers into ``RemoteA2aAgent`` sub-agents.

    Construct it with a ``ClusterConfig`` (usually via dependency injection —
    see ``app/cluster/di.py``), then call :meth:`resolve_all` to get every peer
    as a sub-agent.
    """

    def __init__(
        self, config: ClusterConfig, suspending: Iterable[str] | None = None
    ) -> None:
        """Initialize the resolver.

        Args:
            config: The resolved cluster configuration listing reachable peers.
            suspending: Names of the peers that own a gated tool and can
                therefore suspend awaiting a human. Passed in rather than
                imported so this module keeps knowing nothing about
                ``app.agents`` — importing it would cycle through
                ``agents.base``. A peer not named here is reached as a tool.
        """
        self._config = config
        self._suspending = frozenset(suspending or ())

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
            peer: The peer to address.

        Returns:
            A ``RemoteA2aAgent`` pointed at the peer's agent card. The card
            (and the peer's real description/capabilities) is resolved lazily by
            ADK on first invocation, so this call does no network I/O.
        """
        return RemoteA2aAgent(
            name=peer.name,
            agent_card=self.card_url(peer),
            description=(
                f"Remote specialist agent '{peer.name}' reachable over A2A at "
                f"{peer.base_url}."
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

    def suspends(self, name: str) -> bool:
        """Return whether a peer can suspend awaiting human authorization.

        Args:
            name: The peer's name.

        Returns:
            True when that peer owns a gated tool.
        """
        return name in self._suspending

    def resolve_sub_agents(self) -> list[RemoteA2aAgent]:
        """Resolve the peers that must be sub-agents.

        Returns:
            A ``RemoteA2aAgent`` per peer that can suspend, so an authorization
            request it raises propagates to this agent instead of being
            swallowed.
        """
        return [
            self.resolve_peer(peer)
            for peer in self._config.peers
            if self.suspends(peer.name)
        ]

    def resolve_tools(self) -> list[AgentTool]:
        """Resolve the peers that must be tools.

        Returns:
            An ``AgentTool`` per peer that cannot suspend, so the caller gets
            its answer back and can carry on — which ``transfer_to_agent``
            would not allow.
        """
        return [
            AgentTool(self.resolve_peer(peer))
            for peer in self._config.peers
            if not self.suspends(peer.name)
        ]
