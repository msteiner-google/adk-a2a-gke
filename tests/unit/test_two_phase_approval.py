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

"""The gated action: suspend when nobody has decided, publish when they have.

The property under test is that the effect is unreachable without a human
decision -- a fact about the code, not an instruction the model is asked to
respect. A gated tool asks :func:`require_approval` and returns without acting
while the answer is pending; ADK re-executes it with the decision attached.

These call the tool directly, with a fake ``ToolContext``, because that is the
seam the gate lives on. The A2A half -- that the suspension is reported as
``TASK_STATE_AUTH_REQUIRED`` and that a grant reaches the right task -- is in
``test_authorization.py``.
"""

import json
from typing import Any, cast

import pytest
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.adk.tools.tool_context import ToolContext

from app.agents.math.tools import PUBLICATIONS, canonical_value, publish_result
from app.agents.statuses import AWAITING_APPROVAL, REFUSED
from tests.unit.conftest import FakeToolContext


def _pending() -> FakeToolContext:
    """A context for a call nobody has decided on yet."""
    return FakeToolContext()


def _decided(
    *, confirmed: bool, approved_by: str = "compliance@bnp", note: str = ""
) -> FakeToolContext:
    """A context carrying a human's decision, as ADK re-supplies it."""
    return FakeToolContext(
        ToolConfirmation(
            confirmed=confirmed,
            payload={"approved_by": approved_by, "note": note},
        )
    )


def _publish(ctx: FakeToolContext, value: str = "42", label: str = "q3-revenue"):
    return publish_result(value, label, cast(ToolContext, ctx))


@pytest.fixture(autouse=True)
def _clear_effects():
    """Reset the stand-in effect log between tests."""
    PUBLICATIONS.clear()
    yield
    PUBLICATIONS.clear()


def test_asking_performs_nothing():
    # Phase one must be inert. If this ever fails, the gate is not a gate.
    ctx = _pending()
    out = _publish(ctx)
    assert out["status"] == AWAITING_APPROVAL
    assert PUBLICATIONS == []


def test_asking_suspends_rather_than_finishing():
    # Returning a value without requesting a confirmation would complete the
    # task, reporting a gated action as declined-but-done instead of pending.
    ctx = _pending()
    _publish(ctx)
    assert ctx.requested is not None
    assert ctx.actions.requested_tool_confirmations != {}
    # Suppressed so the model cannot narrate the placeholder as an outcome.
    assert ctx.actions.skip_summarization is True


def test_the_request_describes_exactly_what_would_happen():
    ctx = _pending()
    _publish(ctx)
    assert ctx.requested is not None
    assert ctx.requested.payload == {
        "action": "publish_result",
        "value": "42",
        "label": "q3-revenue",
    }
    assert "42" in ctx.requested.hint
    assert "q3-revenue" in ctx.requested.hint


def test_a_refusal_is_not_an_approval():
    # A decision that arrived and said no must not open the gate.
    out = _publish(_decided(confirmed=False, note="not authorised"))
    assert out["status"] == REFUSED
    assert PUBLICATIONS == []


def test_publishing_requires_a_decision_and_then_happens():
    result = _publish(_decided(confirmed=True, note="checked"))
    assert result["status"] == "published"
    assert PUBLICATIONS == [
        {"value": "42", "label": "q3-revenue", "approved_by": "compliance@bnp"}
    ]


def test_the_result_carries_who_approved_it():
    # The audit trail lives in the result, so it survives into the case record.
    result = _publish(_decided(confirmed=True, note="ok"), label="q3")
    assert result["approved_by"] == "compliance@bnp"
    assert result["note"] == "ok"


def test_the_decision_survives_a_serialization_boundary():
    # Request and decision are separated by minutes or weeks, by a process
    # restart, and by an A2A hop, so nothing may depend on in-memory state
    # surviving between them.
    ctx = _pending()
    _publish(ctx)
    assert ctx.requested is not None
    wire = json.loads(json.dumps(ctx.requested.model_dump(by_alias=True)))
    proposal = wire["payload"]

    result = _publish(
        _decided(confirmed=True), value=proposal["value"], label=proposal["label"]
    )
    assert result["status"] == "published"


def test_the_same_call_asks_and_acts():
    # The whole simplification: asking and acting are one tool and one set of
    # arguments, differing only by whether a decision is present. There is no
    # second tool, no second contract, and nothing for a model to re-compose.
    ctx = _pending()
    asked = _publish(ctx)
    acted = _publish(_decided(confirmed=True))
    assert asked["action"] == acted["action"] == "publish_result"
    assert asked["status"] == AWAITING_APPROVAL
    assert acted["status"] == "published"


def test_the_same_number_is_proposed_and_published_identically():
    # A live run proposed '391000000' and then published '391000000.0'. The
    # caller compares content to confirm an execution, so the same number
    # reached twice must produce the same string or a correct execution is
    # reported as unconfirmed. Measured again after the redesign, with
    # '88000.0' against '88000'; the fix is that the tool declares its
    # canonical values in the confirmation payload rather than letting the
    # model's raw arguments stand in as the proposal.
    ctx = _pending()
    _publish(ctx, value="391000000", label="q3")
    assert ctx.requested is not None
    proposed = cast(dict[str, Any], ctx.requested.payload)["value"]

    published = _publish(_decided(confirmed=True), value="391000000.0", label="q3")
    assert proposed == published["value"]


def test_canonicalisation_does_not_hide_a_different_number():
    # The check must stay strict: only formatting is normalised.
    assert canonical_value("391000000.0") == canonical_value("391000000")
    assert canonical_value("391000000") != canonical_value("391000001")
    assert canonical_value("2.5") == "2.5"


def test_non_numeric_values_pass_through_untouched():
    assert canonical_value("q3-revenue") == "q3-revenue"
    assert canonical_value("") == ""
