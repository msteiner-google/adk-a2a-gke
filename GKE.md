# Multi-agent system on GKE

This project is a **cloud-native, multi-agent system** designed to run on Google
Kubernetes Engine (GKE). An orchestrator agent breaks a request into sub-tasks
and delegates them to specialist worker agents, each running as its own
Kubernetes Service and reached over the **A2A** protocol.

```
                       ┌───────────────────┐
   user ───────────▶   │   orchestrator    │   (entry point, models.balanced)
                       │  Deployment/Svc   │
                       └─────────┬─────────┘
              A2A (RemoteA2aAgent, well-known agent card)
         ┌──────────────────┬────┴─────────────┬──────────────────┐
         ▼                  ▼                  ▼                  ▼
 ┌────────────────┐ ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
 │    research    │ │      math      │ │    planner     │ │     trades     │
 │ Deployment/Svc │ │ Deployment/Svc │ │ Deployment/Svc │ │ Deployment/Svc │
 │ (balanced)     │ │ (balanced)     │ │ (balanced)     │ │ (capable)      │
 └────────────────┘ └───────┬────────┘ └────────────────┘ └────────────────┘
                            │  math is not a leaf: it delegates
                            ▼  conversions on, under the same rules
                    ┌────────────────┐
                    │    currency    │
                    │ Deployment/Svc │
                    │ (balanced)     │
                    └────────────────┘

Every arrow above carries ONE typed payload, never the caller's transcript —
including the second-level one. Depth changes nothing about what a specialist
can see.
```

**This is the architecture document** — how the pieces fit and why they were
chosen. [README.md](README.md) is the shorter overview and the entry point for
everything else; start there if you have not.

