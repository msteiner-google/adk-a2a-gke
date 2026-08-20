# Design decisions

Why this cluster is built the way it is, and the measurements behind each
choice. [GKE.md](../GKE.md) describes *what* the architecture is; this file
records *why*, including the alternatives that were tried and rejected.

Read it before changing anything in `app/cluster/` or `app/agents/gating.py`.
Several decisions here look like incidental wiring and are not — reversing them
reintroduces failures that took real effort to find. The numbered decisions
**D1**–**D6** are cited from the code and from AGENTS.md's invariants.

Everything quoted below was executed against ADK 2.6.1 / a2a-sdk 0.3.26 on
Vertex AI. Where a claim is inference rather than measurement, it says so.

The repo has since moved to **a2a-sdk 1.x (A2A protocol v1.0)** — see
[`a2a-v1-migration.md`](a2a-v1-migration.md). Every decision below still holds
and none was re-measured against 1.x: the upgrade changed the wire encoding and
the server wiring, not the delegation topology these measurements are about. The
one number to re-read with that in mind is D1's ten message parts. The mechanism
that produced it — `RemoteA2aAgent` rebuilding an outbound message from the
caller's session events — is unchanged in 1.x, so the leak it describes is too.

---

## The rule: share nothing at *runtime*, share code freely

The agents run in separate pods and must stay independently deployable by teams
that may not be using ADK at all. So no shared runtime state — no shared session
tables, no shared conversation, no shared artifact handles, no suspended
execution spanning a network hop.

That is not an argument against shared *code*:

| Layer | Shared? | Why |
| --- | --- | --- |
| `app/shared/**` | **Yes, deliberately** | A library, at the bottom of the dependency graph, kept portable to Python 3.11 so another squad can vendor it. Its `traceparent` wiring is the framework-agnostic piece of the blueprint. |
| `app/agents/statuses.py` | **Yes, deliberately** | The status vocabulary a specialist reports and a caller keys on. Plain strings, so an agent on another framework produces them without importing anything. What remains of the deleted per-peer payload contracts (D1). |
| AlloyDB instance | Yes, for cost | Isolation is already at the schema + IAM level (`app/cluster/db.py`, `infra/terraform/alloydb.tf`). One instance is a prototype decision; a squad wanting its own runs its own. |
| Session state, conversation, artifacts | **No** | D1–D4 below. |
| Execution state across a hop | **No** | D5. |

---

## D1 — A peer's wiring follows from whether it can suspend

`build_agent` puts a peer in `sub_agents` if that peer owns a `@gated` tool, and
in `tools` otherwise. The split is derived by `suspending_agents()` from the
registry, not declared twice.

This reverses an earlier invariant ("peers are tools, never sub-agents"). The
measurement that motivated it still stands and is kept below; what changed is
that a second requirement arrived which the old rule made impossible.

### Why a gated peer must be a sub-agent

In-task authorization (A2A spec 7.6) suspends the *specialist's* Task in
`TASK_STATE_AUTH_REQUIRED`, and spec 7.6.2 expects that suspension to propagate
to whoever can reach a human.

`AgentTool` cannot propagate it. It runs the peer to exhaustion against a
throwaway in-memory session and keeps exactly three things off the resulting
events — `state_delta`, `error_message`, `content`
(`google/adk/tools/agent_tool.py`). It never reads `long_running_tool_ids`. A
suspended peer therefore returns the empty string, and the caller carries on as
though the peer simply had nothing to say. There is no hook to change that from
outside ADK.

### Why every other peer must be a tool

`transfer_to_agent` is a one-way handoff: the caller's invocation **ends**.
Measured here, with `currency` wired as a sub-agent of `math`:

```
[3] transfer_to_agent {"agent_name": "currency"}
[5] convert_currency  {"amount": 500.0, "from_currency": "USD", ...}
[6] convert_currency  -> 458.72 EUR
[7] "500 USD converts to 458.72 EUR …"      <- and that is the whole answer
```

Math never resumed. The conversion succeeded and the sum was abandoned
half-done — no addition, no `publish_result`, no case. A caller that needs a
peer's answer cannot hand control away to get it.

So neither wiring does both jobs, and a uniform rule breaks one of them.

### What the sub-agent half costs

The original measurement, unchanged. Identical two-turn conversation, only the
wiring differing:

| | `sub_agents` | typed tool |
| --- | --- | --- |
| message parts sent to the peer | **8–10** | **1** |
| user's phone number included | **yes** | no |
| a different specialist's answer included | **yes** | no |

