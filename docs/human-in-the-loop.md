# Human-in-the-loop

How to make an agent stop and wait for a person, and which of the three
mechanisms to reach for. Everything here was run in this repo, locally and on the
cluster; the evidence is in [`plans/hitl/results.md`](plans/hitl/results.md).

## Pick one

| You want to… | Use | Where |
| --- | --- | --- |
| Stop an action until someone approves it | **A. `require_confirmation`** | a tool on any agent |
| Ask a person a question mid-task | **B. `request_input`** | a tool on any agent |
| Run a fixed sequence with a human step inside it | **C. a graph agent** | its own agent, root is a `Workflow` |

**Rules of thumb.** If the point is *permission*, use **A** — it is the only one
that structurally prevents the action from happening. If the point is
*information*, use **B**. If the human step is one stage of a fixed pipeline
whose later stages must run, use **C**. A and B are tools and can live in the
same agent; C is a different agent.

## How it works underneath

All three are the same mechanism: **a long-running function call**.

1. The agent emits a function call marked long-running (`adk_request_confirmation`,
   `adk_request_input`) and the invocation **stops** with no final answer.
2. `HitlPlugin` (`app/cluster/hitl.py`) sees the event on the Runner and records a
   pending approval. It is a plugin, so it captures pauses from every surface —
   the `/hitl` routes, the ADK web UI, and inbound A2A calls alike.
3. A human answers via `POST /hitl/approvals/{id}`.
4. `hitl.resume` appends the matching `FunctionResponse` to the session and
   re-runs the invocation, which continues where it stopped.

**Across A2A this works unchanged.** A pause inside a peer surfaces in the
caller's event stream, and the reply is relayed back to that peer on the same A2A
task — no relay code of ours. So the orchestrator can own the whole human-facing
API while the pause happens two hops away.

```
human ──▶ POST /hitl/approvals/{id} ──▶ orchestrator ──A2A──▶ math (paused here)
                                                                    │
        final answer ◀──────────────────────────────────────────────┘
```

## A. Gate an action — `require_confirmation`

```python
from google.adk.tools.function_tool import FunctionTool


def publish_result(value: str, tool_context: ToolContext) -> dict[str, str]:
    """Publish a result. Requires approval."""
    return {"status": "published", "value": value}


publish_result_tool = FunctionTool(func=publish_result, require_confirmation=True)
# or gate conditionally on the arguments:
# FunctionTool(func=transfer, require_confirmation=lambda amount, **_: amount > 10_000)
```

Add it to the agent's `tools` (see `app/agents/math/agent.py`). ADK stops the call
**before the body runs** and synthesises a confirmation request carrying the
original call, so the approval UI can show exactly what is about to happen.

- The human sends `{"approved": true|false, "text": "..."}`.
- On approval, the note reaches the tool as
  `tool_context.tool_confirmation.payload["note"]`.
- On rejection, ADK returns a fixed `'This tool call is rejected.'` to the model —
  **the note does not reach it**. Say why in a follow-up turn if the model should
  re-plan.
- The arguments are never rewritten. A human who wants different arguments should
  reject with a reason and let the model propose a corrected call.

## B. Ask a question — `request_input`

```python
from google.adk.tools import request_input  # public despite the lazy re-export

SPEC = AgentSpec(..., tools=(web_search, request_input, remember, recall))
```

Then tell the agent when to use it, e.g. *"if the request is ambiguous and one
clarification would change your answer, call `adk_request_input` before
searching"*. The reply is ordinary conversation context, so it genuinely steers
the outcome — in testing, `"Cambridge, UK - and give the most recent figure"`
changed both the city and the year of the answer.

Send it with `{"text": "..."}`. No `approved` flag: nothing is being gated.

## C. A graph agent with a human step

When the flow is fixed and the steps after the human must run, make the agent a
graph. `app/agents/planner/` is the worked example:

```python
def collect_feedback(node_input):
    yield RequestInput(
        message=f"{node_input}\n\nWhat should change?", response_schema=str
    )


planner_workflow = Workflow(
    name="planner",
    edges=[("START", draft_plan, collect_feedback), (collect_feedback, apply_feedback)],
)

SPEC = AgentSpec(
    name="planner",
    description=...,
    instruction="",
    tier="fast",
    root_node=planner_workflow,
)
```

`build_agent` serves the graph directly, and it is an ordinary cluster member:
its own Deployment, Service and agent card, reachable over A2A.

Three constraints that are easy to get wrong:

- **A graph agent cannot have peers.** It has no `sub_agents`, so `build_agent`
  raises if a spec declares both. It can be delegated *to*, not *from*.
