# Human-in-the-loop (HITL) — implementation plan

Plan for adding human approval gates to this cluster: an agent pauses before a
gated action, a human approves or rejects out of band, and the paused invocation
resumes — **including when the pause happens inside a peer reached over A2A**.

Grounded in [`spike-findings.md`](spike-findings.md), which records what was
actually run against ADK 2.6.1. Findings are cited below as **F1**–**F8**.

> **Status:** phases 0-3 are implemented and were run in the live cluster on
> AlloyDB, including migration `0004` and both D4.2 acceptance kills. See
> [`results.md`](results.md) for what actually happened (**R1**-**R10**); it
> supersedes this document where they disagree. Headlines: strategies A and B work
> end to end over A2A, **C does not** (R3), the resume payload contract is
> `{"result": …}` (R4), approvals now survive a pod restart (R6 fixed), and a
> crash *during* a resume is recovered by the D4.2 lease, and the entry point
> scales out (R11). One gap is left open: the **answer** cannot be replayed once
> the A2A peer's task is terminal (R10).

## Goal and non-goals

**Goal.** A tool can be declared "needs a human". When the model tries to call it,
the invocation pauses before the tool body runs, an approval is recorded durably,
and an operator can approve/reject via an HTTP endpoint on the orchestrator. The
answer resumes on the same session, whichever agent paused.

**Non-goals for now.** An approval UI; multi-approver / quorum; approval routing by
role; and direct human *editing* of a pending call's arguments — the human can approve, reject, or reply with feedback the model re-plans
on (D8), but never authors the executed action itself.

## Design decisions

### D1 — Two mechanisms, chosen per intent: gate an action vs ask a question

ADK 2.6.1 ships two HITL mechanisms usable from an `LlmAgent`, plus a third that
belongs to graph workflows. They are not competing options — they answer different
questions, and this project needs both.

**(a) Gate an action — `require_confirmation`:**

```python
FunctionTool(func=transfer_funds, require_confirmation=True)
# or, conditionally on the arguments:
FunctionTool(
    func=transfer_funds, require_confirmation=lambda amount, **_: amount > 10_000
)
```

The flow (`flows/llm_flows/functions.py:374`) synthesizes a long-running
`adk_request_confirmation` call carrying the original call plus a `ToolConfirmation`,
and **the tool body does not execute** until a confirmation arrives. Structural: the
gated effect cannot happen early. Yes/no only — see D8 for what the human may say.

**(b) Ask a question — `request_input`:**

```python
from google.adk.tools import request_input  # public, tools/__init__.py:86
```

`request_input` is a `LongRunningFunctionTool` named `adk_request_input` taking
`message` and `response_schema`. It is the LLM-agent-level bridge to the same
`RequestInput` concept the graph runtime exposes as a node
(`google.adk.events.RequestInput`, with `message` / `payload` / `response_schema`).
The model calls it when it needs the human; the human's reply is an ordinary
`FunctionResponse`, so **arbitrary content flows back to the model** — free text,
structured data, corrections. That is what makes "give feedback" and "revise the
instructions" possible at all (D8).

Note `response_schema` is advisory: ADK does not coerce or validate the human's
reply against it (the upstream docs say so explicitly). Validate at our API boundary.

**Why its pause does not leak, unlike the spike's.** `_request_input_func` returns
`None`, and `functions.py:648-657` skips building a function response when a
long-running tool returns nothing. No response, nothing for the model to continue
from. The spike's F8 leak came from returning a truthy `{"status":"pending"}` dict —
that was our bug, not an inherent property of long-running tools. **Correcting the
spike's conclusion here matters:** the pattern is sound if the tool returns `None`.

