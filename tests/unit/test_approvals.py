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

"""Unit tests for the approval store's state machine (D4 + D4.2).

Every test runs against **both** backends: the in-memory store and the real
SQLAlchemy one on a file-backed SQLite database. That symmetry is the point --
the durable store must behave identically to the dict it replaces, except for
surviving a restart, and a divergence between the two is exactly the kind of bug
that only shows up in the cluster. Set ``HITL_TEST_PG_DSN`` to add real
PostgreSQL as a third backend.

The tests are ``async`` rather than wrapping each call in ``asyncio.run``: a
fresh event loop per operation would leave asyncpg's pooled connections bound to
a dead loop, which is a property of the harness and not of the store.

The crash tests never call ``release``: a pod killed by SIGKILL runs no
``except`` handler, so simulating a crash means *abandoning* the claim, not
rolling it back.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.cluster import approvals
from app.cluster.approvals import (
    APPROVED,
    DECIDING,
    PENDING,
    REJECTED,
    ApprovalStore,
    ClaimOutcome,
    DatabaseApprovalStore,
    InMemoryApprovalStore,
    PendingApproval,
)
from app.cluster.db import URL, Database, DatabaseConfig

#: Opt-in third backend. Set to an async DSN
#: (``postgresql+asyncpg://user:pw@host:port/db``) to run this whole suite
#: against real PostgreSQL -- the only way to exercise the conditional
#: ``UPDATE ... RETURNING`` claim, JSONB round-tripping and timezone-aware
#: timestamps as the cluster will. SQLite silently tolerates all three.
PG_DSN_ENV = "HITL_TEST_PG_DSN"


def _backends() -> list[str]:
    names = ["memory", "sqlite"]
    if os.environ.get(PG_DSN_ENV, "").strip():
        names.append("postgres")
    return names


def _approval(approval_id: str = "a1", call_id: str = "adk-1") -> PendingApproval:
    return PendingApproval(
        approval_id=approval_id,
        kind="confirmation",
        call_id=call_id,
        call_name="adk_request_confirmation",
        tool_name="publish_result",
        message="Approve publishing 42?",
        args={"value": "42"},
        app_name="app",
        user_id="u",
        session_id="s",
        invocation_id="e-1",
        author="math",
    )


@pytest_asyncio.fixture(params=_backends(), loop_scope="function")
async def store(
    request: pytest.FixtureRequest, tmp_path
) -> AsyncIterator[ApprovalStore]:
    """Yield each available backend in turn.

    SQLite is file-backed rather than ``:memory:`` so the schema survives across
    the separate connections each store method opens.
    """
    if request.param == "memory":
        yield InMemoryApprovalStore()
        return

    dsn = (
        os.environ[PG_DSN_ENV]
        if request.param == "postgres"
        else f"sqlite+aiosqlite:///{tmp_path}/hitl.db"
    )
    database = Database(DatabaseConfig.from_env({"DB_BACKEND": URL, "DB_URL": dsn}))

    engine = database.engine()
    async with engine.begin() as conn:
        # Start from a clean table so a real-database run is repeatable.
        await conn.run_sync(approvals.METADATA.drop_all)
        await conn.run_sync(approvals.METADATA.create_all)

    yield DatabaseApprovalStore(database)
    await database.aclose()


# --- capture -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_stores_a_pause(store: ApprovalStore) -> None:
    assert await store.add(_approval()) is True
    found = await store.get("a1")
    assert found is not None
    assert found.status == PENDING
    assert found.tool_name == "publish_result"
    assert found.args == {"value": "42"}


@pytest.mark.asyncio
async def test_add_is_idempotent_per_session_and_call(store: ApprovalStore) -> None:
    """ADK resumption is at-least-once, so the same pause arrives twice."""
    assert await store.add(_approval("a1", "adk-1")) is True
    assert await store.add(_approval("a2", "adk-1")) is False
    assert len(await store.list_by_status(PENDING)) == 1


@pytest.mark.asyncio
async def test_unknown_approval_reads_as_none(store: ApprovalStore) -> None:
    assert await store.get("nope") is None


# --- the claim ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_records_the_decision_before_the_resume(
    store: ApprovalStore,
) -> None:
    """D4.2: the decision is persisted by the CLAIM, not after a good resume.

    This is what lets a sweeper re-drive an interrupted resume without asking
    the human again.
    """
    await store.add(_approval())
    outcome, claimed = await store.claim(
        "a1", decision={"approved": True, "text": "ok"}, decided_by="me"
    )
    assert outcome is ClaimOutcome.CLAIMED
    assert claimed is not None

    # Read it back from the store, not from the return value: the point is that
    # it is durably recorded, not merely returned to the caller.
    stored = await store.get("a1")
    assert stored is not None
    assert stored.status == DECIDING
    assert stored.decision == {"approved": True, "text": "ok"}
    assert stored.decided_by == "me"
    assert stored.decided_at is not None
    assert stored.deciding_since is not None
    assert stored.deciding_by == approvals.OWNER
    # Not finished yet -- the continuation has not run.
    assert stored.resumed_at is None


@pytest.mark.asyncio
async def test_claim_is_exclusive_while_the_lease_is_live(
    store: ApprovalStore,
) -> None:
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True}, decided_by="first")

    outcome, _ = await store.claim(
        "a1", decision={"approved": False}, decided_by="second"
    )
    assert outcome is ClaimOutcome.IN_PROGRESS
    # The first decision stands.
    stored = await store.get("a1")
    assert stored is not None
    assert stored.decided_by == "first"


@pytest.mark.asyncio
async def test_claim_of_unknown_approval_reports_not_found(
    store: ApprovalStore,
) -> None:
    outcome, found = await store.claim("nope", decision={}, decided_by="")
    assert outcome is ClaimOutcome.NOT_FOUND
    assert found is None


@pytest.mark.asyncio
async def test_claim_of_finished_approval_reports_already_decided(
    store: ApprovalStore,
) -> None:
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True}, decided_by="me")
    await store.complete("a1", status=APPROVED)

    outcome, found = await store.claim(
        "a1", decision={"approved": True}, decided_by="x"
    )
    assert outcome is ClaimOutcome.ALREADY_DECIDED
    assert found is not None
    assert found.status == APPROVED


# --- the lease (D4.2) --------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_lease_can_be_reclaimed(store: ApprovalStore) -> None:
    """The R4 regression guard: a crashed resume must not lock the row forever.

    A pod killed mid-resume never runs the rollback, so the row stays
    ``deciding``. Without a reclaimable lease every retry would answer
    "already_decided" and the approval would be permanently unanswerable --
    strictly worse than the in-memory store it replaced.
    """
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True}, decided_by="crashed")

    # No release() -- that is the crash.
    later = datetime.now(UTC) + timedelta(hours=1)
    outcome, _ = await store.claim(
        "a1",
        decision={"approved": True, "text": "retry"},
        decided_by="human",
        now=later,
        lease_ttl=timedelta(minutes=5),
    )
    assert outcome is ClaimOutcome.CLAIMED
    stored = await store.get("a1")
    assert stored is not None
    assert stored.decided_by == "human"


@pytest.mark.asyncio
async def test_lease_is_not_reclaimed_before_the_ttl(store: ApprovalStore) -> None:
    """Stealing a live resume would drive the same invocation twice."""
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True}, decided_by="running")

    soon = datetime.now(UTC) + timedelta(seconds=5)
    outcome, _ = await store.claim(
        "a1",
        decision={"approved": True},
        decided_by="impatient",
        now=soon,
        lease_ttl=timedelta(minutes=5),
    )
    assert outcome is ClaimOutcome.IN_PROGRESS


@pytest.mark.asyncio
async def test_complete_finishes_and_drops_the_lease(store: ApprovalStore) -> None:
    await store.add(_approval())
    await store.claim("a1", decision={"approved": False}, decided_by="me")
    await store.complete("a1", status=REJECTED)

    stored = await store.get("a1")
    assert stored is not None
    assert stored.status == REJECTED
    assert stored.resumed_at is not None
    assert stored.deciding_since is None
    assert stored.deciding_by is None


@pytest.mark.asyncio
async def test_release_returns_the_approval_to_pending(store: ApprovalStore) -> None:
    """A failed resume must leave the approval answerable, decision forgotten."""
    await store.add(_approval())
    await store.claim(
        "a1", decision={"approved": True, "text": "oops"}, decided_by="me"
    )
    await store.release("a1")

    stored = await store.get("a1")
    assert stored is not None
    assert stored.status == PENDING
    assert stored.decision is None
    assert stored.decided_by is None
    assert stored.deciding_since is None


# --- the startup sweep -------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_takes_over_a_lease_that_stopped_being_renewed(
    store: ApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead owner is one whose lease went stale, whoever it was.

    Rewriting ``OWNER`` stands in for the pod restart; what makes the row
    eligible is the un-renewed lease, not the change of identity.
    """
    monkeypatch.setattr(approvals, "OWNER", "pod-a/1111")
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True, "text": "yes"}, decided_by="me")

    monkeypatch.setattr(approvals, "OWNER", "pod-b/2222")
    later = datetime.now(UTC) + timedelta(hours=1)
    taken = await store.sweep_abandoned(now=later, lease_ttl=timedelta(seconds=30))

    assert [item.approval_id for item in taken] == ["a1"]
    # The decision survived the crash, which is what makes re-driving possible
    # without going back to the human.
    assert taken[0].decision == {"approved": True, "text": "yes"}
    stored = await store.get("a1")
    assert stored is not None
    assert stored.deciding_by == "pod-b/2222"


