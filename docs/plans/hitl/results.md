# HITL strategies — live results on GKE

What actually happened when the three strategies from [`README.md`](README.md) (D1)
were implemented and run, first locally and then **in the live cluster**
(`gke_msteiner_europe-west4_agents-cluster`, image `agent:hitl-2`, one replica per
agent, `SESSION_BACKEND=alloydb`, `TASK_STORE_BACKEND=database`).

Every row below was executed. Where something is inferred rather than observed it
says so.

## Scoreboard

Letters match [`../../human-in-the-loop.md`](../../human-in-the-loop.md). The
`run_node` shortcut was tried between B and C and does not work, so it carries no
letter — it is a dead end, not an option.

| | A — `require_confirmation` | B — `request_input` | C — graph **as an agent** | *(dead end)* graph via `run_node` |
| --- | --- | --- | --- | --- |
| Pauses the invocation | **yes** | **yes** | **yes** | yes |
| Gates the action (body cannot run early) | **yes** | n/a — asks, doesn't guard | no | no |
| Human can send free text | note only, tool-visible | **yes, steers the answer** | **yes** | yes |
| Survives an A2A hop (pause up) | **yes** | **yes** | **yes** | n/a (local tool) |
| Resume reaches the paused agent | **yes** | **yes** | **yes** | partial — R3 |
| Completes the downstream work | **yes** | **yes** | **yes** — R7 | **no** — next node skipped |
| Verdict | ship for gating actions | ship for questions | ship for fixed flows | not usable — R3 |

## Results

### R1 — Strategy A gates an action, across A2A, in-cluster

`POST /hitl/run` on the orchestrator, delegating to `math`:

```
orchestrator | calls=['transfer_to_agent']
orchestrator | resp=['transfer_to_agent']
orchestrator
math | lrt=['adk-97cdd3c0-…'] | calls=['adk_request_confirmation']
→ status: paused, final_text: null
```

The pause carries the original call, so the API can show the human exactly what is
about to happen (`publish_result(value="437.0")`). Approving:

```
POST /hitl/approvals/91bc5322 {"approved": true, "text": "approved in-cluster by ops"}
→ "The product of 23 and 19 is 437. I have successfully computed and published this result."
```

Before approval the tool body does **not** run: the model receives
`{'error': 'This tool call requires confirmation, please approve or reject.'}` and
the real `publish_result` response only appears after the decision. The reviewer's
note reaches the tool — asked afterwards, the agent quoted it back verbatim:
*The reviewer left the note: "ok, publish it"*.

### R2 — Strategy B collects free-form input that genuinely steers the answer

Research agent, asked an ambiguous question over A2A:

```
research | lrt=['uoddFALq'] | calls=['adk_request_input']
message: "Which Cambridge are you referring to? (e.g. Cambridge, UK; …)"
```

The reply carried a disambiguation *and* an extra constraint, and both were
honoured:

```
"Cambridge, UK - and give the most recent figure you can find."
→ "According to the 2021 Census … the population of Cambridge, UK was 145,700."
```

Locally the same tool answered `"Cambridge, Massachusetts - and please give the
figure for 2020 only."` with the 2020 figure specifically. This is the mechanism to
use when the human needs to *say something*, not just approve.

### R3 — The `run_node` shortcut pauses, but the graph never finishes

A `Workflow` (draft → ask human → apply feedback) executed from an ordinary tool via
`tool_context.run_node`. It runs inside the caller's invocation, as intended:

```
orchestrator   | calls=['review_plan']
review_workflow| lrt=['0adf704f-…'] | calls=['adk_request_input']
→ status: paused
```

On resume the answer looks right — the orchestrator returns a revised two-phase plan
with a rollback section — but **the graph's final node never executed**. Its output
string (`"Revised plan incorporating: …"`) appears zero times in the logs, locally
and in-cluster. Asked directly what the tool returned, the agent quoted the human's
raw reply: `'Add a rollback plan and do it in two phases.'`

So the human's response short-circuits back as the *tool's return value* and the
remaining nodes are skipped; the plausible-looking answer is the LLM re-writing the
plan itself, not the graph doing it. Anything you put after the `RequestInput` node
silently does not happen — the failure mode is invisible if you only read the reply.

Cause not established: it may be our resume shape, or `run_node` rehydration
returning to the caller rather than continuing the graph. **Do not use C until this
is understood.** A/B cover the same ground today.

### R4 — The resume payload must be `{"result": <value>}`, or the invocation dies

The first in-cluster B run returned a dropped connection and a 500. Root cause, from
the orchestrator logs:

