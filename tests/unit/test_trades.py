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

"""The trades specialist: a BigQuery read that cannot happen unreviewed.

Same property as ``test_two_phase_approval.py`` asserts for the gated write, on
a gated *read*: with no approver the query is unreachable, and an approved query
that comes back changed is not accepted as the approved one. Nothing here talks
to BigQuery — the one function that would is stubbed, which is why it exists as
a separate function at all.
"""

import json

import pytest

from app.agents.contracts import APPROVAL_REQUIRED, EFFECT_PERFORMED, TradesRequest
from app.agents.trades import tools
from app.agents.trades.dataset import TABLE
from app.agents.trades.tools import (
    EXECUTIONS,
    canonical_sql,
    run_trade_query,
    validate_sql,
)
from app.cluster import cases

GOOD_SQL = f"""
SELECT p.PartyID AS bot, COUNT(*) AS trades
FROM `{TABLE}` AS t,
UNNEST(t.Sides) AS s,
UNNEST(s.PartyIDs) AS p
GROUP BY bot
ORDER BY trades DESC
"""


@pytest.fixture(autouse=True)
def _clear_effects():
    """Reset the stand-in effect log between tests."""
    EXECUTIONS.clear()
    yield
    EXECUTIONS.clear()


@pytest.fixture
def _fake_bigquery(monkeypatch: pytest.MonkeyPatch):
    """Replace the only function that touches the network."""
    calls: list[tuple[str, int]] = []

    def _execute(statement: str, row_limit: int) -> dict[str, object]:
        calls.append((statement, row_limit))
        return {
            "rows": [{"bot": "PREDICTES", "trades": 193809}],
            "row_count": 1,
            "total_rows": 9,
            "bytes_processed": 12345,
            "cache_hit": False,
        }

    monkeypatch.setattr(tools, "_execute", _execute)
    return calls


# --- The gate -----------------------------------------------------------------


def test_proposing_queries_nothing(_fake_bigquery):
    out = run_trade_query(GOOD_SQL)
    assert out["status"] == APPROVAL_REQUIRED
    assert _fake_bigquery == []
    assert EXECUTIONS == []


def test_whitespace_is_not_an_approval(_fake_bigquery):
    out = run_trade_query(GOOD_SQL, approved_by="   ")
    assert out["status"] == APPROVAL_REQUIRED
    assert _fake_bigquery == []


def test_a_proposal_shows_the_reviewer_the_exact_query(_fake_bigquery):
    out = run_trade_query(GOOD_SQL, row_limit=5)
    assert out["proposal"] == {
        "action": "run_trade_query",
        "sql": canonical_sql(GOOD_SQL),
        "row_limit": 5,
    }
    assert TABLE in out["summary"]


def test_an_approved_query_runs_once(_fake_bigquery):
    result = run_trade_query(GOOD_SQL, row_limit=5, approved_by="desk-head@cymbal")
    assert result["status"] in EFFECT_PERFORMED
    assert result["rows"] == [{"bot": "PREDICTES", "trades": 193809}]
    assert len(_fake_bigquery) == 1
    assert len(EXECUTIONS) == 1


def test_a_failed_query_is_not_recorded_as_a_performed_effect(monkeypatch):
    # An error must leave the case re-drivable rather than closing it as done.
    monkeypatch.setattr(
        tools,
        "_execute",
        lambda statement, row_limit: {"status": "error", "error": "boom"},
    )
    result = run_trade_query(GOOD_SQL, approved_by="desk-head@cymbal")
    assert result["status"] == "error"
    assert result["status"] not in EFFECT_PERFORMED
    assert EXECUTIONS == []


def test_row_limit_is_clamped_rather_than_trusted(_fake_bigquery):
    out = run_trade_query(GOOD_SQL, row_limit=10_000)
    assert out["proposal"]["row_limit"] == tools.DEFAULT_MAX_ROWS


def test_a_missing_row_limit_falls_back_to_the_default(_fake_bigquery):
    assert run_trade_query(GOOD_SQL, row_limit=0)["proposal"]["row_limit"] == (
        tools.DEFAULT_ROW_LIMIT
    )


# --- The validator ------------------------------------------------------------


def test_a_well_formed_read_is_accepted():
    assert validate_sql(GOOD_SQL) is None


def test_a_cte_is_accepted():
    assert (
        validate_sql(f"WITH x AS (SELECT Symbol FROM `{TABLE}`) SELECT COUNT(*) FROM x")
        is None
    )


