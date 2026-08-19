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

"""The wire contracts: what each specialist accepts in a delegated request.

This module is the **interface definition** for the cluster. An agent that
declares a model here is delegated to with exactly those fields and nothing
else — no transcript, no shared session state, no implicit context (measured in
``docs/design-decisions.md``, D1).

Declaring a contract is OPTIONAL
--------------------------------
Delegation works without one. What a contract changes is how much structure the
caller is held to, and there are three tiers:

1. **The session transcript.** Attach a peer as an ADK ``sub_agent`` and the
   de-facto contract is the caller's conversation history, which
   ``RemoteA2aAgent`` rebuilds from session events. It is the framework default,
   and this repo rejects it (D1) — it is named here because it is what you get
   by not deciding.
2. **Free text.** A peer attached as a tool but absent from :data:`PAYLOADS`
   resolves to ``app.cluster.resolver.UnknownPeerRequest``: a correlation id
   plus one free-text ``task`` field. Nothing breaks — the caller's model writes
   prose and the specialist reads it. This is the honest tier for a peer another
   squad owns, reached by URL, whose real schema lives in its own agent card.
3. **A declared contract.** Add a model here and the calling model sees named,
   documented parameters, the payload is validated before it leaves the pod, and
   the JSON Schema is published in this agent's A2A card.

Tier 3 is what this repo uses for every agent it owns, because one free-text
field is where a caller quietly starts pasting conversation context back in —
the transcript returning a paragraph at a time. That is a **policy** of this
codebase, enforced by
``test_agents.py::test_every_delegatable_agent_declares_a_contract``, not a
requirement of the mechanism: drop the :data:`PAYLOADS` entry and the agent is
still reachable and still answers.

Why the schemas live in one module rather than next to each agent
-----------------------------------------------------------------
A contract has two sides. The caller needs it to *build* a request and the
specialist needs it to *validate* one, so putting it in the specialist's package
would force every caller to import that package — reintroducing exactly the
code coupling this architecture is trying to remove. Keeping the contracts
together makes the whole surface reviewable in one file and lets a caller depend
on the interface without depending on any implementation.

**This is shared code, not shared state.** It is the same kind of artefact as a
``.proto`` file: a schema both sides agree on, versioned in the open. A squad on
another framework does not import it at all — the equivalent JSON Schema is
published in each agent's A2A card (``parameters_json_schema``, derived from
these models by ``app/cluster/peer_tool.py``), which is what makes a LangGraph
or plain-FastAPI specialist a first-class participant.

Adding a field
--------------
Adding an optional field is backwards compatible. Adding a *required* one is a
breaking change for every caller, so give it a default and tighten later.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# --- The approval vocabulary --------------------------------------------------
#
# Three strings, and they are as much a part of the wire contract as any field
# below: a caller keys its business workflow on them, and a specialist written
# on another framework produces them without importing anything from here.

APPROVAL_REQUIRED = "approval_required"
"""Returned INSTEAD of performing an action that needs a human's sign-off."""

PUBLISHED = "published"
"""A gated write happened (``app/agents/math/tools.py``)."""

EXECUTED = "executed"
"""A gated read happened (``app/agents/trades/tools.py``)."""

#: Every status meaning "the gated effect actually happened". The caller scans a
#: specialist's reply for one of these and then checks the values against the
#: approved proposal (``app.cluster.cases.find_execution``); a status outside
#: this set is not treated as confirmation, so a new gated action MUST add its
#: status here or its executions are reported as `approved_not_confirmed`.
#:
#: Two spellings rather than one because the status names the effect, and
#: "published" is not a truthful description of running a query. The set is the
#: seam that lets each specialist say what it did while the caller stays
#: generic.
EFFECT_PERFORMED: frozenset[str] = frozenset({PUBLISHED, EXECUTED})


class PeerRequest(BaseModel):
    """Fields every delegated request carries, whatever the specialist.

    Attributes:
        case_id: Correlates every call belonging to one piece of business work.
            This is the share-nothing replacement for a shared session: a
            specialist that needs continuity across several calls keys **its
            own** private store on this value, so continuity is a declared part
            of the contract rather than an implicit property of the transport.
            See ``docs/design-decisions.md``.
        document_refs: Pointers to large inputs, never their contents — the
            claim-check pattern (D4). This is what lets a caller hand a
            specialist a raw 200-page filing without pre-filtering it: the
            caller has the context to know *which* document is relevant, and the
            specialist has the domain knowledge to know what to extract from it.
            Embedding the document instead would blow the payload limit and
            force the caller to do lossy extraction it is not qualified for.
    """

    case_id: str = Field(
        description=(
            "Identifier correlating all work for one case or request. Reuse the "
            "same value for follow-up calls about the same subject."
        )
    )
    document_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Object-store URIs of documents the specialist should read itself, "
            "e.g. 'gs://bucket/cases/123/dossier.pdf'. Pass the reference, not "
            "the contents: never paste a document into this request."
        ),
    )


