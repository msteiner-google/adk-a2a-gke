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

"""Unit tests for approval cases: the store, and reading proposals off the wire.

The store is exercised against **both** backends — in-memory and
SQLAlchemy-on-SQLite — because a state machine that only holds for the dict is
worth nothing in the cluster.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.agents.statuses import AWAITING_APPROVAL, PUBLISHED
from app.cluster.cases import (
    APPROVED,
    CASES,
    EXECUTED,
    FAILED,
    PENDING,
    REJECTED,
    STATUSES,
    ApprovalCase,
    CaseStore,
    DatabaseCaseStore,
    DecisionOutcome,
    InMemoryCaseStore,
    case_from_confirmation,
    case_from_proposal,
    execution_instruction,
    find_execution,
    find_proposals,
    new_proposal_id,
    task_texts,
)
from app.cluster.db import Database, DatabaseConfig

pytestmark = pytest.mark.asyncio


def _case(proposal_id: str = "p1", **kw) -> ApprovalCase:
    proposal = kw.pop("proposal", {"action": "publish_result", "value": "42"})
    return ApprovalCase(
        case_id=kw.pop("case_id", "case-1"),
        proposal_id=proposal_id,
        agent=kw.pop("agent", "math"),
        action="publish_result",
        summary="Publish '42'.",
        proposal=proposal,
        session_id="s-1",
        **kw,
    )


@pytest_asyncio.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path) -> AsyncIterator[CaseStore]:
    """Yield each backend in turn, so every test runs against both."""
    if request.param == "memory":
        yield InMemoryCaseStore()
        return

    database = Database(
        DatabaseConfig.from_env(
            {"DB_BACKEND": "url", "DB_URL": f"sqlite+aiosqlite:///{tmp_path}/cases.db"}
        )
    )
    engine = database.engine()
    async with engine.begin() as conn:
        await conn.run_sync(CASES.metadata.create_all)
    yield DatabaseCaseStore(database)
    await database.aclose()


async def test_open_then_read_back(store: CaseStore):
    assert await store.open(_case()) is True
    found = await store.get("p1")
    assert found is not None
    assert found.status == PENDING
    assert found.proposal == {"action": "publish_result", "value": "42"}


async def test_open_is_idempotent(store: CaseStore):
    assert await store.open(_case()) is True
    assert await store.open(_case()) is False


async def test_get_unknown_returns_none(store: CaseStore):
    assert await store.get("nope") is None


async def test_list_by_status_is_oldest_first(store: CaseStore):
    await store.open(_case("p1"))
    await store.open(_case("p2"))
    pending = await store.list_by_status(PENDING)
    assert [c.proposal_id for c in pending] == ["p1", "p2"]


async def test_approve_records_the_decision(store: CaseStore):
    await store.open(_case())
    outcome, case = await store.decide(
        "p1", approved=True, decided_by="compliance@bnp", note="ok"
    )
    assert outcome is DecisionOutcome.RECORDED
    assert case is not None
    assert case.status == APPROVED
    assert case.decided_by == "compliance@bnp"
    assert case.note == "ok"
    assert case.decided_at is not None


async def test_reject_records_the_decision(store: CaseStore):
    await store.open(_case())
    _, case = await store.decide("p1", approved=False, decided_by="x")
    assert case is not None
    assert case.status == REJECTED


async def test_deciding_twice_is_refused_not_overwritten(store: CaseStore):
    # Two reviewers racing must produce ONE decision. This is the whole of the
    # concurrency design: a single conditional write, and no lease to reclaim
    # because nothing is held open.
    await store.open(_case())
    await store.decide("p1", approved=True, decided_by="first")
    outcome, case = await store.decide("p1", approved=False, decided_by="second")
    assert outcome is DecisionOutcome.ALREADY_DECIDED
    assert case is not None
    assert case.status == APPROVED
    assert case.decided_by == "first"


async def test_deciding_an_unknown_case(store: CaseStore):
    outcome, case = await store.decide("nope", approved=True, decided_by="x")
    assert outcome is DecisionOutcome.NOT_FOUND
    assert case is None


async def test_record_execution_closes_the_case(store: CaseStore):
    await store.open(_case())
    await store.decide("p1", approved=True, decided_by="x")
    case = await store.record_execution(
        "p1", succeeded=True, result={"status": "published"}
    )
    assert case is not None
    assert case.status == EXECUTED
    assert case.executed_at is not None
    assert case.result == {"status": "published"}


async def test_record_execution_marks_failure(store: CaseStore):
    await store.open(_case())
    await store.decide("p1", approved=True, decided_by="x")
    case = await store.record_execution(
        "p1",
        succeeded=False,
        result={"status": "rejected", "reason": "specialist refused"},
    )
    assert case is not None
    assert case.status == FAILED


async def test_a_pending_case_survives_a_new_store_on_the_same_database(tmp_path):
    # The property the in-memory backend cannot offer, and the one that made the
    # previous design lose approvals on restart (docs/design-decisions.md).
    url = f"sqlite+aiosqlite:///{tmp_path}/persist.db"
    first = Database(DatabaseConfig.from_env({"DB_BACKEND": "url", "DB_URL": url}))
    async with first.engine().begin() as conn:
        await conn.run_sync(CASES.metadata.create_all)
    await DatabaseCaseStore(first).open(_case())
    await first.aclose()

    # A different process would build a different Database over the same file.
    second = Database(DatabaseConfig.from_env({"DB_BACKEND": "url", "DB_URL": url}))
    recovered = await DatabaseCaseStore(second).get("p1")
    assert recovered is not None
    assert recovered.status == PENDING
    await second.aclose()


async def test_statuses_match_the_check_constraint():
    # The CHECK constraint in migration 0005 is generated from this tuple; a
    # state added in code but not there fails at runtime, not at deploy.
    assert set(STATUSES) == {PENDING, APPROVED, REJECTED, EXECUTED, FAILED}


async def test_table_columns_match_the_dataclass():
    # Guards the same drift `test_migrations.py` guards for the DDL: a field
    # added to ApprovalCase but not to CASES is a runtime SQL error.
    columns = {c.name for c in CASES.columns}
    fields = set(ApprovalCase.__dataclass_fields__)
    assert fields == columns, fields ^ columns


async def test_new_proposal_ids_are_unique():
    assert len({new_proposal_id() for _ in range(100)}) == 100


async def test_sqlite_round_trip_preserves_json_and_timezone(tmp_path):
    database = Database(
        DatabaseConfig.from_env(
            {"DB_BACKEND": "url", "DB_URL": f"sqlite+aiosqlite:///{tmp_path}/tz.db"}
        )
    )
    async with database.engine().begin() as conn:
        await conn.run_sync(CASES.metadata.create_all)
    store = DatabaseCaseStore(database)
    await store.open(_case(proposal={"action": "x", "nested": {"a": [1, 2]}}))
    case = await store.get("p1")
    assert case is not None
    assert case.proposal == {"action": "x", "nested": {"a": [1, 2]}}
    # SQLite drops the offset; the store must put it back or comparisons break.
    assert case.created_at.tzinfo is not None
    async with database.engine().connect() as conn:
        assert (
            await conn.execute(sa.select(sa.func.count()).select_from(CASES))
        ).scalar() == 1
    await database.aclose()


# --- Reading proposals back off the A2A text boundary -------------------------


def _proposal_reply(value: str = "42", label: str = "q3") -> str:
    """A realistic specialist reply: the proposal JSON wrapped in prose."""
    proposal = {"action": "publish_result", "value": value, "label": label}
    blob = json.dumps(
        {
            "status": AWAITING_APPROVAL,
            "action": "publish_result",
            "proposal": proposal,
            "summary": f"Publish {value!r} under label {label!r}.",
        }
    )
    return (
        "I calculated the result. Nothing has been published yet -- this needs "
        f"approval:\n{blob}"
    )


def test_find_proposals_reads_json_embedded_in_prose():
    found = find_proposals([_proposal_reply()])
    assert len(found) == 1
    assert found[0]["proposal"]["value"] == "42"


def test_find_proposals_ignores_ordinary_replies():
    assert find_proposals(["17 * 23 = 391", "no json here at all", "{broken"]) == []


def test_find_proposals_deduplicates_the_same_proposal():
    # A proposal usually appears twice: once in the tool result and again in the
    # agent's narration of it. That must open ONE case, not two.
    reply = _proposal_reply()
    assert len(find_proposals([reply, reply])) == 1


def test_find_execution_requires_the_approved_content():
    # THE safety property, now obtained by comparison rather than a fingerprint:
    # a result only confirms a case if it reports what was actually approved.
    approved = {"action": "publish_result", "value": "42", "label": "q3"}
    good = [json.dumps({"status": "published", **approved})]
    assert find_execution(good, approved) is not None


def test_find_execution_rejects_a_different_value():
    approved = {"action": "publish_result", "value": "42", "label": "q3"}
    tampered = [
        json.dumps(
            {
                "status": "published",
                "action": "publish_result",
                "value": "999999",
                "label": "q3",
            }
        )
    ]
    assert find_execution(tampered, approved) is None


def test_find_execution_rejects_a_different_label():
    approved = {"action": "publish_result", "value": "42", "label": "internal"}
    tampered = [
        json.dumps(
            {
                "status": "published",
                "action": "publish_result",
                "value": "42",
                "label": "published-accounts",
            }
        )
    ]
    assert find_execution(tampered, approved) is None


def test_find_execution_returns_none_when_nothing_was_confirmed():
    # A confident-sounding reply is not evidence the effect ran.
    approved = {"action": "publish_result", "value": "42", "label": "q3"}
    assert find_execution(["Done! I have published the result."], approved) is None


def test_case_from_proposal_carries_the_proposal_and_action():
    found = find_proposals([_proposal_reply()])[0]
    case = case_from_proposal(found, session_id="s-9", case_id="c-9", agent="math")
    assert case.status == PENDING
    assert case.agent == "math"
    assert case.action == "publish_result"
    assert case.proposal == found["proposal"]
    assert case.session_id == "s-9"


def test_execution_instruction_restates_the_approved_proposal():
    case = _case()
    case.decided_by = "compliance@bnp"
    case.note = "fine by me"
    text = execution_instruction(case)
    assert "compliance@bnp" in text
    assert "fine by me" in text
    assert "publish_result" in text
    # It must tell the agent to re-send the ORIGINAL request rather than
    # composing a new one, which is what keeps the values from drifting.
    assert "SAME request" in text


def test_unwrapping_finds_json_inside_a_wrapped_tool_result():
    # The bug the first end-to-end run hit: ADK wraps a tool result as
    # {"result": "<text>"}, and json.dumps of that wrapper escapes the peer's
    # own JSON inside a string, where no object scanner can reach it.
    from app.cluster.cases import _unpack

    inner = _proposal_reply()
    texts = _unpack({"result": inner})
    assert find_proposals(texts), "proposal must survive the {'result': ...} wrapper"


# --- Building a case from a suspended confirmation ----------------------------


def _confirmation(payload: dict[str, Any] | None) -> dict[str, Any]:
    """An adk_request_confirmation call as it arrives over A2A."""
    return {
        "id": "adk-confirm",
        "name": "adk_request_confirmation",
        "args": {
            "originalFunctionCall": {
                "id": "adk-original",
                "name": "publish_result",
                # The model's raw spelling, which the tool normalises.
                "args": {"value": "391000000.0", "label": "q3"},
            },
            "toolConfirmation": {"hint": "Publish it.", "payload": payload},
        },
    }


async def test_a_case_records_where_the_decision_must_be_delivered():
    case = case_from_confirmation(
        _confirmation(
            {"action": "publish_result", "value": "391000000", "label": "q3"}
        ),
        session_id="s1",
        case_id="s1",
        agent="math",
        owner_task_id="task-m",
        owner_context_id="ctx-m",
    )
    assert case.agent == "math"
    assert case.owner_task_id == "task-m"
    assert case.owner_context_id == "ctx-m"
    assert case.confirmation_id == "adk-confirm"
    assert case.action == "publish_result"
    assert case.status == PENDING


async def test_the_proposal_prefers_the_tools_canonical_values():
    # Measured: a proposal recorded from the model's raw arguments said
    # '391000000.0' while the tool published '391000000', and find_execution
    # correctly refused to confirm a perfectly good execution. The tool
    # declares what it will act on; that is what goes on the record.
    canonical = {"action": "publish_result", "value": "391000000", "label": "q3"}
    case = case_from_confirmation(
        _confirmation(canonical),
        session_id="s1",
        case_id="s1",
        agent="math",
        owner_task_id="t",
        owner_context_id="c",
    )
    assert case.proposal == canonical

    performed = {
        "status": PUBLISHED,
        "action": "publish_result",
        "value": "391000000",
        "label": "q3",
    }
    assert find_execution([json.dumps(performed)], case.proposal) is not None


async def test_a_tool_that_declares_nothing_falls_back_to_its_arguments():
    # A gated tool that does not normalise its inputs need not pass a proposal;
    # the pending call's own arguments are still an honest description.
    case = case_from_confirmation(
        _confirmation(None),
        session_id="s1",
        case_id="s1",
        agent="math",
        owner_task_id="t",
        owner_context_id="c",
    )
    assert case.proposal == {
        "value": "391000000.0",
        "label": "q3",
        "action": "publish_result",
    }


async def test_task_texts_finds_a_result_buried_in_task_history():
    # A tool's result crosses A2A as a data part inside the task history, not
    # as prose, so confirming an execution means reading the history rather
    # than the final message.
    task = {
        "status": {"message": {"parts": [{"text": "All done."}]}},
        "history": [
            {"parts": [{"text": "Please publish it."}]},
            {
                "parts": [
                    {
                        "data": {
                            "name": "publish_result",
                            "response": {
                                "status": PUBLISHED,
                                "action": "publish_result",
                                "value": "391000000",
                                "label": "q3",
                            },
                        }
                    }
                ]
            },
        ],
    }
    texts = task_texts(task)
    assert "All done." in texts
    proposal = {"action": "publish_result", "value": "391000000", "label": "q3"}
    assert find_execution(texts, proposal) is not None


async def test_task_texts_is_empty_for_a_task_that_did_nothing():
    assert task_texts({}) == []
