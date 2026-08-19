# Human-in-the-loop

How an action in this cluster gets a human's sign-off before it happens.

> **Why a durable case rather than a paused invocation.** ADK can suspend a call
> mid-flight and resume it when the human answers, which needs a reclaimable
> lease, a heartbeat and a background sweeper — and still cannot deliver the
> answer once the peer's A2A task has gone terminal. It is also unimplementable
> by a squad not using ADK. [`design-decisions.md`](design-decisions.md) (D5)
> has the measurements behind rejecting it.

## The system in 90 seconds

A specialist that must not act alone **proposes instead of acting**, and
finishes its turn:

```json
{"status": "approval_required",
 "action": "publish_result",
 "proposal": {"action": "publish_result", "value": "391.0", "label": "q3"},
 "summary": "Publish '391.0' under label 'q3'."}
```

The caller records that as an **approval case**, tells the user, and stops.
Nothing is held open. Later — a minute or a fortnight — a human approves it, and
the caller re-sends **the same request** with the approver's name attached. The
specialist recomputes the result from the same input and goes ahead.

```
specialist                caller                        human
    │                       │                             │
    │◀── MathRequest ───────│                             │
    │    (expression,       │                             │
    │     publish_as)       │                             │
    │                       │                             │
    │─── approval_required ▶│                             │
    │    + proposal         │── writes approval_cases row │
    │                       │─── "needs sign-off" ───────▶│
    ▼ (done; holds nothing) │                             │
                            │                             │
       ...hours or days pass, pods restart, nothing is lost...
                            │                             │
                            │◀── POST /cases/{id} ────────│
                            │    {approved: true}         │
    │◀── MathRequest ───────│                             │
    │    (same request +    │                             │
    │     approved_by)      │                             │
    │─── published ────────▶│── closes the row            │
```

Three properties fall out of that shape:

- **Waiting is free.** A pending approval is a row. No coroutine, no session
  pinned in memory, nothing to renew or reclaim.
- **What was approved is what runs.** The specialist recomputes its result from
  the original request rather than from values someone retyped, and the caller
  checks the returned values against the proposal it stored. Confirming that the
  *call* happened, rather than what it produced, would catch neither.
- **Any framework can do this.** Two ordinary skills and a JSON contract. A
  LangGraph or plain-FastAPI specialist implements the same thing with no ADK
  hooks.

## Try it yourself

The transcript below is a real run against four agents (orchestrator + three
specialists) over live A2A hops, not an illustration. It predates the `trades`
and `currency` agents; the flow is identical for the second gated action, which
is the point of it being a shape rather than a feature.

```bash
uv run uvicorn app.fast_api_app:app --port 8000
```

**1. Ask for something that needs sign-off.** This request is doing three jobs
at once: it needs delegation to the math specialist, it asks for a gated
publish, and it carries a phone number the specialist has no business seeing.

```bash
curl -s -X POST localhost:8000/cases/run -H 'content-type: application/json' -d '{
  "session_id": "demo-1",
  "text": "This is Marc Steiner, my direct line is +353 87 555 0101. Our Q3 revenue for the Ireland desk came to 17 batches of 23 million each. Please work out the total and publish it under the label q3-revenue-ireland."
}' | jq
```

```
STATUS : awaiting_approval
CASE   : 3b9482eb225b
  proposal: {'action': 'publish_result', 'label': 'q3-revenue-ireland',
             'value': '391000000.0'}
```

> The total Q3 revenue for the Ireland desk is **391,000,000**. […] Publishing
> requires human sign-off.

The word problem was solved, and **nothing was published**.

**2. Approve it.** The effect happens here and not before.

```bash
curl -s -X POST localhost:8000/cases/3b9482eb225b -H 'content-type: application/json' \
  -d '{"approved":true,"decided_by":"cfo@bnp.example","note":"reconciled with the ledger"}' | jq
```

```
STATUS  : executed
result  : {'status': 'published', 'value': '391000000.0',
           'label': 'q3-revenue-ireland', 'approved_by': 'cfo@bnp.example',
           'note': 'reconciled with the ledger'}
```

**What to check, beyond it working:**