```
ValueError: Validation failed for interrupt uoddFALq:
  Failed to coerce data to string: Input should be a valid string
  [input_value={'response': 'Cambridge, …'}, input_type=dict]
  google/adk/workflow/utils/_rehydration_utils.py:211
```

ADK unwraps a function response of **exactly** `{"result": value}`
(`_rehydration_utils.py:68`) and validates the inner value against the pause's
`response_schema`. We had sent `{"response": …}`, so the dict itself was validated
against `str` and the whole invocation crashed — taking the HTTP connection with it.

Three things came out of this:

- `hitl.input_response` now sends `{"result": text}`, with a unit test pinning the
  shape (`tests/unit/test_hitl.py`).
- The route catches `ValueError` and returns **422** with the message, instead of an
  unhandled ASGI exception that tells the caller nothing. The blast radius was wider
  than one request: the exception drops the HTTP connection, and the `kubectl
  port-forward` tunnel died with it (`error: lost connection to pod`) -- which looks
  like a flaky tunnel rather than an application bug.
- **The crash also exposed a bug of our own.** The route recorded the decision
  *before* resuming, so a failed resume left the approval marked `approved`: every
  retry answered `already_decided` while the invocation stayed paused forever. It now
  claims the approval (`deciding`), resumes, and only records the outcome on success;
  every failure path puts it back to `pending`. Guarded by
  `test_failed_resume_leaves_the_approval_answerable`.

Note ADK JSON-parses a string value when it can, so a reply of `"42"` arrives as an
int. Validate at the API boundary — `response_schema` is advisory (the upstream docs
say ADK does not coerce; here it *does* attempt coercion and fails hard).

### R5 — The F6 resume-routing workaround is still required, and works

`hitl.resume` appends the `FunctionResponse` to the session and then calls
`run_async(invocation_id=…, new_message=None)`. With that, a single decision call
resumes an A2A-delegated pause and produces the final answer (R1, R2). The bug it
works around (`runners.py:1089` choosing the continuing agent before the message is
appended) is unchanged in 2.6.1.

### R6 — In-memory approvals do not survive a pod restart; the session does

With a pause outstanding, the orchestrator pod was deleted. After it came back:

```
GET  /hitl/approvals            → {"count": 0}
POST /hitl/approvals/dbc7aecf   → 404 {"detail": "unknown approval_id"}
```

The conversation itself is safe — it is in AlloyDB — but the *index of what is
waiting* was lost, so nothing can be approved any more. This is the empirical case
for the durable table in phase 2 (D4).

Worth noting for that phase: the pause is still recoverable from the durable session
(an unanswered long-running call is visible in the event history), so the table is an
index and an audit trail rather than the only source of truth. A recovery sweep on
startup would make the system self-healing.

### R7 — Strategy C: a graph served *as an A2A agent* does resume its graph

The same three-node graph that fails as a tool (R3) works when it **is** the
agent. This is strategy C in the guide; the R3 shortcut it replaces has no
letter. `app/agents/planner` has an ADK `Workflow` as its root node, its own
Deployment/Service/agent card, and is delegated to by the orchestrator like any
other peer. In-cluster, over real A2A:

```
orchestrator | calls=['transfer_to_agent']
planner      | lrt=['6108c34a-…'] | calls=['adk_request_input']   → paused

POST /hitl/approvals/f6f899c6 {"text": "add a rollback step and split it into two phases"}
→ "PLAN-APPLIED-BY-GRAPH
   Revised plan, incorporating the reviewer's instruction ('add a rollback step …')"
```

and the planner pod's own log proves every node ran, not just the pause:

```
planner: draft_plan ran
planner: collect_feedback ran, pausing for a human
planner: apply_feedback ran (PLAN-APPLIED-BY-GRAPH)
```

The difference from the R3 shortcut is the tool boundary. As a tool, the human's reply is
handed back to the *caller* as the tool's return value and the graph is
abandoned. As an agent, the peer's own runner owns the invocation and the node
runtime resumes the interrupted node — which is also why
`runners.py:1763` short-circuits agent routing for a `Workflow` root.

Cost of admission: a graph agent cannot have peers (no `sub_agents` to attach
them to), so it is a leaf. `build_agent` raises if a spec declares both.

### R8 — A graph's output is invisible unless the last node returns `Content`

First attempt at C resumed correctly and returned **nothing**: `final_text: null`,
with the caller seeing only a replay of the pause. All the graph's events had no
content.

A node that returns a plain value is wrapped in `Event(output=…)`
(`workflow/_function_node.py:394`), and `Event.output` is real — but **nothing
converts it into an A2A message**: `a2a/converters/event_converter.py` only reads
`content`. A `Workflow` sets its terminal output on `ctx.output` for a *parent* to
read, and a root served over A2A has no parent.

