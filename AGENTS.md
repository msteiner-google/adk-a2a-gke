# AGENTS.md — bnpp-gke

Context for coding agents working in this repo. Read this first; it should
remove the need to re-scan the tree.

## What this project is

A **multi-agent system (MAS) for GKE**: a planner/orchestrator agent that
delegates sub-tasks to specialist worker agents, each deployed as its own
Kubernetes Deployment/Service and reached over the **A2A** protocol.

Built on Google **ADK** (`google-adk>=2.2.0`) and the **a2a** SDK, deployed to
GKE Autopilot with Terraform + Kustomize.

```
                       ┌───────────────────┐
   user ───────────▶   │   orchestrator    │  planner, tier=capable
                       │  Deployment/Svc   │
                       └─────────┬─────────┘
              A2A (RemoteA2aAgent, well-known agent card)
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
        ┌────────────────┐              ┌────────────────┐
        │    research    │              │      math      │  leaf specialists
        │ Deployment/Svc │              │ Deployment/Svc │  tier=balanced/fast
        └────────────────┘              └────────────────┘
```

**One container image runs every agent.** `AGENT_NAME` selects which agent from
the registry this process becomes at startup. There is no orchestrator-specific
code path.

## Verified commands

All of these were run in this repo and pass as of the last update to this file.

```bash
# Unit tests — 191 passed. The GEMINI_*_MODEL pins are MANDATORY for hermeticity
# (see "Importing app hits the network" below).
GEMINI_FAST_MODEL=gemini-2.5-flash-lite \
GEMINI_BALANCED_MODEL=gemini-2.5-flash \
GEMINI_CAPABLE_MODEL=gemini-2.5-pro \
GEMINI_EMBEDDING_MODEL=gemini-embedding-001 \
  uv run pytest tests/unit app/shared/tests -q
```

```bash
agents-cli lint        # ruff check + ruff format --check + codespell + ty check
agents-cli lint --skip-ty
```

```bash
# basedpyright — the editor LSP type checker, and a second opinion to ty.
# Clean (0 diagnostics) as of the last update to this file. NOT part of
# `agents-cli lint`; ty remains the gate.
uv run basedpyright
```

Other commands (require a server / GCP creds, not part of the fast loop):

| Command | Purpose |
| --- | --- |
| `uv sync` | Install deps (or `agents-cli install`) |
| `agents-cli playground` | Interactive local UI |
| `uv run adk web` | ADK dev UI (becomes `DEFAULT_AGENT` unless `AGENT_NAME` set) |
| `uv run uvicorn app.fast_api_app:app --port 8000` | Serve directly |
| `uv run pytest tests/integration` | Needs a running server |
| `agents-cli eval generate` / `grade` / `compare` / `analyze` / `optimize` | Eval loop; needs GCP creds |

Tooling present on this machine: `agents-cli`, `uv`, `kubectl`, `terraform`,
`gcloud`, and **`podman` 6.0.0** (machine `podman-machine-default` running).
**`docker` is NOT installed** — use `podman` for image builds, and read the
`--platform` warning in "Deploy to GKE" before building.

## File map

Read the module docstrings; they are thorough. This is the index.

### Agent code — `app/`

