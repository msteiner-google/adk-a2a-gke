"""Unit tests for the pure cluster configuration parsing."""

from app.cluster.config import (
    DEFAULT_AGENT_NAME,
    DEFAULT_PEER_PORT,
    DEFAULT_RPC_PATH,
    ClusterConfig,
    service_dns_url,
)


def test_defaults_when_env_empty():
    config = ClusterConfig.from_env(env={}, default_peers=("research", "math"))
    assert config.name == DEFAULT_AGENT_NAME
    assert config.namespace == "agents"
    assert config.peer_port == DEFAULT_PEER_PORT
    # With A2A_PEERS unset, the agent falls back to its declared default peers.
    assert [p.name for p in config.peers] == ["research", "math"]


def test_name_read_from_env():
    config = ClusterConfig.from_env(env={"AGENT_NAME": "research"})
    assert config.name == "research"


def test_explicit_name_overrides_env():
    config = ClusterConfig.from_env(env={"AGENT_NAME": "research"}, name="math")
    assert config.name == "math"


def test_no_default_peers_means_no_peers():
    # A leaf agent passes no default peers (its AgentSpec.peers is empty), so with
    # A2A_PEERS unset it resolves to no peers.
    config = ClusterConfig.from_env(env={"AGENT_NAME": "research"}, default_peers=())
    assert config.peers == ()


def test_a2a_peers_overrides_default_peers():
    config = ClusterConfig.from_env(
        env={"A2A_PEERS": "math"}, default_peers=("research", "math")
    )
    assert [p.name for p in config.peers] == ["math"]


def test_dns_urls_derived_for_bare_peer_names():
    config = ClusterConfig.from_env(
        env={"A2A_PEERS": "research,math", "A2A_NAMESPACE": "team"},
    )
    urls = {p.name: p.base_url for p in config.peers}
    assert urls["research"] == "http://research.team.svc.cluster.local"
    assert urls["math"] == "http://math.team.svc.cluster.local"


def test_explicit_peer_urls_are_respected():
    config = ClusterConfig.from_env(
        env={"A2A_PEERS": "research=https://research.example.com,math"},
    )
    urls = {p.name: p.base_url for p in config.peers}
    assert urls["research"] == "https://research.example.com"
    assert urls["math"] == "http://math.agents.svc.cluster.local"


def test_peers_are_deduplicated_preserving_order():
    config = ClusterConfig.from_env(env={"A2A_PEERS": "a, b , a"})
    assert [p.name for p in config.peers] == ["a", "b"]


def test_non_default_port_is_included_in_url():
    assert (
        service_dns_url("research", namespace="agents", port=8080)
        == "http://research.agents.svc.cluster.local:8080"
    )


def test_default_ports_are_omitted():
    assert (
        service_dns_url("x", scheme="http", port=80)
        == "http://x.agents.svc.cluster.local"
    )
    assert (
        service_dns_url("x", scheme="https", port=443)
        == "https://x.agents.svc.cluster.local"
    )


def test_invalid_port_falls_back_to_default():
    config = ClusterConfig.from_env(env={"A2A_PEER_PORT": "not-a-number"})
    assert config.peer_port == DEFAULT_PEER_PORT


def test_rpc_path_defaults_to_a2a_app():
    config = ClusterConfig.from_env(env={})
    assert config.rpc_path == DEFAULT_RPC_PATH == "/a2a/app"


def test_rpc_path_override_is_normalized():
    # Leading/trailing slashes are normalized to a single leading slash.
    config = ClusterConfig.from_env(env={"A2A_RPC_PATH": "custom/rpc/"})
    assert config.rpc_path == "/custom/rpc"