Returning `types.Content` from the terminal node produces `Event(content=…)`,
which the A2A converter, the ADK web UI and any text-reading caller all
understand. That one-line difference is what turned C from "silently empty" into
the R7 result.

Same lesson as R3 in a different disguise: **a plausible answer is not evidence
the flow ran.** Both failures produced confident prose — from the *caller's* model
in the R3 shortcut, and an empty string here that a coordinating LLM would
happily paper over.

### R9 — the planner has no GSA (not applied yet)

`var.agents` lists `planner`, but the planner Deployment borrows `agent-math`'s
service account and runs with `DB_BACKEND=none` plus in-memory session/task
backends, all commented in `workers.yaml`. Fine for the experiment (single
replica, orchestrator holds the durable conversation), wrong for production.

> **Correction.** This was first recorded as "Terraform could not be applied",
> on the strength of `terraform plan` failing with:
>
> ```
> Error: Invalid reference in variable validation
>   on variables.tf line 85, in variable "agent_extra_iam_roles":
>   The condition ... can only refer to the variable itself
> ```
>
> That diagnosis was wrong — it was the wrong binary, not a blocked config. The
> Homebrew `terraform` here is v1.5.7, which is below the `>= 1.6` floor that
> `versions.tf` already declares. **OpenTofu v1.12.3 is installed and runs the
> configuration unmodified**: `tofu validate` passes and `tofu plan` succeeds,
> creating `agent-planner` plus its IAM, AlloyDB user and Workload Identity
> binding. `docs/deploy-to-another-project.md` said to use `tofu` all along, and
> `terraform.tfstate` was written by OpenTofu 1.12.3 — v1.5.7 could not have read
> it regardless.
>
> So the planner's missing identity is a *not-yet-applied* gap, not a tooling
> block. Applying is not purely additive though: the plan is 19 to add, 1 to
> change and 2 to destroy, the destroys being the AlloyDB private-services-access
> address and peering being replaced under a live cluster. Read the plan first.

### R10 — durable approvals work; the *replay* does not cross a finished A2A hop

Phase 2 is built (migration `0004`, `app/cluster/approvals.py`, the D4.2 lease).
Both acceptance kills were run against the live cluster on AlloyDB.

**Kill 1 — between pause and approval.** This is R6, and it is fixed. The pod was
force-deleted with an approval outstanding; a brand-new pod listed it and the
approval resumed to a correct final answer:

```
GET  /hitl/approvals   -> count=1, a97baa61, status=pending   (was count=0)
POST /hitl/approvals/a97baa61 -> "resumed"
final_text: "The product of 23 and 19 is 437. ... published the result."
```

**Kill 2 — during the resume.** The uvicorn process was SIGKILLed ~0.3 s after the
decision was accepted (`curl` exit 52, empty reply). On restart the sweep took
over the abandoned lease, and the persisted decision was still there — no human
was asked twice. But the replay itself **failed**, and the first implementation
reported success anyway:

```
A2A request failed: JSON-RPC Error code=-32602
  message='Task 523b4fde-... is in terminal state: completed'
HITL: recovered 1 abandoned resume(s)          <- WRONG
```

The orchestrator's session ended with an empty event carrying that
`error_message`, where the clean run had the final answer. **The failure is
structural, not transient.** The crash came *after* the math peer had finished,
and `TASK_STORE_BACKEND=database` makes the peer's A2A task durably terminal, so
re-sending is rejected. Checked in the peer's schema: `publish_result` executed
**exactly once** (every session shows one call/response pair), and the caller's
session holds **one** `adk_request_confirmation` response — so neither the gated
action nor the append was duplicated. Only the narration was lost.

The bug was ours: ADK folds a failed A2A hop into an error *event* instead of
raising, so `run_async` returned normally and "no exception" was mistaken for
success. Third time this project has been caught by the same thing (R3, R7):
**a plausible outcome is not proof the flow ran.** Fixed by checking the trace
for error events and for a final response before claiming a replay happened.
`complete(..., resumed=False)` now finishes the row while leaving `resumed_at`
NULL, and the log says so:

```
WARNING HITL: approval fa90a7ed finished as approved WITHOUT a replayed answer
        (decision stands, narration lost): ['math | error="A2A request failed ..."']
```

Re-verified after the fix, same crash, on the live cluster:

| approval | path | status | decided_at | resumed_at |
| --- | --- | --- | --- | --- |
| `46445103` | clean resume | approved | 09:58:46 | 09:58:47 |
| `fa90a7ed` | crashed + swept | approved | 09:59:30 | **NULL** |

Nothing left in `pending` or `deciding` either way. `tests/unit/test_hitl.py`
guards both halves.

