# Human-in-the-loop

An agent here can **pause mid-task**, durably, and wait for a human — to approve
an action before it happens, or to answer a question it needs answered. This
file explains how, and which of the three mechanisms to reach for.

**Reading order.** [The system in 90 seconds](#the-system-in-90-seconds) →
[What a pause looks like](#what-a-pause-looks-like) →
[Try it yourself](#try-it-yourself) → [Vocabulary](#vocabulary). That is enough
to use the HTTP API. [Pick one](#pick-one) and the sections after it are for
when you are adding a human step to an agent of your own;
[The API](#the-api), [production](#before-relying-on-this-in-production) and
[gotchas](#gotchas-worth-knowing) are reference.

Everything here was run in this repo, locally and on the cluster; the evidence
is in [`plans/hitl/results.md`](plans/hitl/results.md), a lab notebook whose
findings are numbered `R1`, `R2`, … and cited as such below. The transcripts in
this file are real captured output, not illustrations.

## The system in 90 seconds

You do not need to know this repo to follow the rest, but you do need four
facts about it. ([README.md](../README.md) has the full picture.)

**1. It is a cluster of small agents that call each other.** One entry-point
agent plans and delegates to specialists over **A2A** (an HTTP protocol for
agent-to-agent calls). A *peer* is an agent another agent can delegate to; a
*hop* is one such call. In production each agent is its own Kubernetes
Deployment and Service.

**2. There are four agents, and three of them demonstrate one HITL mechanism
each:**

| Agent | What it does | Human step |
| --- | --- | --- |
| `orchestrator` | Entry point. Plans, delegates to `research` and `math` | none — it relays its peers' pauses |
| `math` | Arithmetic. Tools: `calculate`, `publish_result` | **A** — `publish_result` needs approval |
| `research` | Answers factual questions. Tool: `web_search` | **B** — asks you to disambiguate |
| `planner` | Drafts a plan, takes feedback, revises it | **C** — a fixed pipeline with a human stage |

**3. These are demo agents.** `publish_result` publishes nothing — it returns
`{"status": "published"}` so there is something meaningful-looking to gate.
`calculate` is real but trivial. Read them as stand-ins for the action *you*
want a human to approve.

**4. One image, one process, one agent.** Every agent runs the same container;
the `AGENT_NAME` environment variable picks which one this process becomes at
startup. That is why the commands below start a server with `AGENT_NAME=math`
rather than pointing at a per-agent module.

Where the HITL code lives, for when a name below is unfamiliar:

| Path | What is in it |
| --- | --- |
| `app/agents/<name>/agent.py` | An agent's definition — an `AgentSpec`, this repo's declarative description of one agent (name, instruction, tools, peers) |
| `app/cluster/hitl.py` | `HitlPlugin`, which captures pauses, and `resume()`, which continues them |
| `app/cluster/approvals.py` | The store of captured pauses and its state machine |
| `app/fast_api_app.py` | The `/hitl` HTTP routes |

## What a pause looks like

Ask the `math` agent to compute something and publish it. Publishing is gated,
so the turn does not finish — it stops and tells you what it is waiting for:

```
POST /hitl/run   {"text": "Compute 23 * 19 and publish the result."}

  math | calls=['calculate']
  math | resp=['calculate']
  math | calls=['publish_result'] | text='...the result is 437. I will now publish...'
  math | lrt=['adk-3bca...'] | calls=['adk_request_confirmation']   ← the pause
  math | resp=['publish_result']

→ status: "paused",  pending: [ approval_id "17a0fa52", tool "publish_result",
                                args {"value": "437.0"} ]
```

Nothing was published. The model *decided* to publish, ADK stopped the call
**before the function body ran**, and the pending record carries the exact
arguments the human is being asked to bless. A person then answers:

```
POST /hitl/approvals/17a0fa52   {"approved": true, "text": "approved by ops"}

→ status: "resumed",  final_text: "The result 437.0 has been published."
```

The invocation picked up where it stopped, ran the tool, and produced the final
answer it would have produced had nobody interrupted.

**Across A2A this works unchanged**, which is the point. A pause raised inside a
peer surfaces in the *caller's* event stream, and the answer is relayed back to
that peer on the same A2A task — ADK and the A2A SDK do that, this repo adds no
relay code. So the orchestrator can own the entire human-facing API even though
the pause happened in an agent the human has never heard of.

```
human ──▶ POST /hitl/approvals/{id} ──▶ orchestrator ──A2A──▶ math (paused here)
                                                                    │
        final answer ◀──────────────────────────────────────────────┘
```

## Try it yourself

One process on your laptop — no Kubernetes, no database. You need a Google
Cloud project with Vertex AI enabled and `gcloud auth application-default
login` already done, because the agent calls a real model.

(Without a database, pending pauses live in the process's memory and are lost
when you stop it. Fine for a look around;
[production](#before-relying-on-this-in-production) covers the durable setup.)

```bash
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT=<your-project>
export GOOGLE_CLOUD_LOCATION=global

AGENT_NAME=math APP_URL=http://127.0.0.1:8099 \
  uv run uvicorn app.fast_api_app:app --port 8099
```

In another shell:

```bash
# 1. Start a turn. It pauses instead of finishing.
curl -s -X POST localhost:8099/hitl/run -H 'content-type: application/json' \
  -d '{"text":"Compute 23 * 19 and publish the result."}'

# 2. See what is waiting (also: GET /hitl/approvals).
#    Copy the approval_id from either response.

# 3. Answer it.
curl -s -X POST localhost:8099/hitl/approvals/<approval_id> \
  -H 'content-type: application/json' \
  -d '{"approved":true,"text":"approved by ops"}'
```

Step 3 returns `"final_text": "The result 437.0 has been published."`

**Now reject one.** Repeat with `{"approved": false, "text": "not cleared for
publication"}` and the agent answers:

> *I am sorry, I cannot publish the result. However, the result of 12 \* 12 is 144.*

Note what is missing: your reason. ADK returns a fixed `'This tool call is
rejected.'` to the model, so the model knows it was refused but not why — see
[A](#a-gate-an-action--require_confirmation).

**For a pause that is a question rather than a gate**, restart the server with
`AGENT_NAME=planner` and ask it to plan something — say *"Plan a two-day
onboarding for a new engineer."* It pauses with `"kind": "input"` and no tool
name, because nothing is being gated; you reply with just `{"text": "add a step
4: pair with a buddy on day two"}` and no `approved` flag. The revised plan
comes back beginning `PLAN-APPLIED-BY-GRAPH` — a marker emitted by the pipeline
stage *after* the human step, which is how you know that stage really ran. See
[C](#c-a-graph-agent-with-a-human-step).

## Vocabulary

| Term | What it means here |
| --- | --- |
| **pause** | An invocation stopped mid-flight, waiting for a person. Underneath it is always a long-running function call (below) |
| **approval record** | The row describing one pause: id, which agent, which tool, the arguments, the status. Returned by `GET /hitl/approvals` |
| **kind** | Which sort of pause it is: `confirmation` (permission), `input` (a question), or `other` |
| **decision** | The human's answer — `{"approved": bool, "text": "…"}`. `approved` is meaningful only for `confirmation` |
| **resume** | Appending the human's answer to the session and re-running the invocation so it continues from the pause |
| **claim / lease** | The bookkeeping that stops two pods resuming the same pause at once. Only relevant with replicas — see [production](#before-relying-on-this-in-production) |

> **One naming trap, worth 30 seconds now.** Everything is called an
> "approval" — the store, the table, the routes — even when nothing is being
> approved. A `request_input` question is an approval record with
> `kind: "input"`, it shows up in `GET /hitl/approvals`, and once answered its
> status becomes `approved` even though the human merely replied "add a step 4".
> Read *approval record* as "captured pause". Check `kind` to know what you are
> actually looking at.

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

# In app/agents/research/agent.py -- request_input is just another entry in the
# agent's tool tuple, alongside its own tools.
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
graph — an ADK `Workflow` of nodes wired by edges, rather than a model deciding
what to do next. `app/agents/planner/` is the worked example: draft a plan, ask
the human what to change, apply the change.

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

A spec that sets `root_node` serves that graph instead of a model-driven agent —
`build_agent` (this repo's single agent factory, `app/agents/base.py`) handles
both shapes. The result is an ordinary cluster member: its own Deployment,
Service and agent card (the JSON descriptor peers fetch to discover it),
reachable over A2A like any other agent.

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
shortcut, R7 the working shape. (`R<n>` are numbered findings in that file.)

This is why the planner's last node emits the literal `PLAN-APPLIED-BY-GRAPH`:
it is a marker only the post-pause node can produce, so a passing test cannot be
satisfied by a plausible-sounding answer.

## The API

| Route | Purpose |
| --- | --- |
| `POST /hitl/run` | run a turn; reports `completed` or `paused` with what is pending |
| `GET /hitl/approvals` | what is waiting for a human (`?status=` to filter) |
| `POST /hitl/approvals/{id}` | answer it: `{"approved": bool, "text": "…", "decided_by": "…"}` |
| `GET /hitl/session/{id}` | dump a session's events (diagnostics) |

`POST /hitl/run` takes `{"text": …, "session_id": …?, "user_id": …?}` and omitting
`session_id` starts a new one. It returns:

```json
{
  "status": "paused",
  "session_id": "hitl-d5c9b5b7",
  "final_text": null,
  "pending": [ { "approval_id": "17a0fa52", "kind": "confirmation", "...": "..." } ],
  "trace": ["math | calls=['calculate']", "..."]
}
```

`status` is `paused` when that turn created a new pending record and `completed`
otherwise; `final_text` is the answer, `null` while paused. `trace` is a
per-event summary, and it is the honest way to see what happened — read it
rather than trusting the prose.

An approval record, exactly as `GET /hitl/approvals` returns it:

```json
{
  "approval_id": "17a0fa52",
  "kind": "confirmation",
  "tool_name": "publish_result",
  "message": "Please approve or reject the tool call publish_result() ...",
  "args": {
    "originalFunctionCall": {
      "id": "adk-abca1287-...",
      "name": "publish_result",
      "args": { "value": "437.0" }
    },
    "toolConfirmation": { "hint": "...", "confirmed": false }
  },
  "agent": "math",
  "session_id": "hitl-d5c9b5b7",
  "status": "pending",
  "decision": null,
  "created_at": "2026-08-05T14:28:06+00:00",
  "decided_at": null,
  "resumed_at": null
}
```

`args.originalFunctionCall.args` is what to show a human: the exact call that is
being gated. For `kind: "input"` there is no tool, so `tool_name` is `""`,
`message` is the question, and `args` carries the `response_schema`.

Answering returns `{"status": "resumed", "approval": {…}, "final_text": …,
"trace": […]}` with the record's `status`, `decided_at` and `resumed_at` filled
in.

### Against the cluster

Same routes, reached through the orchestrator — which is the interesting case,
because the pause itself happens one A2A hop away in `math`:

```bash
kubectl -n agents port-forward svc/orchestrator 8080:80

curl -X POST localhost:8080/hitl/run -H 'content-type: application/json' \
  -d '{"text":"Ask the math specialist to compute 23 * 19 and publish the result."}'
curl localhost:8080/hitl/approvals
curl -X POST localhost:8080/hitl/approvals/<id> -H 'content-type: application/json' \
  -d '{"approved":true,"text":"approved by ops"}'
```

The approval record's `agent` field reads `math`, not `orchestrator` — the pause
is captured on the caller while it was raised in the peer
([`plans/hitl/results.md`](plans/hitl/results.md) R1).

### Lifecycle

```
pending ──claim──▶ deciding ──continuation completes──▶ approved | rejected
   ▲                   │
   └──lease expired────┘        (expired is reserved, not yet used)
```

- **`pending`** — nobody is working on it; answerable.
- **`deciding`** — a decision was accepted and the resume is running. The
  decision is written *by the claim*, which is what makes an interrupted resume
  recoverable: a restart knows what to re-drive it with.
- **`approved` / `rejected`** — the continuation finished. Note `approved` is
  the terminal state for an answered `input` pause too.

The claim is a single conditional `UPDATE`, so two deciders cannot both win.

## How it works underneath

All three mechanisms are the same thing: **a long-running function call**.

1. The agent emits a function call marked long-running (`adk_request_confirmation`,
   `adk_request_input`) and the invocation **stops** with no final answer.
2. `HitlPlugin` (`app/cluster/hitl.py`) sees the event on the Runner and records a
   pending approval. It is a plugin, so it captures pauses from every surface —
   the `/hitl` routes, the ADK web UI, and inbound A2A calls alike.
3. A human answers via `POST /hitl/approvals/{id}`.
4. `hitl.resume` appends the matching `FunctionResponse` to the session and
   re-runs the invocation, which continues where it stopped.

Because the pause lives in the session rather than in a process's memory, step 4
does not have to happen on the pod — or in the process — that saw step 1.

## Before relying on this in production

- **Approvals are durable when a database is configured** (`DB_BACKEND=alloydb`
  or `url` — see
  [environment-variables.md](environment-variables.md#database)): migration
  `0004` creates a `hitl_approvals` table in the agent's own schema and
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
  handles this, so you only meet it if you bypass the `/hitl` routes.
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
