# mas-gke

A **multi-agent system on Kubernetes**, built on Google [ADK](https://adk.dev/):
an orchestrator agent breaks a request into sub-tasks and delegates them to
specialist agents, each deployed as its own Kubernetes Service and reached over
the [A2A protocol](https://a2a-protocol.org/).

```
                       ┌───────────────────┐
   user ───────────▶   │   orchestrator    │  entry point; plans and delegates
                       │  Deployment/Svc   │
                       └─────────┬─────────┘
              A2A (RemoteA2aAgent, well-known agent card)
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
        ┌────────────────┐              ┌────────────────┐
        │    research    │              │      math      │  specialists
        │ Deployment/Svc │              │ Deployment/Svc │
        └────────────────┘              └────────────────┘

        ┌────────────────┐
        │    planner     │  a graph agent: fixed pipeline with a human step.
        │ Deployment/Svc │  Reachable over A2A; not one of the orchestrator's peers.
        └────────────────┘
```

## What this is

Most agent examples run in a single process. This repo is the other case: **what
you actually have to build once agents are separate deployments talking to each
other.** Splitting them apart buys independent scaling, per-agent identity and
blast-radius isolation — and immediately raises questions a single process never
has to answer. How does an agent find its peers? Where does state live when the
next request lands on a different pod? What does one trace look like when a task
crosses three services? How does a human approve an action that is happening two
hops away?

Those answers are the substance of this repo. The agents themselves are
deliberately trivial — arithmetic, a web search, a toy plan-and-revise pipeline —
because they are scaffolding for the parts that are not.

**What is wired up:**

| | |
| --- | --- |
| **Agent definition** | Every agent is a declarative `AgentSpec` in a registry, built by one factory. There is no orchestrator class — the orchestrator is just the agent whose spec declares `peers` |
| **One image, every agent** | `AGENT_NAME` selects which agent a process becomes at startup, so there is a single build and a single container to deploy |
| **Service discovery** | Peers are declared in code, resolved from cluster DNS, and discovered through their published A2A agent cards |
| **Durable state** | Sessions, A2A tasks and human approvals persist in AlloyDB, each agent in its own PostgreSQL schema, with Alembic-managed migrations |
| **Per-agent identity** | One Google service account per agent, bound by Workload Identity, authenticating to the database with IAM — no passwords anywhere |
| **Human-in-the-loop** | An agent can pause mid-task and wait for a person, durably and across A2A hops. Three mechanisms — [docs/human-in-the-loop.md](docs/human-in-the-loop.md) |
| **Observability** | Structured logs and distributed traces that follow a request across every hop, so one trace covers the whole cluster |
| **Infrastructure** | Terraform for the Google Cloud side, Kustomize for the Kubernetes side, both in `infra/` |

**Status.** This is a working reference implementation, not a product. Several
ADK features it relies on are marked experimental, so pin the ADK version
deliberately.

**Where to go next:**

| If you want to… | Read |
| --- | --- |
| Understand the architecture in depth | **[GKE.md](GKE.md)** — service discovery, session backends, observability, deployment |
| Get it running locally | [Quick Start](#quick-start), below |
| Add an agent of your own | [docs/adding-an-agent.md](docs/adding-an-agent.md) |
| Configure anything | [docs/environment-variables.md](docs/environment-variables.md) |
| Make an agent wait for a human | [docs/human-in-the-loop.md](docs/human-in-the-loop.md) |
| Deploy to your own GCP project | [docs/deploy-to-another-project.md](docs/deploy-to-another-project.md) |
| Work on this with a coding agent | `AGENTS.md` — invariants, gotchas, verified commands |

## Project Structure

```
mas-gke/
├── app/
│   ├── agent.py                # Entry point: picks this process's agent by AGENT_NAME
│   ├── fast_api_app.py         # FastAPI serving app (+ inbound A2A trace extraction)
│   ├── agents/                 # WHO the agents are
│   │   ├── __init__.py         #   the registry (AGENTS + DEFAULT_AGENT)
│   │   ├── base.py             #   AgentSpec + the single build_agent()
│   │   ├── common.py           #   shared tools (remember / recall)
│   │   ├── hitl_strategies.py  #   the human-in-the-loop tool strategies
│   │   ├── orchestrator/       #   entry point (declares peers)
│   │   ├── research/           #   specialist + web_search tool
│   │   ├── math/               #   specialist + calculate tool
│   │   └── planner/            #   graph agent with a human step
│   ├── cluster/                # The PLUMBING
│   │   ├── config.py           #   env -> ClusterConfig / peers
│   │   ├── resolver.py         #   peers -> RemoteA2aAgent (agent-card discovery)
│   │   ├── di.py               #   injector modules
│   │   ├── session.py          #   pluggable session + memory backends
│   │   ├── artifacts.py        #   blob-store-agnostic artifact storage (cloudpathlib)
│   │   ├── tasks.py            #   pluggable A2A task store
│   │   ├── hitl.py             #   pause capture + resume
│   │   ├── approvals.py        #   the approval store and its state machine
│   │   ├── db.py               #   the one AsyncEngine per pod (AlloyDB, IAM auth)
│   │   └── bootstrap.py        #   creates the database (no Terraform resource exists)
│   ├── migrations/             # Alembic. Under app/ so the image carries it.
│   ├── shared/                 # Shared library (models, observability, secrets)
│   └── app_utils/              # Low-level serving/A2A helpers
├── infra/
│   ├── terraform/              # GKE Autopilot, per-agent Workload Identity, AlloyDB,
│   │                           #   artifact bucket
│   └── kustomize/              # Namespace, ServiceAccounts, ConfigMap, NetworkPolicy,
│                               #   migration Job, Deployments
├── scripts/                    # dbcheck, grant_readers, sql/ (request tracing)
├── tests/                      # unit / integration / eval
├── docs/                       # environment-variables, adding-an-agent,
│                               #   inspecting-the-database, human-in-the-loop,
│                               #   deploy-to-another-project, plans/
├── AGENTS.md                   # AI-assisted development guide
└── pyproject.toml              # Project dependencies
```

> 💡 **Tip:** For AI-assisted development, `AGENTS.md` carries the full project
> context — architecture invariants, gotchas, and verified commands.

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager (used for all dependency management in this project) - [Install](https://docs.astral.sh/uv/getting-started/installation/) ([add packages](https://docs.astral.sh/uv/concepts/dependencies/) with `uv add <package>`)
- **agents-cli**: Agents CLI - Install with `uv tool install google-agents-cli`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)
- For deploying: **kubectl**, **terraform** (>= 1.9), and an OCI image builder
  (**docker** or **podman** — the commands below show `docker`; podman accepts
  the same arguments)

## Quick Start

Install `agents-cli` and its skills if not already installed:

```bash
uvx google-agents-cli setup
```

Install required packages:

```bash
agents-cli install
```

Test the agent with a local web server:

```bash
agents-cli playground
```

By default the process becomes the **orchestrator**. To run a specialist
instead, set `AGENT_NAME`:

```bash
AGENT_NAME=math uv run adk web
```

You can also use features from the [ADK](https://adk.dev/) CLI with `uv run adk`.

### Exercise A2A delegation locally

Run two processes and point the orchestrator at the specialist:

```bash
# Terminal 1 — the math specialist
AGENT_NAME=math APP_URL=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8091

# Terminal 2 — the orchestrator delegating to it
AGENT_NAME=orchestrator A2A_PEERS=math=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8090
```

## Commands

| Command              | Description                                                                                 |
| -------------------- | ------------------------------------------------------------------------------------------- |
| `agents-cli install` | Install dependencies using uv |
| `agents-cli playground` | Launch local development environment |
| `agents-cli lint`    | Run code quality checks (ruff, codespell, ty) |
| `agents-cli eval`    | Evaluate agent behavior (generate, grade, analyze, and more — see `agents-cli eval --help`) |
| `uv run pytest tests/unit app/shared/tests` | Run the hermetic unit tests (see note below) |
| `uv run pytest tests/integration` | Run integration tests (needs a running server) |
| [A2A Inspector](https://github.com/a2aproject/a2a-inspector) | Launch A2A Protocol Inspector |

> ⚠️ **Unit tests need the model tiers pinned.** Importing `app` resolves models
> from the live Vertex AI catalog, so pin them to keep the run offline:
>
> ```bash
> GEMINI_FAST_MODEL=gemini-2.5-flash-lite \
> GEMINI_BALANCED_MODEL=gemini-2.5-flash \
> GEMINI_CAPABLE_MODEL=gemini-2.5-pro \
> GEMINI_EMBEDDING_MODEL=gemini-embedding-001 \
>   uv run pytest tests/unit app/shared/tests -q
> ```

---

## Development

Each agent is a declarative `AgentSpec` in `app/agents/<name>/agent.py`,
registered in `app/agents/__init__.py`. There is no special orchestrator class —
the orchestrator is simply the agent whose spec declares `peers`. (One variation:
a spec may set `root_node` to serve a fixed graph instead of a model-driven
agent, which is what `planner` does.)

**To add an agent**, in code:

1. Create `app/agents/<name>/agent.py` exposing a `SPEC = AgentSpec(...)`
   (agent-specific tools go in `app/agents/<name>/tools.py`).
2. Register it in `app/agents/__init__.py`.
3. Add the name to another agent's `AgentSpec.peers` if it should be delegated to.

Use a single lowercase word valid as **both** a Python identifier and a
Kubernetes DNS label (e.g. `research`, `math`) — the Service name must equal the
agent name for peer discovery to resolve.

Deploying it to the cluster touches five more files (Terraform's `var.agents`,
the ServiceAccounts, a Deployment/Service pair, the NetworkPolicy allow-list and
the migration Job). Omitting the NetworkPolicy is the easy one to miss: the agent
then has no ingress at all and delegation fails as a timeout rather than an
error. **[docs/adding-an-agent.md](docs/adding-an-agent.md)** is the full
walkthrough with a checklist.

## Configuration

Everything is configured by environment variable — which agent a process becomes,
which model tier it uses, whether sessions, tasks, artifacts and HITL approvals
are durable or per-pod.

**[docs/environment-variables.md](docs/environment-variables.md)** is the
complete reference: every variable with its default and effect, which companions
each backend makes mandatory, whether a bad value raises at startup or fails
silently, what the Kubernetes manifests set, and minimum viable configurations
for local runs, hermetic tests and the cluster.

The four that most often cost time: `AGENT_NAME` (which agent this process
becomes), `APP_URL` (its default is unreachable from other pods, so delegation
hangs while probes stay green), the `GEMINI_*_MODEL` pins (unset means a live
catalog lookup at import), and `DB_BACKEND` (gates session, task and approval
durability in one switch).

## Deployment

This project deploys as a **multi-agent cluster** via `infra/` (Terraform +
Kustomize). See **[GKE.md](GKE.md)** for the full walkthrough, and
**[docs/deploy-to-another-project.md](docs/deploy-to-another-project.md)** to
stand this repo up in a *different* Google Cloud project (prerequisites, the
project-specific values to change, verification, teardown, troubleshooting).

The short version:

```bash
# 1. Provision the cluster, Workload Identity, and Artifact Registry
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform init && terraform apply
eval "$(terraform output -raw get_credentials_command)"

# 2. Build and push the image
REPO=$(terraform output -raw artifact_registry_repo)
docker login -u oauth2accesstoken -p "$(gcloud auth print-access-token)" "${REPO%%/*}"
docker build --platform linux/amd64 -t "$REPO/agent:latest" .
docker push "$REPO/agent:latest"

# 3. Fill the placeholders below, then deploy. Let the Alembic Job finish first:
#    the agents have no CREATE privilege by design and crash-loop until the
#    schema exists.
kubectl -n agents delete job/agent-migrate --ignore-not-found  # spec is immutable
kubectl apply -k infra/kustomize/overlays/dev
kubectl -n agents wait --for=condition=complete job/agent-migrate --timeout=10m
kubectl -n agents get pods,svc
kubectl -n agents port-forward svc/orchestrator 8080:80
```

> ⚠️ `--platform linux/amd64` is **required**. The GKE Autopilot nodes these
> manifests target are amd64, so on an arm64 workstation a native build produces
> an image whose pods fail with `exec format error` — which reads like an
> application bug rather than a build one.

The project-specific values to fill before step 3 — all of them come from
`terraform output -json kustomize_values`:

- `infra/kustomize/base/serviceaccounts.yaml` → the per-agent GSA emails (one
  ServiceAccount per agent, plus the migrator)
- `infra/kustomize/base/configmap.yaml` → `ALLOYDB_INSTANCE_URI`
- `orchestrator.yaml` / `workers.yaml` / `migrate-job.yaml` → `ALLOYDB_IAM_USER`
  (each agent's GSA email minus `.gserviceaccount.com`)
- `infra/kustomize/overlays/dev/kustomization.yaml` → the image repo
  (`terraform output artifact_registry_repo`)
- `infra/kustomize/base/configmap.yaml` → `OTEL_RESOURCE_ATTRIBUTES:
  "gcp.project_id=<project>"`. Not cosmetic: Cloud Trace rejects every span
  batch whose resource lacks it, so tracing fails silently if it's stale.

## Human-in-the-loop

An agent can stop mid-task and wait for a person — to approve an action before it
runs, or to answer a question. The pause is durable, survives a restart when a
database is configured, and works unchanged when the paused agent is an A2A hop
away from the human, so the orchestrator can own the whole human-facing API.

```bash
curl -X POST localhost:8080/hitl/run -H 'content-type: application/json' \
  -d '{"text":"Ask the math specialist to compute 23 * 19 and publish the result."}'
# → status "paused", with what is waiting and the exact call being gated
curl -X POST localhost:8080/hitl/approvals/<id> -H 'content-type: application/json' \
  -d '{"approved":true,"text":"approved by ops"}'
```

Three mechanisms — gate an action, ask a question, or a fixed pipeline with a
human stage. **[docs/human-in-the-loop.md](docs/human-in-the-loop.md)** covers
which to reach for, with a local walkthrough.

## Observability

Structured logging (loguru → Cloud Logging) and distributed tracing
(OpenTelemetry → Cloud Trace) are wired for every agent. Trace context
propagates across A2A hops, so a single trace spans the whole cluster —
`orchestrator → research → generate_content` in one waterfall — and every log
line is correlated to its trace.

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to redirect traces to an OpenTelemetry
Collector (Grafana Tempo, Jaeger, Datadog, ...) instead. See
[GKE.md](GKE.md#observability-logs--distributed-tracing-across-a2a).

## A2A Inspector

This agent supports the [A2A Protocol](https://a2a-protocol.org/). Use the [A2A Inspector](https://github.com/a2aproject/a2a-inspector) to test interoperability.
Each agent publishes its card at `/a2a/app/.well-known/agent-card.json`.