| Path | Role |
| --- | --- |
| `app/agent.py` | Entry point. Calls `configure_observability()`, builds the `Injector`, selects this process's agent by `AGENT_NAME`, exports `root_agent` and `app = App(name="app")`. |
| `app/agents/__init__.py` | **The registry.** `AGENTS: dict[str, AgentSpec]` + `DEFAULT_AGENT`. Single source of truth for which agents exist. |
| `app/agents/base.py` | `AgentSpec` (frozen dataclass: name, description, instruction, tier, tools, peers) and the single `build_agent()`. `TIERS = ("fast","balanced","capable")`. |
| `app/agents/common.py` | `remember` / `recall` tools — shared session-state context that propagates across A2A hops. State keys prefixed `shared:`. |
| `app/agents/orchestrator/agent.py` | `SPEC` with `peers=("research","math")`, `tier="capable"`. |
| `app/agents/research/agent.py` + `tools.py` | Leaf agent, `tier="balanced"`, tool `web_search`. |
| `app/agents/math/agent.py` + `tools.py` | Leaf agent, `tier="fast"`, tool `calculate` (AST-based, rejects non-arithmetic). |
| `app/cluster/config.py` | **Pure stdlib** (no ADK/genai imports). `PeerSpec`, `ClusterConfig.from_env()`, `service_dns_url()`. Parses `AGENT_NAME` / `A2A_*`. |
| `app/cluster/resolver.py` | `AgentResolver` — turns peers into `RemoteA2aAgent`s pointed at their well-known agent cards. No network I/O at construction (ADK resolves cards lazily). |
| `app/cluster/di.py` | `ClusterModule` (config + resolver) and `SessionModule` (`Database`, session + memory + artifact services, A2A `TaskStore`). |
| `app/cluster/session.py` | `build_session_service()` / `build_memory_service()` — backend selected by env. |
| `app/cluster/artifacts.py` | `build_artifact_service()` — the shared `CloudPathArtifactService` when `ARTIFACT_STORAGE_URI` is set, else in-memory. Same URI for every agent on purpose (artifacts are keyed by the ADK app name `app`, not `AGENT_NAME`). |
| `app/cluster/db.py` | `DatabaseConfig` + `Database`: the one `AsyncEngine` per pod. AlloyDB via the connector (IAM auth) or a plain DSN. `get_database()` is the process-wide singleton so every consumer shares one pool. |
| `app/cluster/tasks.py` | `build_task_store()` — A2A `TaskStore`: in-memory, or the a2a SDK's `DatabaseTaskStore` on the shared engine. |
| `app/cluster/bootstrap.py` | `python -m app.cluster.bootstrap` — creates the database. Exists because the Terraform provider has no `google_alloydb_database` resource. |
| `app/migrations/` | Alembic (`alembic.ini`, `env.py`, `versions/`). Lives under `app/` on purpose — see gotchas. |
| `app/fast_api_app.py` | Serving app. Wires the injector's services into the ADK Runner and adds `instrument_fastapi_app(app)` (see gotchas). |
| `app/app_utils/` | Low-level serving/A2A plumbing (`a2a.py`, `services.py`, `typing.py`). Stable boilerplate; prefer changing `app/fast_api_app.py` instead. |
| `app/shared/` | Cross-cutting library: models, observability, secrets, artifacts (see "Layering"). |

### Shared library — `app/shared/`

| Module | Public surface |
| --- | --- |
| `config.py` | `ModelModule` (injector Module), `Models` bundle (`.fast` / `.balanced` / `.capable`). `DEFAULT_LOCATION = "global"`. |
| `project_types.py` | `NewType` DI keys: `FastModel`, `BalancedModel`, `CapableModel`, `EmbeddingModel`, `GoogleCloudProject`, `GoogleCloudLocation`. |
| `model_catalog.py` / `model_selection.py` / `model_factory.py` | Live Vertex catalog listing, "latest in family" selection, `build_model()`. |
| `observability.py` | `configure_observability()` (idempotent bootstrap), `ObservabilityModule`. |
| `telemetry.py` | `configure_tracing()`, `instrument_fastapi_app()`, `get_tracer()`, `current_trace_ids()`, `select_exporter_kind()`. |
| `logging.py` | `configure_logging()` — loguru, JSON for Cloud Logging, trace-correlated. |
| `secrets.py` | `Secrets`, `SecretResolver`, `SecretModule`. Reuses `ModelModule`'s `GoogleCloudProject` — install both together. |
| `artifacts.py` | `CloudPathArtifactService`, `ArtifactStorageModule` (self-contained; needs `ARTIFACT_STORAGE_URI` or a constructor override, else raises). |
| `tools.py` | `echo` (demo tool). |
| `tests/` | Shared-library tests; run as `app.shared.tests.*`. |

### Tests — `tests/`

- `tests/unit/` — `test_agents.py`, `test_cluster_config.py`, `test_cluster_resolver.py`,
  `test_cluster_session.py`, `test_cluster_artifacts.py`, `test_cluster_db.py`,
  `test_cluster_tasks.py`, `test_migrations.py`, `test_a2a_tracing.py`,
  `test_dummy.py`. Hermetic. `test_cluster_artifacts.py` exercises the
  cloudpathlib service against a `tmp_path` (a non-URI path yields a local
  `pathlib.Path`), so no bucket or credentials are involved.
  `test_migrations.py` renders the Alembic migrations **offline** (no database)
  and diffs them against ADK's and the a2a SDK's own metadata — it is the guard
  that catches a library adding a column.
- `tests/integration/` — `test_agent.py`, `test_server_e2e.py`. Needs a server.
- `tests/eval/` — `eval_config.yaml`, `response_quality.py`, `datasets/`. Needs GCP creds.

No `testpaths` is configured, so bare `uv run pytest` auto-discovers
`app/shared/tests/` too.

