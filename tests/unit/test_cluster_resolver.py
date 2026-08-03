"""Unit tests for the AgentResolver (peer -> RemoteA2aAgent)."""

import pytest
from google.adk.agents.remote_a2a_agent import (
    AGENT_CARD_WELL_KNOWN_PATH,
    RemoteA2aAgent,
)

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


def test_resolve_all_returns_remote_agents_for_each_peer():
    resolver = AgentResolver(_CONFIG)
    remotes = resolver.resolve_all()
    assert [r.name for r in remotes] == ["research", "math"]
    assert all(isinstance(r, RemoteA2aAgent) for r in remotes)


def test_card_url_appends_rpc_path_and_well_known_path():
    resolver = AgentResolver(_CONFIG)
    peer = _CONFIG.peers[0]
    # The base serving mounts the card under /a2a/app, so the resolver must
    # compose base_url + rpc_path + well-known path (NOT the service root).
    assert resolver.card_url(peer) == (
        f"http://research.agents.svc.cluster.local/a2a/app{AGENT_CARD_WELL_KNOWN_PATH}"
    )


def test_card_url_honors_custom_rpc_path():
    from dataclasses import replace

    resolver = AgentResolver(replace(_CONFIG, rpc_path="/custom/rpc"))
    assert resolver.card_url(_CONFIG.peers[0]) == (
        f"http://research.agents.svc.cluster.local/custom/rpc{AGENT_CARD_WELL_KNOWN_PATH}"
    )


def test_resolve_peer_points_remote_agent_at_card_url():
    resolver = AgentResolver(_CONFIG)
    remote = resolver.resolve_peer(_CONFIG.peers[1])
    # RemoteA2aAgent stores the URL string and resolves the card lazily (no
    # network at construction), so we can assert on the stored source.
    assert remote.name == "math"
    assert remote._agent_card_source == resolver.card_url(_CONFIG.peers[1])


def test_resolve_by_name_and_missing_peer():
    resolver = AgentResolver(_CONFIG)
    assert resolver.resolve("research").name == "research"
    with pytest.raises(KeyError):
        resolver.resolve("does-not-exist")


def test_resolve_all_empty_when_no_peers():
    config = ClusterConfig(
        name="research",
        namespace="agents",
        cluster_domain="svc.cluster.local",
        peer_scheme="http",
        peer_port=80,
        peers=(),
    )
    assert AgentResolver(config).resolve_all() == []
