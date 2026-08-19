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

"""The currency specialist, and the second-tier delegation it demonstrates.

Two things are being guarded here. The conversions themselves — including the
property the USD-anchored table exists to give, that no triangle of rates
disagrees with itself — and the wiring that lets ``math`` reach this agent at
all, which is the first time this repo has a peer that is not the
orchestrator's.
"""

from types import SimpleNamespace
from typing import cast

from app.agents import AGENTS, PAYLOADS, build_agent
from app.agents.contracts import CurrencyRequest
from app.agents.currency.tools import (
    RATES_AS_OF,
    convert_currency,
    list_supported_currencies,
    supported_currencies,
)
from app.cluster.config import ClusterConfig, PeerSpec
from app.cluster.peer_tool import PeerTool
from app.cluster.resolver import AgentResolver
from app.shared.config import Models

_FAKE_MODELS = cast(
    Models,
    SimpleNamespace(
        fast="gemini-2.5-flash-lite",
        balanced="gemini-2.5-flash",
        capable="gemini-2.5-pro",
    ),
)

# What the math agent's process sees: its own declared peer, resolved.
_MATH_CONFIG = ClusterConfig(
    name="math",
    namespace="agents",
    cluster_domain="svc.cluster.local",
    peer_scheme="http",
    peer_port=80,
    peers=(
        PeerSpec(name="currency", base_url="http://currency.agents.svc.cluster.local"),
    ),
)


# --- The conversions ----------------------------------------------------------


def test_converting_to_the_same_currency_changes_nothing():
    out = convert_currency(100.0, "USD", "USD")
    assert out["status"] == "ok"
    assert out["converted"] == "100.00"
    assert out["rate"] == "1.000000"


def test_a_conversion_reports_the_rate_and_when_it_is_from():
    # A converted figure with no provenance is the thing this agent must never
    # emit: the caller has to be able to say where the number came from.
    out = convert_currency(250.0, "eur", "usd")
    assert out["from_currency"] == "EUR"
    assert out["to_currency"] == "USD"
    assert out["converted"] == "272.50"
    assert out["rate_source"] == "hardcoded"
    assert out["as_of"] == RATES_AS_OF


def test_codes_are_case_and_whitespace_insensitive():
    assert convert_currency(1.0, " gbp ", "usd")["converted"] == "1.27"


def test_no_triangle_of_rates_disagrees_with_itself():
    # The reason rates are stored as one USD price per currency rather than as
    # quoted pairs: a matrix can hold EUR->GBP->USD landing somewhere other
    # than EUR->USD, and the arbitrage it invents is invisible in review.
    direct = float(convert_currency(1000.0, "EUR", "GBP")["converted"])
    via_usd = float(
        convert_currency(
            float(convert_currency(1000.0, "EUR", "USD")["converted"]), "USD", "GBP"
        )["converted"]
    )
    assert abs(direct - via_usd) < 0.01


def test_a_currency_without_minor_units_is_rounded_to_whole_numbers():
    out = convert_currency(100.0, "USD", "JPY")
    assert "." not in out["converted"]
    assert out["converted"] == "14925"


def test_a_rate_is_quoted_more_precisely_than_an_amount():
    # USD/JPY rounded to 2 decimals is 0.01, which is not a rate.
    assert convert_currency(1.0, "USD", "JPY")["rate"] == "149.253731"


def test_an_unsupported_code_is_refused_by_name():
    out = convert_currency(10.0, "USD", "XYZ")
    assert out["status"] == "error"
    assert "XYZ" in out["error"]
    # The model is told what it CAN do, so its next turn is a useful one.
    assert "EUR" in out["supported"]


def test_an_unsupported_code_does_not_get_silently_substituted():
    assert convert_currency(10.0, "ZZZ", "QQQ")["status"] == "error"


def test_the_supported_list_matches_the_rate_table():
    listed = list_supported_currencies()
    assert listed["status"] == "ok"
    assert listed["count"] == str(len(supported_currencies()))
    assert set(listed["supported"].split(", ")) == set(supported_currencies())


# --- The wiring ---------------------------------------------------------------


def test_the_currency_agent_is_a_leaf_with_its_own_tools():
    agent = build_agent(
        AGENTS["currency"],
        _FAKE_MODELS,
        AgentResolver(
            ClusterConfig(
                name="currency",
                namespace="agents",
                cluster_domain="svc.cluster.local",
                peer_scheme="http",
                peer_port=80,
                peers=(),
            ),
            payload_schemas=PAYLOADS,
        ),
    )
    assert agent.sub_agents == []
    names = {
        getattr(t, "name", None) or getattr(t, "__name__", "?") for t in agent.tools
    }
    assert names == {"convert_currency", "list_supported_currencies"}


def test_math_reaches_currency_as_a_typed_tool_not_a_sub_agent():
    # The D1 invariant has to hold at every depth, not just below the
    # orchestrator. A peer in `sub_agents` is reached with transfer_to_agent,
    # which rebuilds the outbound message from the CALLER's session events --
    # here that would hand the currency agent the math agent's transcript.
    agent = build_agent(
        AGENTS["math"],
        _FAKE_MODELS,
        AgentResolver(_MATH_CONFIG, payload_schemas=PAYLOADS),
    )
    assert agent.sub_agents == []
    peers = [t for t in agent.tools if isinstance(t, PeerTool)]
    assert [t.name for t in peers] == ["currency"]
    assert peers[0].payload_schema is CurrencyRequest


def test_the_currency_contract_names_the_conversion_and_nothing_else():
    # No free-text field: the moment one exists, a caller starts pasting the
    # conversation into it (see app/agents/contracts.py).
    fields = set(CurrencyRequest.model_fields)
    assert fields == {
        "case_id",
        "document_refs",
        "amount",
        "from_currency",
        "to_currency",
    }