**(c) Graph `Workflow` + `RequestInput` node — SHIPPED as strategy C.** The
deferral below was overtaken by events: `app/agents/planner` serves a graph as an
agent and works end to end over A2A (results.md R7). Original reasoning kept for
the record: ADK's own docs position
this as the graph-workflow mechanism and tool-confirmation as the LLM-agent one.
Adopting it means restructuring the orchestrator from an `LlmAgent` with A2A peers
into a node graph, which collides with this repo's invariant that every agent is the
same `AgentSpec` built by one `build_agent`. One intriguing property if we ever
revisit: `runners.py:1763` returns the root directly for a `Workflow`
("Workflow will figure which node is interrupted and should be resumed"), which
suggests a graph root may sidestep the F6 resume-routing bug entirely — untested.

**Both (a) and (b) ride the long-running function-call path** that the spike verified
propagates up over A2A and relays back down on resume (F2/F4) — but neither specific
call was in the spike. **Phase 1 proves both.**

### D2 — The human decision is owned at the top (orchestrator), not per worker

The spike's Model 1 vs Model 2 question is settled by F4: the relay down already
works, so there is no reason to build B-side capture and A-side re-query. The
orchestrator exposes the approval API; workers stay unaware that a human exists.

Consequence: a worker can gate a tool without any worker-side HITL code.

### D3 — Capture pauses with an ADK plugin, not inside the HTTP handler

The spike captured the pause by inspecting events in its own `/hitl/start` handler.
That only works for traffic driven by that handler — real traffic arrives via
`/run_sse` (ADK web) and the A2A executor.

Use `App(plugins=[...])` with `on_event_callback`
(`plugins/base_plugin.py:155`), which runs on the Runner and therefore covers every
serving surface, since `app/fast_api_app.py` hands the *same* Runner to the web app
and to `attach_a2a_routes`. The plugin records any event carrying
`long_running_tool_ids` into the approvals table and lets the event through
untouched.

### D4 — Approvals live in the database, in our own table

In-memory approvals die with the pod and cannot be read by the replica that happens
to serve `/hitl/resume`. Add a table in the per-agent schema via a hand-written
Alembic migration `0004` (autogenerate stays off — see AGENTS.md).

Unlike the ADK/a2a tables, this one is **ours**; the rule that carries over is the
same: no `create_all()` at runtime, the migration Job owns the DDL, and the agent
roles keep no `CREATE` privilege.

```
hitl_approvals
  approval_id      text  PK
  app_name         text  not null
  user_id          text  not null   -- the SESSION's user; also the approver (D4.1)
  session_id       text  not null
  invocation_id    text  not null
  function_call_id text  not null
  tool_name        text  not null
  args             jsonb not null
  status           text  not null   -- pending | deciding | approved | rejected | expired
  decision         jsonb
  decided_by       text
  created_at       timestamptz not null
  decided_at       timestamptz
  deciding_since   timestamptz      -- lease start; null unless status = 'deciding' (D4.2)
  deciding_by      text             -- pod that holds the claim, for diagnosis (D4.2)
  resumed_at       timestamptz      -- audit: when the continuation finished (D4.2)
  unique (session_id, function_call_id)
  index (status, created_at)                               -- global queue
  index (user_id, created_at)    where status = 'pending'  -- the per-user UI
  index (deciding_since)         where status = 'deciding' -- the reclaim sweep (D4.2)
```

`(session_id, function_call_id)` unique is what makes the plugin idempotent — ADK
resumption is at-least-once by design (`ResumabilityConfig` docstring), so the same
pause can be observed twice.

#### D4.1 — Approvals are self-service: `user_id` is also the approver

**Decided:** each user approves their own gated actions. There are no admin/reviewer
figures who act on someone else's pause, so the table needs no assignee: the person
who must decide is the `user_id` on the row. `decided_by` is kept anyway — it records
*who actually clicked* for audit, and it is the column that would catch a decision
arriving from an unexpected identity.

That makes the per-user UI query
`WHERE status = 'pending' AND user_id = ? ORDER BY created_at`, which the second index
serves directly. Both indexes are **partial** on `status = 'pending'`, and composite
with `created_at` last:

- equality column first, `created_at` last, so the same index answers the filter and
  the sort;
- pending is a small and shrinking fraction of the table (every row ends up decided or
  expired), so the index stays small and hot however much history accumulates.