### Infrastructure

**`infra/` holds everything needed to provision and deploy the cluster** —
Terraform for the Google Cloud side, Kustomize for the Kubernetes side.

| Path | Contents |
| --- | --- |
| `infra/terraform/main.tf` | GKE Autopilot cluster, Artifact Registry, APIs, **one GSA per agent** + a migrator GSA, IAM roles, Workload Identity bindings. |
| `infra/terraform/alloydb.tf` | AlloyDB cluster + instance (`c4a-highmem-1`, 1 vCPU / 8 GB), private-services-access peering, IAM database users. |
| `infra/terraform/artifacts.tf` | GCS bucket for ADK artifacts (uniform access, public-access prevention, 30-day lifecycle) + `roles/storage.objectUser` per agent GSA on that bucket. One shared bucket by design — see the header comment. |
| `infra/kustomize/base/` | `namespace.yaml`, `serviceaccounts.yaml`, `configmap.yaml`, `networkpolicy.yaml`, `migrate-job.yaml`, `orchestrator.yaml`, `workers.yaml`. |
| `infra/kustomize/overlays/dev/` | Image name/tag overlay. |
| `Dockerfile` | The single image every agent runs. Copies only `./app`. |

Key vars in `variables.tf`: `project_id` (required), `region` (default
`us-central1`), `agents` (default `["orchestrator","research","math"]` — must
match `app/agents/`), `service_account_prefix` (`agent`), `alloydb_machine_type`
(`c4a-highmem-1`), `alloydb_database` (`agents`), `artifact_bucket_name`
(default `<project_id>-agent-artifacts`), `artifact_retention_days` (30).

**Placeholders to fill before `kubectl apply`** (all from
`terraform output -json kustomize_values`):
1. `infra/kustomize/base/serviceaccounts.yaml` → the
   `iam.gke.io/gcp-service-account` annotation on each of the 4 ServiceAccounts.
2. `infra/kustomize/base/configmap.yaml` → `ALLOYDB_INSTANCE_URI`,
   `ARTIFACT_STORAGE_URI`.
3. `orchestrator.yaml` / `workers.yaml` → `ALLOYDB_IAM_USER` per agent.
4. `migrate-job.yaml` → `ALLOYDB_IAM_USER`, `AGENT_ROLE_SUFFIX`, `MIGRATE_AGENTS`.
5. `infra/kustomize/overlays/dev/kustomization.yaml` → image `newName`.

**Run the migration Job before the agents settle.** They have no `CREATE`
privilege by design, so they crash-loop until the schema exists:
```bash
kubectl -n agents delete job/agent-migrate --ignore-not-found  # spec is immutable
kubectl apply -k infra/kustomize/overlays/dev
kubectl -n agents wait --for=condition=complete job/agent-migrate --timeout=10m
```

All Services listen on port **80** → `targetPort` **8080**. Four places must
agree if you change the port: the `Dockerfile` `CMD` (which hardcodes
`--port 8080`, it does *not* read `$PORT`), `EXPOSE`, each Deployment's
`containerPort` + Service `targetPort`, and `PORT: "8080"` in the ConfigMap.
Orchestrator Service is `ClusterIP` — switch to `LoadBalancer`/Ingress to expose
it; workers stay internal.

## Architecture invariants

Violating these is how this codebase breaks. They are deliberate.

1. **All agents are equal — one spec, one builder.** Every agent is an
   `AgentSpec` built by the single `build_agent()`. The "orchestrator" is just
   the agent whose spec declares `peers`; `build_agent` attaches whatever peers
   the cluster config resolved (empty for a leaf). **Do not reintroduce a
   per-role builder or an orchestrator subclass.**
2. **`agents/` = who the agents are. `cluster/` = the plumbing.** Keep config,
   resolution, DI, and session backends out of `agents/`.
3. **`app/cluster/config.py` must NOT import `app.agents`.** It would cycle:
   `di → agents → agents.base → cluster.resolver → cluster.config`. That is why
   it keeps a plain `DEFAULT_AGENT_NAME = "orchestrator"` string mirroring
   `agents.DEFAULT_AGENT`. Keep those two in sync manually.
4. **Peers are declared in code, resolved by env.** Defaults live in
   `AgentSpec.peers`; `di.py` feeds them to `ClusterConfig.from_env(default_peers=...)`;
   `A2A_PEERS` overrides at deploy time.