```
sub_agents outbound:
  "Hi, I'm Marc. My mobile is +353 87 555 0101 …"
  "For context:"
  "[orchestrator] said: … Thanks for sharing your number!"
  "Thanks. Now work out 17 * 23…"
  …
```

Personal data reaching an agent with no need for it, nine of ten parts diluting
attention, and the transcript re-sent on every turn. That cost is now **accepted
deliberately** for gated peers only, on the customer's explicit decision, and it
is the price of being able to ask a human at all. It does not apply to the tool
half, which still sends one composed request.

The typed payload contracts that used to make that request precise
(`app/agents/contracts.py`, `PeerTool`) are **deleted**: `transfer_to_agent`
carries no arguments, so there was nothing left for a per-peer request model to
describe. What survives is the status vocabulary, in `app/agents/statuses.py`.

One consequence worth stating: a rule that used to be enforced by a field
description is now enforced by instruction text. `MathRequest.expression` said
"pass the user's own wording, do not resolve it"; that now lives in the `math`
instruction, and `test_currency.py` asserts on the prose. Verified live — an
ambiguous `"dollars"` still reaches `currency` verbatim and still comes back as
a question.

Guarded by `tests/unit/test_agents.py` and `tests/unit/test_cluster_resolver.py`.

## D2 — Continuity is declared, not implicit

A tool call gives the peer a fresh session, which is the right default: a
specialist called functionally should not accumulate hidden state.

Where genuine continuity is needed — entity disambiguation, a follow-up on the
same case — the caller passes the same `case_id` **in the payload**, and the
specialist keys *its own* private store on it. A2A's `contextId` expresses the
same idea at the protocol level.

Continuity therefore appears in the contract rather than as a side effect of the
transport, and it works for a specialist that shares no infrastructure with the
caller.

## D3 — No shared session state

No agent writes state for another agent to read. Continuity is D2's `case_id`;
anything else a specialist needs is a field in its request.

**A tool that promises otherwise cannot deliver it.** Measured: a sentinel
placed in `session.state` does **not** appear in the outbound A2A message.
`AgentRunRequest` has a `state_delta` field, but nothing in `google/adk/a2a/`
populates it from the wire — an extension point, not a live path. So a
`remember`-style tool is not distributed context sharing however it is
documented; it is a within-pod convenience. All that crosses the hop is its
`FunctionResponse`, as text in the transcript — and a tool like that is worse
than useless, because it teaches callers to rely on context the transport never
carries.

**Isolation here is conventional, not enforced.** `AgentTool` copies the
parent's state dict into the child session (filtering only `_adk` keys). That
state does not reach the wire, so it is not a cross-pod leak — but the
share-nothing property depends on nobody writing shared state, not on a
framework boundary. Worth enforcing in review.

## D4 — Large inputs travel by reference

A caller passes `document_refs: ["gs://bucket/cases/123/dossier.pdf"]`; the
specialist reads it with `read_document` (`app/agents/documents.py`) using its
own credentials.

The payload-size argument is the obvious one. The better argument is **division
of labour**: if the caller had to pre-summarise a 200-page filing to make it
fit, the caller would be performing domain extraction it is not qualified for. A
planner knows *which* document matters; the entity specialist knows what to take
out of it.

This replaces reaching a document through ADK's artifact service, which keys
blobs by app name + user + session — sharing one means sharing a session.
`ARTIFACT_STORAGE_URI` remains for an agent's own storage and no longer needs to
match across agents.

## D5 — Human approval is in-task authorization, gated in code

A specialist that must not act alone **suspends its A2A Task** in
`TASK_STATE_AUTH_REQUIRED` (A2A spec 7.6). The request bubbles up to the agent
talking to the human, which records an `approval_cases` row. The decision is
delivered straight back to the suspended task, and ADK re-executes the tool.

```
math suspends ──▶ orchestrator AUTH_REQUIRED ──▶ case row ──▶ human
                                                               │
math re-executes ◀── grant delivered to math's task ◀──────────┘
```

This replaces an earlier design in which a specialist returned a proposal,
finished, and the approved action was an ordinary later call. That version
worked; it was replaced because a proposal-and-re-send handshake is invisible to
a client. A caller could not tell "waiting on a human" from "answered", and
nothing in the protocol said so. `TASK_STATE_AUTH_REQUIRED` is the standard way
to say it, and it is what a non-ADK client can act on.

### The two directions are asymmetric, and that is forced

The request goes **up** the chain of callers. The decision goes **straight down**
to the agent that owns the tool. The natural reading of spec 7.6.2 — resolve the
top task and the chain unwinds — does not work on ADK, and this was measured
twice:

