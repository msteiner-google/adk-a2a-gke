# bnpp-gke

Multi-agent system for GKE — an A2A orchestrator + specialist workers.
Generated with `agents-cli` version `1.2.1` (the `gke` variant of `agentic-template`).

A planner/orchestrator agent breaks a request into sub-tasks and delegates them
to specialist worker agents, each running as its own Kubernetes Service and
reached over the [A2A protocol](https://a2a-protocol.org/).

```
                       ┌───────────────────┐
   user ───────────▶   │   orchestrator    │  planner
                       │  Deployment/Svc   │
                       └─────────┬─────────┘
              A2A (RemoteA2aAgent, well-known agent card)
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
        ┌────────────────┐              ┌────────────────┐
        │    research    │              │      math      │  specialists
        │ Deployment/Svc │              │ Deployment/Svc │
        └────────────────┘              └────────────────┘
```

**One image runs every agent** — the `AGENT_NAME` environment variable selects
which one a given process becomes at startup.

📖 **[GKE.md](GKE.md)** is the full guide (architecture, service discovery,
session backends, observability, deployment).

## Project Structure

```
bnpp-gke/
├── app/
│   ├── agent.py                # Entry point: picks this process's agent by AGENT_NAME
│   ├── fast_api_app.py         # FastAPI serving app (+ inbound A2A trace extraction)
│   ├── agents/                 # WHO the agents are
│   │   ├── __init__.py         #   the registry (AGENTS + DEFAULT_AGENT)
│   │   ├── base.py             #   AgentSpec + the single build_agent()
│   │   ├── common.py           #   shared tools (remember / recall)
│   │   ├── orchestrator/       #   planner (declares peers)
│   │   ├── research/           #   specialist + web_search tool
│   │   └── math/               #   specialist + calculate tool
│   ├── cluster/                # The PLUMBING
│   │   ├── config.py           #   env -> ClusterConfig / peers
│   │   ├── resolver.py         #   peers -> RemoteA2aAgent (agent-card discovery)
│   │   ├── di.py               #   injector modules
│   │   └── session.py          #   pluggable session + memory backends
│   ├── shared/                 # Shared library (models, observability, secrets)
│   └── app_utils/              # Base-template serving/A2A helpers
├── infra/
│   ├── terraform/              # GKE Autopilot + Workload Identity + Artifact Registry
│   └── kustomize/              # Namespace, ServiceAccount, ConfigMap, Deployments
├── deployment/                 # Base-template single-service CI/CD Terraform
├── tests/                      # unit / integration / eval
├── AGENTS.md                   # AI-assisted development guide
└── pyproject.toml              # Project dependencies
```

> 💡 **Tip:** Use [Antigravity CLI](https://antigravity.google/) for AI-assisted development - project context is pre-configured in `AGENTS.md`.

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager (used for all dependency management in this project) - [Install](https://docs.astral.sh/uv/getting-started/installation/) ([add packages](https://docs.astral.sh/uv/concepts/dependencies/) with `uv add <package>`)
- **agents-cli**: Agents CLI - Install with `uv tool install google-agents-cli`
- **Google Cloud SDK**: For GCP services - [Install](https://cloud.google.com/sdk/docs/install)
- For deploying: **kubectl**, **terraform**, and a container builder
  (**podman** or docker)

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

## 🛠️ Project Management

| Command | What It Does |
|---------|--------------|
| `agents-cli scaffold enhance` | Add CI/CD pipelines and Terraform infrastructure |
| `agents-cli infra cicd` | One-command setup of entire CI/CD pipeline + infrastructure |
| `agents-cli scaffold upgrade` | Auto-upgrade to latest version while preserving customizations |

---

## Development

Each agent is a declarative `AgentSpec` in `app/agents/<name>/agent.py`,
registered in `app/agents/__init__.py`. There is no special orchestrator class —
the orchestrator is simply the agent whose spec declares `peers`.

**To add an agent:**

1. Create `app/agents/<name>/agent.py` exposing a `SPEC = AgentSpec(...)`
   (agent-specific tools go in `app/agents/<name>/tools.py`).
2. Register it in `app/agents/__init__.py`.
3. Copy a Deployment/Service pair in `infra/kustomize/base/workers.yaml`.
4. Add the name to another agent's `AgentSpec.peers` if it should be delegated to.

Use a single lowercase word valid as **both** a Python identifier and a
Kubernetes DNS label (e.g. `research`, `math`) — the Service name must equal the
agent name for peer discovery to resolve.

## Deployment

This project deploys as a **multi-agent cluster** via `infra/` (Terraform +
Kustomize), not via `agents-cli deploy`. See **[GKE.md](GKE.md)** for the full
walkthrough, and
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

# 2. Build and push the image (podman shown; docker works the same)
REPO=$(terraform output -raw artifact_registry_repo)
podman login -u oauth2accesstoken -p "$(gcloud auth print-access-token)" "${REPO%%/*}"
podman build --platform linux/amd64 -t "$REPO/agent:latest" .
podman push "$REPO/agent:latest"

# 3. Fill the two placeholders, then deploy
kubectl apply -k infra/kustomize/overlays/dev
kubectl -n agents get pods,svc
kubectl -n agents port-forward svc/orchestrator 8080:80
```

> ⚠️ On Apple Silicon, `--platform linux/amd64` is **required** — the GKE
> Autopilot nodes these manifests target are amd64, and an arm64 image fails
> there with `exec format error`.

The project-specific values to fill before step 3:

- `infra/kustomize/base/serviceaccount.yaml` → the GSA email
  (`terraform output google_service_account_email`)
- `infra/kustomize/overlays/dev/kustomization.yaml` → the image repo
  (`terraform output artifact_registry_repo`)
- `infra/kustomize/base/configmap.yaml` → `OTEL_RESOURCE_ATTRIBUTES:
  "gcp.project_id=<project>"`. Not cosmetic: Cloud Trace rejects every span
  batch whose resource lacks it, so tracing fails silently if it's stale.

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