@pytest.mark.parametrize(
    "sql",
    [
        f"DELETE FROM `{TABLE}`",
        f"SELECT 1 FROM `{TABLE}`; DROP TABLE x",
        f"CREATE VIEW v AS SELECT 1 FROM `{TABLE}`",
        f"SELECT * FROM `{TABLE}` UNION ALL SELECT * FROM `other.tbl.name`",
        "SELECT * FROM `some-other-project.dataset.table`",
        "SELECT 1",
        f"SELECT * FROM `{TABLE}` JOIN secret.dataset.table USING (OrderID)",
        "",
    ],
)
def test_anything_that_is_not_a_read_of_the_one_table_is_refused(sql: str):
    assert validate_sql(sql) is not None


def test_a_rejected_query_is_refused_even_with_an_approval(_fake_bigquery):
    # The approval is for a query, not for the agent. A tampered statement on
    # the execution turn must not ride in on someone else's sign-off.
    out = run_trade_query(f"DROP TABLE `{TABLE}`", approved_by="desk-head@cymbal")
    assert out["status"] == "error"
    assert _fake_bigquery == []


def test_a_keyword_inside_a_string_literal_is_not_a_keyword():
    # The validator masks literals first. Without that, a legitimate filter is
    # rejected and the model has no way to understand why.
    assert validate_sql(f"SELECT * FROM `{TABLE}` WHERE Symbol = 'DROP'") is None


def test_a_keyword_inside_a_comment_is_not_a_keyword():
    assert validate_sql(f"-- delete this later\nSELECT 1 FROM `{TABLE}`") is None


def test_a_comment_cannot_hide_the_rest_of_a_statement():
    assert validate_sql(f"SELECT 1 FROM `{TABLE}` /* */ ; DELETE FROM x") is not None


# --- Reproducing the approved query ------------------------------------------


def test_canonicalisation_absorbs_reformatting_but_not_a_different_query():
    reflowed = f"  SELECT   Symbol\nFROM  `{TABLE}` ;  "
    assert canonical_sql(reflowed) == f"SELECT Symbol FROM `{TABLE}`"
    assert canonical_sql(reflowed) != canonical_sql(f"SELECT OrderID FROM `{TABLE}`")


def test_the_caller_can_confirm_the_approved_query_actually_ran(_fake_bigquery):
    # The full round trip the /cases endpoint performs: propose, serialise the
    # way an A2A reply would, approve, re-send, and match the result against
    # the record. This is what turns "the model said it ran" into evidence.
    proposed = json.loads(json.dumps(run_trade_query(GOOD_SQL, row_limit=5)))
    proposal = proposed["proposal"]
    assert cases.find_proposals([json.dumps(proposed)])

    performed = run_trade_query(
        proposal["sql"],
        row_limit=proposal["row_limit"],
        approved_by="desk-head@cymbal",
    )
    assert cases.find_execution([json.dumps(performed)], proposal) is not None


def test_a_different_query_is_not_accepted_as_the_approved_one(_fake_bigquery):
    proposal = run_trade_query(GOOD_SQL, row_limit=5)["proposal"]
    performed = run_trade_query(
        f"SELECT COUNT(*) FROM `{TABLE}`",
        row_limit=5,
        approved_by="desk-head@cymbal",
    )
    # It ran -- the approver said yes to *a* query -- but it is not the one on
    # the record, so the case is reported unconfirmed rather than closed.
    assert performed["status"] in EFFECT_PERFORMED
    assert cases.find_execution([json.dumps(performed)], proposal) is None


# --- The contract -------------------------------------------------------------


def test_the_contract_carries_the_approved_sql_back():
    # Unlike MathRequest, which is reproducible from `expression`, SQL has to
    # travel: a model asked the same question twice does not emit the same text.
    first = TradesRequest(case_id="c1", question="Which bot traded most?")
    assert first.sql == ""
    assert first.approved_by == ""

    approved = TradesRequest(
        case_id="c1",
        question="Which bot traded most?",
        sql=canonical_sql(GOOD_SQL),
        approved_by="desk-head@cymbal",
    )
    assert approved.sql == canonical_sql(GOOD_SQL)


def test_every_gated_tool_reports_a_status_the_caller_recognises():
    # A new gated action whose success status is missing from EFFECT_PERFORMED
    # executes correctly and is then reported as `approved_not_confirmed` --
    # a failure that looks like a model problem and is a vocabulary problem.
    assert "executed" in EFFECT_PERFORMED
    assert "published" in EFFECT_PERFORMED