A peer's confirmation arrives in the caller's session as an ordinary function
call, so the caller believes it is its own to resolve. On a grant it looks for
the tool among its own and finds nothing, because the tool is one hop away:

```python
# google/adk/flows/llm_flows/request_confirmation.py
if not tools_to_resume_with_confirmation:
    return
```

The grant is dropped in silence. Observed: after granting at the orchestrator
the specialist received **no traffic at all**, and the orchestrator's task sat in
`working` indefinitely. Suppressing the caller's duplicate confirmation was
tried and did not help — the duplicate is a function-call part, not the
`requested_tool_confirmations` action. So `app/cluster/grants.py` sends the
decision to the owner, whose task id and confirmation id are recorded on the
case when the request bubbles up.

### Why not a long-running tool that returns None

The first attempt used ADK's long-running-tool pause, with the human's answer
arriving as the function response. It does not work, and it fails *silently*:
ADK hands that response to the **model** as the tool's result and never calls
the tool again. Measured:

```
[3] function_call     publish_result {"approved_by": "", "value": "391000000.0"}
[5] function_response publish_result {"approved_by": "alice@bnpp.com"}
[6] text  "The result 391000000.0 has been published under the label q3-revenue,
           approved by alice@bnpp.com"
```

Nothing was published. ADK's *tool confirmation* flow does not have the gap —
`request_confirmation.py` explicitly re-executes the tool with the decision
attached — so the effect is performed by code, with the arguments a human
actually saw, rather than by a model choosing to call the tool again.

### The gate

Spec 7.6.4 is explicit that `TASK_STATE_AUTH_REQUIRED` "by itself" authorises
nothing, and that an implementation must define "how the authorized operation is
identified and how that authorization is checked before the operation is
performed".

Here that is the branch on `Approval.granted` in the tool
(`app/agents/gating.py`). Suspending is how the human is *asked*; the branch is
what keeps the effect unreachable until one answers. Removing it and trusting
the task state would mean anything able to resume the task could also perform
the effect.

### Why the proposal is generated, not restated

What a reviewer reads is ADK's `adk_request_confirmation` payload, built from
the call that is actually suspended. It cannot describe something other than
what will run. The previous design asked a model to restate its proposal as JSON
and parsed it back out of prose, which is how a proposal and its execution could
quietly disagree.

A tool that normalises its inputs must declare the normalised values, via
`require_approval(..., proposal=...)`. Measured twice: a proposal recorded from
the model's raw arguments said `391000000.0` while the tool published
`391000000`, and again `88000.0` against `88000`. Both were correctly reported
as `approved_not_confirmed`. Canonicalise at the source — do not loosen the
comparison, which is what catches a specialist doing something *else*.

### What is kept from the previous design

The decision is still written to the row **before** delivery is attempted, so a
pod that dies mid-delivery leaves a re-drivable `approved` case. Execution is
still confirmed by matching the result's values against the approved proposal,
not by trusting prose. Both survived the redesign unchanged.

## D6 — What stays coupled, deliberately

- **`app/shared/**`** — a versioned, vendorable library. Adopting the blueprint
  means taking a library dependency, not a runtime one.
- **One AlloyDB instance, per-agent schema and IAM role** — the isolation
  boundary is the schema and the role, both already per agent.
- **One image selected by `AGENT_NAME`** — convenient for one squad in a
  monorepo. The cross-squad recommendation is an independent image and pipeline,
  but that is a packaging decision, not a state-coupling one.

---

## Rejected alternatives

### `output_schema` instead of the reporting callback

ADK 2.6.1 supports `output_schema` alongside tools, which looks like a
first-class replacement for `app/agents/reporting.py`. Measured on the gated
request, 5 runs each, identical input:

| | gated tool actually ran | reply *claimed* an approval |
| --- | --- | --- |
| with `output_schema` | **0/5** | **4/5** |
| without | 2/5 | 0/5 |

With the schema the model satisfied the contract by **fabricating** it —
inventing a proposal shape this codebase never emits, without calling the tool
or doing the arithmetic. Schema-valid, confidently wrong, four times in five.

**A schema constrains the shape of a reply, never whether the work happened.**
Offering a model a well-formed way to answer without doing the work makes it
likelier to take it. The callback is the opposite: it reports what the tools
actually returned.

The protocol offers no help either — `AgentSkill` carries only
`inputModes`/`outputModes` MIME hints and no schema, `DataPart` exists but ADK's
`AgentTool` drops non-text parts, and the A2A spec's own structured-data example
(§6.8) returns JSON as a string inside a `TextPart`.