**What is still open.** Recovering the *answer* after a crash that lands past a
completed A2A hop needs the peer's result read back from the task store rather
than re-sent — the task is terminal but its output is durable. Until that exists,
`resumed_at IS NULL AND status IN ('approved','rejected')` is the query for
"decided and effected, but the user never saw the reply".

### R11 — the entry point scales out; liveness has to be measured, not inferred

Phase 5 is built. Three things were checked in the live cluster, at 2 replicas of
`orchestrator` on AlloyDB.

**A replica can resume an invocation it never paused.** A pause created on pod A
was approved on pod B:

```
pod B: GET  /hitl/approvals           -> count=1, f2526997   (B never saw the pause)
pod B: POST /hitl/approvals/f2526997  -> "resumed"
final_text: "The product of 29 and 21 is 609, and the result has been ... published."
```

Nothing about a pause is pod-local: `ResumabilityConfig(is_resumable=True)`
rebuilds the invocation from the session, and both the session and the approval
live in the database. **This was already true before Phase 5** — the
single-replica constraint documented up to that point was wrong, and this is what
disproved it.

**What actually blocked scaling was the sweep's reclaim rule.** It took any lease
whose `deciding_by` was not this process, which means "a dead predecessor" only
at one replica; with two it also matches a peer that is alive and mid-resume, and
taking that lease drives the same human decision twice. Fixed by measuring
liveness rather than inferring it: a resume renews its lease every TTL/3 while it
runs, and both reclaim paths now test staleness alone. `deciding_by` survives as
diagnosis — which pod holds a row — and no logic branches on it.

**A live peer recovers a dead owner's work, with no restart.** This is what a
startup-only sweep could never do. The state a killed pod leaves behind was
injected directly (`status='deciding'`, `deciding_by='dead-pod/deadbeef'`,
`deciding_since` an hour old) so the test did not depend on winning a race:

```
08:22:51  injected: ('deciding', 'dead-pod/deadbeef')
08:22:56  HITL: replayed abandoned resume 12b0da35    <- periodic sweep, restarts unchanged
          status=approved  decision={"approved": true, "text": "owner died"}  resumed_at set
```

Five seconds, by a pod that never restarted, honouring the recorded decision
without asking anyone again. `resumed_at` is set, so this is a genuine replay and
not the R10 close-out.

**No double execution anywhere.** Across all 13 sessions that reached
`publish_result` — clean resumes, two crash recoveries, and this peer recovery —
every one shows exactly one call/response pair.

An earlier attempt at this test is worth recording, because it looked like a
pass. Pod A was SIGKILLed mid-resume with pod B alive, and the approval was
recovered — but the logs showed **pod A's own restart** got there first, not B. A
container restart is quicker than a 30 s sweep interval, so the peer path was
never exercised at all. Hence the injection above. Watching the outcome instead
of the actor would have banked an untested path as verified.

## What this changes in the plan

1. **D1 becomes A, B and C**, where C is now the *graph agent* (R7), not the
   `run_node` shortcut. R3 makes that shortcut unusable and R7 supersedes its
   motivation entirely: a graph gets a working human step by being an agent. The
   shortcut survives only as a documented dead end, deliberately without a letter
   so the guide reads A, B, C.
2. **D8 is confirmed, with the payload contract corrected** — `{"result": …}`, not
   any other key (R4).
3. **Phase 2 (durable store) is not optional** for anything real (R6), and should
   include a session-scan recovery path.
4. **Add a schema-validation boundary** on the API: today a malformed reply reaches
   ADK and fails deep inside the runtime (R4).

## Reproducing

```bash
kubectl -n agents port-forward svc/orchestrator 8080:80

# A — gated action over A2A
curl -X POST localhost:8080/hitl/run -H 'content-type: application/json' \
  -d '{"text":"Ask the math specialist to compute 23 * 19 and publish the result."}'
curl -X POST localhost:8080/hitl/approvals/<id> -H 'content-type: application/json' \
  -d '{"approved":true,"text":"approved by ops"}'

# B — free-form question over A2A
curl -X POST localhost:8080/hitl/run -H 'content-type: application/json' \
  -d '{"text":"Ask the research specialist about the population of Cambridge."}'
curl -X POST localhost:8080/hitl/approvals/<id> -H 'content-type: application/json' \
  -d '{"text":"Cambridge, UK - most recent figure."}'

curl localhost:8080/hitl/approvals      # what is waiting
```

Local equivalent: three processes (`AGENT_NAME=math|research|orchestrator`) per the
recipe in AGENTS.md, with `A2A_PEERS=math=…,research=…` on the orchestrator.