At the volumes expected here Postgres would sequential-scan either way — the indexes
are cheap, and they document the intended access paths.

**If a reviewer model ever appears**, add an `assignee` column then. Under today's
policy every historical row has a well-defined assignee (`assignee = user_id`), so
that migration backfills exactly and loses nothing — which is why carrying a nullable
column now would buy nothing.

#### D4.2 — Durability is not restart-safety: the claim needs a lease and a re-drive

**Decided:** persisting the row is necessary but *not sufficient*, and a naive `0004`
would make things worse rather than better. Three things have to land together.

**1. The lost thing is the continuation, not the decision.** `resume()` appends the
`FunctionResponse` to the session and then drives `runner.run_async(...)` **in
process** (`app/cluster/hitl.py:268`). The append is durable once the session is on
AlloyDB, so a crash mid-resume does not lose the human's answer — but nothing
re-drives the invocation afterwards. Without a re-drive the conversation stays
paused forever, having recorded a decision nobody acted on. "The approval survived"
and "the run resumes" are separate properties; only the first comes free with a table.

**2. Persisting the claim as designed reintroduces R4, permanently.** The endpoint
claims the row before resuming and rolls back in `except` handlers
(`app/fast_api_app.py:301-317`), which is right for an *exception* — but a SIGKILL,
an eviction or an OOM runs no handler. Since `hitl_decide` treats any
`status != "pending"` as `already_decided`, a row stranded mid-flight answers
`already_decided` to every retry and can never be decided again. In memory this
self-heals, because the dict dies with the pod; **write it to a table and the
self-healing is what you lose.** That is exactly the R4 failure the claim/rollback
comment was written to prevent, resurrected by durability.

Note the state the implementation already writes — `deciding` — is absent from the
enum first drafted above (`pending | approved | rejected | expired`). A `0004`
built from that spec cannot represent the one state that matters here. It is now in
the schema, with `expired` still unimplemented and reserved.

**3. So the claim must be reclaimable.** `deciding_since` + `deciding_by` turn the
claim into a **lease**: a `deciding` row older than the lease TTL is presumed
abandoned and becomes answerable again. Two levels, in order of cost:

- **Floor — reclaim on read.** When `hitl_decide` (or the list endpoint) meets a
  `deciding` row whose `deciding_since` exceeds the TTL, treat it as `pending` and
  let the human retry. No background machinery, no scheduler, and it is enough to
  make an approval permanently answerable, which is the property R6 actually needs.
- **Upgrade — sweep on startup.** For unattended recovery, reclaim expired leases in
  the `lifespan` hook and re-drive them. Only then does a restart heal *without a
  human retrying*.

**Detecting a dead claim: identity, as a single-replica shortcut.** A TTL alone
forces an awkward tradeoff — too short and a live, slow resume gets reclaimed and
driven twice; too long and recovery is delayed by exactly that margin. `deciding_by`
sidesteps it at one replica: stamp the pod identity (`HOSTNAME` plus a process-start
UUID, since a restarted pod reuses its name) and a row claimed by any *other*
identity must belong to a dead predecessor, reclaimable immediately. The TTL then
only covers the case identity cannot decide — a claim held by this same process.

**This shortcut is what pins the entry point to one replica**, and it is the whole
subject of Phase 5: with two replicas, "not me" also matches a peer that is alive
and mid-resume, so a restarting pod would steal a live lease. The fix is to
heartbeat the lease and sweep on staleness instead, at which point `deciding_by`
becomes diagnostic rather than load-bearing.

**The upgrade tier needs the decision persisted at claim time.** Today
`pending.decision` is written only *after* a successful resume
(`app/fast_api_app.py:320`), so a crash loses what the human actually said — a
sweeper would meet a `deciding` row with `decision IS NULL` and have nothing to
re-drive *with*. Auto-recovery therefore requires writing `decision` / `decided_by` /
`decided_at` in the **same** write that sets `status = 'deciding'`, and only flipping
to `approved`/`rejected` once the continuation completes. This does **not** re-open
R4: answerability is keyed on the lease, not on `decision` being null, so a stranded
row is still reclaimable. Under the floor tier this is optional — the human re-enters
the decision on retry — which is the honest reason the floor is cheaper.