### Uniform wiring, either way

Both uniform rules were tried and each breaks something. All peers as tools:
a suspended specialist is swallowed and no authorization request ever reaches a
human. All peers as sub-agents: a caller that needs a peer's answer never gets
it, and `math -> currency -> back to math` abandons the calculation half-done.
See D1 for both measurements. The derived split is not a compromise between
them — it is the only wiring under which both work.

---

## Why `app/agents/reporting.py` exists

The one piece of the approval flow that is not self-evident.

A specialist's reply crosses A2A as **text** — `AgentTool` reduces a peer's
response to its merged text parts, so a dict the specialist's tool returned is
not a dict when it arrives. The first end-to-end run failed silently because of
it: the specialist reported its proposal as accurate prose, the caller found no
proposal, opened no case, and the orchestrator told the user *"I have published
the result."* Nothing had been published.

`attach_structured_results` is an **after-model** callback that appends any
audit-relevant tool result to the model's own final reply as verbatim JSON,
before ADK turns that reply into an event. `AgentTool` returns the *last*
content, so the structured payload crosses deterministically whatever prose the
model produced — and the prose crosses with it, in the same event.

It started as an after-agent callback that emitted the JSON as an additional
event, with the model's wording repeated above it so the readable version
crossed too. That works, and it renders the same answer **twice** in the ADK web
UI, because ADK *appends* an after-agent callback's content rather than
replacing the agent's own event. `restate_structured_results` is still attached,
now as the fallback for a turn that ends without the model speaking, and it
emits only the results the reply does not already carry.

A second, related trap: ADK wraps a tool result as `{"result": <text>}`, and
serialising that wrapper escapes the peer's JSON inside a string where no
scanner can reach it. `cases._unpack` walks the wrapper and returns the strings
inside it.

Both failures were HTTP 200 with plausible answers. They were caught only
because the endpoint reports `status` from the case store rather than from the
model's prose — which is the discipline to keep when extending any of this:
**assert on a marker the code emits.**

---

## Measured reliability

Model tier matters for whether a gate is *reached*, though not for whether it
holds. Running the real instruction from `app/agents/math/agent.py`, 5 runs per
cell:

| tier | asked to publish → must propose | not asked → must not |
| --- | --- | --- |
| `fast` (flash-lite) | **3/5** ❌ | 0/5 ✅ |
| `balanced` (flash) | **5/5** ✅ | 0/5 ✅ |

Safety held in every run: the effect is unreachable without an approver, so a
skipped tool call publishes nothing. The failure is a false *negative* — a user
asks to publish and no proposal appears.

Every agent therefore declares `tier="balanced"`. `fast` remains in `TIERS` for
an agent that calls no tools; nothing here is that simple.

Re-measured on `gemini-3.7-flash` after the change: 5/5 propose when asked, 0/3
spurious when not.

---

## Known gaps

- **A suspended task is pinned to one pod.** `a2a-sdk`'s `ActiveTaskRegistry` is
  a plain in-process dict; there is no distributed queue manager in the SDK.
  Every Deployment runs `replicas: 1` today, so this is latent — but with two
  replicas a grant routed to the wrong pod builds a *second* `ActiveTask` from
  the DB row, which is double execution rather than a clean error. Fix before
  scaling out: sticky routing by task id, or a shared queue manager.
- **The caller's task is left suspended after a grant.** The decision is
  delivered to the owner and the case is closed from its reply, but the
  orchestrator's own task stays in `auth-required` — nothing resolves it, since
  ADK cannot resolve a peer's confirmation locally (D5). Harmless with a
  durable task store and a retention sweep; untidy.
- **`decided_by` is not authenticated.** `POST /cases/{id}` takes the approver's
  name on trust. Put a real identity in front of that route before it means
  anything.
- **"Approved" means this case, not these exact bytes.** The caller compares
  content, which catches a specialist doing something different, but there is no
  signed token binding a decision to a payload. A compliance context needing
  after-the-fact proof should add one field and one check.
- **The instruction-only guards are weaker than the contracts they replaced.**
  "Pass the user's own wording, do not resolve it" was a field description a
  model saw as a parameter; it is now prose. `test_currency.py` asserts the
  prose exists and a live run confirmed the behaviour, but neither is as strong
  as a schema.
- **Eval impact of the sub-agent half is unmeasured.** A gated peer now receives
  the caller's recent conversation rather than a composed request. Whether that
  helps or hurts answer quality is a question unit tests cannot answer.
- **Not exercised:** concurrent decisions on one case from two replicas, and the
  flow against live AlloyDB rather than the in-memory store.
