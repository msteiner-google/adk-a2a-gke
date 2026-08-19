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

"""Approval cases: human sign-off as business state, not a suspended coroutine.

A specialist that must not act without a human returns a **proposal** and
finishes (``app/agents/math/tools.py``). This module is the other half: the
caller's record of what was proposed, what the human decided, and whether the
approved action was carried out.

The lifecycle is four states and no concurrency protocol:

``pending -> approved -> executed``
``pending -> rejected``

That is the whole design, and its shape is the point. The mechanism it replaces
froze an ADK invocation across an A2A hop and replayed it later, which required
a reclaimable lease, a heartbeat, a background sweeper and a workaround for an
ADK routing bug — and still could not deliver the answer once the peer's task
had gone terminal (``docs/design-decisions.md``, D5). Here nothing is held
open: a case is a row, an approval is an UPDATE, and executing is an ordinary
new call. An approval that takes a week costs exactly as much as one that takes
a second.

Ownership
---------
A case belongs to the agent that **asked** for the approval — normally the
orchestrator, since it is the one talking to the human. The row lives in that
agent's own schema (``search_path``, see ``app/cluster/db.py``); no other agent
reads or writes it. Backed by the ``approval_cases`` table when a database is
configured, per-pod memory otherwise.

What is stored, and why
-----------------------
``proposal`` is the whole point of the row: it is what the human was shown and
what they signed off. Keeping it means the approval can be audited after the
fact — the row says exactly what was approved, by whom, when, and what came back
when it ran. It is also what the caller compares the execution result against,
so a specialist that publishes something other than the approved thing is
reported rather than recorded as success.
"""

from __future__ import annotations

import abc
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from app.agents.contracts import APPROVAL_REQUIRED, EFFECT_PERFORMED

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.cluster.db import Database

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXECUTED = "executed"
FAILED = "failed"

#: Every state a case may be in. Mirrored by the CHECK constraint in migration
#: 0005; `tests/unit/test_migrations.py` fails if the two drift apart.
STATUSES = (PENDING, APPROVED, REJECTED, EXECUTED, FAILED)

#: States from which no further transition is allowed.
TERMINAL = (REJECTED, EXECUTED, FAILED)


class DecisionOutcome(StrEnum):
    """Why a :meth:`CaseStore.decide` call succeeded or failed."""

    RECORDED = "recorded"
    """The decision was written; the caller may now execute if approved."""

    NOT_FOUND = "not_found"
    """No case with that id."""

    ALREADY_DECIDED = "already_decided"
    """A decision is already on record. Report it rather than deciding twice."""


