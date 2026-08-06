# Multi-agent system on GKE

This project is a **cloud-native, multi-agent system** designed to run on Google
Kubernetes Engine (GKE). An orchestrator agent breaks a request into sub-tasks
and delegates them to specialist worker agents, each running as its own
Kubernetes Service and reached over the **A2A** protocol.

```
                       ┌───────────────────┐
   user ───────────▶   │   orchestrator    │   (entry point, models.capable)
                       │  Deployment/Svc   │
                       └─────────┬─────────┘
              A2A (RemoteA2aAgent, well-known agent card)
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
┌────────────────┐    ┌────────────────┐    ┌────────────────┐
│    research    │    │      math      │    │    planner     │  specialists
│ Deployment/Svc │    │ Deployment/Svc │    │ Deployment/Svc │  (workers)
└────────────────┘    └────────────────┘    └────────────────┘
                                             a graph agent with
                                             a human approval step
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
| Orchestration / planner layer | `app/agents/orchestrator/agent.py` (an agent whose spec declares peers; delegates via ADK agent transfer) |
| Agent-to-agent communication | `app/cluster/resolver.py` builds `RemoteA2aAgent`s from peer agent cards |
| Context sharing & propagation | `remember`/`recall` tools write session state that travels across A2A hops — `app/agents/common.py` |
| Session & memory persistence | Pluggable, env-selectable backends — `app/cluster/session.py` + `SessionModule` |
| Artifact (blob) storage | Blob-store-agnostic via `cloudpathlib` — `app/cluster/artifacts.py` + `app/shared/artifacts.py` |
| Human-in-the-loop | Durable pauses captured by a plugin and resumed from the session — `app/cluster/hitl.py` + `app/cluster/approvals.py` |
| Cloud-native reference architecture | `infra/terraform` (GKE Autopilot + WI + Artifact Registry) and `infra/kustomize` |
| ADK / DI best practices | `injector` modules (`ModelModule`, `ClusterModule`, `SessionModule`) resolved in `app/agent.py` |
| Observability across the MAS | Trace context propagates over A2A (httpx inject + `instrument_fastapi_app` extract) → one Cloud Trace per request; trace-correlated loguru logs — see "Observability" below |

## Agents: all equal, one selected per pod

Every agent is the **same kind of thing** — a declarative `AgentSpec` (name,
model tier, instruction, tools, and optional `peers`) built by the single
`build_agent` in `app/agents/base.py`. There is no special "orchestrator" class:
the orchestrator is simply the agent whose spec lists `peers`, so `build_agent`
attaches those peers as `RemoteA2aAgent` children. A leaf agent lists no peers
and just gets no sub-agents. Any agent could declare peers and coordinate others.

Every pod runs the same image; `AGENT_NAME` selects which agent from the registry
(`app/agents/__init__.py`) this process becomes:

- `AGENT_NAME=orchestrator` (default) → the agent with `peers=("research",
  "math", "planner")`. It attaches them as remote sub-agents and delegates.
- `AGENT_NAME=research` / `math` / `planner` → a leaf agent (no peers), served
  over A2A so others can reach it.

The one asymmetry that remains — which agent is exposed to users — is a
**deployment** concern (which Service gets external ingress), not a code one.

**One deliberate variation.** A spec may set `root_node` to serve an ADK
`Workflow` — a fixed graph of nodes — instead of a model-driven `LlmAgent`. That
is the `planner` agent, and it is how a pipeline with a mandatory human stage is
built. It is still one spec shape and one builder (the graph is data on the
spec, not a second code path), but such an agent **cannot declare peers**: a
graph has no `sub_agents`, so `build_agent` raises if a spec sets both. See
[Human-in-the-loop](#human-in-the-loop).

Add an agent by:

1. Creating `app/agents/<name>/agent.py` exposing a `SPEC = AgentSpec(...)` (put
   agent-specific tools in `app/agents/<name>/tools.py`; shared context tools
   live in `app/agents/common.py`).
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

**Every agent shares one artifact namespace, on purpose.** Artifacts are keyed by
`{app_name}/{user_id}/{session_id}/{filename}/{version}`, and `app_name` is the
ADK `App` name (`"app"`) for every agent — not `AGENT_NAME`. Pointing all agents
at the same URI is therefore what lets `research` save a document that the
orchestrator loads back on the same session: the artifact counterpart of the
`shared:` session state written by `remember`/`recall`. This deliberately does
*not* mirror the schema-per-agent split used for AlloyDB; the trade-off is that
bucket-level IAM lets any agent read any artifact, which
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
- `hitl_approvals` (revision `0004`), the pending-pause store described next.
  Unlike the other two this table is ours, not a library's.

## Human-in-the-loop

An agent can stop mid-task and wait for a person — to approve an action before
it runs, or to answer a question — and then carry on exactly where it stopped.
All three mechanisms reduce to the same thing: a **long-running function call**
that ends the invocation with no final answer.

Three properties make this a cluster feature rather than a request-scoped trick:

**It is captured wherever it happens.** `HitlPlugin` (`app/cluster/hitl.py`)
observes events on the Runner rather than hooking a route, so it catches pauses
from the `/hitl` API, the ADK web UI, and inbound A2A calls alike.

**It crosses A2A hops unchanged.** A pause raised inside a peer surfaces in the
*caller's* event stream, and the answer is relayed back to that peer on the same
A2A task — ADK and the a2a SDK do that; this repo adds no relay code. So the
orchestrator can own the entire human-facing API while the pause happens in a
specialist. The consequence for operators: approvals live in the **caller's**
schema, not the paused agent's.

**It is not pod-local.** A paused invocation is rebuilt from the session rather
than held in a process's memory, so any replica can answer any approval —
measured, not assumed. Three things keep that safe:

```
   pending ──claim (single conditional UPDATE)──▶ deciding ──▶ approved | rejected
                                                     │
                                    heartbeat every TTL/3 while the resume runs;
                                    a lease that stops advancing is reclaimed
                                    by a peer's sweep after HITL_LEASE_TTL_SECONDS