5. **Agent name = folder name = `AgentSpec.name` = Kubernetes Service name =
   `AGENT_NAME` value.** ADK requires a valid Python identifier (no hyphens);
   K8s DNS labels forbid underscores. Use a single lowercase word (`research`,
   `math`). The Service name must equal the agent name or DNS resolution
   (`<name>.<namespace>.svc.cluster.local`) fails.
6. **`App(name=...)` must equal the agent directory** (`"app"`). It determines
   the A2A mount path `/a2a/app`. Renaming one without the other breaks peer
   discovery.

## Gotchas

These are real traps that have bitten this codebase.

- **`ToolContext` must be imported at RUNTIME, not under `TYPE_CHECKING`.**
  With `from __future__ import annotations`, ADK builds each tool's declaration
  via `typing.get_type_hints()`, which evaluates the annotation — a
  `TYPE_CHECKING`-only import raises `NameError: name 'ToolContext' is not
  defined` at request time, breaking *every* tool in the module. Import from
  `google.adk.tools.tool_context`, **not** the `google.adk.tools` re-export
  (`ty` rejects the latter). See the comment block in `app/agents/common.py:17`.
  Same class of bug applies to injector bindings (e.g. OTel `Tracer`): **any
  type a runtime framework reflects over must be imported at runtime.**

- **Importing `app` hits the network.** `app/agent.py` resolves model tiers at
  import time from the **live Vertex AI catalog** — there are no hardcoded model
  names, and a failed lookup **raises** rather than falling back. Unit tests
  import `app.*`, so always pin `GEMINI_*_MODEL` when running them. Agent-builder
  tests dodge the catalog entirely with a `cast(Models, SimpleNamespace(...))`
  fake.

- **The agent card lives at `/a2a/app/.well-known/agent-card.json`, not the
  service root.** A `PeerSpec.base_url` is the service **root**; the resolver
  appends `rpc_path` (`A2A_RPC_PATH`, default `/a2a/app`) + the well-known path.
  Pointing at the root's `/.well-known/...` 404s.

- **`APP_URL` must be set per agent.** It is the URL the agent advertises in its
  own card — what peers call. It defaults to `http://0.0.0.0:8000`, unreachable
  from other pods. The kustomize Deployments set it per role; set it too for
  local two-process runs.

- **`app/fast_api_app.py` does three things the stock ADK serving wiring does
  not** (listed in its module header): the `instrument_fastapi_app(app)` call,
  and the Runner taking the injector's *session* and *artifact* services instead
  of `app/app_utils/services.py`'s. `instrument_fastapi_app` extracts the W3C
  `traceparent` from inbound A2A requests; ADK's built-in propagation middleware
  only reads Google-Agent-Engine headers, so without it each pod starts a fresh
  trace instead of continuing the caller's. All three are load-bearing — don't
  simplify them away; `tests/unit/test_a2a_tracing.py` guards the tracing one.

- **`RemoteA2aAgent` is experimental** and emits a `UserWarning` on every
  construction. Expected noise in test output.

- **`SESSION_BACKEND=database` needs `SESSION_DB_URL`**; `vertex_ai` backends
  need `AGENT_ENGINE_ID`. Prefer `SESSION_BACKEND=alloydb` in the cluster, which
  reuses the shared engine from `app/cluster/db.py` instead of opening a second
  pool from a DSN.

- **There are TWO session services, and only one of them is wired.** The
  injector provides one (`app/agent.py`); `app/app_utils/services.py` provides
  another via `SESSION_SERVICE_URI = "shared://session"`. The Runner in
  `app/fast_api_app.py` is what actually matters — it now takes the injector's.
  When a database is configured, `fast_api_app.py` also re-registers the
  `shared` scheme so the ADK web routes resolve to the *same* instance.
  **If you change the serving layer, make sure it does not go back to
  `services.get_session_service()`** — that helper only understands a plain DSN,
  so agents would silently run on per-pod in-memory state while
  `SESSION_BACKEND=alloydb` suggests otherwise.

- **The same trap applies to artifacts.** `app/app_utils/services.py` registers
  `shared://artifact` → `get_artifact_service()`, which only knows about a GCS
  bucket in `LOGS_BUCKET_NAME` and otherwise hands back a per-pod in-memory
  store. The Runner takes the injector's `CloudPathArtifactService` instead, and
  `fast_api_app.py` re-registers the `shared` artifact scheme when
  `ARTIFACT_STORAGE_URI` is set so the ADK web upload/download routes hit the
  same instance.

