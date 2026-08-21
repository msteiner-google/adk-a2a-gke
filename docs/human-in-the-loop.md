# Human-in-the-loop

How an action in this cluster gets a human's sign-off before it happens.

> **This is A2A in-task authorization** (spec section 7.6), not a scheme
> invented here. A specialist that needs a human suspends its Task in
> `TASK_STATE_AUTH_REQUIRED`; a client that speaks A2A can act on that without
> knowing anything about this repo.
> [`design-decisions.md`](design-decisions.md) (D5) records the two designs
> this replaced and what each of them got wrong.

## The system in 90 seconds

A specialist that must not act alone **suspends instead of acting**. Its A2A
Task settles in `TASK_STATE_AUTH_REQUIRED`, carrying the pending call and a
one-line hint for whoever must decide:

```json
{"name": "adk_request_confirmation",
 "args": {
   "originalFunctionCall": {"name": "publish_result",
                            "args": {"value": "391000000", "label": "q3"}},
   "toolConfirmation": {"hint": "Publish '391000000' under label 'q3'.",
                        "confirmed": false}}}
```

That request bubbles up to the agent talking to the human, which records an
**approval case** and reports that nothing has happened. Later — a minute or a
fortnight — a human approves, the decision is delivered **straight to the agent
that owns the tool**, and ADK re-executes that tool with the decision attached.

```
specialist                caller                        human
    │                       │                             │
    │◀── transfer ──────────│                             │
    │                       │                             │
    │  publish_result()     │                             │
    │  suspends ────────────│                             │
    │  AUTH_REQUIRED ──────▶│── writes approval_cases row │
    │  (task stays alive)   │─── "needs sign-off" ───────▶│
    │                       │                             │
       ...hours or days pass; the case row is durable...
                            │                             │
                            │◀── POST /cases/{id} ────────│
                            │    {approved: true}         │
    │◀── decision delivered │                             │
    │    to THIS task       │                             │
    │  ADK re-runs the tool │                             │
    │─── published ────────▶│── closes the row            │
```

Note the asymmetry: the **request** travels up the chain of callers, the
**decision** goes straight down to the owner. That is forced, not stylistic —
ADK resolves a confirmation against the local tool set, so a caller silently
drops a grant meant for a peer's tool. D5 has the measurement.

Three properties fall out of that shape:

- **The client is told, in the protocol.** `TASK_STATE_AUTH_REQUIRED` is an
  *interrupted* state, not a terminal one. A poller, an SSE subscriber or a
  webhook all learn that work is outstanding without parsing prose.
- **What was approved is what runs.** The reviewer reads arguments generated
  from the suspended call, and ADK re-executes *that* call — no model is asked
  to restate or re-send it. The caller still checks the returned values against
  the stored proposal.
- **The effect is unreachable without a decision.** The gate is a branch in the
  tool, not the task state. Spec 7.6.4 insists on exactly this: the state
  authorises nothing by itself.

## Try it yourself

The transcript below is a real run against three agents (orchestrator, math,
currency) over live A2A hops, not an illustration.

```bash
make up          # or three uvicorn processes; see the Makefile
```

**1. Ask for something that needs sign-off.** This request is doing three jobs
at once: it delegates to the math specialist, it needs a currency conversion one
hop further down, and it asks for a gated publish.

Any A2A client will do. Note the `A2A-Version: 1.0` header — without it the
JSON-RPC route treats the call as protocol 0.3 and answers a v1.0 method name
with an error body inside an HTTP 200.

```bash
curl -s localhost:8090/a2a/app -H 'A2A-Version: 1.0' -H 'content-type: application/json' -d '{
  "jsonrpc": "2.0", "id": "1", "method": "SendMessage",
  "params": {"message": {"messageId": "m1", "role": "ROLE_USER",
    "parts": [{"text": "Add 500 USD and 200 EUR, total in EUR, and publish it under the label fy-total."}]}}
}' | jq -r '.result.status.state'
```

```
TASK_STATE_AUTH_REQUIRED
```

The task is **interrupted, not finished** — that is the whole point. Its status
message carries what a reviewer needs:

