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

"""The gated action: propose when nobody has approved, publish when they have.

These guard the property `require_confirmation=True` used to give structurally
and that D5 moved into application code: the effect cannot happen before a human
approves. The gate is that the effect is unreachable without an approver -- not
an instruction the model is asked to respect.
"""

import json

import pytest

from app.agents.contracts import APPROVAL_REQUIRED, MathRequest
from app.agents.math.tools import PUBLICATIONS, canonical_value, publish_result


@pytest.fixture(autouse=True)
def _clear_effects():
    """Reset the stand-in effect log between tests."""
    PUBLICATIONS.clear()
    yield
    PUBLICATIONS.clear()


def test_proposing_performs_nothing():
    # Phase one must be inert. If this ever fails, the gate is not a gate.
    out = publish_result("42", "q3-revenue")
    assert out["status"] == APPROVAL_REQUIRED
    assert PUBLICATIONS == []


def test_a_proposal_describes_exactly_what_would_happen():
    out = publish_result("42", "q3-revenue")
    assert out["proposal"] == {
        "action": "publish_result",
        "value": "42",
        "label": "q3-revenue",
    }
    assert "42" in out["summary"] and "q3-revenue" in out["summary"]


def test_whitespace_is_not_an_approval():
    # An approver field that is present but blank must not open the gate.
    out = publish_result("42", "q3-revenue", approved_by="   ")
    assert out["status"] == APPROVAL_REQUIRED
    assert PUBLICATIONS == []


def test_publishing_requires_an_approver_and_then_happens():
    result = publish_result(
        "42", "q3-revenue", approved_by="compliance@bnp", note="checked"
    )
    assert result["status"] == "published"
    assert PUBLICATIONS == [
        {"value": "42", "label": "q3-revenue", "approved_by": "compliance@bnp"}
    ]


def test_the_result_carries_who_approved_it():
    # The audit trail lives in the result, so it survives into the case record.
    result = publish_result("42", "q3", approved_by="compliance@bnp", note="ok")
    assert result["approved_by"] == "compliance@bnp"
    assert result["note"] == "ok"


def test_the_approval_survives_a_serialization_boundary():
    # Proposal and approval are separated by minutes or weeks and by a process
    # restart, so nothing may depend on in-memory state surviving between them.
    proposed = json.loads(json.dumps(publish_result("42", "q3-revenue")))
    proposal = proposed["proposal"]
    result = publish_result(
        proposal["value"], proposal["label"], approved_by="compliance@bnp"
    )
    assert result["status"] == "published"


def test_math_contract_expresses_both_modes_with_one_field():
    # The whole simplification: proposing and executing differ by `approved_by`,
    # not by a second tool, a second contract, or a fingerprint.
    propose = MathRequest(case_id="c1", expression="17 * 23", publish_as="q3")
    assert propose.approved_by == ""

    execute = MathRequest(
        case_id="c1",
        expression="17 * 23",
        publish_as="q3",
        approved_by="compliance@bnp",
        decision_note="ok",
    )
    assert execute.approved_by == "compliance@bnp"
    # The request is otherwise IDENTICAL, which is what stops values drifting:
    # the specialist recomputes the result from `expression`.
    assert execute.expression == propose.expression
    assert execute.publish_as == propose.publish_as


def test_the_same_number_proposes_and_publishes_identically():
    # A live run proposed '391000000' and then published '391000000.0'. The
    # caller compares content to confirm an execution, so the same number
    # reached twice must produce the same string or a correct execution is
    # reported as unconfirmed.
    proposed = publish_result("391000000", "q3")["proposal"]["value"]
    published = publish_result("391000000.0", "q3", approved_by="compliance@bnp")[
        "value"
    ]
    assert proposed == published


def test_canonicalisation_does_not_hide_a_different_number():
    # The check must stay strict: only formatting is normalised.
    assert canonical_value("391000000.0") == canonical_value("391000000")
    assert canonical_value("391000000") != canonical_value("391000001")
    assert canonical_value("2.5") == "2.5"


def test_non_numeric_values_pass_through_untouched():
    assert canonical_value("q3-revenue") == "q3-revenue"
    assert canonical_value("") == ""
