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
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.adk.tools.tool_context import ToolContext

from app.agents import AGENTS, build_agent, suspending_agents
from app.agents.currency.tools import (
    RATES_AS_OF,
    convert_currency,
    convert_to_crypto,
    list_supported_currencies,
    resolve_currency,
    supported_crypto_assets,
    supported_currencies,
)
from app.agents.reporting import AUDITED_STATUSES
from app.agents.statuses import (
    AWAITING_APPROVAL,
    CONVERTED,
    EFFECT_PERFORMED,
    NEEDS_CONFIRMATION,
    NEEDS_INPUT,
    REFUSED,
)
from app.cluster.config import ClusterConfig, PeerSpec
from app.cluster.resolver import AgentResolver
from app.shared.config import Models
from tests.unit.conftest import FakeToolContext

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
        ),
    )
    assert agent.sub_agents == []
    names = {
        getattr(t, "name", None) or getattr(t, "__name__", "?") for t in agent.tools
    }
    assert names == {
        "convert_currency",
        "convert_to_crypto",
        "list_supported_currencies",
    }


def test_math_still_gets_currency_s_answer_back_now_that_it_can_suspend():
    # currency owns a gated tool (`convert_to_crypto`), so it has to be a
    # sub-agent or the authorization request it raises is swallowed. That used
    # to be mutually exclusive with math finishing the sum: transfer_to_agent
    # ended the caller's invocation, so math never saw the converted number.
    # Task-mode delegation is what makes both true at once -- ADK wraps the
    # sub-agent in a _TaskAgentTool, so the pause propagates AND the answer
    # comes back. Assert both halves: losing either is silent.
    agent = build_agent(
        AGENTS["math"],
        _FAKE_MODELS,
        AgentResolver(_MATH_CONFIG, suspending=suspending_agents()),
    )
    assert {a.name for a in agent.sub_agents} == {"currency"}
    tool_names = {
        getattr(t, "name", None) or getattr(t, "__name__", "?") for t in agent.tools
    }
    assert "currency" in tool_names


def test_currency_is_wired_as_a_suspending_peer():
    # The rule that decides the slot above, and why it flipped: a gated crypto
    # quote must be able to stop and ask a human two hops up.
    assert "currency" in suspending_agents()


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


def test_no_instruction_on_the_path_to_currency_resolves_the_word_itself():
    # Regression, found in the cluster and not by any test here. "dollars" is
    # six currencies, and only this agent holds the list -- so any agent above
    # it that maps the word to a code silently answers a question that should
    # have been asked. It happened once already, one hop up in math.
    #
    # This used to assert on the peer contracts' field descriptions. Those are
    # gone (peers are sub-agents now, and transfer_to_agent carries no typed
    # arguments), so the same rule now lives where a model will actually read
    # it: the instruction text. Asserting on prose is unusual and correct here
    # -- with no schema in between, the instruction IS the interface.
    math_instruction = AGENTS["math"].instruction.lower()
    assert "user's own word" in math_instruction

    currency_instruction = AGENTS["currency"].instruction.lower()
    assert "resolving it yourself" in currency_instruction


def test_money_reaches_the_currency_specialist_even_with_no_conversion():
    # Regression, found in the cluster. Both of this agent's refusals live
    # inside `convert_currency`, so an amount that never reaches it is never
    # checked. `target_currency` used to say "set it when the amounts are not
    # all in the same currency", which meant "500M dollars" -- one currency, no
    # conversion needed -- was added up as plain arithmetic: the ambiguous word
    # was never questioned and the size threshold was never applied.
    # Re-pointed from the deleted MathRequest.target_currency description to
    # the instruction, which is now the only place the rule is stated.
    described = AGENTS["math"].instruction.lower()
    assert "including an amount already in the target currency" in described
    assert "not a no-op" in described


def test_a_same_currency_call_still_checks_the_amount():
    # Which is what makes the rule above worth following: converting USD to USD
    # is arithmetically a no-op and still catches both failures.
    assert convert_currency(500_000_000.0, "USD", "USD")["status"] == NEEDS_CONFIRMATION
    assert convert_currency(10.0, "dollars", "dollars")["status"] == NEEDS_INPUT
    # ...and stays a no-op when there is nothing wrong with it.
    ok = convert_currency(10.0, "USD", "USD")
    assert ok["status"] == "ok"
    assert ok["converted"] == "10.00"


# --- The gated crypto quote ---------------------------------------------------
#
# The property is the same one `test_two_phase_approval.py` pins for
# `publish_result`: the number is unreachable without a human decision, as a
# fact about the code rather than an instruction a model is asked to respect.
# What is specific to this agent is the bypass -- `convert_currency` sits right
# next door and would otherwise quote the same asset with nobody asked.


def _crypto(
    ctx: FakeToolContext,
    amount: float = 10_000.0,
    from_currency: str = "EUR",
    to_asset: str = "BTC",
):
    return convert_to_crypto(amount, from_currency, to_asset, cast(ToolContext, ctx))


def test_asking_for_a_crypto_quote_produces_no_quote():
    # Phase one must be inert. A number here would mean the gate is decorative.
    out = _crypto(FakeToolContext())
    assert out["status"] == AWAITING_APPROVAL
    assert "converted" not in out


