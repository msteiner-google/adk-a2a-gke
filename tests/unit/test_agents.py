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

"""Unit tests for the agent registry, the uniform builder, and the tools."""

from types import SimpleNamespace
from typing import cast

import pytest
from google.adk.agents import LlmAgent

from app.agents import AGENTS, DEFAULT_AGENT, PAYLOADS, build_agent
from app.agents.base import AgentSpec
from app.agents.math.tools import calculate
from app.cluster.config import ClusterConfig, PeerSpec
from app.cluster.peer_tool import PeerTool
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


def _resolver(config: ClusterConfig) -> AgentResolver:
    return AgentResolver(config, payload_schemas=PAYLOADS)


def test_registry_lists_expected_agents():
    assert set(AGENTS) == {"orchestrator", "research", "math", "planner"}
    assert DEFAULT_AGENT == "orchestrator"


def test_orchestrator_declares_peers_others_do_not():
    assert AGENTS["orchestrator"].peers == ("research", "math", "planner")
    assert AGENTS["research"].peers == ()
    assert AGENTS["math"].peers == ()
    assert AGENTS["planner"].peers == ()


def test_every_delegatable_agent_declares_a_contract():
    # An agent reachable as a peer but missing from PAYLOADS silently degrades
    # to an untyped request, which is the failure mode D1 exists to prevent.
    delegatable = {name for spec in AGENTS.values() for name in spec.peers}
    assert delegatable <= set(PAYLOADS), delegatable - set(PAYLOADS)


def _tool_names(agent: LlmAgent) -> set[str]:
    """Tool names, whether the tool is a plain callable or a ``BaseTool``."""
    return {
        getattr(t, "name", None) or getattr(t, "__name__", "?") for t in agent.tools
    }


def test_build_agent_leaf_has_only_its_own_tools():
    agent = build_agent(AGENTS["research"], _FAKE_MODELS, _resolver(_CONFIG_NO_PEERS))
    assert isinstance(agent, LlmAgent)
    assert agent.name == "research"
    assert agent.sub_agents == []
    # Names rather than a count: a count breaks whenever a tool is added and
    # says nothing about which tools the agent actually got.
    assert _tool_names(agent) == {"web_search", "read_document"}


def test_build_agent_attaches_resolved_peers_as_tools():
    orchestrator = build_agent(
        AGENTS["orchestrator"], _FAKE_MODELS, _resolver(_CONFIG_WITH_PEERS)
    )
    assert orchestrator.name == "orchestrator"
    assert _tool_names(orchestrator) == {"research", "math"}
    assert all(isinstance(t, PeerTool) for t in orchestrator.tools)


def test_build_agent_never_attaches_peers_as_sub_agents():
    # The invariant behind D1. A peer in sub_agents is reached with
    # transfer_to_agent, which forwards the caller's transcript to it.
    orchestrator = build_agent(
        AGENTS["orchestrator"], _FAKE_MODELS, _resolver(_CONFIG_WITH_PEERS)
    )
    assert orchestrator.sub_agents == []


def test_build_agent_keeps_own_tools_alongside_peers():
    # A coordinating agent that also has local tools must keep both.
    spec = AgentSpec(
        name="hybrid",
        description="x",
        instruction="x",
        tier="fast",
        tools=(calculate,),
        peers=("research",),
    )
    agent = build_agent(spec, _FAKE_MODELS, _resolver(_CONFIG_WITH_PEERS))
    assert _tool_names(agent) == {"calculate", "research", "math"}


def test_build_agent_peers_come_from_config_not_spec():
    # Even a normally-leaf agent gets peers if the config resolved some (e.g. via
    # an A2A_PEERS override): peers are attached uniformly from the resolver.
    agent = build_agent(AGENTS["research"], _FAKE_MODELS, _resolver(_CONFIG_WITH_PEERS))
    assert _tool_names(agent) == {
        "web_search",
        "read_document",
        "research",
        "math",
    }


def test_build_agent_unknown_tier_raises():
    spec = AgentSpec(
        name="broken",
        description="x",
        instruction="x",
        tier="ludicrous",
    )
    with pytest.raises(ValueError, match="tier"):
        build_agent(spec, _FAKE_MODELS, _resolver(_CONFIG_NO_PEERS))


def test_no_agent_carries_a_shared_state_tool():
    # D3: `remember`/`recall` wrote `shared:` session state and were documented
    # as propagating across A2A hops. They did not (docs/design-decisions.md D3),
    # leaving that pattern in place teaches callers to rely on implicit context.
    for spec in AGENTS.values():
        names = {
            getattr(t, "name", None) or getattr(t, "__name__", "?") for t in spec.tools
        }
        assert not names & {"remember", "recall"}, spec.name


def test_calculate_evaluates_arithmetic():
    assert calculate("(2 + 3) * 4")["result"] == "20.0"


def test_calculate_rejects_non_arithmetic():
    result = calculate("__import__('os').system('echo hi')")
    assert result["status"] == "error"
