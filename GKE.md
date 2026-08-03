# Multi-agent system on GKE

This project is a **cloud-native, multi-agent system** designed to run on Google
Kubernetes Engine (GKE). A planner/orchestrator agent breaks a request into
sub-tasks and delegates them to specialist worker agents, each running as its own
Kubernetes Service and reached over the **A2A** protocol.

```
                       ┌───────────────────┐
   user ───────────▶   │   orchestrator    │   (planner, models.capable)
                       │  Deployment/Svc   │
                       └─────────┬─────────┘
              A2A (RemoteA2aAgent, well-known agent card)
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
        ┌────────────────┐              ┌────────────────┐
        │    research    │              │      math      │   specialists
        │ Deployment/Svc │              │ Deployment/Svc │   (workers)
        └────────────────┘              └────────────────┘
```

## How it maps to the requirements

| Requirement | Where it lives |
| --- | --- |
| Multi-agent runtime architecture | One image, agent-selected at startup (`AGENT_NAME`) — `app/agent.py` |
| Uniform agent model | Every agent is an `AgentSpec` built by one `build_agent` — `app/agents/base.py` |
| Orchestration / planner layer | `app/agents/orchestrator/agent.py` (an agent whose spec declares peers; delegates via ADK agent transfer) |
| Agent-to-agent communication | `app/cluster/resolver.py` builds `RemoteA2aAgent`s from peer agent cards |
| Context sharing & propagation | `remember`/`recall` tools write session state that travels across A2A hops — `app/agents/common.py` |
| Session & memory persistence | Pluggable, env-selectable backends — `app/cluster/session.py` + `SessionModule` |
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
  "math")`. It attaches them as remote sub-agents and delegates.
- `AGENT_NAME=research` / `AGENT_NAME=math` → a leaf agent (no peers), served over
  A2A so others can reach it.

The one asymmetry that remains — which agent is exposed to users — is a
**deployment** concern (which Service gets external ingress), not a code one.

Add an agent by:

1. Creating `app/agents/<name>/agent.py` exposing a `SPEC = AgentSpec(...)` (put
   agent-specific tools in `app/agents/<name>/tools.py`; shared context tools
   live in `app/agents/common.py`).
2. Registering it in `app/agents/__init__.py`.
3. Copying a Deployment/Service pair in `infra/kustomize/base/workers.yaml`.

Use a single-word name valid as **both** a Python identifier and a Kubernetes DNS
label (no hyphens/underscores). The Service name **must equal** the agent name so
peers can resolve it by DNS.

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
| `SESSION_BACKEND` | `in_memory` | `database` (`+ SESSION_DB_URL`, needs `google-adk[db]`), `vertex_ai` (`+ AGENT_ENGINE_ID`) |
| `MEMORY_BACKEND` | `in_memory` | `vertex_ai` (`+ AGENT_ENGINE_ID`) |

In-memory keeps local runs and tests hermetic. For a real cluster choose a
durable backend so state survives pod restarts and is shared across replicas —
set the same values in `infra/kustomize/base/configmap.yaml`.

> For the `database` backend, add the extra first: `uv add "google-adk[db]"`.

## Observability: logs + distributed tracing across A2A

The shared library (`app/shared/observability.py`) wires **structured logging**
(loguru) and **OpenTelemetry tracing** for every agent. `app/agent.py` calls
`configure_observability()` at startup, using this pod's `AGENT_NAME` as the
trace `service.name` so `orchestrator`, `research`, and `math` appear as distinct
services in Cloud Trace.

**One trace across the whole cluster.** ADK already emits spans (`invoke_agent`,
`execute_tool`, `generate_content`). This variant makes them span *pods*:

- **Outbound:** the httpx client is instrumented, so when the orchestrator calls
  a specialist over A2A it injects the W3C `traceparent` header.
- **Inbound:** `app/fast_api_app.py` (a thin overlay of the base serving file)
  calls `instrument_fastapi_app(app)`, so each agent **extracts** that header and
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
Cloud Trace *and* your own backend. This template ships no visualization stack;
it relies on Google Cloud's managed observability by default.

| Env | Default | Effect |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | Log level (loguru + stdlib) |
| `LOG_FORMAT` | `json` | `json` (Cloud Logging) or `console` (local) |
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
  uv run pytest tests/unit app/shared/tests -q
```

## Deploy to GKE

### 1. Provision infrastructure (Terraform)

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform init && terraform apply

eval "$(terraform output -raw get_credentials_command)"   # wire kubectl
terraform output google_service_account_email             # for step 3
terraform output artifact_registry_repo                   # for step 2
```

### 2. Build and push the image

```bash
REPO=$(cd infra/terraform && terraform output -raw artifact_registry_repo)
gcloud auth configure-docker "${REPO%%/*}"
docker build -t "$REPO/agent:latest" .
docker push "$REPO/agent:latest"
```

### 3. Deploy the agents (Kustomize)

Edit two placeholders first:

- `infra/kustomize/base/serviceaccount.yaml` → set the
  `iam.gke.io/gcp-service-account` annotation to the GSA email from step 1.
- `infra/kustomize/overlays/dev/kustomization.yaml` → set `images[].newName` to
  `$REPO/agent`.

```bash
kubectl apply -k infra/kustomize/overlays/dev
kubectl -n agents get pods,svc
```

### 4. Try it

```bash
kubectl -n agents port-forward svc/orchestrator 8080:80
# then POST to the orchestrator's A2A endpoint / ADK API on localhost:8080
```

To expose the orchestrator externally, switch its Service to `type: LoadBalancer`
or front it with an Ingress/Gateway (the workers stay internal `ClusterIP`).

## Notes & knobs

- **Container port:** the manifests use `8080` (`PORT` in the ConfigMap and
  `containerPort`/`targetPort`). If the base image serves on a different port,
  update those three together.
- **Least privilege:** the workers use internal `ClusterIP` Services; only the
  orchestrator needs external exposure.
- **Scaling:** bump `replicas` per role in the overlay; the resolver addresses
  Services (not pods), so load-balancing across replicas is automatic.
- **`infra/` vs `deployment/`:** `infra/` (this variant) provisions and deploys
  the *multi-agent cluster*. The base template's `deployment/` holds the standard
  single-service CI/CD Terraform; use `infra/` for the multi-agent topology.