@pytest.mark.asyncio
async def test_sweep_spares_another_replicas_live_lease(
    store: ApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property that lets the entry point scale out.

    The previous design reclaimed any lease whose owner was not this process,
    which at two replicas means stealing a peer's in-flight resume and driving
    the same human decision twice. Liveness is now the renewed lease, so a
    foreign owner is left alone until it stops heartbeating.
    """
    monkeypatch.setattr(approvals, "OWNER", "pod-a/1111")
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True}, decided_by="me")

    # A different, *live* replica sweeps while pod A is still working.
    monkeypatch.setattr(approvals, "OWNER", "pod-b/2222")
    assert await store.sweep_abandoned(lease_ttl=timedelta(seconds=30)) == []

    stored = await store.get("a1")
    assert stored is not None
    assert stored.deciding_by == "pod-a/1111", "pod A must keep its lease"


@pytest.mark.asyncio
async def test_sweep_ignores_leases_this_process_still_owns(
    store: ApprovalStore,
) -> None:
    """Our own in-flight resume must not be swept out from under us."""
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True}, decided_by="me")

    assert await store.sweep_abandoned() == []


@pytest.mark.asyncio
async def test_sweep_ignores_pending_and_finished_rows(store: ApprovalStore) -> None:
    await store.add(_approval("a1", "adk-1"))
    await store.add(_approval("a2", "adk-2"))
    await store.claim("a2", decision={"approved": True}, decided_by="me")
    await store.complete("a2", status=APPROVED)

    later = datetime.now(UTC) + timedelta(hours=1)
    assert await store.sweep_abandoned(now=later) == []


@pytest.mark.asyncio
async def test_two_sweepers_cannot_both_take_the_same_row(
    store: ApprovalStore,
) -> None:
    """Taking a lease renews it, so the second sweeper finds nothing."""
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True}, decided_by="me")

    later = datetime.now(UTC) + timedelta(hours=1)
    ttl = timedelta(seconds=30)
    first = await store.sweep_abandoned(now=later, lease_ttl=ttl)
    second = await store.sweep_abandoned(now=later, lease_ttl=ttl)

    assert [item.approval_id for item in first] == ["a1"]
    assert second == []


# --- the heartbeat -----------------------------------------------------------


@pytest.mark.asyncio
async def test_touch_renews_our_own_lease(store: ApprovalStore) -> None:
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True}, decided_by="me")
    before = await store.get("a1")
    assert before is not None and before.deciding_since is not None

    later = before.deciding_since + timedelta(minutes=5)
    assert await store.touch("a1", now=later) is True

    after = await store.get("a1")
    assert after is not None
    assert after.deciding_since is not None
    assert after.deciding_since > before.deciding_since


@pytest.mark.asyncio
async def test_touch_refuses_a_lease_we_no_longer_hold(
    store: ApprovalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resurrecting a reclaimed lease would give one row two live owners."""
    monkeypatch.setattr(approvals, "OWNER", "pod-a/1111")
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True}, decided_by="me")

    # Pod B reclaims it after pod A went quiet.
    monkeypatch.setattr(approvals, "OWNER", "pod-b/2222")
    later = datetime.now(UTC) + timedelta(hours=1)
    assert await store.sweep_abandoned(now=later, lease_ttl=timedelta(seconds=30))

    # Pod A wakes up and tries to renew. It must not win the row back.
    monkeypatch.setattr(approvals, "OWNER", "pod-a/1111")
    assert await store.touch("a1") is False
    stored = await store.get("a1")
    assert stored is not None
    assert stored.deciding_by == "pod-b/2222"


