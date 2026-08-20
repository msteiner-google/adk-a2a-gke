# Design decisions

Why this cluster is built the way it is, and the measurements behind each
choice. [GKE.md](../GKE.md) describes *what* the architecture is; this file
records *why*, including the alternatives that were tried and rejected.

Read it before changing anything in `app/cluster/` or `app/agents/contracts.py`.
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
| `app/agents/contracts.py` | **Yes, deliberately — and optional** | The interface definition — the same kind of artefact as a `.proto`. Opt-in per peer: without a model here, delegation falls back to a free-text task (see D1). A non-ADK squad reads the equivalent JSON Schema from the agent card instead. |
| AlloyDB instance | Yes, for cost | Isolation is already at the schema + IAM level (`app/cluster/db.py`, `infra/terraform/alloydb.tf`). One instance is a prototype decision; a squad wanting its own runs its own. |
| Session state, conversation, artifacts | **No** | D1–D4 below. |
| Execution state across a hop | **No** | D5. |

---

## D1 — Peers are tools, never sub-agents

`build_agent` puts resolved peers in `tools` and leaves `sub_agents` empty.

**Why it matters.** A peer in `sub_agents` is reached with `transfer_to_agent`,
and `RemoteA2aAgent` then rebuilds the outbound message from the caller's
*session events*. Measured on this repo's own agents, with an identical two-turn
conversation and only the wiring differing:

| | `sub_agents` | `PeerTool` |
| --- | --- | --- |
| message parts sent to the peer | **8–10** | **1** |
| user's phone number included | **yes** | no |
| a different specialist's answer included | **yes** | no |
| caller can see the peer's result | **no** | `function_response -> dict` |

```
sub_agents outbound:                    PeerTool outbound:
  "Hi, I'm Marc. My mobile is             {"case_id": "…",
   +353 87 555 0101 …"                     "expression": "17 * 23",
  "For context:"                           "publish_as": "q3-revenue"}
  "[orchestrator] said: … Thanks for
    sharing your number!"
  "Thanks. Now work out 17 * 23…"
  "For context:"
  "[orchestrator] called tool
    `transfer_to_agent` …"
  …
```

Three distinct problems in one output: personal data reaching an agent with no
need for it, nine of ten parts diluting attention (ADK even prefixes them
`For context:`), and the whole transcript re-sent to every specialist on every
turn.

**Also worse where it looks better.** Under `sub_agents` control *transfers* —
the specialist's reply goes to the user, and the caller's inbound structure is
only `function_response(transfer_to_agent) -> {'result': None}`. The caller
never sees the specialist's result, which makes recording an approval (D5)
impossible.

This was re-tested after the HITL redesign removed the other objection to
`sub_agents`, in case that changed the calculus. It does not: the leak is a
property of `transfer_to_agent`, unrelated to HITL.

**The typed contract is a separate and optional decision.** Peers-as-tools is
the invariant; declaring a payload model is not. A peer with no entry in
`PAYLOADS` falls back to `resolver.UnknownPeerRequest` — a `case_id` plus one
free-text `task` — and that already delivers the table above, because the win
comes from the caller *composing a payload* rather than from the payload being
typed. Declaring a model in `app/agents/contracts.py` buys three further things:
the calling model sees documented parameters instead of guessing what to write,
a malformed request is rejected in-process rather than several hops away, and
the schema is published in the agent card for a non-ADK caller to read. This
repo declares one for every agent it owns; a peer another squad owns is
perfectly well delegated to in free text.

Guarded by `tests/unit/test_peer_tool.py`.

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

## D5 — Human approval is business state, not a suspended invocation

A specialist that must not act alone returns a proposal and **finishes**. The
caller records an `approval_cases` row, answers the user, and stops. The
approved action is an ordinary later call.

```
pending ──decide (one conditional UPDATE)──▶ approved ──execute──▶ executed
     │                                                                ▲
     └──────────────────────────▶ rejected     re-drivable ───────────┘
```

### Why not suspend the invocation instead

The obvious alternative — ADK supports it — is to freeze the invocation across
the A2A hop and replay it once the human answers. It was built end to end here
and measured before this state machine replaced it, so the objection is not
theoretical. It works, and it costs a reclaimable lease, a heartbeat, a
background sweeper and a workaround pinned to one ADK version. The decisive
failure: **a decision could take effect while its answer never reached the
user**, because the peer's A2A task had gone terminal and the reply could not be
replayed. That is not a bug with a fix — it is structural to replaying a
distributed execution stack. It is also unimplementable by a squad not using
ADK.

Waiting as a row has none of it. An approval taking a fortnight costs what one
taking a second costs, any replica can decide any case, and there is no recovery
machinery because nothing is in flight.

### The gate

`publish_result` returns a proposal when `approved_by` is empty and performs the
write only when it is set. The effect is **unreachable** without an approver —
a property of the code, not an instruction the model is asked to respect. A
second code path that publishes without that check removes the guarantee
entirely, however well the prompt is worded.

### Why execution re-sends the request, and why the result is canonicalised

Approval does not compose a second payload describing what to do. It re-sends
**the same request** with `approved_by` filled in, and the specialist recomputes
from `expression`. Drift is removed rather than detected: a caller's LLM asked to
restate a proposal for execution will render it as markdown prose and stall the
flow, and no digest or signature on the wire prevents that — it only catches it
afterwards. The check that remains is a content comparison on the caller
(`cases.find_execution`), which refuses a result that does not match what was
approved.

"The specialist recomputes, so nothing can drift" is true of the number and
false of its *spelling*: a live run proposed `391000000` and published
`391000000.0`, and the comparison correctly refused to confirm a correct
execution. The fix is to canonicalise the value where it is produced
(`math.tools.canonical_value`) rather than to loosen the comparison. The general
lesson: when two independently produced values must be compared later,
canonicalise at the source, not at the comparison.

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

### `sub_agents`, re-tested

See D1. Worse on both axes, and the HITL relaxation did not change it.

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

- **The web UI does not record approval cases.** Proposal detection lives in the
  `POST /cases/run` route, so a gated action driven from the ADK dev UI
  correctly refuses to publish but records nothing. Moving detection into a
  plugin would capture from any surface, and is a small change.
- **The proposal handshake depends on a model following an instruction.** The
  callback above makes it deterministic *given* the specialist called its tool,
  but nothing forces the call. Mitigated by tier choice and by never assuming an
  effect happened.
- **"Approved" means this case, not these exact bytes.** The caller compares
  content, which catches a specialist returning something different, but there is
  no signed token binding a decision to a payload. A compliance context needing
  after-the-fact proof should add one field and one check.
- **Eval impact of D1 is unmeasured.** A typed payload carries less context than
  a transcript; whether the orchestrator extracts the right constraints is a
  quality question unit tests cannot answer.
- **Not exercised:** multi-hop delegation (A → B → C), concurrent decisions on
  one case from two replicas, and the approval flow against live AlloyDB rather
  than the in-memory store.
