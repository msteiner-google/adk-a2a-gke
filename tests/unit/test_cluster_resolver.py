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

"""Unit tests for the AgentResolver (peer -> typed PeerTool over A2A)."""

import pytest
from google.adk.agents.remote_a2a_agent import (
    AGENT_CARD_WELL_KNOWN_PATH,
    RemoteA2aAgent,
)

from app.agents.contracts import PAYLOADS, MathRequest, ResearchRequest
from app.cluster.config import ClusterConfig, PeerSpec
from app.cluster.peer_tool import PeerTool
from app.cluster.resolver import AgentResolver, UnknownPeerRequest

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
    return AgentResolver(config, payload_schemas=PAYLOADS)


def test_resolve_all_returns_a_tool_per_peer():
    tools = _resolver().resolve_all()
    assert [t.name for t in tools] == ["research", "math"]
    assert all(isinstance(t, PeerTool) for t in tools)


def test_peers_are_tools_not_sub_agents():
    # The whole of D1: a peer reached as a sub-agent gets the caller's
    # transcript (docs/design-decisions.md D1), a peer as a tool gets only the
    # payload. Guard the wiring, since the difference is invisible at runtime
    # until something leaks.
    tool = _resolver().resolve_all()[0]
    assert isinstance(tool, PeerTool)
    assert isinstance(tool.agent, RemoteA2aAgent)


def test_each_peer_carries_its_declared_contract():
    tools = {t.name: t for t in _resolver().resolve_all()}
    assert tools["research"].payload_schema is ResearchRequest
    assert tools["math"].payload_schema is MathRequest


def test_unknown_peer_falls_back_to_a_generic_contract():
    # A peer configured by URL that this repo has no local model for: still an
    # explicit request, just not a domain-specific one.
    config = ClusterConfig(
        name="orchestrator",
        namespace="agents",
        cluster_domain="svc.cluster.local",
        peer_scheme="http",
        peer_port=80,
        peers=(PeerSpec(name="extraction", base_url="http://extraction.other"),),
    )
    tool = _resolver(config).resolve_all()[0]
    assert tool.payload_schema is UnknownPeerRequest
    assert set(UnknownPeerRequest.model_fields) == {"case_id", "task"}


def test_declaration_exposes_the_contract_fields_not_a_free_text_request():
    # AgentTool's default declaration is a single `request` string, which would
    # be a transcript in one field. The typed declaration is what makes the
    # delegation functional.
    tools = {t.name: t for t in _resolver().resolve_all()}
    declaration = tools["research"]._get_declaration()
    schema = declaration.parameters_json_schema
    assert isinstance(schema, dict)
    assert declaration.name == "research"
    assert set(schema["properties"]) == {
        "case_id",
        "document_refs",
        "question",
        "constraints",
    }
    assert sorted(schema["required"]) == ["case_id", "question"]
    assert "request" not in schema["properties"]


def test_card_url_appends_rpc_path_and_well_known_path():
    resolver = _resolver()
    peer = _CONFIG.peers[0]
    # The base serving mounts the card under /a2a/app, so the resolver must
    # compose base_url + rpc_path + well-known path (NOT the service root).
    assert resolver.card_url(peer) == (
        f"http://research.agents.svc.cluster.local/a2a/app{AGENT_CARD_WELL_KNOWN_PATH}"
    )


def test_card_url_honors_custom_rpc_path():
    from dataclasses import replace

    resolver = _resolver(replace(_CONFIG, rpc_path="/custom/rpc"))
    assert resolver.card_url(_CONFIG.peers[0]) == (
        f"http://research.agents.svc.cluster.local/custom/rpc{AGENT_CARD_WELL_KNOWN_PATH}"
    )


def test_resolve_peer_points_remote_agent_at_card_url():
    resolver = _resolver()
    tool = resolver.resolve_peer(_CONFIG.peers[1])
    # RemoteA2aAgent stores the URL string and resolves the card lazily (no
    # network at construction), so we can assert on the stored source.
    assert tool.name == "math"
    assert tool.agent._agent_card_source == resolver.card_url(_CONFIG.peers[1])


def test_resolve_by_name_and_missing_peer():
    resolver = _resolver()
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
    assert _resolver(config).resolve_all() == []
