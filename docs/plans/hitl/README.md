# Human-in-the-loop (HITL) — implementation plan

Plan for adding human approval gates to this cluster: an agent pauses before a
gated action, a human approves or rejects out of band, and the paused invocation
resumes — **including when the pause happens inside a peer reached over A2A**.

Grounded in [`spike-findings.md`](spike-findings.md), which records what was
actually run against ADK 2.6.1. Findings are cited below as **F1**–**F8**. Nothing
in this plan has been implemented yet.

## Goal and non-goals

**Goal.** A tool can be declared "needs a human". When the model tries to call it,
the invocation pauses before the tool body runs, an approval is recorded durably,
and an operator can approve/reject via an HTTP endpoint on the orchestrator. The
answer resumes on the same session, whichever agent paused.

**Non-goals for now.** An approval UI; multi-approver / quorum; approval routing by
role; approvals initiated by anything other than a tool call; human *edits* to tool
arguments (approve-or-reject only, at first).

## Design decisions

### D1 — Gate with `require_confirmation`, not a "pending" long-running tool

The spike used `LongRunningFunctionTool` returning `{"status":"pending"}`. That is
**not a gate** (F8): the return value goes back to the model, which happily keeps
talking. Only a stern instruction stopped it leaking the answer, and a prompt is
not a security control.

ADK 2.6.1 has a first-class mechanism instead:

```python
FunctionTool(func=transfer_funds, require_confirmation=True)
# or, conditionally on the arguments:
FunctionTool(func=transfer_funds, require_confirmation=lambda amount, **_: amount > 10_000)
```

The flow (`flows/llm_flows/functions.py:374`) synthesizes a long-running
`adk_request_confirmation` function call carrying the original call plus a
`ToolConfirmation`, and **the tool body does not execute** until the confirmation
response arrives. That is structural: the gated effect cannot happen early, and
there is no result for the model to leak.

Because the synthesized call is an ordinary long-running function call, F2/F4
(propagation up over A2A, relay down on resume) should apply unchanged — *should*,
because this specific path was not in the spike. **Phase 1 must prove it.** If it
does not propagate, fall back to `LongRunningFunctionTool` with the gated action
placed *behind* the tool (so the model never holds the result), accepting the
weaker guarantee.

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
  status           text  not null   -- pending | approved | rejected | expired
  decision         jsonb
  decided_by       text
  created_at       timestamptz not null
  decided_at       timestamptz
  unique (session_id, function_call_id)
  index (status, created_at)                              -- global queue
  index (user_id, created_at)  where status = 'pending'   -- the per-user UI
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

### D5 — One resume helper, with the F6 workaround quarantined in it

Every resume goes through a single function implementing F7(b) — append the
`FunctionResponse` to the session, then `run_async(invocation_id=…, new_message=None)`.
One place to comment, one place to delete when ADK fixes `runners.py:1089`.

Do **not** use F7(a) (call resume twice): it appends the response twice and relies on
a side effect of a failure.

### D6 — Layering: `app/cluster/hitl.py` + routes in `app/fast_api_app.py`

Per AGENTS.md's layering table this is cluster plumbing (durable state and runner
mechanics), not agent identity — so no new top-level package:

- `app/cluster/hitl.py` — the store (SQLAlchemy table + queries on the shared
  engine from `app/cluster/db.py`), the capture plugin, and the resume helper.
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

## Phases

Each phase is independently shippable and ends green on
`agents-cli lint` + the hermetic unit-test command from AGENTS.md.

### Phase 0 — Enable resumability (no behaviour change)

- `app/agent.py`: `App(..., resumability_config=ResumabilityConfig(is_resumable=True))`.
- `tests/unit/test_agents.py`: assert the flag is on, so nobody drops it silently.

**Acceptance:** full unit suite green; a normal (ungated) request behaves exactly as
before. **Risk:** resumability changes runner routing semantics
(`_find_agent_to_run` only consults past function responses when it is enabled) —
this phase exists to isolate that change from everything else.