@dataclass
class ApprovalCase:
    """A proposed action awaiting, or having received, a human decision."""

    case_id: str
    """Correlates this approval with the wider piece of work (the payload's
    ``case_id``). Not unique: one case may raise several approvals."""

    proposal_id: str
    """Unique id for this approval. What the HTTP surface addresses."""

    agent: str
    """Which specialist proposed the action."""

    action: str
    """What the action is, e.g. ``publish_result``."""

    summary: str
    """One line describing the action, for whoever has to approve it."""

    proposal: dict[str, Any] = field(default_factory=dict)
    """The proposed action itself. Exactly what gets executed if approved."""

    status: str = PENDING
    session_id: str = ""
    """The conversation the approval arose in, so an answer can be reported."""

    decided_by: str = ""
    """Who decided. NOT authenticated -- see docs/human-in-the-loop.md."""

    note: str = ""
    """Free-form feedback the human attached to the decision."""

    result: dict[str, Any] | None = None
    """What the specialist returned when the approved action was executed."""

    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    executed_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        """Return a JSON-safe view for the API.

        Returns:
            A dict of the fields the HTTP surface exposes.
        """

        def stamp(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat(timespec="seconds")

        return {
            "proposal_id": self.proposal_id,
            "case_id": self.case_id,
            "agent": self.agent,
            "action": self.action,
            "summary": self.summary,
            "proposal": self.proposal,
            "status": self.status,
            "session_id": self.session_id,
            "decided_by": self.decided_by,
            "note": self.note,
            "result": self.result,
            "created_at": stamp(self.created_at),
            "decided_at": stamp(self.decided_at),
            "executed_at": stamp(self.executed_at),
        }


def new_proposal_id() -> str:
    """Return a short unique id for a new approval case."""
    return uuid.uuid4().hex[:12]


class CaseStore(abc.ABC):
    """What is awaiting a human decision, and what became of it."""

    @abc.abstractmethod
    async def open(self, case: ApprovalCase) -> bool:
        """Record a new proposal awaiting a decision.

        Args:
            case: The case to record.

        Returns:
            ``True`` when recorded, ``False`` when that ``proposal_id`` was
            already known (so re-reporting a proposal is harmless).
        """

    @abc.abstractmethod
    async def get(self, proposal_id: str) -> ApprovalCase | None:
        """Read one case.

        Args:
            proposal_id: The case to read.

        Returns:
            The case, or ``None`` when unknown.
        """

    @abc.abstractmethod
    async def list_by_status(self, status: str) -> list[ApprovalCase]:
        """Read every case in a status, oldest first.

        Args:
            status: One of :data:`STATUSES`.

        Returns:
            The matching cases.
        """

    @abc.abstractmethod
    async def decide(
        self, proposal_id: str, *, approved: bool, decided_by: str, note: str = ""
    ) -> tuple[DecisionOutcome, ApprovalCase | None]:
        """Record a human's decision, exactly once.

        The single conditional write is the whole concurrency story: two
        reviewers racing produce one decision and one
        :data:`DecisionOutcome.ALREADY_DECIDED`, with no lease to reclaim
        because nothing is held.

        Args:
            proposal_id: The case being decided.
            approved: Whether the action may proceed.
            decided_by: Who decided, for audit.
            note: Optional feedback to carry back to the agent.

        Returns:
            The outcome and the case as it now stands.
        """

    @abc.abstractmethod
    async def record_execution(
        self, proposal_id: str, *, succeeded: bool, result: dict[str, Any]
    ) -> ApprovalCase | None:
        """Close an approved case once its action has been carried out.

        Args:
            proposal_id: The case to close.
            succeeded: Whether the specialist actually performed the action.
            result: What the specialist returned.

        Returns:
            The updated case, or ``None`` when unknown.
        """


class InMemoryCaseStore(CaseStore):
    """Per-pod store. Loses every pending case on restart."""

    def __init__(self) -> None:
        """Start empty."""
        self._cases: dict[str, ApprovalCase] = {}

    async def open(self, case: ApprovalCase) -> bool:
        """Record a case unless its proposal id is already known."""
        if case.proposal_id in self._cases:
            return False
        self._cases[case.proposal_id] = case
        return True

    async def get(self, proposal_id: str) -> ApprovalCase | None:
        """Read one case."""
        return self._cases.get(proposal_id)

    async def list_by_status(self, status: str) -> list[ApprovalCase]:
        """Read every case in a status, oldest first."""
        return sorted(
            (c for c in self._cases.values() if c.status == status),
            key=lambda c: c.created_at,
        )

    async def decide(
        self, proposal_id: str, *, approved: bool, decided_by: str, note: str = ""
    ) -> tuple[DecisionOutcome, ApprovalCase | None]:
        """Record a decision on a pending case."""
        case = self._cases.get(proposal_id)
        if case is None:
            return DecisionOutcome.NOT_FOUND, None
        if case.status != PENDING:
            return DecisionOutcome.ALREADY_DECIDED, case
        case.status = APPROVED if approved else REJECTED
        case.decided_by = decided_by
        case.note = note
        case.decided_at = datetime.now(UTC)
        return DecisionOutcome.RECORDED, case

    async def record_execution(
        self, proposal_id: str, *, succeeded: bool, result: dict[str, Any]
    ) -> ApprovalCase | None:
        """Close an approved case."""
        case = self._cases.get(proposal_id)
        if case is None:
            return None
        case.status = EXECUTED if succeeded else FAILED
        case.result = result
        case.executed_at = datetime.now(UTC)
        return case


def _json_type() -> sa.types.TypeEngine[Any]:
    """Return JSONB on PostgreSQL and generic JSON elsewhere.

    Returns:
        The dialect-appropriate JSON type, matching migration 0005.
    """
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


#: Core table mirroring migration 0005. Unqualified on purpose: ``search_path``
#: routes it into the running agent's schema (see app/cluster/db.py).
METADATA = sa.MetaData()

CASES = sa.Table(
    "approval_cases",
    METADATA,
    sa.Column("proposal_id", sa.String(32), primary_key=True),
    sa.Column("case_id", sa.String(128), nullable=False),
    sa.Column("agent", sa.String(128), nullable=False),
    sa.Column("action", sa.String(128), nullable=False),
    sa.Column("summary", sa.Text(), nullable=False),
    sa.Column("proposal", _json_type(), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("session_id", sa.String(128), nullable=False),
    sa.Column("decided_by", sa.String(256), nullable=False),
    sa.Column("note", sa.Text(), nullable=False),
    sa.Column("result", _json_type(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
)


def _as_utc(value: datetime | None) -> Any:
    """Attach UTC to a naive timestamp.

    SQLite drops the offset, so a round-trip would otherwise yield a naive
    datetime that cannot be compared with an aware ``now``.

    Args:
        value: The timestamp read back from the database.

    Returns:
        An aware datetime, or ``None``.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _row_to_case(row: sa.Row[Any]) -> ApprovalCase:
    """Rebuild an :class:`ApprovalCase` from a database row.

    Args:
        row: A row selected from :data:`CASES`.

    Returns:
        The reconstructed case.
    """
    mapping = row._mapping
    return ApprovalCase(
        case_id=mapping["case_id"],
        proposal_id=mapping["proposal_id"],
        agent=mapping["agent"],
        action=mapping["action"],
        summary=mapping["summary"],
        proposal=dict(mapping["proposal"] or {}),
        status=mapping["status"],
        session_id=mapping["session_id"],
        decided_by=mapping["decided_by"],
        note=mapping["note"],
        result=mapping["result"],
        created_at=_as_utc(mapping["created_at"]),
        decided_at=_as_utc(mapping["decided_at"]),
        executed_at=_as_utc(mapping["executed_at"]),
    )


class DatabaseCaseStore(CaseStore):
    """Durable store over the ``approval_cases`` table from migration 0005."""

    def __init__(self, database: Database) -> None:
        """Bind to the process-wide engine.

        Args:
            database: The shared :class:`~app.cluster.db.Database`.
        """
        self._database = database

    async def open(self, case: ApprovalCase) -> bool:
        """Insert a case, ignoring a repeat of the same proposal id."""
        engine = self._database.engine()
        async with engine.begin() as conn:
            existing = await conn.execute(
                sa.select(CASES.c.proposal_id).where(
                    CASES.c.proposal_id == case.proposal_id
                )
            )
            if existing.first() is not None:
                return False
            await conn.execute(
                sa.insert(CASES).values(
                    proposal_id=case.proposal_id,
                    case_id=case.case_id,
                    agent=case.agent,
                    action=case.action,
                    summary=case.summary,
                    proposal=case.proposal,
                    status=case.status,
                    session_id=case.session_id,
                    decided_by=case.decided_by,
                    note=case.note,
                    created_at=case.created_at,
                )
            )
        return True

    async def get(self, proposal_id: str) -> ApprovalCase | None:
        """Read one case."""
        engine = self._database.engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.select(CASES).where(CASES.c.proposal_id == proposal_id)
            )
            row = result.first()
        return None if row is None else _row_to_case(row)

    async def list_by_status(self, status: str) -> list[ApprovalCase]:
        """Read every case in a status, oldest first."""
        engine = self._database.engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.select(CASES)
                .where(CASES.c.status == status)
                .order_by(CASES.c.created_at)
            )
            rows = result.fetchall()
        return [_row_to_case(row) for row in rows]

    async def decide(
        self, proposal_id: str, *, approved: bool, decided_by: str, note: str = ""
    ) -> tuple[DecisionOutcome, ApprovalCase | None]:
        """Record a decision with one conditional UPDATE."""
        engine = self._database.engine()
        async with engine.begin() as conn:
            # `status == PENDING` in the WHERE clause is the concurrency
            # control: whichever reviewer's statement lands first wins, and the
            # loser reads back a decided row instead of overwriting it.
            decided = await conn.execute(
                sa.update(CASES)
                .where(CASES.c.proposal_id == proposal_id, CASES.c.status == PENDING)
                .values(
                    status=APPROVED if approved else REJECTED,
                    decided_by=decided_by,
                    note=note,
                    decided_at=datetime.now(UTC),
                )
                .returning(CASES)
            )
            row = decided.first()
            if row is not None:
                return DecisionOutcome.RECORDED, _row_to_case(row)

            current = await conn.execute(
                sa.select(CASES).where(CASES.c.proposal_id == proposal_id)
            )
            existing = current.first()
        if existing is None:
            return DecisionOutcome.NOT_FOUND, None
        return DecisionOutcome.ALREADY_DECIDED, _row_to_case(existing)

    async def record_execution(
        self, proposal_id: str, *, succeeded: bool, result: dict[str, Any]
    ) -> ApprovalCase | None:
        """Close an approved case once the action has been carried out."""
        engine = self._database.engine()
        async with engine.begin() as conn:
            updated = await conn.execute(
                sa.update(CASES)
                .where(CASES.c.proposal_id == proposal_id)
                .values(
                    status=EXECUTED if succeeded else FAILED,
                    result=result,
                    executed_at=datetime.now(UTC),
                )
                .returning(CASES)
            )
            row = updated.first()
        return None if row is None else _row_to_case(row)


def build_case_store(database: Database) -> CaseStore:
    """Return the durable store when a database is configured, else in-memory.

    Args:
        database: The shared database, from the injector.

    Returns:
        A :class:`DatabaseCaseStore` or an :class:`InMemoryCaseStore`.
    """
    if database.enabled:
        return DatabaseCaseStore(database)
    return InMemoryCaseStore()


# --- Reading proposals back off the wire --------------------------------------
#
# A specialist's reply crosses A2A as TEXT. ADK's AgentTool reduces a peer's
# response to its merged text parts (`agent_tool.py`), so a structured tool
# result the specialist produced internally does not arrive here as structure --
# it arrives as whatever the specialist wrote. The specialist's instruction
# therefore requires it to report the proposal JSON verbatim, and these helpers
# recover it.
#
# That handshake is the weakest link in this design and it is worth being blunt
# about: it depends on a model following an instruction. It is guarded two ways.
# The parser below is tolerant (it finds an embedded object in surrounding
# prose), and nothing is ever assumed to have happened -- an approved case whose
# execution cannot be confirmed is reported as `approved_not_confirmed` and left
# re-drivable, rather than being recorded as done. A deployment that needs a
# stronger guarantee should give the specialist a structured output channel; see
# `docs/human-in-the-loop.md`.


def _json_objects_in(text: str) -> list[dict[str, Any]]:
    """Extract every JSON object embedded anywhere in a string.

    Tolerant on purpose: a model asked to report a JSON blob commonly wraps it
    in prose or a fenced code block. Scanning for decodable objects handles all
    of those without a format negotiation.

    Args:
        text: The reply to scan.

    Returns:
        Every top-level JSON object found, in order.
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            found.append(value)
            index = text.find("{", end)
        else:
            index = text.find("{", index + 1)
    return found


def find_proposals(texts: Sequence[str]) -> list[dict[str, Any]]:
    """Return every approval-required proposal reported in these replies.

    Args:
        texts: Reply texts produced during a turn (tool results and the final
            answer).

    Returns:
        One entry per distinct proposal. A proposal usually appears twice -- in
        the tool result and again in the agent's narration of it -- so entries
        are de-duplicated on the proposal itself.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in texts:
        for obj in _json_objects_in(text):
            if obj.get("status") != APPROVAL_REQUIRED:
                continue
            proposal = obj.get("proposal")
            if not isinstance(proposal, dict):
                continue
            key = json.dumps(proposal, sort_keys=True)
            if key not in seen:
                seen.add(key)
                found.append(obj)
    return found


def find_execution(
    texts: Sequence[str], proposal: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the confirmed outcome of executing ``proposal``.

    The match is on **content**: a result counts as confirmation only if the
    values it reports are the ones that were approved. That is what stops a
    specialist (or a model relaying for it) from publishing something other than
    the approved thing and having it recorded as success — the property a
    separate approval fingerprint used to provide, obtained here by comparing
    against the record the caller already holds, with nothing extra on the wire.

    Which statuses count as "it happened" comes from
    :data:`~app.agents.contracts.EFFECT_PERFORMED`, not from a literal here: a
    gated *write* reports ``published`` and a gated *read* reports ``executed``,
    and this function stays indifferent to which specialist it is confirming.

    Args:
        texts: Reply texts produced during the execution turn.
        proposal: The approved proposal, from the case record.

    Returns:
        The specialist's result, or ``None`` when nothing in the reply confirms
        that *this* proposal ran.
    """
    wanted = {
        key: value
        for key, value in proposal.items()
        if isinstance(value, str | int | float | bool)
    }
    for text in texts:
        for obj in _json_objects_in(text):
            if obj.get("status") not in EFFECT_PERFORMED:
                continue
            if all(obj.get(key) == value for key, value in wanted.items()):
                return obj
    return None


def case_from_proposal(
    found: dict[str, Any], *, session_id: str, case_id: str, agent: str = ""
) -> ApprovalCase:
    """Build a case record from a proposal a specialist reported.

    Args:
        found: The proposal object recovered from the reply.
        session_id: The conversation it arose in.
        case_id: The wider unit of work it belongs to.
        agent: Which specialist proposed it, when known.

    Returns:
        A pending :class:`ApprovalCase`.
    """
    proposal = dict(found.get("proposal") or {})
    return ApprovalCase(
        case_id=case_id,
        proposal_id=new_proposal_id(),
        agent=agent or str(found.get("agent", "")),
        action=str(found.get("action") or proposal.get("action", "")),
        summary=str(found.get("summary", "")),
        proposal=proposal,
        session_id=session_id,
    )


def execution_instruction(case: ApprovalCase) -> str:
    """Compose the message that asks the agent to carry out an approved action.

    Args:
        case: The approved case.

    Returns:
        The instruction text for the execution turn.
    """
    proposal = json.dumps(case.proposal, sort_keys=True)
    note = f"\nThe approver noted: {case.note}" if case.note else ""
    return (
        f"A human has APPROVED this proposal from the '{case.agent}' "
        f"specialist:\n{proposal}\n"
        f"Approved by: {case.decided_by or 'unknown'}{note}\n\n"
        f"Send the specialist the SAME request that produced this proposal, "
        f"with `approved_by` set to the approver above and `decision_note` set "
        f"to their note. Where the proposal above carries a field the request "
        f"also has (an exact `sql` string, for instance), copy that value "
        f"across character for character rather than composing it again. "
        f"Change nothing else. Then report what the specialist returned."
    )


def summarise(event: Any) -> str:
    """Return a one-line, log-friendly description of an event.

    Args:
        event: An ADK event.

    Returns:
        A compact single-line summary.
    """
    bits = [event.author]
    if calls := [c.name for c in event.get_function_calls()]:
        bits.append(f"calls={calls}")
    if responses := [r.name for r in event.get_function_responses()]:
        bits.append(f"resp={responses}")
    text = "".join(
        p.text or "" for p in (event.content.parts if event.content else []) or []
    ).strip()
    if text:
        bits.append(f"text={text[:160]!r}")
    if event.error_message:
        bits.append(f"error={event.error_message!r}")
    return " | ".join(bits)


def _unpack(payload: Any) -> list[str]:
    """Flatten a tool result into every string it contains.

    A peer's reply arrives as a *string* tool result, but ADK routinely wraps a
    tool's return value — ``{"result": "<the peer's text>"}``. Serialising that
    wrapper with ``json.dumps`` would escape the peer's own JSON *inside* a
    string value, where no object scanner can find it. That is not hypothetical:
    it is why the first end-to-end run recorded no approval case even though the
    specialist reported the proposal correctly.

    So both the wrapper and the strings inside it are returned, and the scanner
    is run over all of them.

    Args:
        payload: A function response's payload.

    Returns:
        Every string worth scanning.
    """
    if payload is None:
        return []
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        out = [json.dumps(payload, default=str)]
        for value in payload.values():
            out.extend(_unpack(value))
        return out
    if isinstance(payload, list):
        out: list[str] = []
        for item in payload:
            out.extend(_unpack(item))
        return out
    return []


def reply_texts(event: Any) -> list[str]:
    """Return every text an event carries that could hold a proposal.

    Both the agent's own words and any tool result are candidates: a specialist
    reached over A2A arrives as a tool result on the caller.

    Args:
        event: An ADK event.

    Returns:
        The text bodies to scan.
    """
    texts: list[str] = []
    for response in event.get_function_responses():
        texts.extend(_unpack(response.response))
    if event.content and event.content.parts:
        body = "".join(p.text or "" for p in event.content.parts).strip()
        if body:
            texts.append(body)
    return texts