| Section | What it covers |
| --- | --- |
| [Agents](#agents-all-equal-one-selected-per-pod) | The uniform agent model and how one image becomes any agent |
| [Service discovery](#service-discovery-the-resolver) | How an agent finds and calls its peers |
| [Session & memory](#session--memory-persistence) · [Artifacts](#artifact-storage-blobs) | Where conversation state and files live |
| [Durable storage](#durable-storage-on-alloydb) | AlloyDB, schema-per-agent isolation, IAM auth, migrations |
| [Human-in-the-loop](#human-in-the-loop) | Pausing an agent for a person, durably and across hops |
| [Observability](#observability-logs--distributed-tracing-across-a2a) | One trace across every pod, trace-correlated logs |
| [Run locally](#run-locally) · [Deploy](#deploy-to-gke) | Getting it running |

Configuration throughout is by environment variable;
[docs/environment-variables.md](docs/environment-variables.md) is the complete
reference for every one mentioned below.

## How it maps to the requirements

| Requirement | Where it lives |
| --- | --- |
| Multi-agent runtime architecture | One image, agent-selected at startup (`AGENT_NAME`) — `app/agent.py` |
| Uniform agent model | Every agent is an `AgentSpec` built by one `build_agent` — `app/agents/base.py` |
| Orchestration / planner layer | `app/agents/orchestrator/agent.py` (an agent whose spec declares peers; delegates through typed peer tools). Not limited to one level — `app/agents/math/agent.py` declares peers of its own |
| Agent-to-agent communication | `app/cluster/resolver.py` builds typed `PeerTool`s from peer agent cards |
| Context provisioning | Explicit payload per call, free text by default and typed where a contract is declared — `app/agents/contracts.py`; no transcript, no shared session state |
| Large inputs | Claim-check: object-store references in the payload, read by the specialist — `app/agents/documents.py` |
| Session & memory persistence | Pluggable, env-selectable backends — `app/cluster/session.py` + `SessionModule` |
| Artifact (blob) storage | Blob-store-agnostic via `cloudpathlib` — `app/cluster/artifacts.py` + `app/shared/artifacts.py` |
| Human-in-the-loop | Propose → durable case → execute — `app/cluster/cases.py`, plus the two gated actions in `app/agents/math/tools.py` (a write) and `app/agents/trades/tools.py` (a read) |
| Cloud-native reference architecture | `infra/terraform` (GKE Autopilot + WI + Artifact Registry) and `infra/kustomize` |
| ADK / DI best practices | `injector` modules (`ModelModule`, `ClusterModule`, `SessionModule`) resolved in `app/agent.py` |
| Observability across the MAS | Trace context propagates over A2A (httpx inject + `instrument_fastapi_app` extract) → one Cloud Trace per request; trace-correlated loguru logs — see "Observability" below |

## Agents: all equal, one selected per pod

Every agent is the **same kind of thing** — a declarative `AgentSpec` (name,
model tier, instruction, tools, and optional `peers`) built by the single
`build_agent` in `app/agents/base.py`. There is no special "orchestrator" class:
the orchestrator is simply the agent whose spec lists `peers`, so `build_agent`
appends those peers to the agent's own tools. A leaf agent lists no peers and
just gets its own tools. Any agent can declare peers and coordinate others —
and `math` does, which is what makes the graph two levels deep rather than a
star. Nothing in `build_agent` had to change for that.

Every pod runs the same image; `AGENT_NAME` selects which agent from the registry
(`app/agents/__init__.py`) this process becomes:

- `AGENT_NAME=orchestrator` (default) → the agent with `peers=("research",
  "math", "planner", "trades")`. It attaches them as peer tools and delegates.
- `AGENT_NAME=math` → also a coordinator: `peers=("currency",)`, so a request
  carrying `target_currency` is converted by the currency specialist before the
  arithmetic happens. The rate belongs to the agent that owns rates.
- `AGENT_NAME=research` / `planner` / `trades` / `currency` → a leaf agent (no
  peers), served over A2A so others can reach it.

The one asymmetry that remains — which agent is exposed to users — is a
**deployment** concern (which Service gets external ingress), not a code one.

**Peers are attached as tools, not sub-agents**, and that is the load-bearing
detail of the whole design. A peer in `sub_agents` is reached with
`transfer_to_agent`, which hands it the caller's session — measured at ten
message parts, including the user's phone number and a different specialist's
answer, where the task needed one. A peer in `tools` receives exactly the payload
the caller composed. See [Context provisioning](#context-provisioning) below and
[`docs/design-decisions.md`](docs/design-decisions.md) (D1).

Add an agent by:

1. Creating `app/agents/<name>/agent.py` exposing a `SPEC = AgentSpec(...)` (put
   agent-specific tools in `app/agents/<name>/tools.py`) and — optional in the
   mechanism, conventional here — declaring its request contract in
   `app/agents/contracts.py`.
2. Registering it in `app/agents/__init__.py`.
3. Adding it to the delegating agent's `AgentSpec.peers`.
4. For the cluster: five files under `infra/` — `var.agents`,
   `serviceaccounts.yaml`, `workers.yaml`, `networkpolicy.yaml`, and
   `migrate-job.yaml`. No new migration or database is needed; the agent gets
   the same tables in its own schema.

Use a single-word name valid as **both** a Python identifier and a Kubernetes DNS
label (no hyphens/underscores). The Service name **must equal** the agent name so
peers can resolve it by DNS.

> **[`docs/adding-an-agent.md`](docs/adding-an-agent.md)** has the full
> walkthrough: worked example, checklist, and a troubleshooting table.

## Service discovery (the resolver)

The injectable `AgentResolver` (`app/cluster/resolver.py`) turns each configured
peer into an ADK `RemoteA2aAgent` pointed at that peer's **well-known agent
card**. The base A2A serving mounts the JSON-RPC endpoint and the card under
`/a2a/app` (i.e. `/a2a/<app_name>`, and the app is `App(name="app")`), so the
card lives at
`http://<name>.<namespace>.svc.cluster.local/a2a/app/.well-known/agent-card.json`
— **not** at the service root. A peer's base URL is the service root; the
resolver appends `/a2a/app` (configurable via `A2A_RPC_PATH`) plus the well-known
path. ADK fetches and validates the card lazily on first use, so the orchestrator
picks up each specialist's real description and capabilities at call time.

Peers are configured by environment (see `app/cluster/config.py`):

- `A2A_PEERS` — comma-separated `name` or `name=url` entries (a `url` is a service
  **root**, e.g. `math=https://math.example.com`, not the `/a2a/app` path). When
  unset, the agent falls back to the default peers declared in its own
  `AgentSpec.peers`.
- `A2A_NAMESPACE`, `A2A_CLUSTER_DOMAIN`, `A2A_PEER_SCHEME`, `A2A_PEER_PORT` —
  control how bare names become in-cluster URLs.
- `A2A_RPC_PATH` — the mount path for the JSON-RPC endpoint + card. Defaults to
  `/a2a/app`; override only if you change the app name or mount path.

Each agent must also advertise a reachable base URL in **its own** card via
`APP_URL` (the card's `url`, which peers call). It defaults to
`http://0.0.0.0:8000` — unreachable from other pods — so the manifests set it per
role to `http://<service-name>.<namespace>.svc.cluster.local` (see
`infra/kustomize/base/orchestrator.yaml` and `workers.yaml`). It must match how
peers address that Service.

## Context provisioning

**A specialist sees the payload it was sent, and nothing else.** No conversation
history, no shared session state, no shared artifact handles. Everything it needs
arrives in that payload; everything else stays with the caller.

**How structured the payload is, is a per-peer choice.** The default contract
between two agents here is one free-text `task` plus a `case_id`
(`UnknownPeerRequest` in `app/cluster/resolver.py`) — enough to keep delegation
explicit, and the honest level for a peer another squad owns whose real schema
lives in its own agent card. Declaring a model in `app/agents/contracts.py` is
the **opt-in** upgrade: named parameters the calling model sees, validation
before the call leaves the pod, and a JSON Schema published in the card. Every
agent in this repo takes the upgrade — one free-text field is where a caller
starts pasting the conversation back in — but the mechanism supports both, and
a peer with no declared model is still a first-class peer.

This is a wiring decision, and it is easy to reverse by accident. A peer attached
as an ADK **sub-agent** is reached with `transfer_to_agent`, and
`RemoteA2aAgent` then rebuilds the outbound A2A message from the caller's
*session events* — every turn since the peer last replied, with other agents'
replies folded in and prefixed `For context:`. Measured on this repo's own
agents, that was **ten message parts** where the task needed one, and it
included the user's personal phone number and a different specialist's answer
([`docs/design-decisions.md`](docs/design-decisions.md), D1).

A peer attached as a **tool** (`app/cluster/peer_tool.py`) runs against a fresh
session whose only content is the arguments the caller composed:

```json
{"case_id": "case-123", "question": "Is BNP Paribas registered in IE?"}
```

Four things follow, and each is a problem under `sub_agents`:

- **Data segregation.** A specialist cannot receive personal or unrelated detail
  that merely happened to be earlier in the conversation.
- **Attention.** The model is not handed nine irrelevant parts, each explicitly
  labelled as context to weigh.
- **Cost and latency.** The transcript is not re-sent to every specialist on
  every turn.
- **Framework independence.** The contract is a JSON Schema published in the
  agent card, so a LangGraph or plain-FastAPI specialist is a first-class peer.

**Continuity is declared, not implicit.** Where a specialist genuinely needs to
build on earlier work — entity disambiguation, say — the caller passes the same
`case_id` and the specialist keys **its own** private store on it. A2A's
`contextId` carries the same idea at the protocol level. Either way, continuity
is part of the contract rather than a side effect of the transport.

**Large inputs travel by reference.** A caller passes
`document_refs: ["gs://bucket/cases/123/dossier.pdf"]` and the specialist reads
it with `read_document` (`app/agents/documents.py`), using its own credentials.
Embedding the document would blow the payload and the context window, but the
real reason is division of labour: if the caller had to pre-summarise a 200-page
filing to fit, the caller would be doing domain extraction it is not qualified
for. The planner knows *which* document matters; the specialist knows what to
take out of it.

**The one thing that is convention rather than enforcement:** ADK's `AgentTool`
copies the parent's session state into the child session (filtering only
`_adk`-prefixed keys). That state never reaches the wire — measured — so it
is not a cross-pod leak, but it does mean the isolation depends on nobody
writing shared state in the first place. A tool that stashes a value for another
agent to pick up is the thing to reject in review: the transport does not carry
it, so it cannot work as advertised
([`docs/design-decisions.md`](docs/design-decisions.md), D3).

## Session & memory persistence

Wired through dependency injection so agent code depends only on ADK base
classes; the backend is a deployment concern (`app/cluster/session.py`):

| Env | Default | Options |
| --- | --- | --- |
| `SESSION_BACKEND` | `in_memory` | `alloydb` (shared engine, see below), `database` (`+ SESSION_DB_URL`), `vertex_ai` (`+ AGENT_ENGINE_ID`) |
| `MEMORY_BACKEND` | `in_memory` | `vertex_ai` (`+ AGENT_ENGINE_ID`) |
| `TASK_STORE_BACKEND` | `in_memory` | `database` (shared engine) |

In-memory keeps local runs and tests hermetic. For a real cluster choose a
durable backend so state survives pod restarts and is shared across replicas —
the deployed values live in `infra/kustomize/base/configmap.yaml`.

## Artifact storage (blobs)

Session state answers "what was said"; an **artifact** is the file that came out
of it — a generated report, a fetched page, an image — kept out of the
conversation history and referenced by filename and version.

The service is `CloudPathArtifactService` (`app/shared/artifacts.py`): an ADK
`BaseArtifactService` implemented on `cloudpathlib`, so the storage backend is a
URI scheme rather than a code path. `app/cluster/artifacts.py` selects it and
`SessionModule` provides it, the same way the session service is wired.

| Env | Default | Effect |
| --- | --- | --- |
| `ARTIFACT_STORAGE_URI` | *(unset)* | Unset → `InMemoryArtifactService` (per-pod, ephemeral). Set → `CloudPathArtifactService` at that location: `gs://bucket/prefix`, `s3://bucket/prefix`, `az://container/prefix`, or a local path. |

There is no separate `ARTIFACT_BACKEND` switch: the scheme already names the
backend, so a second selector could only contradict it. Credentials follow each
provider's normal discovery — in the cluster that is ADC via Workload Identity,
with no key material anywhere.

**This is not how a document reaches a specialist.** Artifacts are keyed by
`{app_name}/{user_id}/{session_id}/{filename}/{version}`, and `app_name` is the
ADK `App` name (`"app"`) for every agent — so reaching one across agents means
sharing a session, which is exactly the implicit coupling this architecture
removes. Large inputs travel as explicit `document_refs` in the request payload
and the specialist reads them itself
([Context provisioning](#context-provisioning)). `ARTIFACT_STORAGE_URI` is for
an agent's *own* artifact storage; it need not be identical across agents, and a
per-agent prefix is the safer default. The trade-off with one shared bucket is
that bucket-level IAM lets any agent read any object, which
`infra/terraform/artifacts.tf` documents along with how to tighten it.

The bucket is provisioned by `infra/terraform/artifacts.tf` (uniform bucket-level
access, public access prevention, a 30-day lifecycle rule by default, and
`roles/storage.objectUser` for each agent's GSA on that bucket only). Take the
value for the ConfigMap from `terraform output artifact_storage_uri`. Leaving
`ARTIFACT_STORAGE_URI` out of the ConfigMap is what keeps the old per-pod
in-memory behaviour.

## Durable storage on AlloyDB

The cluster manifests default to AlloyDB for both session state and A2A tasks.

**Why the task store matters too.** The default `InMemoryTaskStore` is per-pod,
so a task created by the pod that answered `message/send` is invisible to the
pod that later receives `tasks/get`. That silently breaks task polling and
resubscription the moment an agent has more than one replica — it is a
correctness bug, not just a durability gap.

### One database, one schema per agent

Not a database per agent: PostgreSQL cannot query across databases without
FDW/dblink, so separate databases make any cross-agent question ("trace this
request through orchestrator → research → math") impossible, while multiplying
connection pools against a single-vCPU instance.

Not one flat schema either. Every agent runs `App(name="app")` (invariant 6), so
in a shared schema every `sessions` row carries `app_name='app'` and is
unattributable. Each agent therefore gets its own PostgreSQL schema, selected by
`DB_SCHEMA` (defaulting to `AGENT_NAME`) and applied as the connection's
`search_path`. ADK and the a2a SDK both emit **unqualified** table names, so
`search_path` does the routing with no library patching.

The isolation is enforced by `GRANT`, which maps one-to-one onto the per-agent
service account:

```
KSA agent-research ──WI──▶ GSA agent-research@PROJECT
                              │
                              ▼  (IAM DB auth, no password)
                    role "agent-research@PROJECT.iam"
                              │
                              ▼  USAGE + SELECT/INSERT/UPDATE/DELETE
                         schema "research"        ← and nothing else
```

`CREATE` is deliberately withheld, so a compromised agent cannot add or drop
tables. The only identity with DDL rights is the migration Job's `agent-migrator`.

### Authentication: IAM, no passwords

`app/cluster/db.py` builds one `AsyncEngine` per pod through the AlloyDB Python
connector. It mints a short-lived OAuth token per connection from the pod's
Workload Identity credentials and wraps the socket in mTLS — no password exists
anywhere, and nothing sensitive lands in Terraform state. No Auth Proxy sidecar
is needed: the Autopilot cluster is VPC-native, so pod IPs route to the
instance's private IP across the private-services-access peering.

| Env | Meaning |
| --- | --- |
| `DB_BACKEND` | `none` (default), `alloydb`, or `url` (plain DSN, for local/dev) |
| `ALLOYDB_INSTANCE_URI` | `projects/P/locations/L/clusters/C/instances/I` |
| `ALLOYDB_IAM_USER` | GSA email **minus** `.gserviceaccount.com` |
| `DB_NAME` | Database holding every agent's schema (`agents`) |
| `DB_SCHEMA` | Defaults to `AGENT_NAME` — leave unset |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | Per-pod pool. Small: the prototype instance has 1 vCPU |

> To look at the data: **[`docs/inspecting-the-database.md`](docs/inspecting-the-database.md)**.
> AlloyDB Studio in the Cloud console works despite the private IP — it goes
> through the Admin API, not the network.

### Migrations (Alembic)

Schema is owned by Alembic, never by the application — both libraries would
otherwise call `create_all()` at startup, which races across replicas, needs DDL
privileges the agents deliberately lack, and bypasses any migration history.

Migrations live under `app/migrations/` rather than the repo root because the
`Dockerfile` copies only `./app`; this way the migration Job runs the exact same
image as the agents.

```bash
# One schema at a time. Each schema keeps its own alembic_version table, so
# agents are versioned independently.
DB_SCHEMA=research DB_AGENT_ROLE='agent-research@PROJECT.iam' \
  uv run alembic -c app/migrations/alembic.ini upgrade head

# Review the DDL without a database (works for any schema):
DB_BACKEND=url DB_URL=postgresql://u@h/d \
  uv run alembic -c app/migrations/alembic.ini -x schema=research upgrade head --sql
```

Autogenerate is switched **off**. The tables belong to ADK's session schema and
the a2a SDK's task schema, not to models in this repo, so autogenerate would let
a library upgrade rewrite production DDL unreviewed. Instead
`tests/unit/test_migrations.py` renders the migrations offline and compares them
column-by-column against what those two libraries would generate themselves, so
drift fails the build rather than a live request.

Two deliberate additions beyond what the libraries declare:

- `created_at` / `updated_at` on `tasks`, with a trigger. `TaskMixin` has no
  timestamps at all, so tasks would accumulate with no way to identify old rows.
  A trigger is required because PostgreSQL has no `ON UPDATE` clause and
  `DatabaseTaskStore` writes through `session.merge()`, which touches only its
  own columns. The columns stay invisible to the ORM.
- Indexes on `sessions.update_time` and `tasks.updated_at`, so a retention sweep
  is a range scan rather than a full table scan.
- `approval_cases` (revision `0005`), the approval store described next. Unlike
  the other two this table is ours, not a library's. Revision `0005` also drops
  `hitl_approvals`, the coroutine-era table it replaces.

## Human-in-the-loop

An action that must not happen without a person is **proposed, not performed**.
The specialist returns a proposal and finishes its turn; the caller records a
durable case and answers the user; the approved action is carried out later by
an ordinary new call.

```
   pending ──decide (single conditional UPDATE)──▶ approved ──execute──▶ executed
        │                                                                    ▲
        └────────────────────────────────▶ rejected      re-drivable ────────┘
                                                         if execution is not
                                                         confirmed
```

Four properties make this a cluster feature rather than a request-scoped trick:

**Waiting is free.** A pending approval is a row in `approval_cases`. No
coroutine is suspended, no session is pinned in memory, nothing needs renewing.
An approval that takes a fortnight costs exactly what one taking a second costs
— which matters, because enterprise sign-off genuinely does take days.

**Nothing is pod-local.** Any replica can decide any case, because the only
state they share is the row. There is no recovery machinery to run because
nothing is in flight: the decision is written *before* the action is attempted,
so a pod that dies mid-execution leaves a re-drivable `approved` case rather
than an unanswerable one.

**What was approved is what runs.** Two things enforce it. The specialist
recomputes its result from the original request rather than from values a caller
retypes, and the caller confirms execution by checking the returned values
against the proposal it stored — a result that does not match is reported, not
recorded. Confirming that the *call* happened, rather than what it produced,
would catch neither.

**Nothing here is ADK-specific.** Two ordinary skills and a JSON contract — a
specialist written in LangGraph or plain FastAPI implements the same thing with
no framework hooks, which is what makes the pattern usable across squads.

**Two gated actions, one mechanism.** `math`'s `publish_result` is a gated
*write*; `trades`'s `run_trade_query` is a gated *read* — the model writes SQL,
returns it as a proposal, and touches BigQuery not at all until the approved
re-send. Gating a read is not a lesser case: the risk is not corruption but a
query nobody reviewed, against a table nobody scoped, returning a confident
number derived from the wrong rows. Both tools are the same shape — one
function, two behaviours, chosen by whether `approved_by` is present, with no
second code path that acts without the check.

Each reports the effect it actually performed, so the status vocabulary lives in
`app/agents/contracts.py`: `published` for the write, `executed` for the read,
and `EFFECT_PERFORMED` as the set of both. `cases.find_execution` matches
against that set rather than a literal, so a new gated action can name its own
effect truthfully — and **must** add its status to the set, or its executions
are reported as `approved_not_confirmed`.

One difference is worth knowing before writing a third. The math specialist
recomputes from `expression` because arithmetic is deterministic; SQL generation
is not, so the approved query text travels back in `TradesRequest.sql` and the
tool refuses to run without it rather than regenerating something similar.
Reproducibility of the effect from the request is the property that decides
which of the two shapes a gated action takes.

**There is no `HITL_BACKEND`.** The store follows `DB_BACKEND`: durable in
`approval_cases` when a database is configured, per-pod memory otherwise. With
`DB_BACKEND=none` a restart loses every pending approval and only the replica
that recorded a case can act on it — so scaling an agent past one replica
requires a database.

The reconciliation query is `status = 'approved'` (indexed by `0005`): an
approved case whose action never completed. Every such row is actionable —
re-drive it by calling `POST /cases/{proposal_id}` again.

**Why not suspend the invocation instead?** ADK can pause a call and resume it,
so the tempting design freezes the invocation across the A2A hop until the human
answers. It needs a reclaimable lease, a heartbeat and a background sweeper, and
it still cannot deliver the answer once the peer's A2A task has gone terminal.
[`docs/design-decisions.md`](docs/design-decisions.md) (D5) has the measurements
behind rejecting it.

> **[`docs/human-in-the-loop.md`](docs/human-in-the-loop.md)** is the full guide:
> the HTTP API with real payloads, how to write a gated action, a local
> walkthrough, and the known limits.

## Observability: logs + distributed tracing across A2A

The shared library (`app/shared/observability.py`) wires **structured logging**
(loguru) and **OpenTelemetry tracing** for every agent. `app/agent.py` calls
`configure_observability()` at startup, using this pod's `AGENT_NAME` as the
trace `service.name`, so every agent appears as a distinct service in Cloud
Trace.

**One trace across the whole cluster.** ADK already emits spans (`invoke_agent`,
`execute_tool`, `generate_content`). Here they are made to span *pods*:

- **Outbound:** the httpx client is instrumented, so when the orchestrator calls
  a specialist over A2A it injects the W3C `traceparent` header.
- **Inbound:** `app/fast_api_app.py` calls `instrument_fastapi_app(app)` as part
  of building the serving app, so each agent **extracts** that header and
  continues the caller's trace instead of starting a new one. (ADK's built-in
  middleware only handles Google-Agent-Engine headers, not the standard
  `traceparent`, so this explicit step is what makes cross-pod tracing work.)

The result is a single waterfall: `orchestrator.invoke_agent → httpx POST →
research.invoke_agent → generate_content`, linked by one trace id.

**Logs are trace-correlated.** loguru emits JSON to stdout (picked up by GKE's
logging agent → Cloud Logging), and every line carries the active
`logging.googleapis.com/trace` field, so a log entry links to its trace in the
console. stdlib logs (ADK, uvicorn, `google.genai`) are routed through loguru too.

**Where it goes (and how to redirect it).** Traces + logs go to **Google Cloud**
(Cloud Trace / Cloud Logging) by default — no extra infrastructure. The Terraform
grants the runtime service account `telemetry.tracesWriter`, `logging.logWriter`,
and `monitoring.metricWriter` and enables the Telemetry, Cloud Trace, Logging, and
Monitoring APIs. View traces in **Trace Explorer** (filter by span name, e.g.
`invoke_agent`).

To use a vendor-neutral stack instead (Grafana Tempo, Jaeger, Datadog, ...), set
`OTEL_EXPORTER_OTLP_ENDPOINT` in `infra/kustomize/base/configmap.yaml` to point at
an in-cluster OpenTelemetry Collector — no code change. A Collector can fan out to
Cloud Trace *and* your own backend. This project ships no visualization stack of
its own; it relies on Google Cloud's managed observability by default.

| Env | Default | Effect |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | Log level (loguru + stdlib) |
| `LOG_FORMAT` | *(auto-detected)* | `json` (Cloud Logging) or `console` (local). Unset means "`json` unless stderr is a TTY", so pods get JSON and an interactive shell gets the readable format. The ConfigMap pins `json` anyway |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)* | Redirect traces to an OTLP collector instead of Cloud Trace |
| `OTEL_SERVICE_NAME` | `AGENT_NAME` | Override the trace service name |

## Run locally

```bash
uv sync
# Orchestrator with no reachable peers (answers directly):
uv run adk web        # or: uv run uvicorn app.fast_api_app:app --port 8000

# Be a specialist instead:
AGENT_NAME=math uv run adk web
```

To exercise A2A **delegation locally**, run two processes and point the
orchestrator at the specialist. Set `APP_URL` on the specialist so its card
advertises a reachable URL, and give the peer as a service **root** (the resolver
appends `/a2a/app`):

```bash
# Terminal 1 — the math specialist on :8091
AGENT_NAME=math APP_URL=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8091

# Terminal 2 — the orchestrator on :8090, delegating to it
AGENT_NAME=orchestrator A2A_PEERS=math=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8090
```

`make up` does the whole cluster instead — six processes on 8090 orchestrator,
8091 math, 8092 research, 8093 planner, 8094 trades, 8095 currency — and wires
**two** sets of peers, because the graph is two levels deep: the orchestrator
gets `research`, `math`, `planner` and `trades`, and `math` gets
`A2A_PEERS=currency=http://127.0.0.1:8095`. Omitting the second set fails
quietly: every agent starts and reports healthy, and `math` simply has no
currency tool. `make serve-<agent>` runs any one of them in the foreground.

Unit tests (hermetic — pin the model tiers so no catalog lookup is needed):

```bash
GEMINI_FAST_MODEL=gemini-2.5-flash-lite \
GEMINI_BALANCED_MODEL=gemini-2.5-flash \
GEMINI_CAPABLE_MODEL=gemini-2.5-pro \
GEMINI_EMBEDDING_MODEL=gemini-embedding-001 \
  uv run pytest tests/unit app/shared/tests -q
```

All four pins are needed: importing `app` resolves every tier — the embedding
model included — against the live Vertex catalog, and *raises* on failure.

## Deploy to GKE

### 1. Provision infrastructure (Terraform)

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform init && terraform apply

eval "$(terraform output -raw get_credentials_command)"   # wire kubectl
terraform output build_command                            # for step 2
terraform output -json kustomize_values                   # everything for step 3
```

This also provisions AlloyDB (`c4a-highmem-1`, 1 vCPU / 8 GB), the
private-services-access peering it needs, one Google Service Account **per
agent**, a migrator account, and the `agent-builder` account step 2 runs as.
Provisioning AlloyDB takes several minutes.

Per-agent identity is not uniform by accident. `var.agent_iam_roles` is the
baseline every agent gets; `var.agent_extra_iam_roles` widens exactly one. It
defaults to `{ trades = ["roles/bigquery.jobUser"] }`, so `trades` is the only
agent that can start a query job — and `research`, the agent that ingests
untrusted web content, cannot. Note what `jobUser` withholds: permission to read
data. The trades GSA holds `bigquery.dataViewer` on nothing, so it can only
query datasets that are already public, which is the one table it is pointed at.

> **Upgrading an existing deployment:** this replaces the former single
> `agents-runtime` service account with one per agent, so `terraform apply` will
> destroy it. Any IAM grant made to `agents-runtime` outside this config must be
> re-applied to the new accounts.

> **Region:** the `c4a-highmem-1` machine type is Arm (Axion) and is not
> available everywhere. `europe-west4` and `us-central1` are covered; check
> <https://cloud.google.com/alloydb/docs/choose-machine-type> before changing
> `var.region`. The shape has no uptime SLA — it is Google's documented
> sandbox/dev size. Move to `c4a-highmem-2-lssd` and `REGIONAL` before
> production.

### 2. Build and push the image (Cloud Build)

```bash
cd ../..                       # back to the repo root, where the Makefile lives
make image TAG=demo-1
```

`cloudbuild.yaml` at the repo root is the build; `make image` submits it. Only
the source tarball leaves the workstation — a few hundred KB, because
`.gcloudignore` keeps `infra/` (Terraform state included), `tests/`, `docs/` and
`scripts/` out of it — while the wheels come down and the layers go up inside
Google's network. Keep `TAG` in step with `newTag` in the kustomize overlay: the
cluster pulls what that file names, not what was built last.

The build runs as the dedicated `agent-builder` service account from
`infra/terraform/cloudbuild.tf`, **not** Cloud Build's legacy default account.
That one carries `roles/editor` on the whole project, and in projects created
after mid-2024 it is not provisioned at all — `gcloud builds submit` then fails
with an error about a missing service account that reads like an API-enablement
problem. `agent-builder` holds three things instead: `roles/artifactregistry.writer`
scoped to the agent repository (not project-wide), `roles/logging.logWriter`
(required by `options.logging: CLOUD_LOGGING_ONLY`), and `roles/storage.objectUser`
for the source tarball `gcloud builds submit` stages. None of the agents' roles
are granted here, so a build cannot call Vertex AI or reach AlloyDB.
`var.builder_impersonators` is how a CI runner is allowed to run a build without
being granted owner.

**The image is multi-stage and carries no `uv`.** The build stage takes the uv
binary from its own published image and runs `uv sync --frozen --no-dev`; the
runtime stage copies only `/code/.venv` and `./app`, puts the venv's `bin` on
`PATH`, and runs as uid 1001. That is why `CMD` is a bare `uvicorn ...` and why
`migrate-job.yaml` invokes `python -m app.cluster.bootstrap` and `alembic -c
app/migrations/alembic.ini upgrade head` directly — a leftover `uv run` there
fails with "command not found", which reads like a broken image rather than a
stale command line. Together with dropping the `[evaluation]` extra from the
runtime dependency set (litellm, scipy, scikit-learn, pandas, openai, tokenizers,
tiktoken and huggingface_hub go with it; `agents-cli eval` still requests it
through the `eval` group), the installed runtime tree went from **702 MB to
430 MB**, measured by syncing into a scratch environment before and after.

Quote that number carefully. The **compressed image a node pulls** went only
286 MB -> 266 MB, because the old single-stage build hardlinked uv's cache into
the venv (Docker stores those as links, not copies) and because what is left
compresses badly -- `pyarrow` is 152 MB of the 474 MB installed, and it stays,
since `google-adk[gcp]` requires it. The gain is in disk footprint and attack
surface rather than in pull time. Verified on the running image: no `uv`, no
pandas/scipy/scikit-learn/litellm/openai, and uid 1001 rather than root.

> **If you must build on a workstation**, `--platform linux/amd64` is
> **required**. The Autopilot nodes these manifests target are amd64, so on an
> arm64 machine a native build produces an image whose pods fail with `exec
> format error` — which reads like an application bug rather than a build one.
> The base image is multi-arch, so the cross-build works fine; it is just slower
> under emulation. Cloud Build's workers are amd64, so the flag is a no-op
> there, and `cloudbuild.yaml` passes it anyway to state the target in the file
> rather than in someone's shell history.

### 3. Deploy the agents (Kustomize)

Fill in the placeholders first, from `terraform output -json kustomize_values`:

- `infra/kustomize/base/serviceaccounts.yaml` → the
  `iam.gke.io/gcp-service-account` annotation on **each** ServiceAccount.
- `infra/kustomize/base/configmap.yaml` → `ALLOYDB_INSTANCE_URI`.
- `infra/kustomize/base/orchestrator.yaml` and `workers.yaml` →
  `ALLOYDB_IAM_USER` per agent (the GSA email minus `.gserviceaccount.com`).
- `infra/kustomize/base/migrate-job.yaml` → `ALLOYDB_IAM_USER`,
  `AGENT_ROLE_SUFFIX`, and `MIGRATE_AGENTS` (currently
  `orchestrator research math trades currency`; it must match `var.agents` and
  the registry).
- `infra/kustomize/overlays/dev/kustomization.yaml` → `images[].newName`, and
  `newTag` matching the `TAG` step 2 built.

```bash
# A Job's spec is immutable, so clear any previous run before re-applying.
kubectl -n agents delete job/agent-migrate --ignore-not-found

kubectl apply -k infra/kustomize/overlays/dev

# Let the schema land before the agents settle: they have no CREATE privilege
# by design, so they crash-loop until the migration completes.
kubectl -n agents wait --for=condition=complete job/agent-migrate --timeout=10m
kubectl -n agents get pods,svc
```

The Job creates the `agents` database (the Terraform provider has no
`google_alloydb_database` resource), then per agent creates its schema, runs
every migration, and grants that agent's IAM role access to it.

### 4. Try it

```bash
kubectl -n agents port-forward svc/orchestrator 8080:80
# then POST to the orchestrator's A2A endpoint / ADK API on localhost:8080
```

To expose the orchestrator externally, switch its Service to `type: LoadBalancer`
or front it with an Ingress/Gateway (the workers stay internal `ClusterIP`).

## Notes & knobs

- **Container port:** everything serves on `8080`. Note the ConfigMap's `PORT`
  key is **documentation, not configuration** — nothing reads it; the
  `Dockerfile` `CMD` hardcodes `--port 8080`. Moving the port means editing six
  places by hand — the `CMD`, `EXPOSE`, each `containerPort`, each probe's
  `tcpSocket.port`, each Service's `targetPort`, and the NetworkPolicy `ports` —
  plus that key, to keep it honest.
- **Least privilege:** the workers use internal `ClusterIP` Services; only the
  orchestrator needs external exposure. The NetworkPolicy states the topology
  the code declares rather than a looser one that would also work: `research`,
  `math`, `planner` and `trades` accept traffic from the orchestrator, and
  `currency` accepts it from `math` **only** — not from the orchestrator. A
  blanket "any agent may call any agent" rule would hide that distinction and
  hand a prompt-injected `research` a path to every specialist. Add a
  delegation edge in `AgentSpec.peers` without adding it here and the call fails
  as a connection timeout, not as an error.
- **The trades agent has three knobs of its own**, set on its Deployment:
  `TRADES_LOCATION` (default `US` — `bigquery-public-data` is in the US
  multi-region, and a job submitted elsewhere fails with "dataset not found",
  which reads like a permissions problem), `TRADES_MAX_BYTES_BILLED` (default
  `1073741824`, passed to BigQuery as `maximum_bytes_billed`, so a runaway query
  is killed rather than billed — the one control a human reading the SQL cannot
  apply), and `TRADES_MAX_ROWS` (default `50`, a payload budget: rows cross A2A
  as text inside a model's context, so aggregate in SQL).
- **Scaling:** bump `replicas` per role in the overlay; the resolver addresses
  Services (not pods), so load-balancing across replicas is automatic.
- **Everything lives in `infra/`:** Terraform provisions the Google Cloud side
  (cluster, registry, identities, AlloyDB, bucket) and Kustomize deploys the
  Kubernetes side. There is no second deployment path.