- **The last node must return `types.Content`.** Any other value is wrapped in
  `Event(output=…)`, and nothing converts `event.output` into an A2A message — the
  graph finishes correctly and the caller receives an empty answer.
- **The graph must be the agent's root**, not something a tool runs. See below.

### Why the graph has to be the whole agent

The obvious shortcut — keep an ordinary `LlmAgent` and run the graph from inside
a tool with `tool_context.run_node(...)` — **does not work**, and fails quietly.
The graph pauses correctly, but on resume the human's reply is handed back to the
*calling agent* as the tool's return value and **every node after the pause is
skipped**. The caller's model then writes a plausible answer from that reply, so
the reply looks right while the pipeline never ran.

Serving the graph as the agent removes the tool boundary: the peer's own runner
owns the invocation and the node runtime resumes the interrupted node. Measured
both ways in [`plans/hitl/results.md`](plans/hitl/results.md) — R3 is the failing
shortcut, R7 the working shape.

## The API

| Route | Purpose |
| --- | --- |
| `POST /hitl/run` | run a turn; reports `completed` or `paused` with what is pending |
| `GET /hitl/approvals` | what is waiting for a human |
| `POST /hitl/approvals/{id}` | answer it: `{"approved": bool, "text": "…"}` |
| `GET /hitl/session/{id}` | dump a session's events (diagnostics) |

```bash
kubectl -n agents port-forward svc/orchestrator 8080:80

curl -X POST localhost:8080/hitl/run -H 'content-type: application/json' \
  -d '{"text":"Ask the math specialist to compute 23 * 19 and publish the result."}'
curl localhost:8080/hitl/approvals
curl -X POST localhost:8080/hitl/approvals/<id> -H 'content-type: application/json' \
  -d '{"approved":true,"text":"approved by ops"}'
```

## Before relying on this in production

- **Approvals are durable when a database is configured** (`DB_BACKEND=alloydb`
  or `url`): migration `0004` creates `hitl_approvals` in the agent's schema and
  `app/cluster/approvals.py` reads and writes it. With `DB_BACKEND=none` — the
  default, and what the `planner` runs on — the store falls back to per-pod
  memory and approvals are still lost on restart.
- **A crash mid-resume is recovered, but the answer may not be.** The decision is
  written when the approval is *claimed*, so a restart re-drives it without
  asking the human again. If the pause happened inside an A2A peer that had
  already finished, its task is terminal and the reply cannot be replayed: the
  row is closed with `resumed_at` left NULL and a warning is logged. Query
  `resumed_at IS NULL AND status IN ('approved','rejected')` for approvals whose
  effect happened but whose answer never reached the user (see
  [`plans/hitl/results.md`](plans/hitl/results.md) R10).
- **Agents scale out; nothing about a pause is pod-local.** A paused invocation
  is rebuilt from the session rather than held in memory, so any replica can
  answer any approval — a pause created on one pod resumes correctly when the
  decision arrives at another. Three things make that safe: the approval row and
  the session are both in the database, the claim is a single conditional
  `UPDATE` so two deciders cannot both win, and the lease is heartbeated while a
  resume runs, so recovery reclaims only leases that stopped advancing. If a pod
  dies mid-resume, a live peer finishes the job within
  `HITL_LEASE_TTL_SECONDS` (default 30) without waiting for a restart. Scaling
  out does require a database: with `DB_BACKEND=none` the store is per-pod
  memory again.
- **The endpoints are unauthenticated.** They decide what an agent may do; put
  them behind an authenticated ingress before anything sensitive is gated.
  `decided_by` is not verified.
- **Gated tools must be idempotent** — ADK resumption is at-least-once.
- **Treat the human's text as untrusted input** on its way to a model.
- Everything used here is EXPERIMENTAL in ADK (`ResumabilityConfig`,
  `RemoteA2aAgent`, tool confirmation). Pin the ADK version deliberately.

## Gotchas worth knowing

- **The resume payload for B and C must be `{"result": <value>}`** — exactly that
  one key. ADK unwraps it and validates against `response_schema`; any other key
  reaches the validator still wrapped and kills the invocation. `hitl.input_response`
  handles this.
- **`response_schema` is not coercion.** A reply of `"42"` arrives as an int
  because ADK JSON-parses string values. Validate at the API boundary.
- **The first resume of an A2A-delegated pause is a no-op in stock ADK.** ADK picks
  the agent to continue before appending the incoming message, so the resume is
  routed to the root, which has already finished. `hitl.resume` appends first and
  resumes with `new_message=None`; do not bypass it.
- **A plausible answer is not proof the flow ran.** Both failures found here — the
  `run_node` shortcut's skipped nodes, and a graph whose output never reached the
  caller — produced confident, sensible-looking replies. Assert on a marker the
  code emits, not on prose.