@pytest.mark.asyncio
async def test_touch_ignores_rows_that_are_not_being_decided(
    store: ApprovalStore,
) -> None:
    await store.add(_approval())
    assert await store.touch("a1") is False
    assert await store.touch("nope") is False


@pytest.mark.asyncio
async def test_heartbeat_keeps_a_long_resume_out_of_reach_of_a_sweeper(
    store: ApprovalStore,
) -> None:
    """End to end: a resume that outlives the TTL is still not reclaimable."""
    await store.add(_approval())
    await store.claim("a1", decision={"approved": True}, decided_by="me")

    ttl = timedelta(milliseconds=90)
    async with approvals.heartbeat(store, "a1", lease_ttl=ttl):
        # Several TTLs' worth of work. Without the heartbeat the lease would be
        # long stale by now.
        await asyncio.sleep(0.4)
        assert await store.sweep_abandoned(lease_ttl=ttl) == []

    # Once the work stops, so do the renewals, and the lease goes stale.
    await asyncio.sleep(0.2)
    assert [i.approval_id for i in await store.sweep_abandoned(lease_ttl=ttl)] == ["a1"]


# --- owner identity ----------------------------------------------------------


def test_owner_is_unique_per_process() -> None:
    """Pod name alone is reusable across restarts; the UUID is what saves us."""
    first = approvals._make_owner()
    second = approvals._make_owner()
    assert first != second
    assert first.split("/")[0] == second.split("/")[0]


# --- dependency injection ----------------------------------------------------


def test_injector_provides_one_store_for_the_whole_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Singleton is correctness here, not an optimisation.

    The capture plugin and the HTTP routes must record and read the *same*
    store. With a database a second instance would merely be wasteful, since
    the state is in the table; with the in-memory backend it is two dicts, and
    approvals split between them with no error anywhere.
    """
    from injector import Injector

    from app.cluster.di import SessionModule

    monkeypatch.delenv("DB_BACKEND", raising=False)
    injector = Injector([SessionModule()])

    store = injector.get(ApprovalStore)
    assert isinstance(store, InMemoryApprovalStore)
    assert injector.get(ApprovalStore) is store


def test_injected_store_is_durable_when_a_database_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from injector import Injector

    from app.cluster.di import SessionModule

    monkeypatch.setenv("DB_BACKEND", URL)
    monkeypatch.setenv("DB_URL", f"sqlite+aiosqlite:///{tmp_path}/di.db")

    injector = Injector([SessionModule()])
    assert isinstance(injector.get(ApprovalStore), DatabaseApprovalStore)
