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

"""The pending-approval store: what is waiting for a human, and who holds it.

Split out of ``app/cluster/hitl.py`` (which keeps capture and resume) because
durability turned a one-line dict into a state machine worth reading on its own.
Both live under ``app/cluster/`` per the layering table in AGENTS.md.

Two backends, chosen by whether a database is configured:

- :class:`InMemoryApprovalStore` — the default. Correct but per-pod: approvals
  die with the process. Used by the hermetic tests, local runs, and any agent
  deployed with ``DB_BACKEND=none``.
- :class:`DatabaseApprovalStore` — the ``hitl_approvals`` table from migration
  ``0004``, on the shared engine from ``app/cluster/db.py``.

The state machine (D4.2)
------------------------
``pending -> deciding -> approved | rejected``, with ``expired`` reserved for a
future retention sweep::

    pending    nobody is working on it; answerable
    deciding   a decision was accepted and the resume is running (or its owner
               died). The decision payload is ALREADY written.
    approved   the continuation completed
    rejected   the continuation completed, negatively

Why ``deciding`` carries the decision
-------------------------------------
The claim writes ``decision`` / ``decided_by`` / ``decided_at`` in the *same*
statement that sets ``deciding``. That is what makes an interrupted resume
recoverable without asking the human again: a sweeper finds the row and knows
what to re-drive it with. Flipping to ``approved``/``rejected`` happens only once
the continuation actually finishes, so the two are never confused.

Why the lease is reclaimable
----------------------------
A pod killed mid-resume runs no ``except`` handler, so the claim is never rolled
back. Without a way to reclaim it, a durable ``deciding`` row would answer
"already decided" forever — strictly worse than the in-memory store, which at
least forgot the whole thing on restart.

Liveness is **measured, not inferred from identity**. While a resume runs, its
owner renews the lease (:func:`heartbeat`, calling :meth:`ApprovalStore.touch`),
so a ``deciding_since`` that has stopped advancing means the owner died — however
many replicas exist. Both reclaim paths therefore apply the same test, staleness
against ``lease_ttl``: :meth:`ApprovalStore.claim` for a human retrying, and
:meth:`ApprovalStore.sweep_abandoned` for unattended recovery.

An earlier version reclaimed any lease whose ``deciding_by`` was not this
process. That recovers instantly, but only makes sense at one replica: with two,
"not me" also matches a peer that is alive and mid-resume, and stealing that
lease drives the same human decision twice. ``deciding_by`` is now purely
diagnostic — it tells an operator which pod holds a row — and no logic branches
on it. That is what lets the entry-point agent scale out.

The cost is that recovery waits out the TTL instead of happening at startup,
which is why :data:`DEFAULT_LEASE_TTL` is seconds rather than minutes: it only
has to outlast a few missed heartbeats, not the longest plausible resume.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from loguru import logger
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from app.cluster.db import Database

PENDING = "pending"
DECIDING = "deciding"
APPROVED = "approved"
REJECTED = "rejected"
EXPIRED = "expired"

#: Kept in sync with the CHECK constraint in migration 0004.
STATUSES = (PENDING, DECIDING, APPROVED, REJECTED, EXPIRED)

TERMINAL = (APPROVED, REJECTED, EXPIRED)

LEASE_TTL_ENV = "HITL_LEASE_TTL_SECONDS"
#: How long a lease may go un-renewed before it is presumed abandoned. This is a
#: liveness timeout, not a budget for the resume: a running resume renews its
#: lease, so the TTL only has to outlast a few missed heartbeats. It is therefore
#: also how long unattended recovery takes after a pod dies.
DEFAULT_LEASE_TTL = 30

#: Renewals per TTL. Three tolerates two consecutive misses -- a slow query, a GC
#: pause -- without a healthy owner ever looking dead.
HEARTBEATS_PER_TTL = 3


def _lease_ttl() -> timedelta:
    """Return the configured lease TTL.

    Returns:
        The TTL as a :class:`~datetime.timedelta`, falling back to
        :data:`DEFAULT_LEASE_TTL` when unset or malformed.
    """
    raw = os.environ.get(LEASE_TTL_ENV, "").strip()
    try:
        seconds = int(raw) if raw else DEFAULT_LEASE_TTL
    except ValueError:
        seconds = DEFAULT_LEASE_TTL
    return timedelta(seconds=max(seconds, 1))


def lease_ttl_seconds() -> float:
    """Return the configured lease TTL in seconds.

    Exposed so the serving layer can run its recovery sweep on the same cadence
    the lease is judged by, instead of inventing a second interval that could
    drift out of step with it.

    Returns:
        The TTL in seconds.
    """
    return _lease_ttl().total_seconds()


def _make_owner() -> str:
    """Return an identity for this process, stable for its lifetime.

    Pod name alone is not enough: a restarted pod in a Deployment can reuse it,
    and then a dead predecessor's lease would look like our own. Appending a
    per-process UUID makes "not me" mean "not this process", which is the
    property :meth:`ApprovalStore.sweep_abandoned` relies on.

    Returns:
        A string of the form ``<hostname>/<uuid4-hex-12>``.
    """
    host = os.environ.get("HOSTNAME", "").strip() or socket.gethostname()
    return f"{host}/{uuid.uuid4().hex[:12]}"


#: This process's lease owner. Module-level so it is fixed at import.
OWNER = _make_owner()


class ClaimOutcome(StrEnum):
    """Why a :meth:`ApprovalStore.claim` succeeded or failed."""

    CLAIMED = "claimed"
    """The caller now owns the lease and must resume."""

    NOT_FOUND = "not_found"
    """No approval with that id."""

    ALREADY_DECIDED = "already_decided"
    """Terminal already; report the recorded outcome rather than resuming."""

    IN_PROGRESS = "in_progress"
    """Someone else holds a lease that has not expired yet."""


@dataclass
class PendingApproval:
    """A captured pause awaiting — or undergoing — a human decision."""

    approval_id: str
    kind: str
    """Which pause this is: ``confirmation``, ``input`` or ``other``."""
    call_id: str
    call_name: str
    tool_name: str
    """The tool the human is being asked about (``""`` for a plain question)."""
    message: str
    """What to show the human: the confirmation hint or the question."""
    args: dict[str, Any] = field(default_factory=dict)
    app_name: str = ""
    user_id: str = ""
    session_id: str = ""
    invocation_id: str = ""
    author: str = ""
    """Which agent paused -- the local agent, or a peer reached over A2A."""
    status: str = PENDING
    decision: dict[str, Any] | None = None
    decided_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    deciding_since: datetime | None = None
    deciding_by: str | None = None
    resumed_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        """Return a JSON-safe view for the API.

        Returns:
            A dict of the fields the HTTP surface exposes.
        """

        def stamp(value: datetime | None) -> str | None:
            return None if value is None else value.isoformat(timespec="seconds")

        return {
            "approval_id": self.approval_id,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "message": self.message,
            "args": self.args,
            "agent": self.author,
            "session_id": self.session_id,
            "status": self.status,
            "decision": self.decision,
            "created_at": stamp(self.created_at),
            "decided_at": stamp(self.decided_at),
            "resumed_at": stamp(self.resumed_at),
        }


def lease_is_stale(
    approval: PendingApproval, *, now: datetime, lease_ttl: timedelta
) -> bool:
    """Whether a ``deciding`` approval's lease may be taken over.

    Args:
        approval: The row to judge.
        now: Current time (injected so tests need no clock control).
        lease_ttl: How long a lease stays valid.

    Returns:
        ``True`` when the approval is ``deciding`` and its lease has expired.
    """
    if approval.status != DECIDING:
        return False
    if approval.deciding_since is None:
        # A deciding row with no lease start is corrupt; treat it as stale so it
        # cannot become permanently unanswerable.
        return True
    return now - approval.deciding_since >= lease_ttl


class ApprovalStore(abc.ABC):
    """What is waiting for a human, and who currently holds each item."""

    @abc.abstractmethod
    async def add(self, approval: PendingApproval) -> bool:
        """Record a newly captured pause.

        Args:
            approval: The pause to store.

        Returns:
            ``True`` if it was stored, ``False`` if this
            ``(session_id, call_id)`` was already known — ADK resumption is
            at-least-once, so the same pause can be observed twice.
        """

    @abc.abstractmethod
    async def get(self, approval_id: str) -> PendingApproval | None:
        """Return one approval.

        Args:
            approval_id: The id to look up.

        Returns:
            The approval, or ``None`` when unknown.
        """

    @abc.abstractmethod
    async def list_by_status(self, status: str) -> list[PendingApproval]:
        """Return approvals in a given status, oldest first.

        Args:
            status: One of :data:`STATUSES`.

        Returns:
            The matching approvals.
        """

    @abc.abstractmethod
    async def claim(
        self,
        approval_id: str,
        *,
        decision: dict[str, Any],
        decided_by: str,
        now: datetime | None = None,
        lease_ttl: timedelta | None = None,
    ) -> tuple[ClaimOutcome, PendingApproval | None]:
        """Atomically take the lease and record the human's decision.

        Writing the decision here — rather than after a successful resume — is
        what lets an interrupted resume be re-driven without asking the human
        again (D4.2).

        Args:
            approval_id: The approval to claim.
            decision: The decision payload to persist now.
            decided_by: Who decided, for audit.
            now: Current time; defaults to wall clock.
            lease_ttl: Lease validity; defaults to :func:`_lease_ttl`.

        Returns:
            The outcome and, when known, the approval it refers to.
        """

    @abc.abstractmethod
    async def complete(
        self, approval_id: str, *, status: str, resumed: bool = True
    ) -> None:
        """Mark a claimed approval finished and release its lease.

        Args:
            approval_id: The approval to finish.
            status: ``approved`` or ``rejected``.
            resumed: Whether the continuation actually produced an answer. Pass
                ``False`` when the decision stands and its effect already
                happened, but the conversation could not be replayed --
                ``resumed_at`` then stays ``NULL`` instead of claiming a
                completion that never occurred. See
                :func:`app.cluster.hitl.redrive_abandoned`.
        """

    @abc.abstractmethod
    async def release(self, approval_id: str) -> None:
        """Return a claimed approval to ``pending`` so it can be retried.

        Clears the recorded decision: the resume failed, and the human may well
        answer differently next time.

        Args:
            approval_id: The approval to release.
        """

    @abc.abstractmethod
    async def touch(self, approval_id: str, *, now: datetime | None = None) -> bool:
        """Renew this process's lease on a claimed approval.

        Called on a timer by :func:`heartbeat` for as long as a resume runs. A
        lease that stops being renewed is what "the owner died" means here, so
        this is the signal every reclaim decision rests on.

        Only renews a lease this process actually holds: if ours was already
        reclaimed, resurrecting it would let two owners believe they hold the
        same row.

        Args:
            approval_id: The approval whose lease to renew.
            now: Current time; defaults to wall clock.

        Returns:
            ``True`` if the lease was renewed, ``False`` if this process no
            longer holds it.
        """

    @abc.abstractmethod
    async def sweep_abandoned(
        self, *, now: datetime | None = None, lease_ttl: timedelta | None = None
    ) -> list[PendingApproval]:
        """Take over every lease that has stopped being renewed.

        Safe to call from any replica, and to call repeatedly: a live resume
        renews its lease, so only leases whose owner stopped heartbeating are
        eligible. Deliberately *not* keyed on ``deciding_by`` -- see the module
        docstring for why that shortcut pinned the entry point to one replica.

        Args:
            now: Current time; defaults to wall clock.
            lease_ttl: How long a lease may go un-renewed; defaults to
                :envvar:`HITL_LEASE_TTL_SECONDS`.

        Returns:
            The approvals now owned by this process, ready to be re-driven.
        """


class InMemoryApprovalStore(ApprovalStore):
    """Per-pod store. Loses everything on restart — that is R6, by design."""

    def __init__(self) -> None:
        """Start empty."""
        self._items: dict[str, PendingApproval] = {}

    async def add(self, approval: PendingApproval) -> bool:
        """Store a pause unless its call is already known."""
        if any(
            item.call_id == approval.call_id and item.session_id == approval.session_id
            for item in self._items.values()
        ):
            return False
        self._items[approval.approval_id] = approval
        return True

    async def get(self, approval_id: str) -> PendingApproval | None:
        """Return a copy so callers cannot mutate stored state by accident."""
        found = self._items.get(approval_id)
        return None if found is None else replace(found)

    async def list_by_status(self, status: str) -> list[PendingApproval]:
        """Return matching approvals, oldest first."""
        return sorted(
            (replace(item) for item in self._items.values() if item.status == status),
            key=lambda item: item.created_at,
        )

    async def claim(
        self,
        approval_id: str,
        *,
        decision: dict[str, Any],
        decided_by: str,
        now: datetime | None = None,
        lease_ttl: timedelta | None = None,
    ) -> tuple[ClaimOutcome, PendingApproval | None]:
        """Take the lease if the row is pending or its lease has expired."""
        moment = now or datetime.now(UTC)
        ttl = lease_ttl or _lease_ttl()
        found = self._items.get(approval_id)
        if found is None:
            return ClaimOutcome.NOT_FOUND, None
        if found.status in TERMINAL:
            return ClaimOutcome.ALREADY_DECIDED, replace(found)
        if found.status == DECIDING and not lease_is_stale(
            found, now=moment, lease_ttl=ttl
        ):
            return ClaimOutcome.IN_PROGRESS, replace(found)

        found.status = DECIDING
        found.decision = decision
        found.decided_by = decided_by
        found.decided_at = moment
        found.deciding_since = moment
        found.deciding_by = OWNER
        return ClaimOutcome.CLAIMED, replace(found)

    async def complete(
        self, approval_id: str, *, status: str, resumed: bool = True
    ) -> None:
        """Finish a claimed approval."""
        found = self._items.get(approval_id)
        if found is None:
            return
        found.status = status
        found.resumed_at = datetime.now(UTC) if resumed else None
        found.deciding_since = None
        found.deciding_by = None

    async def release(self, approval_id: str) -> None:
        """Put a claimed approval back to pending."""
        found = self._items.get(approval_id)
        if found is None:
            return
        found.status = PENDING
        found.decision = None
        found.decided_by = None
        found.decided_at = None
        found.deciding_since = None
        found.deciding_by = None

    async def touch(self, approval_id: str, *, now: datetime | None = None) -> bool:
        """Renew a lease this process holds."""
        found = self._items.get(approval_id)
        if found is None or found.status != DECIDING or found.deciding_by != OWNER:
            return False
        found.deciding_since = now or datetime.now(UTC)
        return True

    async def sweep_abandoned(
        self, *, now: datetime | None = None, lease_ttl: timedelta | None = None
    ) -> list[PendingApproval]:
        """Take over leases that stopped being renewed."""
        moment = now or datetime.now(UTC)
        ttl = lease_ttl or _lease_ttl()
        taken: list[PendingApproval] = []
        for item in self._items.values():
            if lease_is_stale(item, now=moment, lease_ttl=ttl):
                item.deciding_by = OWNER
                item.deciding_since = moment
                taken.append(replace(item))
        return taken


def _json_type() -> sa.types.TypeEngine[Any]:
    """Return JSONB on PostgreSQL and generic JSON elsewhere.

    Returns:
        The dialect-appropriate JSON type, matching migration 0004.
    """
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


#: Core table mirroring migration 0004. Unqualified on purpose: ``search_path``
#: routes it into the running agent's schema (see app/cluster/db.py).
METADATA = sa.MetaData()

APPROVALS = sa.Table(
    "hitl_approvals",
    METADATA,
    sa.Column("approval_id", sa.String(32), primary_key=True),
    sa.Column("app_name", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("session_id", sa.String(128), nullable=False),
    sa.Column("invocation_id", sa.String(128), nullable=False),
    sa.Column("function_call_id", sa.String(128), nullable=False),
    sa.Column("call_name", sa.String(128), nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("tool_name", sa.String(128), nullable=False),
    sa.Column("author", sa.String(128), nullable=False),
    sa.Column("message", sa.Text(), nullable=False),
    sa.Column("args", _json_type(), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("decision", _json_type(), nullable=True),
    sa.Column("decided_by", sa.String(256), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("deciding_since", sa.DateTime(timezone=True), nullable=True),
    sa.Column("deciding_by", sa.String(256), nullable=True),
    sa.Column("resumed_at", sa.DateTime(timezone=True), nullable=True),
)


def _row_to_approval(row: sa.Row[Any]) -> PendingApproval:
    """Rebuild a :class:`PendingApproval` from a database row.

    Args:
        row: A row selected from :data:`APPROVALS`.

    Returns:
        The reconstructed approval.
    """
    mapping = row._mapping
    return PendingApproval(
        approval_id=mapping["approval_id"],
        kind=mapping["kind"],
        call_id=mapping["function_call_id"],
        call_name=mapping["call_name"],
        tool_name=mapping["tool_name"],
        message=mapping["message"],
        args=dict(mapping["args"] or {}),
        app_name=mapping["app_name"],
        user_id=mapping["user_id"],
        session_id=mapping["session_id"],
        invocation_id=mapping["invocation_id"],
        author=mapping["author"],
        status=mapping["status"],
        decision=mapping["decision"],
        decided_by=mapping["decided_by"],
        created_at=_as_utc(mapping["created_at"]),
        decided_at=_as_utc(mapping["decided_at"]),
        deciding_since=_as_utc(mapping["deciding_since"]),
        deciding_by=mapping["deciding_by"],
        resumed_at=_as_utc(mapping["resumed_at"]),
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


class DatabaseApprovalStore(ApprovalStore):
    """Durable store over the ``hitl_approvals`` table from migration 0004."""

    def __init__(self, database: Database) -> None:
        """Bind to the process-wide engine.

        Args:
            database: The shared :class:`~app.cluster.db.Database`.
        """
        self._database = database

    async def add(self, approval: PendingApproval) -> bool:
        """Insert a pause, ignoring a duplicate observation of the same call."""
        engine = self._database.engine()
        async with engine.begin() as conn:
            existing = await conn.execute(
                sa.select(APPROVALS.c.approval_id).where(
                    APPROVALS.c.session_id == approval.session_id,
                    APPROVALS.c.function_call_id == approval.call_id,
                )
            )
            if existing.first() is not None:
                return False
            await conn.execute(
                sa.insert(APPROVALS).values(
                    approval_id=approval.approval_id,
                    app_name=approval.app_name,
                    user_id=approval.user_id,
                    session_id=approval.session_id,
                    invocation_id=approval.invocation_id,
                    function_call_id=approval.call_id,
                    call_name=approval.call_name,
                    kind=approval.kind,
                    tool_name=approval.tool_name,
                    author=approval.author,
                    message=approval.message,
                    args=approval.args,
                    status=approval.status,
                    created_at=approval.created_at,
                )
            )
        return True

    async def get(self, approval_id: str) -> PendingApproval | None:
        """Read one approval."""
        engine = self._database.engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.select(APPROVALS).where(APPROVALS.c.approval_id == approval_id)
            )
            row = result.first()
        return None if row is None else _row_to_approval(row)

    async def list_by_status(self, status: str) -> list[PendingApproval]:
        """Read every approval in a status, oldest first."""
        engine = self._database.engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                sa.select(APPROVALS)
                .where(APPROVALS.c.status == status)
                .order_by(APPROVALS.c.created_at)
            )
            rows = result.fetchall()
        return [_row_to_approval(row) for row in rows]

    async def claim(
        self,
        approval_id: str,
        *,
        decision: dict[str, Any],
        decided_by: str,
        now: datetime | None = None,
        lease_ttl: timedelta | None = None,
    ) -> tuple[ClaimOutcome, PendingApproval | None]:
        """Claim in one UPDATE, so two callers cannot both win."""
        moment = now or datetime.now(UTC)
        cutoff = moment - (lease_ttl or _lease_ttl())
        engine = self._database.engine()

        async with engine.begin() as conn:
            # The WHERE clause is the concurrency control: pending, or a lease
            # that has outlived its TTL. Doing it as a single conditional UPDATE
            # (rather than read-then-write) is what makes the claim atomic.
            claimed = await conn.execute(
                sa.update(APPROVALS)
                .where(
                    APPROVALS.c.approval_id == approval_id,
                    sa.or_(
                        APPROVALS.c.status == PENDING,
                        sa.and_(
                            APPROVALS.c.status == DECIDING,
                            sa.or_(
                                APPROVALS.c.deciding_since.is_(None),
                                APPROVALS.c.deciding_since <= cutoff,
                            ),
                        ),
                    ),
                )
                .values(
                    status=DECIDING,
                    decision=decision,
                    decided_by=decided_by,
                    decided_at=moment,
                    deciding_since=moment,
                    deciding_by=OWNER,
                )
                .returning(APPROVALS)
            )
            row = claimed.first()
            if row is not None:
                return ClaimOutcome.CLAIMED, _row_to_approval(row)

            # Lost the race (or never existed): report why.
            current = await conn.execute(
                sa.select(APPROVALS).where(APPROVALS.c.approval_id == approval_id)
            )
            existing = current.first()

        if existing is None:
            return ClaimOutcome.NOT_FOUND, None
        approval = _row_to_approval(existing)
        if approval.status in TERMINAL:
            return ClaimOutcome.ALREADY_DECIDED, approval
        return ClaimOutcome.IN_PROGRESS, approval

    async def complete(
        self, approval_id: str, *, status: str, resumed: bool = True
    ) -> None:
        """Record the finished outcome and drop the lease."""
        engine = self._database.engine()
        async with engine.begin() as conn:
            await conn.execute(
                sa.update(APPROVALS)
                .where(APPROVALS.c.approval_id == approval_id)
                .values(
                    status=status,
                    resumed_at=datetime.now(UTC) if resumed else None,
                    deciding_since=None,
                    deciding_by=None,
                )
            )

    async def release(self, approval_id: str) -> None:
        """Return the approval to pending and forget the failed decision."""
        engine = self._database.engine()
        async with engine.begin() as conn:
            await conn.execute(
                sa.update(APPROVALS)
                .where(APPROVALS.c.approval_id == approval_id)
                .values(
                    status=PENDING,
                    decision=None,
                    decided_by=None,
                    decided_at=None,
                    deciding_since=None,
                    deciding_by=None,
                )
            )

    async def touch(self, approval_id: str, *, now: datetime | None = None) -> bool:
        """Renew a lease this process holds, in one statement."""
        moment = now or datetime.now(UTC)
        engine = self._database.engine()
        async with engine.begin() as conn:
            result = await conn.execute(
                sa.update(APPROVALS)
                .where(
                    APPROVALS.c.approval_id == approval_id,
                    APPROVALS.c.status == DECIDING,
                    # Never resurrect a lease that was already reclaimed: two
                    # owners believing they hold the same row is the one thing
                    # the lease exists to prevent.
                    APPROVALS.c.deciding_by == OWNER,
                )
                .values(deciding_since=moment)
            )
        return bool(result.rowcount)

    async def sweep_abandoned(
        self, *, now: datetime | None = None, lease_ttl: timedelta | None = None
    ) -> list[PendingApproval]:
        """Take over every lease that stopped being renewed."""
        moment = now or datetime.now(UTC)
        cutoff = moment - (lease_ttl or _lease_ttl())
        engine = self._database.engine()
        async with engine.begin() as conn:
            # Two replicas can sweep at the same instant. This is safe without
            # extra locking: the loser blocks on the row lock, and PostgreSQL
            # re-evaluates the WHERE against the committed version, which by
            # then carries a fresh deciding_since and no longer matches. So a
            # row is handed to exactly one sweeper.
            result = await conn.execute(
                sa.update(APPROVALS)
                .where(
                    APPROVALS.c.status == DECIDING,
                    sa.or_(
                        APPROVALS.c.deciding_since.is_(None),
                        APPROVALS.c.deciding_since <= cutoff,
                    ),
                )
                .values(deciding_by=OWNER, deciding_since=moment)
                .returning(APPROVALS)
            )
            rows = result.fetchall()
        return [_row_to_approval(row) for row in rows]


@contextlib.asynccontextmanager
async def heartbeat(
    store: ApprovalStore,
    approval_id: str,
    *,
    lease_ttl: timedelta | None = None,
) -> AsyncIterator[None]:
    """Renew a claimed approval's lease for as long as the body runs.

    This is what makes a lease mean "the owner is alive" rather than "the owner
    started recently", and therefore what lets several replicas run at once: a
    resume still in progress keeps its lease fresh, so no sweeper can take it.

    Failures to renew are swallowed. A missed beat is survivable -- the TTL
    allows for :data:`HEARTBEATS_PER_TTL` of them -- and letting a transient
    database error abort a resume that is otherwise fine would trade a small
    problem for a larger one.

    Args:
        store: The store holding the lease.
        approval_id: The claimed approval.
        lease_ttl: Override the TTL the interval is derived from; for tests.

    Yields:
        ``None``; the caller does its work inside the context.
    """
    ttl = (lease_ttl or _lease_ttl()).total_seconds()
    interval = max(ttl / HEARTBEATS_PER_TTL, 0.01)

    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await store.touch(approval_id)
            except Exception:
                logger.warning(
                    "HITL: failed to renew lease on approval {}", approval_id
                )

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        # Await the cancellation, so the beat cannot outlive the resume and
        # renew a lease this process has already finished with.
        with contextlib.suppress(asyncio.CancelledError):
            await task


def build_approval_store(database: Database) -> ApprovalStore:
    """Return the durable store when a database is configured, else in-memory.

    Args:
        database: The shared database, from the injector.

    Returns:
        A :class:`DatabaseApprovalStore` or an :class:`InMemoryApprovalStore`.
    """
    if database.enabled:
        return DatabaseApprovalStore(database)
    return InMemoryApprovalStore()
