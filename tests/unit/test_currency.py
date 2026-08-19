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

import pytest

from app.agents import AGENTS, PAYLOADS, build_agent
from app.agents.contracts import (
    NEEDS_CONFIRMATION,
    NEEDS_INPUT,
    CurrencyRequest,
    MathRequest,
)
from app.agents.currency.tools import (
    RATES_AS_OF,
    convert_currency,
    list_supported_currencies,
    resolve_currency,
    supported_currencies,
)
from app.agents.reporting import AUDITED_STATUSES
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
    # Every code is listed, and each carries its full name -- the list is shown
    # to a user who has just been asked which "dollars" they meant, so bare
    # three-letter codes would be a worse answer than no list.
    for code in supported_currencies():
        assert f"{code} (" in listed["supported"]
    # And it advertises the threshold, so the caller can say why it will ask.
    assert listed["threshold_usd"] == "1,000,000.00"


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
    # conversation into it (see app/agents/contracts.py). `confirmed` is the
    # user's answer to a question this agent asked, not a place for context.
    fields = set(CurrencyRequest.model_fields)
    assert fields == {
        "case_id",
        "document_refs",
        "amount",
        "from_currency",
        "to_currency",
        "confirmed",
    }


# --- Refusing rather than guessing --------------------------------------------


def test_dollars_is_a_question_not_a_default():
    # The whole point. Six real currencies are called "dollars", and resolving
    # that silently is a decision taken in the one place with no rate table.
    out = convert_currency(100.0, "dollars", "EUR")
    assert out["status"] == NEEDS_INPUT
    assert out["field"] == "from_currency"
    assert set(out["candidates"]) == {"USD", "CAD", "AUD", "NZD", "SGD", "HKD"}
    assert "converted" not in out
    # The question is written for a person, not for a log.
    assert "United States dollar" in out["question"]


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("dollars", {"USD", "CAD", "AUD", "NZD", "SGD", "HKD"}),
        ("$", {"USD", "CAD", "AUD", "NZD", "SGD", "HKD"}),
        ("krona", {"SEK", "NOK", "DKK"}),
        ("crowns", {"SEK", "NOK", "DKK"}),
        ("\u00a5", {"JPY", "CNY"}),
    ],
)
def test_every_ambiguous_family_asks(term: str, expected: set[str]):
    out = convert_currency(10.0, term, "EUR")
    assert out["status"] == NEEDS_INPUT
    assert set(out["candidates"]) == expected


@pytest.mark.parametrize(
    ("term", "code"),
    [
        ("euros", "EUR"),
        ("pounds", "GBP"),
        ("yen", "JPY"),
        ("rand", "ZAR"),
        ("usd", "USD"),
        ("  EUR  ", "EUR"),
    ],
)
def test_an_unambiguous_word_is_just_resolved(term: str, code: str):
    assert resolve_currency(term) == (code, ())
    assert convert_currency(10.0, term, "USD")["from_currency"] == code


def test_ambiguity_in_the_target_is_caught_too():
    out = convert_currency(10.0, "EUR", "dollars")
    assert out["status"] == NEEDS_INPUT
    assert out["field"] == "to_currency"


def test_a_large_amount_asks_before_converting():
    out = convert_currency(2_000_000.0, "USD", "EUR")
    assert out["status"] == NEEDS_CONFIRMATION
    assert out["reason"] == "large_amount"
    assert "converted" not in out
    assert "2,000,000.00" in out["question"]


def test_the_threshold_is_in_usd_not_face_value():
    # 1,000,000 EUR is 1.09m USD, so it trips; 900,000 USD does not.
    assert convert_currency(1_000_000.0, "EUR", "USD")["status"] == NEEDS_CONFIRMATION
    assert convert_currency(900_000.0, "USD", "EUR")["status"] == "ok"


def test_a_large_negative_amount_also_asks():
    # A sign error is exactly as worth catching as a decimal-point error.
    assert convert_currency(-2_000_000.0, "USD", "EUR")["status"] == NEEDS_CONFIRMATION


def test_confirmation_lets_a_large_amount_through():
    out = convert_currency(2_000_000.0, "USD", "EUR", confirmed=True)
    assert out["status"] == "ok"
    assert out["converted"] == "1834862.39"


def test_confirmation_cannot_settle_an_ambiguity():
    # `confirmed` answers "is this amount right", never "which currency".
    # A currency nobody named cannot be confirmed into existence.
    out = convert_currency(2_000_000.0, "dollars", "EUR", confirmed=True)
    assert out["status"] == NEEDS_INPUT


def test_a_question_is_not_an_error():
    # These must stay distinguishable: an error means the request was wrong, a
    # question means it was incomplete, and the caller does different things.
    assert convert_currency(1.0, "XYZ", "USD")["status"] == "error"
    assert convert_currency(1.0, "dollars", "USD")["status"] == NEEDS_INPUT


def test_both_questions_survive_the_a2a_boundary():
    # reporting.py restates these verbatim; if a status is missing from
    # AUDITED_STATUSES the question reaches the user only if a model happens to
    # repeat it -- the dependency that callback exists to remove.
    assert NEEDS_INPUT in AUDITED_STATUSES
    assert NEEDS_CONFIRMATION in AUDITED_STATUSES


def test_the_contract_carries_the_users_answer_back_down():
    # Ambiguity is answered by naming the currency; a large amount by a flag.
    # Both travel through math, which is why MathRequest mirrors the flag.
    assert (
        CurrencyRequest(
            case_id="c1", amount=1.0, from_currency="dollars", to_currency="EUR"
        ).confirmed
        is False
    )
    assert MathRequest(case_id="c1", expression="1+1").currency_confirmed is False
    assert (
        MathRequest(
            case_id="c1", expression="1+1", currency_confirmed=True
        ).currency_confirmed
        is True
    )
