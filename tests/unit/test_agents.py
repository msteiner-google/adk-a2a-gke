"""Unit tests for the agent registry, the uniform builder, and the tools."""

from types import SimpleNamespace
from typing import cast

import pytest
from google.adk.agents import LlmAgent
from google.adk.tools import ToolContext

from app.agents import AGENTS, DEFAULT_AGENT, build_agent
from app.agents.base import AgentSpec
from app.agents.common import recall, remember
from app.agents.math.tools import calculate
from app.cluster.config import ClusterConfig, PeerSpec
from app.cluster.resolver import AgentResolver
from app.shared.config import Models

# A fake model bundle: ADK's LlmAgent accepts a model *name* string, so we can
# avoid hitting the live model catalog (keeps these tests hermetic).
_FAKE_MODELS = cast(
    Models,
    SimpleNamespace(
        fast="gemini-2.5-flash-lite",
        balanced="gemini-2.5-flash",
        capable="gemini-2.5-pro",
    ),
)

# A config whose peers make the built agent a coordinator.
_CONFIG_WITH_PEERS = ClusterConfig(
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

_CONFIG_NO_PEERS = ClusterConfig(
    name="research",
    namespace="agents",
    cluster_domain="svc.cluster.local",
    peer_scheme="http",
    peer_port=80,
    peers=(),
)


def test_registry_lists_expected_agents():
    assert set(AGENTS) == {"orchestrator", "research", "math"}
    assert DEFAULT_AGENT == "orchestrator"


def test_orchestrator_declares_peers_others_do_not():
    assert AGENTS["orchestrator"].peers == ("research", "math")
    assert AGENTS["research"].peers == ()
    assert AGENTS["math"].peers == ()


def test_build_agent_leaf_has_no_sub_agents():
    resolver = AgentResolver(_CONFIG_NO_PEERS)
    agent = build_agent(AGENTS["research"], _FAKE_MODELS, resolver)
    assert isinstance(agent, LlmAgent)
    assert agent.name == "research"
    assert agent.sub_agents == []
    assert len(agent.tools) == 3


def test_build_agent_attaches_resolved_peers_as_sub_agents():
    resolver = AgentResolver(_CONFIG_WITH_PEERS)
    orchestrator = build_agent(AGENTS["orchestrator"], _FAKE_MODELS, resolver)
    assert orchestrator.name == "orchestrator"
    assert [a.name for a in orchestrator.sub_agents] == ["research", "math"]


def test_build_agent_peers_come_from_config_not_spec():
    # Even a normally-leaf agent gets peers if the config resolved some (e.g. via
    # an A2A_PEERS override): peers are attached uniformly from the resolver.
    resolver = AgentResolver(_CONFIG_WITH_PEERS)
    agent = build_agent(AGENTS["research"], _FAKE_MODELS, resolver)
    assert [a.name for a in agent.sub_agents] == ["research", "math"]


def test_build_agent_unknown_tier_raises():
    spec = AgentSpec(
        name="broken",
        description="x",
        instruction="x",
        tier="ludicrous",
    )
    with pytest.raises(ValueError, match="tier"):
        build_agent(spec, _FAKE_MODELS, AgentResolver(_CONFIG_NO_PEERS))


def test_calculate_evaluates_arithmetic():
    assert calculate("(2 + 3) * 4")["result"] == "20.0"


def test_calculate_rejects_non_arithmetic():
    result = calculate("__import__('os').system('echo hi')")
    assert result["status"] == "error"


def test_remember_and_recall_roundtrip():
    ctx = cast(ToolContext, SimpleNamespace(state={}))
    remember("topic", "penguins", ctx)
    assert recall("topic", ctx) == {
        "status": "found",
        "key": "topic",
        "value": "penguins",
    }


def test_recall_missing_key():
    ctx = cast(ToolContext, SimpleNamespace(state={}))
    assert recall("absent", ctx)["status"] == "not_found"