- **Alembic lives under `app/migrations/`, not the repo root.** The `Dockerfile`
  copies only `./app`, so migrations anywhere else are invisible to the
  migration Job — which has to run the same image as the agents. Run it with
  `-c app/migrations/alembic.ini`.

- **Never let ADK or a2a create their own tables.** Both would call
  `create_all()` at startup — that races across replicas, needs DDL privileges
  the agent roles deliberately lack, and bypasses the migration history. ADK's
  `prepare_tables()` is safe *because* the tables already exist (it degrades to
  a reflection pass, verified against real Postgres); `DatabaseTaskStore` is
  constructed with `create_table=False`.

- **`adk_internal_metadata` must contain `schema_version='1'`.** ADK reads it at
  startup; if the table exists but the row is missing it raises "Schema version
  not found ... The database might be malformed." Migration `0001` seeds it.

- **JSONB for ADK, plain JSON for a2a.** ADK's `DynamicJSON` resolves to `JSONB`
  on PostgreSQL; a2a's `PydanticType` uses SQLAlchemy's generic `JSON`, which
  renders as `json`. The migrations differ accordingly — that is intentional,
  and matching each library exactly is what keeps the ORM's binding correct.

- **Alembic autogenerate is off.** The tables belong to two third-party
  libraries, so autogenerate would let a library upgrade rewrite production DDL
  unreviewed. `tests/unit/test_migrations.py` is the drift guard instead.

- **Offline mode cannot bind parameters.** `alembic upgrade --sql` renders
  statements without binding, so a `:param` placeholder ends up verbatim in the
  generated script. Inline controlled constants in migrations (see `0001`).

- **`agents-cli` uses `uv`.** Run Python as `uv run python ...`, never bare
  `python`.

- **Model 404s** are a location problem, not a model-name problem — fix
  `GOOGLE_CLOUD_LOCATION` (`global` usually works). **Never change the model
  unless explicitly asked.**

- **Terraform Error 409** (already exists) → `terraform import`, don't retry
  creation.

## Code style

`ruff.toml` and `ty.toml` at the repo root are **standalone and own the policy**.
A standalone file takes precedence over an equivalent `[tool.ruff]` / `[tool.ty]`
section in `pyproject.toml`, so those sections are deliberately absent — keeping
them would be dead config that silently contradicts the real settings. Put lint
and type-check settings in `ruff.toml` / `ty.toml` / `pyrightconfig.json`, never
in `pyproject.toml`.

`pyrightconfig.json` follows the same pattern for **basedpyright** (the nvim LSP;
it also beats any `[tool.basedpyright]` section). Three things to know:

- It sets `typeCheckingMode = "standard"`, **not** basedpyright's default
  `"recommended"`. `"recommended"` turns on the based-only Any-hunting rules
  (`reportAny`, `reportUnknown*`, `reportUnusedCallResult`,
  `reportImplicitStringConcatenation`) which produced **481 diagnostics**, ~85%
  of them against ADK / injector / a2a-SDK surfaces that simply are not typed.
  Each mute in the file carries its reason — read those before re-enabling one.
- It is **stricter than ty in two places**, on purpose: imports and attribute
  access stay checked in `app/` (ty ignores `unresolved-import` and
  `unresolved-attribute` globally) because they produce zero noise here.
- Per-directory relaxations live in `executionEnvironments`, mirroring
  `ruff.toml`'s per-file-ignores: `app/app_utils` and `app/shared` (low-level
  plumbing held to a looser bar) and `tests` (fakes cast to real types,
  internals poked deliberately).

- **Line length 88.** `target-version = "py314"` / ty `python-version = "3.14"`
  / basedpyright `pythonVersion "3.14"`.
- **The project pins exactly one Python version: 3.14.** `requires-python` is
  `>=3.14,<3.15`, the Dockerfile is `python:3.14-slim`, and the dev venv is
  3.14 — there is no older interpreter to stay compatible with, so the full
  3.14 stdlib and syntax are fair game (`typing.override`, PEP 695 type params
  included). This replaces the old "floor, not runtime" rule; the codebase no
  longer supports a range.
  - Changing the version means changing **five** files together:
    `pyproject.toml` (`requires-python`), `Dockerfile` (`FROM python:`),
    `ruff.toml`, `ty.toml`, `pyrightconfig.json`.
    `tests/unit/test_python_version.py` fails if any of them drift apart —
    a quiet failure mode otherwise, since nothing else cross-checks them.
  - **`app/shared/**` is exempt from `UP035`.** That package is deliberately
    kept portable to Python 3.11 so it can be dropped into other services, so
    it imports `override` from `typing_extensions` rather than `typing`. Only
    "outdated" by this project's stricter pin; leave it alone.
