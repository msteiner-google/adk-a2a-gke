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
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

from app.agents import AGENTS, DEFAULT_AGENT, build_agent, suspending_agents
from app.agents.base import AgentSpec
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


def _resolver(config: ClusterConfig) -> AgentResolver:
    # The real derivation, so the tests exercise the wiring the cluster uses.
    return AgentResolver(config, suspending=suspending_agents())


def test_registry_lists_expected_agents():
    assert set(AGENTS) == {
        "orchestrator",
        "research",
        "math",
        "planner",
        "trades",
        "currency",
    }
    assert DEFAULT_AGENT == "orchestrator"


def test_declared_peer_topology():
    # Delegation is a graph, not a one-deep fan-out: `math` declares `currency`,
    # so orchestrator -> math -> currency is a live chain with no special
    # handling anywhere. This asserts the shape the NetworkPolicy has to mirror
    # (infra/kustomize/base/networkpolicy.yaml) -- an edge added here and missed
    # there fails as a connection timeout rather than an error.
    assert AGENTS["orchestrator"].peers == ("research", "math", "planner", "trades")
    assert AGENTS["math"].peers == ("currency",)
    assert AGENTS["research"].peers == ()
    assert AGENTS["planner"].peers == ()
    assert AGENTS["trades"].peers == ()
    assert AGENTS["currency"].peers == ()


def test_every_declared_peer_is_a_registered_agent():
    # A peer naming an agent that does not exist resolves to a card URL nothing
    # serves, and fails at first delegation as a connection error rather than
    # at startup.
    delegatable = {name for spec in AGENTS.values() for name in spec.peers}
    assert delegatable <= set(AGENTS), delegatable - set(AGENTS)


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


def test_a_peer_that_can_suspend_is_a_sub_agent():
    # An AgentTool runs the peer to exhaustion and drops long_running_tool_ids,
    # so a peer suspended awaiting human authorization is indistinguishable
    # from one that answered with an empty string -- the authorization request
    # never reaches anyone. `math` owns a gated tool, so it must be here.
    orchestrator = build_agent(
        AGENTS["orchestrator"], _FAKE_MODELS, _resolver(_CONFIG_WITH_PEERS)
    )
    assert orchestrator.name == "orchestrator"
    assert {a.name for a in orchestrator.sub_agents} == {"math"}
    assert all(isinstance(a, RemoteA2aAgent) for a in orchestrator.sub_agents)


def test_a_peer_that_cannot_suspend_is_a_tool():
    # transfer_to_agent is a one-way handoff: the caller's invocation ends, so
    # it can never use what the peer returned. Measured -- math transferred to
    # currency and never resumed to finish the sum. A peer with nothing to
    # suspend belongs in `tools`, where its answer comes back.
    orchestrator = build_agent(
        AGENTS["orchestrator"], _FAKE_MODELS, _resolver(_CONFIG_WITH_PEERS)
    )
    assert _tool_names(orchestrator) == {"research"}


def test_the_split_is_derived_from_the_gated_tools():
    # Marking a tool @gated is the single act that wires its agent as a
    # sub-agent everywhere. Anything else is two places to keep in sync.
    assert suspending_agents() == {"math", "trades"}


def test_every_tool_that_asks_for_approval_is_marked_gated():
    # The dangerous omission: a new gated tool without @gated leaves its agent
    # wired as an AgentTool, and every authorization request it raises is
    # swallowed in silence. Cross-check the marker against the source rather
    # than trusting it to be remembered.
    import inspect
    from typing import Any, cast

    from app.agents.gating import is_gated

    for name, spec in AGENTS.items():
        for tool in spec.tools:
            func = getattr(tool, "func", tool)
            try:
                source = inspect.getsource(cast(Any, func))
            except OSError, TypeError:  # pragma: no cover - builtins
                continue
            if "require_approval(" in source and "def require_approval" not in source:
                assert is_gated(tool), (
                    f"{name}.{getattr(func, '__name__', tool)} calls "
                    "require_approval but is not marked @gated"
                )


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
    assert _tool_names(agent) == {"calculate", "research"}
    assert {a.name for a in agent.sub_agents} == {"math"}


def test_build_agent_peers_come_from_config_not_spec():
    # Even a normally-leaf agent gets peers if the config resolved some (e.g. via
    # an A2A_PEERS override): peers are attached uniformly from the resolver.
    agent = build_agent(AGENTS["research"], _FAKE_MODELS, _resolver(_CONFIG_WITH_PEERS))
    assert _tool_names(agent) == {"web_search", "read_document", "research"}
    assert {a.name for a in agent.sub_agents} == {"math"}


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