class ResearchRequest(PeerRequest):
    """Ask the research specialist a self-contained factual question."""

    question: str = Field(
        description=(
            "The single factual question to answer. State it in full: the "
            "specialist cannot see the conversation it came from."
        )
    )
    constraints: str = Field(
        default="",
        description=(
            "Optional scope the answer must respect, e.g. a jurisdiction, a "
            "date range, or a required source."
        ),
    )


class MathRequest(PeerRequest):
    """Ask the math specialist to evaluate an expression, and maybe publish it.

    Publishing is the gated action: with ``publish_as`` set and no
    ``approved_by``, the specialist returns a proposal instead of publishing.
    Re-send the same request with ``approved_by`` filled in once a human has
    said yes, and it goes through.
    """

    expression: str = Field(
        description=(
            "The arithmetic expression to evaluate, e.g. '(2 + 3) * 4'. "
            "Supports + - * / // % ** and parentheses only. When "
            "`target_currency` is set, tag each amount with its own ISO-4217 "
            "code instead, e.g. '250 EUR + 300 GBP'."
        )
    )
    target_currency: str = Field(
        default="",
        description=(
            "Optional ISO-4217 code the answer must be expressed in, e.g. "
            "'USD'. Set it when the amounts in `expression` are not all in the "
            "same currency: the specialist converts each one first, then "
            "evaluates. Leave empty for plain arithmetic."
        ),
    )
    publish_as: str = Field(
        default="",
        description=(
            "Label to publish the result under. Publishing needs human "
            "approval, so with this set the specialist replies with a proposal "
            "and publishes nothing."
        ),
    )
    approved_by: str = Field(
        default="",
        description=(
            "Who approved the publication. Set this ONLY after a human "
            "approved, and re-send the request otherwise unchanged -- the "
            "specialist recomputes the value from `expression`, so it must be "
            "identical to the request that was approved."
        ),
    )
    decision_note: str = Field(
        default="",
        description="Optional feedback the approver attached, recorded for audit.",
    )


class PlannerRequest(PeerRequest):
    """Ask the planner specialist to draft a plan for human review."""

    objective: str = Field(
        description="What the plan must achieve, stated as an outcome."
    )
    constraints: str = Field(
        default="",
        description="Optional limits the plan must respect (budget, order, risk).",
    )


class CurrencyRequest(PeerRequest):
    """Ask the currency specialist to convert one amount between currencies.

    One conversion per call, on purpose: a list of conversions would leave the
    caller deciding how to correlate results back to inputs, and a specialist
    call is cheap.
    """

    amount: float = Field(
        description="How much to convert, expressed in `from_currency`."
    )
    from_currency: str = Field(
        description="ISO-4217 code of the currency the amount is in, e.g. 'EUR'."
    )
    to_currency: str = Field(
        description="ISO-4217 code of the currency to convert to, e.g. 'USD'."
    )


class TradesRequest(PeerRequest):
    """Ask the trades specialist a question about Cymbal Investments trades.

    Running the query is the gated action: with no ``approved_by`` the
    specialist replies with the SQL it proposes to run and queries nothing.
    Re-send with ``approved_by`` set **and the approved ``sql`` copied in
    verbatim** once a human has said yes.

    The ``sql`` round trip is what makes this different from
    :class:`MathRequest`. Arithmetic is reproducible from ``expression``, so the
    math specialist recomputes; SQL generation is not, so the approved text has
    to travel back rather than be regenerated from the question.
    """

    question: str = Field(
        description=(
            "The trade question to answer, stated in full — the specialist "
            "cannot see the conversation it came from. It writes the SQL "
            "itself; describe what you want to know, not how to query it."
        )
    )
    sql: str = Field(
        default="",
        description=(
            "Leave EMPTY on a first request. On an approved re-send, the exact "
            "`sql` string from the approved proposal, copied character for "
            "character: the specialist will not regenerate it, and a query "
            "that differs from the approved one is refused."
        ),
    )
    row_limit: int = Field(
        default=20,
        description=(
            "Maximum rows to return, capped by the specialist. Ask for an "
            "aggregate rather than raising this: the rows cross the wire as "
            "text."
        ),
    )
    approved_by: str = Field(
        default="",
        description=(
            "Who approved running the query. Set this ONLY after a human "
            "approved, and send the approved `sql` alongside it."
        ),
    )
    decision_note: str = Field(
        default="",
        description="Optional feedback the approver attached, recorded for audit.",
    )


# Which contract each agent accepts. `app/cluster/resolver.py` reads this to give
# a peer's tool a typed declaration; a peer absent from this mapping (an agent
# owned by another squad, reached by URL) falls back to an untyped request and
# its real schema comes from its published agent card.
PAYLOADS: dict[str, type[PeerRequest]] = {
    "research": ResearchRequest,
    "math": MathRequest,
    "planner": PlannerRequest,
    "currency": CurrencyRequest,
    "trades": TradesRequest,
}

__all__ = [
    "APPROVAL_REQUIRED",
    "EFFECT_PERFORMED",
    "EXECUTED",
    "PAYLOADS",
    "PUBLISHED",
    "CurrencyRequest",
    "MathRequest",
    "PeerRequest",
    "PlannerRequest",
    "ResearchRequest",
    "TradesRequest",
]