def test_the_crypto_request_describes_exactly_what_would_be_quoted():
    ctx = FakeToolContext()
    _crypto(ctx)
    assert ctx.requested is not None
    # The reviewer sees the asset, both amounts and the age of the rate -- a
    # hint that said only "convert some money" would be a signature on nothing.
    assert "BTC" in ctx.requested.hint
    assert RATES_AS_OF in ctx.requested.hint


def test_a_refused_crypto_quote_returns_no_number():
    out = _crypto(
        FakeToolContext(ToolConfirmation(confirmed=False, payload={"approved_by": "a"}))
    )
    assert out["status"] == REFUSED
    assert "converted" not in out


def test_an_approved_crypto_quote_is_produced_and_attributed():
    out = _crypto(
        FakeToolContext(
            ToolConfirmation(
                confirmed=True, payload={"approved_by": "compliance@bnp", "note": "ok"}
            )
        )
    )
    assert out["status"] == CONVERTED
    assert out["to_asset"] == "BTC"
    assert out["approved_by"] == "compliance@bnp"
    # 10,000 EUR at 1.09 USD/EUR = 10,900 USD, over 94,500 USD per BTC.
    assert out["converted"] == f"{10_000 * 1.09 / 94_500:.8f}"
    assert out["as_of"] == RATES_AS_OF
    assert out["warning"]


def test_the_proposal_matches_what_is_reported():
    # The gotcha this repo has hit twice: a case recorded from the model's raw
    # arguments cannot be matched against a result computed from normalised
    # ones, and `find_execution` then reports a perfectly good execution as
    # `approved_not_confirmed`. Same values, same spelling, both times.
    asked = FakeToolContext()
    _crypto(asked)
    assert asked.requested is not None
    proposal = cast(dict[str, str], asked.requested.payload)

    granted = _crypto(
        FakeToolContext(ToolConfirmation(confirmed=True, payload={"approved_by": "a"}))
    )
    for field in ("amount", "from_currency", "to_asset", "converted"):
        assert proposal[field] == granted[field]


def test_a_crypto_success_is_confirmable_as_an_execution():
    # A gated action reporting a status outside EFFECT_PERFORMED runs perfectly
    # and is reported as unconfirmed -- a vocabulary bug wearing a model bug's
    # clothes.
    assert CONVERTED in EFFECT_PERFORMED


def test_the_ungated_tool_cannot_quote_crypto():
    # The bypass. `convert_currency` takes no ToolContext and can never ask
    # anyone, so if it converted crypto the authorization next door would be
    # decoration. It must refuse from BOTH sides.
    into = convert_currency(100.0, "EUR", "BTC")
    out_of = convert_currency(1.0, "bitcoin", "EUR")
    assert into["status"] == "error"
    assert out_of["status"] == "error"
    assert "converted" not in into
    assert "converted" not in out_of


def test_refusing_crypto_names_the_tool_that_can_do_it():
    # Reported as "unsupported currency" instead, a model treats it as a dead
    # end and looks for another way to produce the number -- which is how a
    # gated action gets answered from memory.
    out = convert_currency(100.0, "EUR", "ETH")
    assert out["use_tool"] == "convert_to_crypto"


def test_crypto_is_not_in_the_fiat_rate_table():
    # Membership of that table IS reachability from the ungated tool, so this
    # is the structural half of the test above.
    assert not set(supported_crypto_assets()) & set(supported_currencies())


def test_an_ambiguous_currency_is_settled_before_anyone_is_asked_to_approve():
    # Nobody can authorise a conversion whose meaning is still open: a reviewer
    # shown "10000 dollars" would be approving one of six different amounts.
    ctx = FakeToolContext()
    out = _crypto(ctx, from_currency="dollars")
    assert out["status"] == NEEDS_INPUT
    assert ctx.requested is None


def test_an_unknown_asset_is_refused_before_anyone_is_asked():
    ctx = FakeToolContext()
    out = _crypto(ctx, to_asset="DOGE")
    assert out["status"] == "error"
    assert ctx.requested is None


def test_the_quote_goes_one_way_only():
    # Selling crypto is a different thing to price and nobody asked for it.
    ctx = FakeToolContext()
    out = _crypto(ctx, from_currency="BTC", to_asset="ETH")
    assert out["status"] == "error"
    assert ctx.requested is None


def test_the_gated_tool_s_declaration_can_be_built():
    # The ToolContext trap: with `from __future__ import annotations`, ADK
    # builds each declaration via typing.get_type_hints(), which evaluates the
    # annotation. A TYPE_CHECKING-only import of ToolContext raises NameError
    # at REQUEST time and takes every tool in the module down with it -- so it
    # cannot be caught by importing the module, only by doing this.
    from google.adk.tools.function_tool import FunctionTool

    declaration = FunctionTool(convert_to_crypto)._get_declaration()
    assert declaration is not None
    schema = declaration.parameters_json_schema
    assert isinstance(schema, dict)
    # tool_context is injected by ADK, never asked of the model.
    assert set(schema["required"]) == {"amount", "from_currency", "to_asset"}