```

the approval row and the session are both in the database, the claim is a single
conditional `UPDATE` so two deciders cannot both win, and a running resume
heartbeats its lease so recovery reclaims only work that actually stalled. If a
pod dies mid-resume a live peer finishes the job without waiting for a restart.

**There is no `HITL_BACKEND`.** The store follows `DB_BACKEND`: durable in
`hitl_approvals` when a database is configured, per-pod memory otherwise. With
`DB_BACKEND=none` a restart loses every pending approval and only the pod that
took a decision can act on it — so scaling an agent past one replica requires a
database. The manifests stay at `replicas: 1` for cost, not correctness.

One recovery limit worth knowing before you rely on it: the decision is written
by the *claim*, so a crashed resume is re-driven without asking the human twice —
but if the pause was inside an A2A peer whose task already completed, that task
is terminal and the reply cannot be replayed. Such a row is closed with
`resumed_at` left NULL and a warning logged; query `resumed_at IS NULL AND status
IN ('approved','rejected')` to find approvals whose effect happened but whose
answer never reached the user.

> **[`docs/human-in-the-loop.md`](docs/human-in-the-loop.md)** is the full guide:
> which of the three mechanisms to reach for, the HTTP API with real payloads, a
> local walkthrough, and the production checklist.

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
terraform output artifact_registry_repo                   # for step 2
terraform output -json kustomize_values                   # everything for step 3
```

This also provisions AlloyDB (`c4a-highmem-1`, 1 vCPU / 8 GB), the
private-services-access peering it needs, one Google Service Account **per
agent**, and a migrator account. Provisioning AlloyDB takes several minutes.

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

### 2. Build and push the image

```bash
REPO=$(cd infra/terraform && terraform output -raw artifact_registry_repo)
gcloud auth configure-docker "${REPO%%/*}"
docker build --platform linux/amd64 -t "$REPO/agent:latest" .
docker push "$REPO/agent:latest"
```

> `--platform linux/amd64` is **required**. The Autopilot nodes these manifests
> target are amd64, so on an arm64 workstation a native build produces an image
> whose pods fail with `exec format error` — which reads like an application bug
> rather than a build one. The base image is multi-arch, so the cross-build works
> fine; it is just slower under emulation.

### 3. Deploy the agents (Kustomize)

Fill in the placeholders first, from `terraform output -json kustomize_values`:

- `infra/kustomize/base/serviceaccounts.yaml` → the
  `iam.gke.io/gcp-service-account` annotation on **each** ServiceAccount.
- `infra/kustomize/base/configmap.yaml` → `ALLOYDB_INSTANCE_URI`.
- `infra/kustomize/base/orchestrator.yaml` and `workers.yaml` →
  `ALLOYDB_IAM_USER` per agent (the GSA email minus `.gserviceaccount.com`).
- `infra/kustomize/base/migrate-job.yaml` → `ALLOYDB_IAM_USER`,
  `AGENT_ROLE_SUFFIX`, and `MIGRATE_AGENTS`.
- `infra/kustomize/overlays/dev/kustomization.yaml` → `images[].newName`.

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
  orchestrator needs external exposure.
- **Scaling:** bump `replicas` per role in the overlay; the resolver addresses
  Services (not pods), so load-balancing across replicas is automatic.
- **Everything lives in `infra/`:** Terraform provisions the Google Cloud side
  (cluster, registry, identities, AlloyDB, bucket) and Kustomize deploys the
  Kubernetes side. There is no second deployment path.
