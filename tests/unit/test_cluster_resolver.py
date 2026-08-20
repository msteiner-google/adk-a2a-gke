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

"""Unit tests for the AgentResolver (peer -> sub-agent or tool)."""

import pytest
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.tools.agent_tool import AgentTool

from app.cluster.config import ClusterConfig, PeerSpec
from app.cluster.resolver import AgentResolver

_CONFIG = ClusterConfig(
    name="orchestrator",
    namespace="agents",
    cluster_domain="svc.cluster.local",
    peer_scheme="http",
    peer_port=80,
    peers=(
        PeerSpec(name="research", base_url="http://research.agents.svc.cluster.local"),
        PeerSpec(name="math", base_url="http://math.agents.svc.cluster.local"),
    ),
)


def _resolver(config: ClusterConfig = _CONFIG) -> AgentResolver:
    return AgentResolver(config)


def test_a_suspending_peer_resolves_to_a_sub_agent():
    # The wiring in-task authorization depends on. An AgentTool runs the peer
    # to exhaustion against a throwaway session and keeps only its text, so it
    # drops `long_running_tool_ids` -- a peer suspended awaiting a human is
    # then indistinguishable from one that answered with an empty string, and
    # the caller carries on as if nothing were pending. Invisible at runtime
    # until an approval is actually needed, hence the test.
    resolver = AgentResolver(_CONFIG, suspending={"math"})
    subs = resolver.resolve_sub_agents()
    assert [a.name for a in subs] == ["math"]
    assert all(isinstance(a, RemoteA2aAgent) for a in subs)


def test_every_other_peer_resolves_to_a_tool():
    # transfer_to_agent is one-way: the caller's invocation ends at the
    # handoff, so a peer whose answer the caller needs must be a tool.
    resolver = AgentResolver(_CONFIG, suspending={"math"})
    tools = resolver.resolve_tools()
    assert [t.name for t in tools] == ["research"]
    assert all(isinstance(t, AgentTool) for t in tools)


def test_a_peer_is_in_exactly_one_slot():
    # Both slots would mean two ways to reach the same agent, with the model
    # choosing between them per turn.
    resolver = AgentResolver(_CONFIG, suspending={"math"})
    subs = {a.name for a in resolver.resolve_sub_agents()}
    tools = {t.name for t in resolver.resolve_tools()}
    assert subs & tools == set()
    assert subs | tools == {p.name for p in _CONFIG.peers}


def test_resolve_peer_points_remote_agent_at_card_url():
    resolver = _resolver()
    peer = resolver.resolve_peer(_CONFIG.peers[1])
    # RemoteA2aAgent stores the URL string and resolves the card lazily (no
    # network at construction), so we can assert on the stored source.
    assert peer.name == "math"
    assert peer._agent_card_source == resolver.card_url(_CONFIG.peers[1])


def test_resolve_by_name_and_missing_peer():
    resolver = _resolver()
    assert resolver.resolve("research").name == "research"
    with pytest.raises(KeyError):
        resolver.resolve("does-not-exist")


def test_no_peers_resolves_to_nothing_in_either_slot():
    config = ClusterConfig(
        name="research",
        namespace="agents",
        cluster_domain="svc.cluster.local",
        peer_scheme="http",
        peer_port=80,
        peers=(),
    )
    resolver = _resolver(config)
    assert resolver.resolve_sub_agents() == []
    assert resolver.resolve_tools() == []