- **Docstrings required** (`D` selected, Google convention). Summary on the
  first line, no blank line before a class docstring.
- Rule families: `E W F I B C4 UP RUF SIM N D PTH RET ARG TID`.
  Exempt: `app/app_utils/**`, `app/fast_api_app.py`, `**/tests/**`.
- **No parent-relative imports across subpackages** (`TID252`). From
  `app/cluster/*` or `app/agents/*`, import absolutely: `from app.agents.base
  import AgentSpec`. Only `app/agent.py` (at the `app/` root) uses single-dot
  relatives. Tests are exempt.
- **`ty` catches real type errors** — only `unresolved-import`,
  `unresolved-attribute`, `possibly-missing-attribute` are silenced. Consequences:
  - `injector.Injector.get(SomeNewType)` does **not** type-check (`get()` wants a
    real `type`). Prefer `@inject` constructor injection or resolve a concrete
    class — this is exactly why `Models` exists as a real class wrapping the three
    `NewType` keys.
  - A `NewType` base must be a real class, so DI keys can't be `str | None`;
    the empty string is the "unset" sentinel.
  - Duck-typed fakes in tests must be `cast(...)` to the real type.
  - Dispatch dicts need explicit annotations, e.g.
    `dict[type[ast.operator], Callable[[float, float], float]]`.

## Environment variables

### Cluster / A2A (`app/cluster/config.py`)

| Var | Default | Meaning |
| --- | --- | --- |
| `AGENT_NAME` | `orchestrator` | Which registered agent this process becomes |
| `A2A_PEERS` | *(agent's `AgentSpec.peers`)* | Comma-separated `name` or `name=url`; url is a service **root** |
| `A2A_NAMESPACE` | `agents` | K8s namespace for DNS-derived peer URLs |
| `A2A_CLUSTER_DOMAIN` | `svc.cluster.local` | Cluster DNS domain |
| `A2A_PEER_SCHEME` | `http` | Scheme for derived URLs |
| `A2A_PEER_PORT` | `80` | Port for derived URLs (default port omitted from URL) |
| `A2A_RPC_PATH` | `/a2a/app` | Where the serving layer mounts RPC + card |
| `APP_URL` | `http://0.0.0.0:8000` | Base URL this agent advertises in its own card |

### Models (`app/shared/config.py`)

| Var | Default |
| --- | --- |
| `GOOGLE_CLOUD_PROJECT` | ADC discovery |
| `GOOGLE_CLOUD_LOCATION` | `global` |
| `GEMINI_FAST_MODEL` | latest `flash-lite` from live catalog |
| `GEMINI_BALANCED_MODEL` | latest `flash` |
| `GEMINI_CAPABLE_MODEL` | latest `pro` |
| `GEMINI_EMBEDDING_MODEL` | best embedding model |

### Session / memory / tasks (`app/cluster/session.py`, `app/cluster/tasks.py`)

| Var | Default | Options |
| --- | --- | --- |
| `SESSION_BACKEND` | `in_memory` | `alloydb` (shared engine), `database` (+`SESSION_DB_URL`), `vertex_ai` (+`AGENT_ENGINE_ID`) |
| `MEMORY_BACKEND` | `in_memory` | `vertex_ai` (+`AGENT_ENGINE_ID`) |
| `TASK_STORE_BACKEND` | `in_memory` | `database` (shared engine). In-memory is per-pod, so `tasks/get` breaks past 1 replica. |

### Artifacts (`app/cluster/artifacts.py`)

| Var | Default | Meaning |
| --- | --- | --- |
| `ARTIFACT_STORAGE_URI` | *(unset → in-memory)* | Base path for `CloudPathArtifactService`: `gs://bucket/prefix`, `s3://…`, `az://…`, or a local dir. No `ARTIFACT_BACKEND` switch — the scheme *is* the backend. Set the **same** value for every agent. |

### Database (`app/cluster/db.py`)

| Var | Default | Meaning |
| --- | --- | --- |
| `DB_BACKEND` | `none` | `alloydb` (IAM auth via the connector) or `url` (plain DSN) |
| `ALLOYDB_INSTANCE_URI` | — | `projects/P/locations/L/clusters/C/instances/I` |
| `ALLOYDB_IAM_USER` | — | GSA email **minus** `.gserviceaccount.com` |
| `ALLOYDB_IP_TYPE` | `PRIVATE` | `PRIVATE`, `PUBLIC`, or `PSC` |
| `DB_NAME` | `agents` | Database holding every agent's schema |
| `DB_SCHEMA` | *(`AGENT_NAME`)* | Per-agent schema, applied as `search_path`. Leave unset. |
| `DB_URL` | — | SQLAlchemy async URL for the `url` backend |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `2` | Per-pod pool; small because the instance has 1 vCPU |
| `DB_AGENT_ROLE` | — | Migration-time only: the role revision `0003` grants on the schema |

### Observability (`app/shared/`)

| Var | Default | Effect |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | loguru + stdlib level |
| `LOG_FORMAT` | `json` | `json` (Cloud Logging) or `console` (local) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)* | Redirect traces to an OTLP collector instead of Cloud Trace |
| `OTEL_SERVICE_NAME` | `AGENT_NAME` | Trace service name |

