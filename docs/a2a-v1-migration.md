# Migrating this repo from A2A v0.3 to A2A v1.0

What changed when `a2a-sdk` went from `0.3.26` to `1.1.2`, what this repo had to
change, and — the part worth reading before you touch anything — the two ways
the upgrade fails *quietly*.

The upstream references are the protocol's
[What's New in v1.0](https://a2a-protocol.org/latest/whats-new-v1/) and the
Python SDK's
[v0.3 → v1.0 migration guide](https://github.com/a2aproject/a2a-python/blob/main/docs/migrations/v1_0/README.md).
This document is only about the delta *here*.

## The short version

Five files changed, plus docs:

| File | Change |
| --- | --- |
| `pyproject.toml` | `a2a-sdk` → `>=1.1.2,<2`; `google-adk` floor → `>=2.5.0` |
| `app/app_utils/a2a.py` | Route factories replace the removed `A2AFastAPIApplication` |
| `app/migrations/versions/20260820_0006_a2a_v1_task_columns.py` | New: three columns a2a 1.x added to `tasks` |
| `tests/unit/test_migrations.py` | Drift guard now replays `ALTER TABLE`, not just `CREATE TABLE` |
| `tests/integration/test_server_e2e.py` | v1.0 wire format, plus a v0.3 compat case |

Nothing in `app/agents/**` changed. No agent instruction, contract, tool or
delegation edge was touched: the protocol upgrade did not reach the agent layer,
which is the layering in `AGENTS.md` doing its job.

## Why ADK needed a floor bump

`google-adk` declares `a2a-sdk<2,>=0.3.4`, so pip and uv will happily install
ADK 2.4.0 against a2a-sdk 1.1.2. **That resolves and then fails at request
time.** ADK isolates every 0.3-vs-1.x difference in
`google/adk/a2a/_compat.py`, whose `IS_A2A_V1` flag selects the branch at import
time — and that module first ships in **ADK 2.5.0**. On 2.4.0 and older, ADK
builds 0.3-shaped `Part`s and agent cards against a 1.x SDK, so the pods start,
the health probes pass, and the first delegation fails.

`>=2.5.0` in `pyproject.toml` is therefore a correctness floor, not a
preference. Everything else — `RemoteA2aAgent`, `A2aAgentExecutor`,
`AgentCardBuilder`, the part and event converters — needed **no changes here**,
because `_compat` absorbs the difference.

## The serving layer

`a2a.server.apps` no longer exists. The wrapper classes
(`A2AFastAPIApplication`, `A2AStarletteApplication`) were replaced by route
factories that return plain Starlette routes. Three details are load-bearing in
`app/app_utils/a2a.py`:

1. **`DefaultRequestHandler` now requires `agent_card`.** In 0.3 the card went
   to the application wrapper and the handler never saw it. In 1.0 the handler
   answers `GetExtendedAgentCard` itself, so it cannot be constructed without
   one.
2. **Use `add_a2a_routes_to_fastapi`, not `app.routes.extend(...)`.** Both mount
   working endpoints, but only the helper registers them as `APIRoute`
   instances, which is what keeps the A2A endpoints in `/docs` and
   `/openapi.json`. Extending `app.routes` directly works and silently drops
   them out of the OpenAPI schema.
3. **`EXTENDED_AGENT_CARD_PATH` is gone.** The authenticated extended card moved
   from its own well-known URL to the `GetExtendedAgentCard` RPC method, so
   there is no third URL to mount.

The mount paths are **unchanged**: `/a2a/app` and
`/a2a/app/.well-known/agent-card.json`. Invariant 9 still holds,
`app/cluster/resolver.py` needed no change, and neither did the `Makefile` card
URLs or any deployment manifest.

## Failure mode 1: the version header, not the method name

This is the one that costs an afternoon.

With `enable_v0_3_compat=True` (what this repo sets, and ADK's own default), a
request that arrives **without an `A2A-Version` header is treated as 0.3**. Send
a v1.0 method name on an unversioned request and the server answers:

```
HTTP/1.1 200 OK
{"error":{"code":-32009,
  "message":"A2A version '0.3' is not supported by this handler. Expected version '1.0'.",
  "data":[{"@type":"type.googleapis.com/google.rpc.ErrorInfo",
           "reason":"VERSION_NOT_SUPPORTED","domain":"a2a-protocol.org"}]}}
```

Note the **HTTP 200**. It is a JSON-RPC-level error, so a client that checks the
status code and then iterates SSE frames sees a successful request with an empty
stream — which reads as "the agent had nothing to say", not as a protocol
mismatch. A hand-rolled client must send the header:

```bash
curl -s -X POST http://127.0.0.1:8091/a2a/app \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendStreamingMessage",
       "params":{"message":{"messageId":"m1","role":"ROLE_USER",
                            "parts":[{"text":"What is 2+2?"}]}}}'
```

ADK's client sends it, so agent-to-agent delegation was never affected. The A2A
Inspector, a `curl` reproduction, and `tests/integration/test_server_e2e.py`
all were.

**Accept-but-do-not-advertise.** The cards this repo serves list only a `1.0`
interface, while the routes still answer 0.3. That is deliberate: peers get told
to speak the current protocol, and anything still on 0.3 keeps working instead
of breaking at the same instant. Advertising 0.3 as well means adding a second
`AgentInterface` with `protocol_version='0.3'` to the card.

## Failure mode 2: protobuf enums are integers

Types moved from pydantic to protobuf. Two consequences bite in test and glue
code:

- `task.status.state == "TASK_STATE_COMPLETED"` is **always False**. `state` is
  an `int`. Compare against `TaskState.TASK_STATE_COMPLETED`. The failure looks
  like "the stream never completed".
- `model_dump()` / `model_validate()` are gone. Use `MessageToDict` and
  `ParseDict` from `google.protobuf.json_format`.

The wire-format changes underneath (unified `Part` with no `kind`;
`statusUpdate` / `artifactUpdate` member discrimination in place of `kind`; no
`final` boolean; `ROLE_USER` / `TASK_STATE_*` spellings) only surface where this
repo builds A2A objects by hand, which is one integration test. Everywhere else
ADK's converters own it.

## The database

a2a 1.x adds three nullable columns to its `tasks` table, and
`DatabaseTaskStore` names all three in its statements:

| Column | Why |
| --- | --- |
| `owner` | v1.0 scopes task visibility to the authenticated caller |
| `last_updated` | the library's own mtime |
| `protocol_version` | which protocol version wrote the row |

plus an index on `(owner, last_updated)`. Migration `0006` adds them. It is a
pure additive `ALTER TABLE`, so it needs no table rewrite and is safe against a
live `tasks` table.

**Without it, nothing fails at startup.** The table still exists, the pod's
health probe never touches it, and the first *delegation* fails with
`UndefinedColumn`. Run the migration Job before the agents settle, as always.

`last_updated` does not replace this repo's `updated_at`. `updated_at` is
server-maintained by the trigger from migration `0002` and fires on every write
regardless of who made it, which is what makes it trustworthy for retention;
`last_updated` is ORM-maintained and is NULL for every row 0.3.x wrote.
Retention still keys off `updated_at`.

### The drift guard had a blind spot

`tests/unit/test_migrations.py` compares the migrations against the libraries'
own metadata. It read only `CREATE TABLE`, so it could not see a column added by
a later revision — it correctly flagged the three missing columns, then would
have kept flagging them after `0006` fixed it. It now replays
`ALTER TABLE ... ADD/DROP COLUMN` over the created table and compares the
*effective* schema at head. Worth knowing, because it means the guard is now
correct for any future additive revision rather than only for the table's
first one.

## Verification actually performed

Not "it imports and the answer looked plausible" — per `AGENTS.md`, a plausible
answer is not proof the flow ran.

- 343 hermetic unit tests, `agents-cli lint`, and `uv run basedpyright` clean.
- 6 integration tests, including a v1.0 streaming turn asserted down to
  `TASK_STATE_COMPLETED` and a literal v0.3-shaped payload proving the compat
  flag works.
- All six agents run locally over real A2A. Verified live:
  - `orchestrator → math` HITL publish: `awaiting_approval` → `executed`, with
    the caller's phone number confirmed absent from `math.log` (invariant D1).
  - `orchestrator → math → currency` two-hop delegation returning a converted
    amount, with `currency` resolving a `protocolVersion: "1.0"` card.
  - `needs_input` relayed intact through both hops with its structured JSON.
  - `orchestrator → trades` gated BigQuery read: proposed, approved, executed,
    and content-confirmed against the approved SQL.
- Migrations `0001`–`0006` applied to a real PostgreSQL 16, then a `Task` was
  round-tripped through the SDK's own `DatabaseTaskStore` (`save` → `get` →
  `delete`) against that schema.

## If you are upgrading a sibling repo

In rough order of how much time each one saves:

1. Raise the ADK floor to `>=2.5.0`. The resolver will not do it for you.
2. Send `A2A-Version: 1.0` from any hand-rolled client, and check JSON-RPC
   `error` bodies on HTTP 200 responses.
3. Add the three `tasks` columns before deploying, not after.
4. Grep for enum comparisons against string literals.
5. Replace `a2a.server.apps` wrappers with the route factories; keep your
   existing mount paths by passing `card_url` and `rpc_url` explicitly.
