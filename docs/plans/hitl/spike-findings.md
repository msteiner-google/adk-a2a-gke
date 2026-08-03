# HITL over A2A — spike findings (ADK 2.6.1)

Empirical results from a throwaway copy of this repo (`/tmp/hitl-verify`, since
deleted-or-disposable) run on **ADK 2.6.1 / a2a-sdk 0.3.26**. Two processes from
this repo's code: orchestrator **A** (`:8090`, `A2A_PEERS=math=http://127.0.0.1:8091`)
and math **B** (`:8091`). These are the facts [`README.md`](README.md) builds on;
keep them together, because most of them contradict the older ADK 2.5.0 notes.

## Verified

| # | Finding | Evidence |
| --- | --- | --- |
| F1 | Single-agent pause + resume works | `/hitl/start` → `paused`; `/hitl/resume` → `"The result of 23 * 19 is 437."` |
| F2 | A pause inside a peer propagates **up** over A2A for free | A's stream shows `math \| lrt=['adk-…'] \| calls=['request_human_approval']`, A stops with `final_text: null` |
| F3 | A's session records what a resume needs | the paused event carries `custom_metadata = {"a2a:task_id": …, "a2a:context_id": …}`; B's A2A task is in state `input-required` |
| F4 | Resume flows **down** to the peer — no custom relay needed | `RemoteA2aAgent._create_a2a_request_for_user_function_response` (`remote_a2a_agent.py:389`) re-sends on the stored task id; B logged a real inbound `POST /a2a/app 200` |
| F5 | The human's decision content is honoured downstream | approving → `"The result of 44 * 13 is 572."`; rejecting → `"I have the answer, but I cannot share it with you. It is confidential."` |
| F6 | **The first resume call after an A2A pause is a silent no-op** | `status: resumed`, empty trace, B sees no request. Instrumented: `_find_agent_to_run -> 'orchestrator'` |
| F7 | Two workarounds for F6, both reproduced | (a) call resume twice — #1 empty, #2 succeeds; (b) append the `FunctionResponse` to the session first, then `run_async(invocation_id=…, new_message=None)` — succeeds on the first call |
| F8 | A `LongRunningFunctionTool` does **not** structurally stop the turn | its `{"status":"pending"}` return is fed back to the model, which keeps generating; only a strict instruction stopped it leaking the answer |

### Root cause of F6

For an `LlmAgent` root, ADK 2.6.1 runs through the node runtime. `runners.py:1089`
picks the agent that continues the invocation **before** `_run_node_async` appends
the incoming message at `runners.py:554`. So at routing time the last session event
is still the peer's function *call*, not a user function *response*:
`find_matching_function_call` misses, routing falls back to the root agent, and the
orchestrator — already `end_of_agent` after `transfer_to_agent` — yields nothing.
The `FunctionResponse` does get appended as a side effect, which is why an
identical second call then routes correctly.

`_find_agent_to_run` is explicitly meant to handle this (`runners.py:1768-1786`,
comment: *"a remote a2a agent may surface a credential request as a special
long-running function tool call"*). It just runs one step too early.

## Not tested in the spike

- **`FunctionTool(require_confirmation=…)`** — discovered afterwards while reading
  ADK 2.6.1 (`tools/function_tool.py:82`, `tools/base_tool.py:171`,
  `flows/llm_flows/functions.py:374`). It is the supported, *structural* gate and
  the plan prefers it over F8's leaky pattern, but nothing below the "Verified"
  line above has been run — Phase 1 exists to prove it.
- Behaviour with >1 replica, with AlloyDB sessions, or with a restart mid-pause.
- Multi-hop (A → B → C) pauses.

## Environment caveats

- Everything used here is EXPERIMENTAL in ADK: `RemoteA2aAgent`, `A2aAgentExecutor`,
  `ResumabilityConfig`. The 2.5.0 → 2.6.1 change (legacy path → node runtime) is
  exactly what moved this behaviour once already.
- The spike ran two long-lived processes with in-memory session state. Nothing here
  demonstrates durability.
- With no database configured, the ADK web session routes resolve `shared://session`
  to a *different* in-memory service than the Runner (see the conditional at
  `app/fast_api_app.py:77`). During the spike `/apps/app/users/…/sessions/…`
  returned `Session not found` for a session the Runner had just created. Any HITL
  route must go through `app.state.runner.session_service`.