- **`status`, not the prose.** `awaiting_approval` → `executed` is the
  machine-checked path. An agent saying "I've published it" is not evidence
  (see [Proving it actually happened](#proving-it-actually-happened)).
- **The phone number reached no specialist.** `grep -c "555 0101"` against the
  math agent's log returns **0**. It received
  `{"case_id": …, "expression": "17 * 23000000", "publish_as": "q3-revenue-ireland"}`
  and nothing else.
- **`"approved": false`** on a fresh case → `status: rejected`, and `result`
  stays `null`, because the execution turn never runs.
- **Re-POST the same approval** → `already_executed`, with the original approver
  still on the row.

Restarting the server between the two steps changes nothing, provided a database
is configured. That is the point.

> **Use `/cases/run`, not the ADK web UI.** Proposal detection lives in that
> route, so a gated action driven through the web UI will correctly refuse to
> publish but will not record a case. See
> [Known limits](#known-limits).

## The API

| Route | Purpose |
| --- | --- |
| `POST /cases/run` | Run a turn; reports any approvals it raised |
| `GET /cases?status=pending` | The queue, oldest first |
| `GET /cases/{proposal_id}` | One case in full |
| `POST /cases/{proposal_id}` | Decide it, and carry it out if approved |

`POST /cases/{proposal_id}` takes `{"approved": bool, "note": str,
"decided_by": str}` and answers with one of:

| `status` | Meaning |
| --- | --- |
| `executed` | Decided, carried out, case closed |
| `rejected` | Decided against; nothing ran |
| `already_executed` | Idempotent replay of a case that is already closed |
| `approved_not_confirmed` | Decision recorded, but the reply carried no proof the action ran. **Re-drivable: call the endpoint again.** |

The decision is written *before* the action is attempted, and separately. That
ordering is what makes this recoverable with no machinery at all: if the pod
dies mid-execution the decision still stands on the row, and calling the
endpoint again re-drives it. A case that already ran short-circuits, so a
retry cannot publish twice.

## Writing a gated action

A gated action is **one plain function** that looks at whether an approval is
present. `app/agents/math/tools.py` is the worked example:

```python
def publish_result(
    value: str, label: str, approved_by: str = "", note: str = ""
) -> dict[str, Any]:
    if not approved_by.strip():
        return {
            "status": APPROVAL_REQUIRED,
            "action": PUBLISH_ACTION,
            "proposal": {"action": PUBLISH_ACTION, "value": value, "label": label},
            "summary": f"Publish {value!r} under label {label!r}.",
        }

    record = {"value": value, "label": label, "approved_by": approved_by}
    PUBLICATIONS.append(record)  # the effect, and only here
    return {"status": "published", "action": PUBLISH_ACTION, "note": note, **record}
```

That is the whole gate: **the effect is unreachable without an approver.** It is
a property of the code, not an instruction the model is asked to respect, so a
model that decides to publish on its own simply cannot.

Then give the specialist's contract in `app/agents/contracts.py` an `approved_by`
field (and optionally `decision_note`), and tell the specialist in its
instruction to pass those through from the request. Nothing else is needed:

- **No second tool.** Proposing and executing differ by one argument.
- **No fingerprint, no token.** The approved request is re-sent *unchanged* apart
  from `approved_by`, and the specialist recomputes its result from that same
  input — so there is nothing for a caller to retype incorrectly.

Two things to get right when you add one.

**Say what your effect was, in the vocabulary the caller scans for.**
`app/agents/contracts.py` defines it: `PUBLISHED` (`"published"`) for a gated
write, `EXECUTED` (`"executed"`) for a gated read, and `EFFECT_PERFORMED` as the
set of both. `cases.find_execution` accepts a status in that set and nothing
else. A new action whose success status is missing from it runs correctly and is
then reported as `approved_not_confirmed` — a vocabulary bug that presents as a
model bug, which is why `tests/unit/test_trades.py` asserts the membership
directly.

**Make the approved request reproducible, or carry it back.** The math
specialist recomputes its value from `expression` because arithmetic is
deterministic. `trades` cannot: ask a model the same question twice and the SQL
differs. So the approved `sql` travels back in the request and the tool refuses
to run without it (`TradesRequest.sql`). If your action is not reproducible from
its inputs, the approved artefact has to make the round trip — and be
canonicalised at the source, so a re-send that differs only in whitespace still
matches (`trades.tools.canonical_sql`, `math.tools.canonical_value`).

### The second worked example: a gated *read*

`app/agents/trades/tools.py` gates a BigQuery query. The action is read-only, so
the risk is not corruption — it is that a model composes a query nobody read,
against a table nobody scoped, billed to an account nobody watched, and returns
a confident number derived from the wrong rows. Approval puts a human in front
of the SQL *and* of the number's provenance, which is the review a financial
answer actually needs. Plenty of actions worth gating are reads.

Same shape as `publish_result`, plus two things worth copying:

- **A validator, so a reviewer is never shown a proposal that could not have
  run.** `validate_sql` rejects anything that is not a single read-only
  statement against the one allowed table, working on text whose comments and
  string literals have been masked out first. It is a guard rail, not the
  boundary: what confines this agent is the human reading the SQL, plus IAM
  (`roles/bigquery.jobUser`, and `dataViewer` on nothing), plus
  `maximum_bytes_billed`.
- **A failed effect is not a performed effect.** A BigQuery error returns
  `status: error`, so the case stays approved and re-drivable rather than being
  closed as done.

## Proving it actually happened

This repo has twice shipped a bug where a confident, sensible answer hid a flow
that never ran. The code is built around not repeating it:

- The caller never infers success from the absence of an error. It looks for a
  result whose values **match the approved proposal** (`cases.find_execution`).
  No match means `approved_not_confirmed`, a warning in the log, and a case left
  re-drivable — never a case recorded as done. That comparison is also what stops
  a specialist publishing something other than what was approved and having it
  recorded as success.
- `tests/unit/test_two_phase_approval.py` and `tests/unit/test_trades.py` assert
  on the effect (`PUBLICATIONS`, `EXECUTIONS`), not on a returned string. The
  trades tests go one step further and drive the whole round trip — propose,
  serialise the way an A2A reply would, approve, re-send, then match with
  `cases.find_execution` — including the negative case, where a *different*
  query ran and is correctly not accepted as the approved one.

When you extend this, assert on a marker the code emits.

## Known limits

Worth knowing before relying on any of this.

**The proposal crosses A2A as text.** ADK's `AgentTool` reduces a peer's reply to
its merged text parts, so structure the specialist produced internally is not
structure by the time it arrives. The specialist's instruction tells it to report
the JSON and the caller parses it back out of the surrounding prose. The parser
is tolerant and nothing is ever assumed to have happened, but this handshake
depends on a model following an instruction — it is the weakest link in the
design. A deployment needing a hard guarantee should give the specialist a
structured output channel.

**Exactly-once is the caller's job.** The specialist keeps no ledger, so an
identical, genuinely-approved request replayed twice would publish twice. The
case store is what prevents that: a closed case reports `already_executed`
rather than running again. A specialist that needs the guarantee on its own side
should insert one row keyed by the proposal's contents under a unique
constraint.

**"Approved" means this case, not these exact bytes.** The caller compares the
result against the stored proposal, which catches a specialist returning
something different — but there is no signed token binding the decision to the
content. For a compliance sign-off that must be provable after the fact, add one
field carrying a hash of the proposal and have the specialist verify it; the
hook is a single check inside the gated function.

**`decided_by` is not authenticated.** It is a string the caller supplies, for
the audit trail only. Put real authentication in front of these routes before
anyone relies on who approved what.

**With `DB_BACKEND=none` a pending case dies with the pod**, and is visible only
to the replica that recorded it. Set a database backend for anything real.

**A case belongs to the agent that asked the human** — normally the orchestrator
— and lives in that agent's own schema. No other agent reads or writes it.

## Reconciliation

The query that matters in operations: an approved case whose action never
completed.

```sql
SELECT proposal_id, case_id, agent, action, proposal, decided_by, decided_at
  FROM approval_cases
 WHERE status = 'approved'
 ORDER BY decided_at;
```

Migration `0005` indexes exactly that. Unlike the equivalent hunt under the old
design (`resumed_at IS NULL`, which found approvals whose effect had happened but
whose answer was unrecoverable), every row here is actionable: re-drive it by
calling `POST /cases/{proposal_id}` again.