```json
{"name": "adk_request_confirmation",
 "args": {"originalFunctionCall": {"name": "publish_result",
                                   "args": {"value": "658.72 EUR",
                                            "label": "fy-total"}},
          "toolConfirmation": {"hint": "Publish '658.72 EUR' under label 'fy-total'."}}}
```

500 USD was converted (458.72 EUR), added to 200 EUR, and **nothing was
published**.

```bash
curl -s localhost:8090/cases | jq '.cases[] | {proposal_id, agent, action, summary}'
```

```json
{"proposal_id": "4e2f942bc79d", "agent": "math", "action": "publish_result",
 "summary": "Publish '658.72 EUR' under label 'fy-total'."}
```

**2. Approve it.** The effect happens here and not before.

```bash
curl -s -X POST localhost:8090/cases/4e2f942bc79d -H 'content-type: application/json' \
  -d '{"approved":true,"decided_by":"alice@bnpp.com","note":"FY total checked."}' | jq
```

```
status : executed
result : {"status": "published", "value": "658.72 EUR", "label": "fy-total",
          "action": "publish_result", "approved_by": "alice@bnpp.com",
          "note": "FY total checked."}
```

**What to check, beyond it working:**

- **The task state, not the prose.** `TASK_STATE_AUTH_REQUIRED` → `executed` is
  the machine-checked path. An agent saying "I've published it" is not evidence
  (see [Proving it actually happened](#proving-it-actually-happened)).
- **The specialist really re-ran.** The `result` above is the tool's own return
  value, carried back from `math`'s task — not the orchestrator's summary of it.
- **`"approved": false`** → `status: rejected`, `result` stays `null`, and the
  refusal is still delivered so the specialist's task does not leak.
- **Re-POST the same approval** → `already_executed`, with the original approver
  still on the row.

Restarting the *orchestrator* between the two steps changes nothing, provided a
database is configured. Restarting the **specialist** does: its suspended task
lives in that pod's task store, so use `TASK_STORE_BACKEND=database` for
anything that must survive a restart.

> **A question is not an approval.** Ask the same thing with "500 million
> dollars" and you get `TASK_STATE_COMPLETED` with a question relayed back to
> you — "dollars" is six currencies, and the amount is over the review
> threshold. No case is opened, because nothing was proposed. See
> [Asking the user is not the same as approving](#asking-the-user-is-not-the-same-as-approving).

## The API

| Route | Purpose |
| --- | --- |
| `POST /cases/run` | Run a turn over HTTP instead of A2A; convenience for testing |
| `GET /cases?status=pending` | The queue, oldest first |
| `GET /cases/{proposal_id}` | One case in full |
| `POST /cases/{proposal_id}` | Decide it, and deliver the decision to the owning agent |

`POST /cases/{proposal_id}` takes `{"approved": bool, "note": str,
"decided_by": str}` and answers with one of:

| `status` | Meaning |
| --- | --- |
| `executed` | Decided, delivered, and the owner's reply matched the approved proposal |
| `rejected` | Decided against. The refusal is still delivered, so the specialist's task does not leak |
| `already_executed` | Idempotent replay of a case that is already closed |
| `approved_not_confirmed` | Delivered, but the owner reported no matching execution. **Re-drivable: call the endpoint again.** |
| `approved_not_delivered` | The owning agent could not be reached. Decision stands; **re-drivable.** |
| `approved_not_routable` | The case has no suspended task to resume — a row from before grant routing existed. Nothing to deliver |

The decision is written *before* delivery is attempted, and separately. That
ordering is what makes this recoverable with no machinery at all: if the pod
dies mid-delivery the decision still stands on the row, and calling the endpoint
again re-drives it. A case that already ran short-circuits, so a retry cannot
publish twice.

Three columns on the row make delivery possible: `owner_task_id` (the suspended
task on the agent that owns the tool), `owner_context_id`, and
`confirmation_id` (the pending `adk_request_confirmation` call the answer is
addressed to). They are recorded when the request bubbles up — migration
`0007`.

## Writing a gated action

A gated action is **one plain function** that asks for a decision and returns
without acting until it has one. `app/agents/math/tools.py` is the worked
example:

```python
@gated
def publish_result(value: str, label: str, tool_context: ToolContext):
    value = canonical_value(value)
    decision = require_approval(
        tool_context,
        summary=f"Publish {value!r} under label {label!r}.",
        proposal={"action": PUBLISH_ACTION, "value": value, "label": label},
    )
    if decision.pending:
        return {"status": AWAITING_APPROVAL, "action": PUBLISH_ACTION}
    if not decision.granted:
        return {"status": REFUSED, "action": PUBLISH_ACTION, "note": decision.note}

    record = {"value": value, "label": label, "approved_by": decision.approved_by}
    PUBLICATIONS.append(record)  # the effect, and only here
    return {"status": PUBLISHED, "action": PUBLISH_ACTION, **record}
```

That is the whole gate: **the effect is unreachable without a decision.** It is
a property of the code, not an instruction the model is asked to respect, so a
model that decides to publish on its own simply cannot. The first call suspends
the task; ADK re-executes this same function, with the same arguments, once a
human answers — nothing is re-derived and no model is asked to call it again.

Four things to get right when you add one.

**Mark it `@gated`.** That is not decoration: it decides how the agent owning
this tool is wired into its callers. A gated peer must be a sub-agent, or an
`AgentTool` swallows the suspension and the request never reaches a human.
`suspending_agents()` derives the wiring from this marker, and
`test_agents.py::test_every_tool_that_asks_for_approval_is_marked_gated` reads
your source and fails if you forget.

**Take `tool_context` and import it at runtime.** `require_approval` keys the
confirmation on `tool_context.function_call_id`. Import `ToolContext` from
`google.adk.tools.tool_context` at module level, never under `TYPE_CHECKING` —
ADK evaluates the annotation to build the declaration.

**Declare your canonical values in `proposal=`** if the tool normalises its
inputs. Otherwise the case records the model's raw arguments and the caller
compares them against a normalised result. Measured twice: `391000000.0` against
`391000000`, and `88000.0` against `88000`. Both were correctly refused as
`approved_not_confirmed`.

**Say what your effect was, in the vocabulary the caller scans for.**
`app/agents/statuses.py` defines it: `PUBLISHED` for a gated write, `EXECUTED`
for a gated read, `CONVERTED` for a gated quote, and `EFFECT_PERFORMED` as the
set of all three.
`cases.find_execution` accepts a status in that set and nothing else. A new
action whose success status is missing from it runs correctly and is then
reported as `approved_not_confirmed` — a vocabulary bug that presents as a model
bug, which is why `tests/unit/test_trades.py` asserts the membership directly.

### The second worked example: a gated *read*

`app/agents/trades/tools.py` gates a BigQuery query. The action is read-only, so
the risk is not corruption — it is that a model composes a query nobody read,
against a table nobody scoped, billed to an account nobody watched, and returns
a confident number derived from the wrong rows. Approval puts a human in front
of the SQL *and* of the number's provenance, which is the review a financial
answer actually needs. Plenty of actions worth gating are reads.

Same shape as `publish_result`, plus three things worth copying:

- **A validator, so a reviewer is never shown a proposal that could not have
  run.** `validate_sql` rejects anything that is not a single read-only
  statement against the one allowed table, working on text whose comments and
  string literals have been masked out first. It is a guard rail, not the
  boundary: what confines this agent is the human reading the SQL, plus IAM
  (`roles/bigquery.jobUser`, and `dataViewer` on nothing), plus
  `maximum_bytes_billed`.
- **Validate before asking.** `require_approval` is called only after
  `validate_sql` passes, so a statement that could never run is refused outright
  instead of being put in front of a reviewer.
- **A failed effect is not a performed effect.** A BigQuery error returns
  `status: error`, so the case stays approved and re-drivable rather than being
  closed as done.

### The third worked example: a gated quote, two hops down

`app/agents/currency/tools.py` gates `convert_to_crypto`. Nothing moves and
nothing is written — what is gated is *issuing the number*. A BTC price taken
from a table frozen months ago is indistinguishable from a live quote once it
has left this system, so a human signs off before it is produced.

It adds two things the first two examples do not have:

- **The ungated neighbour has to be closed off.** `convert_currency` sits in
  the same module and would quote the same asset with nobody asked. Crypto
  therefore lives in its own rate table (`_USD_PER_CRYPTO_UNIT`), because
  membership of the fiat table *is* reachability from the ungated tool, and
  `convert_currency` refuses a crypto term **by name** rather than letting it
  fall through as "unsupported" — a model told the asset is unknown looks for
  another way to produce the number, which is how a gated action ends up
  answered from memory. `tests/unit/test_currency.py` asserts both directions.
- **The authorization travels two hops.** `currency` is reached by `math`,
  which is reached by the orchestrator, so the suspension propagates up a chain
  of two A2A tasks before it reaches a person. A2A spec 7.6.2 describes exactly
  this ("a chain of Tasks in `TASK_STATE_AUTH_REQUIRED`") and no new machinery
  was needed for it — but note it is the marker on the tool, not the depth,
  that wires each hop: adding `@gated` here is what moved `currency` from
  `math`'s tool list into its sub-agents.

## Asking the user is not the same as approving

There is a second, lighter escalation in this cluster and it deliberately does
**not** use any of the machinery above. The currency specialist returns
`needs_input` when a currency is ambiguous ("dollars" is six of them here) and
`needs_confirmation` when an amount is over its threshold. Both mean: nothing
happened, and only the user can settle this.

They are not approval cases, for three reasons worth being explicit about:

- **There is no effect to gate.** A fiat conversion has no side effect, so
  re-running it after the user answers costs nothing. (The same agent's crypto
  quote *is* gated — which makes the contrast concrete rather than theoretical:
  one tool asks the user what they meant, the other asks an approver whether it
  may answer at all, and they are not interchangeable.) An approval case buys its complexity
  by making an irreversible action safe; there is nothing here to make safe.
- **The answer is an input, not a decision.** "US dollars, not Canadian" belongs
  in the next request, not in a `decided_by` column. Recording it as an approval
  would file a typo correction as a compliance event.
- **Nothing needs to survive a restart.** The question is part of a
  conversation the session already holds.

What they *do* share with an approval is the hard part: reaching the user
un-paraphrased, from two hops down. That is `app/agents/reporting.py` — both
statuses are in `AUDITED_STATUSES`, and it scans inside a peer's reply as well
as its own tool results, then folds the JSON into the agent's own reply — one
message carrying both the prose and the structure — so the question survives
`currency -> math -> orchestrator` as verbatim JSON rather than as whatever the
middle agent chose to say about it. The instructions at all three levels then
say the same thing: relay it, do not answer it.

Reach for an approval case when an effect must not happen without sign-off.
Reach for `needs_input` / `needs_confirmation` when you simply do not know
enough to proceed.

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
  serialise the way the confirmation crosses A2A, approve, re-execute, then match with
  `cases.find_execution` — including the negative case, where a *different*
  query ran and is correctly not accepted as the approved one.

When you extend this, assert on a marker the code emits.

## Known limits

Worth knowing before relying on any of this.

**A suspended task is pinned to one pod.** `a2a-sdk` keeps live tasks in an
in-process registry; there is no distributed queue manager. Every Deployment is
`replicas: 1` today, so this is latent — but with two replicas a grant routed to
the wrong pod builds a *second* live task from the DB row, which is double
execution rather than a clean error. Fix before scaling out: sticky routing by
task id, or a shared queue manager.

**The caller's own task is left suspended.** The decision goes to the owner and
the case closes from its reply, but the orchestrator's copy of the request stays
in `auth-required` — ADK cannot resolve a peer's confirmation locally, which is
the same limitation that forces the direct delivery. Harmless with a durable
task store and a retention sweep; untidy.

**The request still crosses A2A as text on the way up.** The proposal itself is
structured — it is ADK's own `adk_request_confirmation` payload, generated from
the pending call, so it cannot disagree with what will run. But a specialist's
*narration* around it is prose, and `app/agents/reporting.py` is what keeps the
structure intact through the relay.

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

Migration `0005` indexes exactly that; `0007` adds the three columns that make a
row re-drivable (`owner_task_id`, `owner_context_id`, `confirmation_id`). Every
row here is actionable: call `POST /cases/{proposal_id}` again and the decision
is re-delivered to the suspended task.

One caveat: a task suspended long enough to be swept from the task store cannot
be resumed, and such a case will report `approved_not_delivered` forever. Keep
task retention longer than the longest approval you expect.