### Phase 1 — Prove `require_confirmation` end to end, single agent

- Add a gated demo tool to the math agent (e.g. `calculate` stays open; a new
  `publish_result` requires confirmation), declared per D1.
- Drive it in-process from a test/script: assert the pause surfaces as a long-running
  `adk_request_confirmation` call **and that the tool body did not run** (a sentinel
  the tool would have written).
- Resume by sending the confirmation response; assert the body then runs once.

**Acceptance:** the gate holds without relying on the instruction text — the
regression the spike could not get from `LongRunningFunctionTool` (F8).
**If it fails:** fall back per D1 and record why in `spike-findings.md`.

### Phase 2 — Durable approvals + the API

- Migration `0004` for the D4 table; extend `tests/unit/test_migrations.py`'s offline
  render so the new table is covered by the same drift guard.
- `app/cluster/hitl.py`: store + capture plugin (D3) + resume helper (D5).
- Register the plugin on the `App`; wire routes in `app/fast_api_app.py`:
  - `GET  /hitl/approvals?status=pending` — list
  - `GET  /hitl/approvals/{id}` — detail (tool, args, session, age)
  - `POST /hitl/approvals/{id}/decision` — `{approved, note, decided_by}` → resume
- Idempotency: a decision on an already-decided approval returns the recorded
  outcome instead of re-running the invocation.

**Acceptance:** kill and restart the pod between pause and approval; the resume still
works (needs a database — run it against a local Postgres or in-cluster).

### Phase 3 — A2A end to end

- Two-process local run (the recipe in AGENTS.md) with the gated tool on `math` and
  the API on `orchestrator`.
- Assert: orchestrator pauses with no final answer (F2); one `POST` to the decision
  endpoint produces a real inbound A2A call on the worker and a final answer (F4/F7b);
  rejection changes the outcome (F5).
- **Guard test for F6.** Assert the workaround is still *needed*: a plain
  `run_async(invocation_id=…, new_message=<FunctionResponse>)` after an A2A pause
  yields zero events. When a future ADK release fixes `runners.py:1089` this test
  fails, which is the signal to delete D5's workaround. Mark it clearly as an
  upstream-behaviour guard, not a product requirement.

### Phase 4 — Cluster and operations

- ConfigMap: `HITL_ENABLED` (default off) so the routes and plugin are opt-in.
- Confirm the migration Job runs `0004` before the agents start (unchanged process:
  delete the Job, apply, wait for completion).
- **Expose the approval API deliberately.** It must be reachable by operators and
  *not* by the agents: it is an orchestrator-only route, and the orchestrator
  Service is `ClusterIP` today, so exposure is an explicit ingress decision.
- Expiry: a `status=expired` sweep for approvals older than N hours, so a forgotten
  pause does not pin a session forever.
- Update `GKE.md` + `README.md` (AGENTS.md requires keeping them in sync) and add the
  new env vars to the tables in AGENTS.md.

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
| `require_confirmation` may not propagate over A2A | Phase 1 checks it in isolation before anything is built on it |
| Resumption is at-least-once — a tool may run twice | Gated tools must be idempotent; the unique constraint in D4 dedupes the *approval*, not the effect |
| In-memory task store past 1 replica | Already documented; `TASK_STORE_BACKEND: database` is set in the cluster |

## Open questions

1. **What actually gets gated?** The plan uses a demo tool. Real gates (payments,
   writes, external calls) determine whether approve/reject is enough or humans need
   to edit arguments.
2. **Through what surface?** CLI/curl for Phase 2, with a small per-user UI expected
   later (listing that user's pending approvals — see D4.1, which settles *who*
   approves: the requesting user, always).
3. **Expiry policy** — how long may an invocation stay paused, and what should the
   user see when it expires?
4. **Should workers ever own their own approvals** (spike Model 1)? D2 says no; that
   holds unless a worker must be usable stand-alone by a different caller.