`resumed_at` is **not** what separates a crashed resume from a completed one; with
the ordering above `status` already does that (`deciding` = in flight or abandoned,
`approved`/`rejected` = finished). Keep it for audit and for measuring
decision→completion latency, and as a guard if the ordering is ever changed to record
the outcome earlier.

**Re-driving must not double-append.** After a crash the session may already hold the
`FunctionResponse` — the append at `hitl.py:268` precedes the run. A blind retry
would append it twice, which is precisely the F7(a) mistake D5 rejects. The resume
helper must therefore check the session for an existing response bearing this
`function_call_id` and skip the append if present; `(session_id, function_call_id)`
is already the natural key for that. Re-running the *model* turn is safe under the
existing at-least-once stance (gated tools are required to be idempotent), but
re-appending is not.

**A re-drive cannot cross a finished A2A hop — and "no exception" is not success.**
Found the hard way in the cluster (R10), after this section was first written. If
the crash lands *after* the peer that paused has finished, its A2A task is already
terminal, and with `TASK_STORE_BACKEND=database` that is durable — re-sending gets
`Task <id> is in terminal state: completed`. ADK reports this as an error **event**
rather than raising, so `run_async` returns normally with no answer, and a naive
sweeper marks the row recovered. It is not.

The distinction the implementation now draws:

- The decision stands and its effect already happened **exactly once** (verified:
  the gated tool ran once, the response was appended once), so the row is
  *finished* rather than released — releasing would invite a retry that can never
  succeed.
- But no continuation completed, so `resumed_at` stays NULL and the log warns.
  `resumed_at IS NULL AND status IN ('approved','rejected')` is then exactly the
  set of "decided and effected, but the user never got the reply".

**Still open:** recovering the answer as well would mean reading the peer's result
out of the A2A task store instead of re-sending it — the task is terminal, but its
output is durable. That is future work the lease alone cannot fix, and it is why
the upgrade tier recovers the *narration* only when no peer is involved.

### D5 — One resume helper, with the F6 workaround quarantined in it

Every resume goes through a single function implementing F7(b) — append the
`FunctionResponse` to the session, then `run_async(invocation_id=…, new_message=None)`.
One place to comment, one place to delete when ADK fixes `runners.py:1089`.

Do **not** use F7(a) (call resume twice): it appends the response twice and relies on
a side effect of a failure.

### D6 — Layering: `app/cluster/hitl.py` + routes in `app/fast_api_app.py`

Per AGENTS.md's layering table this is cluster plumbing (durable state and runner
mechanics), not agent identity — so no new top-level package:

- `app/cluster/approvals.py` — the store: the state machine, the D4.2 lease, and
  both backends (the `hitl_approvals` table on the shared engine from
  `app/cluster/db.py`, or per-pod memory when `DB_BACKEND=none`). Split out of
  `hitl.py` once durability turned a one-line dict into something worth reading
  on its own; same layer, still `app/cluster/`.
- `app/cluster/hitl.py` — the capture plugin, the resume helper, and the
  abandoned-resume sweep.
- `app/fast_api_app.py` — the HTTP surface only, delegating to the above. This is
  the documented seam; `app/app_utils/**` stays untouched.
- `app/agents/**` — unchanged except for declaring `require_confirmation` on the
  tools that need it.

### D7 — Durability requirements are already met in the cluster

Resume works from any replica only if session, task and approval state are all
shared. The ConfigMap already sets `SESSION_BACKEND: alloydb`,
`TASK_STORE_BACKEND: database` and `DB_BACKEND: alloydb`; the approvals table joins
them. Locally (`in_memory` defaults) resume only works in the same process — state
that as a documented limitation rather than papering over it.