Local `.env` currently sets `GOOGLE_GENAI_USE_VERTEXAI=true`,
`GOOGLE_CLOUD_PROJECT=msteiner-kubeflow`, `GOOGLE_CLOUD_LOCATION=global`.

## Common tasks

### Add an agent

Full walkthrough, checklist, and troubleshooting table:
[`docs/adding-an-agent.md`](docs/adding-an-agent.md). The short version:

1. `app/agents/<name>/` with `__init__.py` (**needs a docstring** — `D104`),
   `tools.py`, and `agent.py` exposing `SPEC = AgentSpec(...)`. Cross-agent tools
   go in `app/agents/common.py`.
2. Register it in `app/agents/__init__.py` (`AGENTS`).
3. If an existing agent should delegate to it, add the name to that agent's
   `AgentSpec.peers`.
4. Update **two** tests in `tests/unit/test_agents.py`:
   `test_registry_lists_expected_agents` and
   `test_orchestrator_declares_peers_others_do_not`.
5. Cluster only — five files:
   - `infra/terraform/variables.tf` → add to `var.agents`, then `terraform apply`
     (creates the GSA, IAM roles, WI binding, and AlloyDB user via `for_each`).
   - `serviceaccounts.yaml` → KSA `agent-<name>` + its GSA annotation.
   - `workers.yaml` → copy a Deployment/Service pair; set `AGENT_NAME`,
     `APP_URL`, `ALLOYDB_IAM_USER`, `serviceAccountName`.
   - `networkpolicy.yaml` → add to the workers allow-list. **Easy to miss:**
     omitting it leaves the agent with no ingress at all, and delegation fails
     as a timeout rather than an error.
   - `migrate-job.yaml` → add to `MIGRATE_AGENTS`.

No new migration and no new database are needed: every agent gets the same
tables in its own schema, and `DB_SCHEMA` defaults to `AGENT_NAME`.

Name it a single lowercase word (invariant 5).

### Add a tool

Plain function with a Google-style docstring (ADK derives the declaration from
signature + docstring). If it takes `tool_context: ToolContext`, import
`ToolContext` at runtime from `google.adk.tools.tool_context`. Add it to the
spec's `tools` tuple.

### Run two agents locally to exercise A2A

```bash
# Terminal 1 — the specialist
AGENT_NAME=math APP_URL=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8091

# Terminal 2 — the orchestrator delegating to it
AGENT_NAME=orchestrator A2A_PEERS=math=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8090
```

### Deploy to GKE

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform init && terraform apply
eval "$(terraform output -raw get_credentials_command)"

REPO=$(terraform output -raw artifact_registry_repo)

# Auth: no docker on this machine, so log podman straight in (this avoids the
# gcloud docker-credential helper entirely).
podman login -u oauth2accesstoken \
  -p "$(gcloud auth print-access-token)" "${REPO%%/*}"

# --platform linux/amd64 is REQUIRED here — see the warning below.
podman build --platform linux/amd64 -t "$REPO/agent:latest" .
podman push "$REPO/agent:latest"