### D8 — What a human may send back: approve/reject **plus feedback**, not edits

The decision is not a bare boolean. The API accepts:

```json
{"decision": "approved" | "rejected", "note": "free text", "payload": { }}
```

stored in the `decision` jsonb column. How much of it reaches the agent depends on
the gate mechanism, and ADK 2.6.1 is asymmetric here — worth knowing before building
a UI that promises more than the runtime delivers.

**With `require_confirmation` (D1):**

- `ToolConfirmation` carries `hint`, `confirmed` and a free-form JSON `payload`
  (`tools/tool_confirmation.py`), so structured feedback has a transport.
- **On approval** the tool body runs with the **original arguments**
  (`function_tool.py:316`) and can read the human's words via
  `tool_context.tool_confirmation.payload` (`agents/context.py:268`) — a gated tool
  that wants the note simply declares `tool_context` and reads it. The payload is
  *not* merged into the arguments; nothing rewrites the call.
- **On rejection** ADK returns a fixed `{'error': 'This tool call is rejected.'}`
  (`function_tool.py:314`). The human's note does **not** reach the model. Plan
  accordingly: after resuming with `confirmed=false`, append the note as a follow-up
  user turn on the same session so the model can re-plan with the reason. (Verified
  in the spike only for the free-form pattern below — treat as Phase 2 work.)
- If inline rejection feedback turns out to matter, the escape hatch is to leave
  `require_confirmation=False` and call `tool_context.request_confirmation(hint=…)`
  from inside the tool body (`agents/context.py:847`). The tool then owns both
  directions and can return a rich rejection, at the cost of hand-rolling the gate.

**With the fallback long-running pattern**, the whole `FunctionResponse` is ours, so
free text reaches the model directly — spike-verified (**F5**): rejecting with
`note: "denied: value is confidential"` produced *"I have the answer, but I cannot
share it with you. It is confidential."*

**With `request_input` (D1b)** the human's reply is an ordinary `FunctionResponse`
whose content we choose, so free text, structured data and corrections all reach the
model directly — this is the mechanism to use when the point is *feedback*, not
permission. The reply becomes ordinary conversation context the model plans against.

**Revised arguments — supported by re-planning, not by rewriting the call.** No
mechanism substitutes the human's values into the pending call: `require_confirmation`
re-invokes with the original arguments, and `request_input` never had a pending action
to begin with. What works today:

1. **Reject (or answer) with a reason** — the model proposes a corrected call, which is
   gated again. The human steers; the model still authors the action. Nothing to build.
2. **A tool opts in** to honouring `payload["overrides"]`, validating each field itself.
   Per-tool, deliberate, never implicit.

Direct argument editing — the human's values executed verbatim — stays out of v1: it
needs per-tool rules for what may be edited, its own audit trail (the executed action
no longer matches what the model asked for), and a form-style UI. Option 1 reaches the
same outcome for most cases and keeps the model accountable for the call.

Anything the human writes is untrusted input on its way to a model — see the security
notes.

## Phases

### Delivered (phases 0-3)

Resumability, both HITL mechanisms, the graph agent, durable approvals with the
D4.2 lease (migration `0004`), the `/hitl` routes, and A2A end to end. All of it
was exercised in the live cluster on AlloyDB, including both Phase 2 acceptance
kills. The detail that used to live here is not worth repeating: read
[`results.md`](results.md) for what each step actually did (**R1**-**R10**), and
the code for how. What remains below is only what is *not* built.

Two findings are load-bearing and easy to lose if the phase text is skimmed:

- **Strategy C via `tool_context.run_node` does not work** (R3) and the code was
  removed. Do not reintroduce it.
- **A re-drive cannot cross a finished A2A hop** (R10). The decision and its
  effect survive a crash; the *answer* does not, once the peer's task is
  terminal.

### Phase 4 — Cluster and operations (partly done)

Done: migration `0004` runs in the migration Job before the agents start, and the
new env var is in AGENTS.md. Remaining:

- **`HITL_ENABLED`** (default off) so the routes and plugin are opt-in per agent.
  Today they are always on, which is harmless but not a deliberate posture.
- **Expose the approval API deliberately.** It must be reachable by operators and
  *not* by the agents. The orchestrator Service is `ClusterIP`, so exposing it is
  an explicit ingress decision that has not been made.
- **Expiry sweep.** `expired` is in the status CHECK constraint and in
  `STATUSES`, but nothing writes it, so a forgotten pause pins a session forever.
- **`GKE.md` / `README.md`** still say nothing about HITL; AGENTS.md requires
  keeping them in sync.

### Phase 5 — Heartbeat the lease so the entry point can scale out (done)

Delivered and verified in the cluster at 2 replicas; see [`results.md`](results.md)
R11. Two things are worth carrying forward, because both corrected a belief this
plan previously stated as fact:

- **The pause/resume path was never pod-local.** A paused invocation is rebuilt
  from the session, so a replica can resume work it never paused. The
  single-replica constraint documented before Phase 5 was simply wrong, and
  measuring it is what showed that.
- **The real blocker was one predicate.** `sweep_abandoned` reclaimed any lease
  whose owner was "not me" — sound at one replica, and at two it also matches a
  live peer mid-resume. Liveness is now measured: a resume renews its lease every
  TTL/3, both reclaim paths test staleness alone, and `deciding_by` is diagnostic
  only. Recovery also moved off startup onto a timer, so a dead pod's work is
  picked up by any live peer within `HITL_LEASE_TTL_SECONDS` (now 30 s, since it
  only has to outlast a few missed beats rather than the longest resume).

`/hitl/run` also scopes its pending diff to the caller's session, which was
mis-attributing concurrent pauses once more than one replica could serve it.

**Still true:** scaling requires a database. With `DB_BACKEND=none` the store is
per-pod memory again, so only the pod that took a decision can act on it. The
manifests stay at `replicas: 1` as a cost default.

## Security notes

- The approval endpoints change what an agent is allowed to do. They need real
  authn/authz before anything sensitive is gated — at minimum an authenticated
  ingress; `decided_by` is recorded but not verified by the app.
- Nothing in the approval payload should be echoed to the model unfiltered; treat
  `note` as untrusted input.
- Rejection must be an explicit, distinguishable outcome for the tool, not an empty
  response the model can reinterpret (F5 showed the model does read the decision).

## Risks

| Risk | Mitigation |
| --- | --- |
| Every API here is EXPERIMENTAL in ADK (`RemoteA2aAgent`, `A2aAgentExecutor`, `ResumabilityConfig`) | Pin the ADK version deliberately when this ships; the guard test in Phase 3 makes an upstream behaviour change loud |
| F6 workaround silently becomes wrong | Same guard test; D5 keeps it in one function |
| `require_confirmation` / `request_input` may not propagate over A2A | Phase 1 checks each in isolation before anything is built on it |
| `response_schema` on `request_input` is advisory — ADK does not coerce the reply | Validate at the API boundary, never trust the shape downstream |
| Resumption is at-least-once — a tool may run twice | Gated tools must be idempotent; the unique constraint in D4 dedupes the *approval*, not the effect |
| In-memory task store past 1 replica | Already documented; `TASK_STORE_BACKEND: database` is set in the cluster |

## Open questions

1. **What actually gets gated?** The plan uses a demo tool. Real gates (payments,
   writes, external calls) decide whether D8's approve/reject-plus-note is enough, or
   whether humans must edit the arguments before the action runs — the one case that
   needs more than v1 offers.
2. **Through what surface?** CLI/curl for Phase 2, with a small per-user UI expected
   later (listing that user's pending approvals — see D4.1, which settles *who*
   approves: the requesting user, always).
3. **Expiry policy** — how long may an invocation stay paused, and what should the
   user see when it expires?
4. **Should workers ever own their own approvals** (spike Model 1)? D2 says no; that
   holds unless a worker must be usable stand-alone by a different caller.