# fill the two placeholders, then:
kubectl apply -k infra/kustomize/overlays/dev
kubectl -n agents port-forward svc/orchestrator 8080:80
```

**Always pass `--platform linux/amd64` when building here.** This is an Apple
Silicon machine and the podman VM is **arm64**, but the GKE Autopilot nodes these
manifests target are **amd64** (nothing sets a `kubernetes.io/arch: arm64`
nodeSelector). A native build produces an arm64 image whose pods fail on the
cluster with `exec format error` — which looks like an app bug, not a build bug.
The base image (`python:3.14-slim`) is multi-arch, so the cross-build works fine;
it is just slower under emulation.

**Deploying requires explicit human approval. Never run `terraform apply` or
`kubectl apply` without being asked.**

## Development workflow

`agents-cli` is the task runner for linting and evaluation; everything else runs
through `uv`. Install it once with `uv tool install google-agents-cli`.

### Phases

1. **Understand requirements** — constraints and success criteria before any code.
2. **Build and implement** — agent logic in `app/agents/`; `agents-cli playground`
   for interactive testing; iterate on user feedback.
3. **The evaluation loop (main iteration phase)** — start with 1-2 eval cases, run
   `agents-cli eval generate`, then `agents-cli eval grade`, and iterate by making
   changes and rerunning both until satisfied. Expect 5-10+ iterations. Once you
   have a baseline, reach for `eval compare` (regression diffs), `eval analyze`
   (cluster failure modes), and `eval optimize` (auto-tune prompts).
4. **Pre-deployment checks** — the hermetic unit-test command at the top of this
   file, `agents-cli lint`, then `uv run pytest tests/integration` against a
   running server. Fix until green.
5. **Deploy** — build the image and apply the Kustomize overlay ("Deploy to GKE").
   **Requires explicit human approval**; only after the user confirms.

### Command reference

| Command | Purpose |
| --- | --- |
| `agents-cli playground` | Interactive local testing |
| `uv run pytest tests/unit tests/integration` | Run unit and integration tests |
| `agents-cli lint` | Check code quality (ruff, codespell, ty) |
| `agents-cli eval dataset synthesize` | Synthesize multi-turn eval scenarios |
| `agents-cli eval generate` | Run agent on eval dataset, produce traces |
| `agents-cli eval grade` | Run agent evaluations on the traces |
| `agents-cli eval compare` | Compare two grade-results files (regression check) |
| `agents-cli eval analyze` | Cluster failure modes from grade results |
| `agents-cli eval metric list` | List built-in metrics available in the SDK |
| `agents-cli eval optimize` | Auto-tune agent prompts using eval data |

## Layering — where code belongs

| Layer | Paths | What goes there |
| --- | --- | --- |
| Agents | `app/agents/**` | Who the agents are: specs, instructions, tools |
| Cluster plumbing | `app/cluster/**`, `app/migrations/**` | Config, peer resolution, DI, session/artifact/task backends, schema |
| Serving | `app/fast_api_app.py`, `app/app_utils/**` | The HTTP + A2A surface. `fast_api_app.py` is the seam to edit; `app_utils/` is stable low-level plumbing — wire things up in `fast_api_app.py` rather than changing it |
| Shared library | `app/shared/**` | Cross-cutting, project-agnostic utilities (models, telemetry, logging, secrets, artifacts) |
| Infrastructure | `infra/**`, `Dockerfile` | Terraform, Kustomize, the container image |
| Tooling config | `pyproject.toml`, `ruff.toml`, `ty.toml`, `pyrightconfig.json` | See "Code style" |

Three rules worth stating outright:

- **`app/shared/**` sits at the bottom of the dependency graph.** It must not
  import from `app.agents` or `app.cluster`, and it stays portable to Python
  3.11 so it can be reused by other services. Project-specific logic belongs in
  `app/agents/` or `app/cluster/`.
- **`pyproject.toml` is for dependencies and packaging only.** Lint and
  type-check policy lives in the standalone config files. One thing there is
  easy to mistake for cruft: `basedpyright` in the `lint` extra. It is not run
  by `agents-cli lint`, but it must be installed for the nvim LSP to start —
  leave it.
- **`app/fast_api_app.py` looks like boilerplate and is not.** See the gotcha
  above about its three load-bearing deviations from the stock ADK wiring.

## Operational rules

- **Preserve surrounding code.** Only modify what the request targets — keep
  config values (e.g. `model`), comments, and formatting intact.
- **Never change the model** unless explicitly asked. A model 404 is a
  `GOOGLE_CLOUD_LOCATION` problem (use `global`), not a model-name problem.
- **ADK tool imports: import the tool instance, not the module** —
  `from google.adk.tools.load_web_page import load_web_page`. (Same shape as the
  `ToolContext` rule in Gotchas.)
- **Run Python with `uv`**: `uv run python script.py`; `agents-cli install` first.
- **Stop after 3 identical failures.** Fix the root cause instead of retrying.
- **Terraform Error 409** → `terraform import`, don't retry creation.
- `GKE.md` and `README.md` are the human-facing docs; keep them in sync when
  changing architecture or commands.
