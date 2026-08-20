# AGENTS.md — mas-gke

Context for coding agents working in this repo. Read this first; it should
remove the need to re-scan the tree.

## What this project is

A **multi-agent system (MAS) for GKE**: an orchestrator agent that delegates
sub-tasks to specialist worker agents, each deployed as its own Kubernetes
Deployment/Service and reached over the **A2A** protocol.

Built on Google **ADK** (`google-adk>=2.2.0`) and the **a2a** SDK, deployed to
GKE Autopilot with Terraform + Kustomize.

```
                       ┌───────────────────┐
   user ───────────▶   │   orchestrator    │  tier=balanced
                       │  Deployment/Svc   │  holds the conversation
                       └─────────┬─────────┘
        A2A — one typed payload per call, never the transcript
      ┌──────────────┬───────────┼───────────────┐
      ▼              ▼           ▼               ▼
┌──────────┐   ┌──────────┐ ┌─────────┐    ┌──────────┐
│ research │   │  math    │ │ planner │    │  trades  │  specialists
│ balanced │   │ balanced │ │balanced │    │ capable  │
└──────────┘   └────┬─────┘ └─────────┘    └──────────┘
                    │ A2A                   gated: a query
                    ▼                       runs only after
              ┌──────────┐                  a human approves
              │ currency │  tier=fast
              └──────────┘  reached ONLY by math
```

**Delegation is a graph, not one level.** `math` declares `currency` as a peer,
so `orchestrator -> math -> currency` is an ordinary chain of A2A calls that
needed no new machinery: an agent that coordinates is simply one whose
`AgentSpec.peers` is non-empty, at any depth. The rules do not loosen further
down — `currency` sees a `CurrencyRequest` and nothing about the sum `math` is
computing, let alone the conversation the orchestrator is holding.

**The agents share no runtime state.** A specialist is reached with an explicit
request and sees nothing else — no conversation history, no shared session
state, no shared artifact handles. The *default* request is a single free-text
task; every agent here opts into a typed contract from
`app/agents/contracts.py` instead, which is a choice you can make per peer. Large
inputs travel as object-store references it reads itself, and an action needing
human sign-off is a proposal plus a durable case record rather than a suspended
invocation. See [`docs/design-decisions.md`](docs/design-decisions.md) for the
reasoning and the measurements behind it.

**Shared *code* is fine and encouraged** — `app/shared/**` is a library every
agent imports, and one another squad could vendor. The rule is about shared
*state*.

**One container image runs every agent.** `AGENT_NAME` selects which agent from
the registry this process becomes at startup. There is no orchestrator-specific
code path.

## Verified commands

All of these were run in this repo and pass as of the last update to this file.

```bash
# Unit tests — 343 passed. The GEMINI_*_MODEL pins are MANDATORY for hermeticity
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
| `make up` / `make down` | Start or stop all six agents locally, peers wired — including `math -> currency` (see the Makefile) |
| `make check` | Preflight: ADC, project, location, model reachability |
| `make demo` | Drive the approval flow end to end and assert it worked |
| `make image` / `make image TAG=x` | Build + push the image with Cloud Build (`cloudbuild.yaml`) |
| `uv sync` | Install deps (or `agents-cli install`) |
| `agents-cli playground` | Interactive local UI |
| `uv run adk web` | ADK dev UI (becomes `DEFAULT_AGENT` unless `AGENT_NAME` set) |
| `uv run uvicorn app.fast_api_app:app --port 8000` | Serve directly |
| `uv run pytest tests/integration` | Needs a running server |
| `agents-cli eval generate` / `grade` / `compare` / `analyze` / `optimize` | Eval loop; needs GCP creds |

Tooling this repo assumes on your PATH: `agents-cli`, `uv`, `kubectl`,
`terraform` (>= 1.9, see "Deploy to GKE") and `gcloud`. A local OCI image
builder (`docker` or `podman`) is **optional**: images are built by Cloud Build
(`make image`). If you do build locally, read the `--platform` warning in
"Deploy to GKE" first.

## File map

Read the module docstrings; they are thorough. This is the index.

### Agent code — `app/`

| Path | Role |
| --- | --- |
| `app/agent.py` | Entry point. Calls `configure_observability()`, builds the `Injector`, selects this process's agent by `AGENT_NAME`, exports `root_agent` and `app = App(name="app")`. |
| `app/agents/__init__.py` | **The registry.** `AGENTS: dict[str, AgentSpec]` + `DEFAULT_AGENT`. Single source of truth for which agents exist. |
| `app/agents/base.py` | `AgentSpec` (frozen dataclass: name, description, instruction, tier, tools, peers) and the single `build_agent()`. `TIERS = ("fast","balanced","capable")`. |
| `app/agents/contracts.py` | **The wire contracts.** One pydantic model per delegatable agent + `PAYLOADS`. The interface both a caller and a specialist agree on. Opt-in per agent: a peer with no model here is delegated to with a single free-text `task` instead (see its module docstring for the tiers). |
| `app/agents/documents.py` | `read_document` — reads a claim-check reference (`gs://…`) the caller passed in `document_refs`. |
| `app/agents/reporting.py` | Makes a proposal survive the A2A text boundary instead of being paraphrased. **Two** callbacks: `attach_structured_results` (after-model) folds the JSON into the model's own reply so a turn stays *one* message; `restate_structured_results` (after-agent) is the fallback, and emits only what that reply does not already carry. |
| `app/agents/orchestrator/agent.py` | `SPEC` with `peers=("research","math","planner","trades")`, `tier="balanced"`. |
| `app/agents/research/agent.py` + `tools.py` | Leaf agent, `tier="balanced"`, tool `web_search`. |
| `app/agents/math/agent.py` + `tools.py` | `tier="balanced"`, `peers=("currency",)`. Tools: `calculate` (AST-based, rejects non-arithmetic) + the gated `publish_result`. **Not a leaf** — it delegates conversions rather than applying a rate itself. |
| `app/agents/planner/` | Leaf agent, `tier="balanced"`, no tools. Drafts a plan and returns it for review. |
| `app/agents/currency/agent.py` + `tools.py` | Leaf agent, `tier="balanced"`, reached only by `math`. Hardcoded USD-anchored rate table; every pair is derived from it, so no triangle of rates can disagree with itself. **Refuses rather than guesses**: an ambiguous term ("dollars" is six currencies) returns `needs_input`, an amount over `LARGE_AMOUNT_USD` returns `needs_confirmation`, and both carry a question for the user. |
| `app/agents/trades/agent.py` + `tools.py` + `dataset.py` | Leaf agent, `tier="capable"`. Writes SQL against the `cymbal_investments.trade_capture_report` BigQuery public dataset; `run_trade_query` is the **second gated action**. `dataset.py` holds both the schema the instruction is built from and the one-table allow-list the validator enforces. |
| `app/cluster/peer_tool.py` | `PeerTool` — an `AgentTool` that gives a remote peer a **typed payload declaration**, so delegation sends an explicit request instead of the transcript. |
| `app/cluster/cases.py` | The approval case store (`pending → approved → executed`), both backends (in-memory / `approval_cases`), and the helpers that read a proposal back out of a peer's text reply. |
| `app/cluster/config.py` | **Pure stdlib** (no ADK/genai imports). `PeerSpec`, `ClusterConfig.from_env()`, `service_dns_url()`. Parses `AGENT_NAME` / `A2A_*`. |
| `app/cluster/resolver.py` | `AgentResolver` — turns peers into `RemoteA2aAgent`s pointed at their well-known agent cards. No network I/O at construction (ADK resolves cards lazily). |
| `app/cluster/di.py` | `ClusterModule` (config + resolver) and `SessionModule` (`Database`, session + memory + artifact services, A2A `TaskStore`). |
| `app/cluster/session.py` | `build_session_service()` / `build_memory_service()` — backend selected by env. |
| `app/cluster/artifacts.py` | `build_artifact_service()` — the shared `CloudPathArtifactService` when `ARTIFACT_STORAGE_URI` is set, else in-memory. Same URI for every agent on purpose (artifacts are keyed by the ADK app name `app`, not `AGENT_NAME`). |
| `app/cluster/db.py` | `DatabaseConfig` + `Database`: the one `AsyncEngine` per pod. AlloyDB via the connector (IAM auth) or a plain DSN. `build_database()` is a plain factory — the injector's `@singleton` is what makes every consumer share one pool. |
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
  that catches a library adding a column. It compares the **effective** schema
  at head, replaying `ALTER TABLE ... ADD/DROP COLUMN` over each `CREATE TABLE`,
  so a column added by a later revision counts as present. Reading only the
  `CREATE TABLE` would report every additive revision as missing drift forever.
- `tests/unit/test_cases.py` — the approval case state machine, run against
  **both** backends (in-memory and SQLAlchemy-on-SQLite), plus recovering a
  proposal from a peer's text reply.
- `tests/unit/test_peer_tool.py` — the D1 guards: a peer sees the payload and
  not the transcript, and session state never reaches the wire.
- `tests/unit/test_two_phase_approval.py` — propose causes no effect, a tampered
  proposal is refused, and the approval survives serialization.
- `tests/unit/test_trades.py` — the same properties for the gated *read*, plus
  the SQL validator (a keyword inside a string literal is not a keyword; a
  comment cannot hide a second statement) and the full propose → approve →
  re-send → `find_execution` round trip, including the negative case. The one
  function that would touch BigQuery is stubbed, which is why it exists.
- `tests/unit/test_currency.py` — the conversions, the no-arbitrage property the
  USD-anchored table exists to give, and the wiring that lets `math` reach
  `currency` as a `PeerTool` rather than a sub-agent.
- `tests/unit/test_documents.py` — claim-check reads against `tmp_path`.
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
| `infra/terraform/cloudbuild.tf` | The `agent-builder` GSA that Cloud Build runs as: repository-scoped `artifactregistry.writer`, `logging.logWriter`, `storage.objectUser`. Deliberately **not** Cloud Build's legacy default SA, which carries `roles/editor`. |
| `infra/kustomize/base/` | `namespace.yaml`, `serviceaccounts.yaml`, `configmap.yaml`, `networkpolicy.yaml`, `migrate-job.yaml`, `orchestrator.yaml`, `workers.yaml`. |
| `infra/kustomize/overlays/dev/` | Image name/tag overlay. |
| `Dockerfile` | The single image every agent runs. **Multi-stage**; the runtime stage carries the venv and `./app` and no `uv`. |
| `cloudbuild.yaml` + `.gcloudignore` | How the image is built (`make image`). The ignore file keeps the uploaded source to a few hundred KB. |

Key vars in `variables.tf`: `project_id` (required), `region` (default
`us-central1`), `agents` (default
`["orchestrator","research","math","planner","trades","currency"]` — must match
`app/agents/`), `agent_extra_iam_roles` (default
`{ trades = ["roles/bigquery.jobUser"] }` — the one agent that may start a query
job, and it holds `dataViewer` on nothing), `service_account_prefix` (`agent`),
`builder_service_account_id` (`agent-builder`), `alloydb_machine_type`
(`c4a-highmem-1`), `alloydb_database` (`agents`), `artifact_bucket_name`
(default `<project_id>-agent-artifacts`), `artifact_retention_days` (30).

**Placeholders to fill before `kubectl apply`** (all from
`terraform output -json kustomize_values`):
1. `infra/kustomize/base/serviceaccounts.yaml` → the
   `iam.gke.io/gcp-service-account` annotation on each of the 7 ServiceAccounts
   (six agents + the migrator).
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
   per-role builder or an orchestrator subclass.** This is also what makes depth
   free: `math` declares `currency` and becomes a caller as well as a callee,
   with no new type, no new code path and no change to `build_agent`.
2. **Peers are TOOLS, never `sub_agents`.** `build_agent` puts resolved peers in
   `tools` and leaves `sub_agents` empty. A peer in `sub_agents` is reached with
   `transfer_to_agent`, and `RemoteA2aAgent` then rebuilds the outbound message
   from the caller's **session events** — measured at ten message parts,
   including the user's phone number and a different specialist's answer, where
   the task needed one (`docs/design-decisions.md`, D1). A peer
   in `tools` gets only the payload the caller composed. This looks like a
   harmless wiring detail and is the most damaging thing in the repo to undo;
   `tests/unit/test_peer_tool.py` guards it.
3. **Every delegatable agent declares a contract in `app/agents/contracts.py` —
   a policy of this repo, not a requirement of the mechanism.** Contracts are
   **optional**. A peer with no entry in `PAYLOADS` is still reachable and still
   answers; its tool falls back to `UnknownPeerRequest` (a `case_id` plus one
   free-text `task`), which is the honest tier for a peer another squad owns
   whose schema lives in its own agent card. Declare the contract for an agent
   this repo owns: one free-text field is where a caller starts pasting the
   conversation back in, which erodes invariant 2. The three tiers — session
   transcript, free text, declared contract — are laid out in the
   `app/agents/contracts.py` module docstring.
   `test_agents.py::test_every_delegatable_agent_declares_a_contract` enforces
   the policy; it is a repo convention you could drop, unlike invariant 2.
4. **No implicit cross-agent context.** No shared session state, no shared
   artifact handles, no transcript forwarding. Anything a specialist needs is a
   field in its contract; large inputs travel as `document_refs` pointers, never
   as content. A tool that stashes state for another agent to pick up is the
   thing to reject in review — D3 measured that the transport does not carry it,
   so it cannot work as advertised.
5. **`agents/` = who the agents are. `cluster/` = the plumbing.** Keep config,
   resolution, DI, and session backends out of `agents/`.
6. **`app/cluster/config.py` must NOT import `app.agents`.** It would cycle:
   `di → agents → agents.base → cluster.resolver → cluster.config`. That is why
   it keeps a plain `DEFAULT_AGENT_NAME = "orchestrator"` string mirroring
   `agents.DEFAULT_AGENT`. Keep those two in sync manually. (`cluster/cases.py`
   *does* import `app.agents.contracts`, which is safe: contracts imports
   nothing from `cluster`.)
7. **Peers are declared in code, resolved by env.** Defaults live in
   `AgentSpec.peers`; `di.py` feeds them to `ClusterConfig.from_env(default_peers=...)`;
   `A2A_PEERS` overrides at deploy time.
8. **Agent name = folder name = `AgentSpec.name` = Kubernetes Service name =
   `AGENT_NAME` value.** ADK requires a valid Python identifier (no hyphens);
   K8s DNS labels forbid underscores. Use a single lowercase word (`research`,
   `math`). The Service name must equal the agent name or DNS resolution
   (`<name>.<namespace>.svc.cluster.local`) fails.
9. **`App(name=...)` must equal the agent directory** (`"app"`). It determines
   the A2A mount path `/a2a/app`. Renaming one without the other breaks peer
   discovery.
10. **A delegation edge in `AgentSpec.peers` needs a matching rule in
    `infra/kustomize/base/networkpolicy.yaml`.** The namespace is deny-by-default
    for ingress, so an edge added in code and missed there fails as a connection
    **timeout**, not an error — the pods stay green and delegation just hangs.
    `math -> currency` is the case that makes this more than bookkeeping:
    "workers accept traffic from the orchestrator" is no longer the whole
    policy, and `currency` accepts traffic from `math` and from nobody else,
    including the orchestrator.
    `test_agents.py::test_declared_peer_topology` pins the shape the policy has
    to mirror; it cannot check the YAML for you.

## Gotchas

These are real traps that have bitten this codebase.

- **`ToolContext` must be imported at RUNTIME, not under `TYPE_CHECKING`.**
  With `from __future__ import annotations`, ADK builds each tool's declaration
  via `typing.get_type_hints()`, which evaluates the annotation — a
  `TYPE_CHECKING`-only import raises `NameError: name 'ToolContext' is not
  defined` at request time, breaking *every* tool in the module. Import from
  `google.adk.tools.tool_context`, **not** the `google.adk.tools` re-export
  (`ty` rejects the latter). See the comment block in `app/cluster/peer_tool.py`.
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

- **A hand-rolled A2A client must send `A2A-Version: 1.0`.** The JSON-RPC route
  runs with `enable_v0_3_compat=True`, so a request arriving with *no* version
  header is treated as protocol 0.3 — and a v1.0 method name
  (`SendStreamingMessage`, `GetTask`) on an unversioned request comes back as
  **HTTP 200** carrying a JSON-RPC `-32009 VERSION_NOT_SUPPORTED` body. A client
  that checks the status code and then reads SSE frames sees a successful
  request with an empty stream, which reads as "the agent had nothing to say".
  ADK's own client sends the header, so agent-to-agent delegation never hits
  this; `curl`, the A2A Inspector and the integration tests do. See
  [`docs/a2a-v1-migration.md`](docs/a2a-v1-migration.md).

- **A2A v1.0 types are protobuf, so enum comparisons against strings silently
  fail.** `task.status.state == "TASK_STATE_COMPLETED"` is always `False` —
  `state` is an `int`. Compare against `TaskState.TASK_STATE_COMPLETED`. The
  symptom is "the stream never completed", not a type error. Likewise
  `model_dump()` / `model_validate()` are gone; use `MessageToDict` /
  `ParseDict` from `google.protobuf.json_format`.

- **The `google-adk>=2.5.0` floor is a correctness constraint, not a
  preference.** ADK declares `a2a-sdk<2,>=0.3.4`, so the resolver will happily
  pair ADK 2.4.0 with a2a-sdk 1.x. ADK isolates every 0.3-vs-1.x difference in
  `google/adk/a2a/_compat.py` (`IS_A2A_V1` picks the branch at import), and that
  module first ships in **2.5.0**. Below it, ADK builds 0.3-shaped parts and
  cards against a 1.x SDK: pods start, probes pass, first delegation fails.

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

- **There is no `uv` in the runtime image, so no `uv run` in a manifest.** The
  `Dockerfile` is multi-stage: the build stage has uv, the runtime stage gets
  the finished venv with `/code/.venv/bin` on `PATH` and nothing else. Call
  `python`, `uvicorn` and `alembic` directly — `migrate-job.yaml` does. `uv run`
  in a pod fails with `command not found`, which reads as a broken image rather
  than a stale command line. On a workstation `uv run` is still correct.

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

- **A missing a2a task column fails at first delegation, not at startup.**
  Migration `0006` adds the three columns a2a-sdk 1.x introduced (`owner`,
  `last_updated`, `protocol_version`). Skip it and the `tasks` table still
  exists, the pod still passes its health probe, and `DatabaseTaskStore` dies
  with `UndefinedColumn` the first time an agent is actually delegated to. This
  is the general shape of task-store drift, not a one-off: run the migration Job
  to completion before treating green pods as a working deployment.

- **a2a's `last_updated` is not this repo's `updated_at`.** `updated_at` (from
  `0002`) is maintained by a trigger, so it advances on every write no matter
  who made it — which is what makes it trustworthy for retention sweeps.
  `last_updated` is ORM-maintained and is NULL for every row written before the
  upgrade. Retention still keys off `updated_at`; do not "deduplicate" them.

- **Alembic autogenerate is off.** The tables belong to two third-party
  libraries, so autogenerate would let a library upgrade rewrite production DDL
  unreviewed. `tests/unit/test_migrations.py` is the drift guard instead.

- **Offline mode cannot bind parameters.** `alembic upgrade --sql` renders
  statements without binding, so a `:param` placeholder ends up verbatim in the
  generated script. Inline controlled constants in migrations (see `0001`).

- **Terraform >= 1.9 is required in practice, not the declared >= 1.6.**
  `versions.tf` says `required_version = ">= 1.6"`, but the cross-variable
  `validation` at `variables.tf:85` needs 1.9; an older binary fails with
  `Invalid reference in variable validation`, which reads like a broken config
  and is not. Nothing here is OpenTofu-specific, but OpenTofu (>= 1.9) runs the
  configuration unmodified if you prefer it — see
  `docs/deploy-to-another-project.md`. Whichever you use, a binary older than
  the `terraform_version` recorded in an existing state file will refuse that
  state until it is upgraded.

- **Read the plan before applying.** It should now be additive against an
  already-deployed environment — the last measured plan is *48 to add, 1 to
  change, 0 to destroy* — but that is a property to re-check, not to assume.

- **A `depends_on` on a data source can plan a destroy you never asked for.**
  `data.google_compute_network.vpc` (`infra/terraform/alloydb.tf`) depends on
  `google_project_service.services`, so adding *any* API to `local.services`
  gives that resource pending changes, which defers the data-source read to
  apply time, which makes its `id` unknown at plan time. That unknown lands on
  `network` in `google_compute_global_address.alloydb_psa` and
  `google_service_networking_connection.alloydb_psa`, where it is **ForceNew** —
  so adding `bigquery.googleapis.com` planned a destroy-and-recreate of the
  private-services-access peering underneath a live AlloyDB cluster. Replacing
  that connection either fails half-way or cuts every pod off from the database,
  and the reserved range could come back as a different /16.
  - Indexing the dependency does **not** help: the deferral is decided
    per-resource, not per-instance, so
    `services["compute.googleapis.com"]` (already applied, unchanged) defers the
    read just the same. Verified against this state.
  - The `depends_on` has to stay — enabling the compute API is what auto-creates
    the `default` network the data source reads, which is what lets a greenfield
    project apply without pre-enabling anything. So the chain is broken at the
    other end instead: both PSA resources carry
    `lifecycle { ignore_changes = [network] }`. A reserved peering range cannot
    move between VPCs in place anyway, so a real `var.network` change is a
    manual migration — and the AlloyDB cluster's own `network_config` is
    deliberately *not* ignored, so such a change still surfaces there.

- **A plausible answer is not proof the flow ran.** Three separate failures
  (a skipped graph node, a graph output that never reached the caller, an A2A
  reply that could not be replayed) all produced confident, sensible replies.
  Assert on a marker the code emits. The `/cases` endpoint applies the same
  rule: it confirms an execution by matching the result against the **approved
  proposal**, and reports `approved_not_confirmed` rather than assuming success.

- **A specialist's structured reply arrives as TEXT.** ADK's `AgentTool` reduces
  a peer's response to its merged text parts, so a dict a specialist's tool
  returned is not a dict by the time the caller sees it. That is why
  `app/cluster/cases.py` parses JSON back out of prose, and why a specialist's
  instruction has to demand verbatim reporting. Do not "simplify" the parser
  into `json.loads(reply)`.

- **Approval cases are durable, but only with a database.** `approval_cases`
  (migration `0005`, `app/cluster/cases.py`) survives a restart when
  `DB_BACKEND` is `alloydb`/`url`; with `none` — the default, and what `planner`
  runs on — the store is per-pod memory, so a restart loses every pending
  approval and only the replica that recorded a case can act on it. A case
  belongs to the agent that asked the human, so in the cluster they live in the
  orchestrator's schema.

- **Approving and executing are separate writes, in that order.** The decision
  lands on the row before the action is attempted, so a pod that dies
  mid-execution leaves a re-drivable `approved` case rather than an
  unanswerable one. Reconcile with `status = 'approved'` (indexed by `0005`) and
  re-drive by calling `POST /cases/{proposal_id}` again — unlike the mechanism
  this replaced, every such row is actionable.

- **A specialist can ask the USER a question, and that is not an approval.**
  `contracts.NEEDS_INPUT` (the request is ambiguous) and
  `contracts.NEEDS_CONFIRMATION` (the amount is unusual) mean the specialist did
  nothing and needs an answer. They are deliberately NOT approval cases: there
  is no effect to gate, nothing to record for audit, and the answer is a
  corrected *request* rather than a decision on a case. Every agent between the
  specialist and the user must relay the question rather than answer it — the
  instructions say so at all three levels, and `app/agents/reporting.py` is what
  makes the JSON survive intact so the relay cannot quietly become a paraphrase.

- **`reporting.py` has to scan INSIDE a peer's reply, not just its own tools.**
  A peer reached through `PeerTool` answers as text, and ADK delivers it as
  `{"result": "<the peer's text>"}` — `FunctionResponse.response` is typed
  `dict | None`, so the peer's JSON is a value inside the wrapper, never the
  payload. Without scanning that string, anything raised two hops down
  (`orchestrator -> math -> currency`) reaches the user only if the middle
  agent's model chooses to repeat it, which is the exact dependency that
  callback exists to remove.

- **An after-agent callback's content is an EXTRA event, so restating there
  duplicates the reply.** ADK appends whatever `after_agent_callback` returns
  after the agent's own events (`base_agent.py:_handle_after_agent_callback`);
  it does not replace them. `reporting.py` used to return the model's wording
  plus the JSON, which rendered the same answer **twice** in the ADK web UI on
  every HITL turn. The structure is folded into the model's reply by an
  *after-model* callback instead (that one returns an `LlmResponse` and ADK
  builds the event from it), and the after-agent hook only emits results the
  reply does not already contain — the fallback for a turn that ends without the
  model speaking. Keep it conditional; making it unconditional brings the double
  reply straight back.

- **`AUDITED_STATUSES` is derived from the contract vocabulary, not written
  out.** It was a literal `{approval_required, published}` when the `trades`
  agent landed, so a gated *read* reporting `executed` was silently not
  restated. Add a status to `app/agents/contracts.py` and it is audited
  everywhere; hardcode one and you get a flow that works until the model has an
  off day.

- **A gated action must report a status in `contracts.EFFECT_PERFORMED`.**
  `cases.find_execution` confirms an execution by scanning the reply for
  `"published"` or `"executed"` and then matching the values against the
  approved proposal. A new gated action that invents its own success string runs
  perfectly and is reported as `approved_not_confirmed` — a vocabulary bug
  wearing a model bug's clothes. Add the status to the frozenset in
  `app/agents/contracts.py`.

- **A gated action whose input is not reproducible must carry it back.**
  `publish_result` recomputes from `expression`; `run_trade_query` cannot,
  because a model asked the same question twice writes different SQL. So the
  approved `sql` travels in the re-sent request and the tool refuses to run
  without it, and the tool canonicalises it so a reflowed re-send still matches
  what was approved. Do not "fix" a mismatch by loosening the comparison —
  canonicalise at the source instead, or the check stops catching a genuinely
  different action.

- **The gate is that the effect is unreachable without an approver**, not an
  instruction the model is asked to follow. `publish_result` returns a proposal
  when `approved_by` is empty and only performs the write when it is set. Adding
  a second code path that publishes without that check removes the guarantee
  entirely, however well the prompt is worded.

- **Execution is confirmed by comparing content, not by trusting prose.**
  `cases.find_execution` accepts a result only if the values it reports match
  the approved proposal, so a specialist that publishes something *else* is
  reported as `approved_not_confirmed` rather than recorded as success.

- **`agents-cli` uses `uv`.** Run Python as `uv run python ...`, never bare
  `python`. (In the container it is the other way round — see the no-`uv`
  gotcha above.)

- **The trades agent's SQL validator is a guard rail, not the boundary.** It
  rejects anything that is not a single read-only statement against the one
  allowed table, and it works on text whose comments and string literals have
  been masked first — both directions matter, since a keyword in a literal must
  neither trip it nor evade it. What actually confines the agent is the human
  who reads the SQL, the pod's IAM (`roles/bigquery.jobUser`, `dataViewer` on
  nothing) and `maximum_bytes_billed`. Do not add a bypass for a "trusted"
  caller.

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

`pyrightconfig.json` follows the same pattern for **basedpyright** (the editor
LSP; it also beats any `[tool.basedpyright]` section). Three things to know:

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
  included). It is a pin, not a floor: the codebase does not support a range,
  so there is no need to keep anything back-compatible.
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
- Rule families: `E W F I B C4 CPY UP RUF SIM N D PTH RET ARG TID`.
  Exempt: `app/app_utils/**`, `app/fast_api_app.py`, `**/tests/**`.
- **Apache 2.0 header on every `.py` file** (`CPY001`, enforced everywhere —
  the exemptions above do *not* cover it). The root `LICENSE` is what legally
  licenses the repo; the per-file header is for files that get copied out of it,
  which is `app/shared/**`'s whole purpose. `lint.flake8-copyright.author` pins
  the form to `# Copyright <year> Google LLC` — a bare `# Copyright 2026` fails.
  **CPY001 has no autofix**, so paste the 13-line block (copy it from any
  existing file) at the top of a new file, followed by a blank line, before the
  module docstring.
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

**[`docs/environment-variables.md`](docs/environment-variables.md) is the full
reference** — every variable, its default, accepted values, which companions it
makes mandatory, and whether a bad value raises at startup, falls back silently,
or fails later on first use. It also covers what the manifests set, the legacy
variables that look like config but are dead, and minimum viable configurations
for tests / local / cluster.

**Keep it current: when you add, rename or remove an environment variable,
update that file in the same change.** Nothing generates or validates it, so an
omission is invisible until someone goes scavenging through the source. Its
"Maintaining this file" section has the checklist and the grep commands that
find every read site.

The handful that bite most often:

| Var | Default | Why it matters here |
| --- | --- | --- |
| `AGENT_NAME` | `orchestrator` | Selects which registered agent this process becomes; also the default `DB_SCHEMA` |
| `APP_URL` | `http://0.0.0.0:8000` | The default is unreachable from other pods — delegation hangs while probes stay green. Set it per agent |
| `GEMINI_*_MODEL` | *(live Vertex catalog)* | Unset means a network call at import. Pin all four for hermetic tests |
| `A2A_PEERS` | *(agent's `AgentSpec.peers`)* | Comma-separated `name` or `name=url`; the url is a service **root** |
| `DB_BACKEND` | `none` | Gates the session, task and approval-case stores all at once |
| `ARTIFACT_STORAGE_URI` | *(unset → in-memory)* | The scheme *is* the backend; a typo'd scheme silently writes to local disk. Need not match across agents |
| `TRADES_*` | `US` / 1 GiB / 50 rows | `TRADES_LOCATION`, `TRADES_MAX_BYTES_BILLED`, `TRADES_MAX_ROWS`. Read at call time, and only by the `trades` agent |

Local development reads a gitignored `.env`; copy `.env.example` and set at
least `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT`, and
`GOOGLE_CLOUD_LOCATION=global`. Note `.env` is loaded *after* the injector is
built, so it does not reach most configuration — export the variables instead.

## Common tasks

### Add an agent

Full walkthrough, checklist, and troubleshooting table:
[`docs/adding-an-agent.md`](docs/adding-an-agent.md). The short version:

1. `app/agents/<name>/` with `__init__.py` (**needs a docstring** — `D104`),
   `tools.py`, and `agent.py` exposing `SPEC = AgentSpec(...)`.
2. Register it in `app/agents/__init__.py` (`AGENTS`).
3. **Declare its contract** in `app/agents/contracts.py`: a `PeerRequest`
   subclass with a `Field(description=...)` on every field, added to `PAYLOADS`.
   Those descriptions are the only instructions a caller's model gets about how
   to call it. Optional in the mechanism — skip it and the agent is delegated to
   with a single free-text `task` — but required by this repo's policy and its
   test (invariant 3).
4. If an existing agent should delegate to it, add the name to that agent's
   `AgentSpec.peers`.
5. Update **two** tests in `tests/unit/test_agents.py`:
   `test_registry_lists_expected_agents` and `test_declared_peer_topology`.
6. Cluster only — five files:
   - `infra/terraform/variables.tf` → add to `var.agents`, then `terraform apply`
     (creates the GSA, IAM roles, WI binding, and AlloyDB user via `for_each`).
     If it needs a role no other agent needs, put that in
     `var.agent_extra_iam_roles` rather than widening the shared baseline — see
     `trades` and `roles/bigquery.jobUser`.
   - `serviceaccounts.yaml` → KSA `agent-<name>` + its GSA annotation.
   - `workers.yaml` → copy a Deployment/Service pair; set `AGENT_NAME`,
     `APP_URL`, `ALLOYDB_IAM_USER`, `serviceAccountName`.
   - `networkpolicy.yaml` → allow ingress from **whichever agents call it**, not
     reflexively from the orchestrator (invariant 10). **Easy to miss:** omitting
     it leaves the agent with no ingress at all, and delegation fails as a
     timeout rather than an error.
   - `migrate-job.yaml` → add to `MIGRATE_AGENTS`.
7. Local dev — `Makefile`: add it to `AGENTS`, give it a `PORT_<name>` and a
   `PORTS` entry, add a `serve-<name>` target, and if an existing agent should
   call it, add it to that agent's peer list (`PEERS` / `MATH_PEERS`).

No new migration and no new database are needed: every agent gets the same
tables in its own schema, and `DB_SCHEMA` defaults to `AGENT_NAME`.

Name it a single lowercase word (invariant 8).

### Add a tool

Plain function with a Google-style docstring (ADK derives the declaration from
signature + docstring). If it takes `tool_context: ToolContext`, import
`ToolContext` at runtime from `google.adk.tools.tool_context`. Add it to the
spec's `tools` tuple.

### Run two agents locally to exercise A2A

`make up` starts all six with the peers wired. By hand, for two:

```bash
# Terminal 1 — the specialist
AGENT_NAME=math APP_URL=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8091

# Terminal 2 — the orchestrator delegating to it
AGENT_NAME=orchestrator A2A_PEERS=math=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8090
```

An agent in the middle of the graph needs **both**: an `APP_URL` so its caller
can reach it, and its own `A2A_PEERS` so it can reach further. Miss the second
and everything still starts and reports healthy — `math` simply has no currency
tool.

```bash
AGENT_NAME=math APP_URL=http://127.0.0.1:8091 \
  A2A_PEERS=currency=http://127.0.0.1:8095 \
  uv run uvicorn app.fast_api_app:app --port 8091
```

### Deploy to GKE

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform init
terraform plan -var-file=terraform.tfvars   # READ THE PLAN, see gotchas
terraform apply -var-file=terraform.tfvars
eval "$(terraform output -raw get_credentials_command)"
cd -

# Build the image IN Cloud Build. No local docker/podman, no registry login,
# no --platform trap: the workers are amd64. Only the source tarball leaves
# your machine (a few hundred KB -- see .gcloudignore).
make image TAG=demo-1
# equivalently: terraform output -raw build_command

# set newTag to the same value in the overlay, fill the placeholders, then:
kubectl apply -k infra/kustomize/overlays/dev
kubectl -n agents port-forward svc/orchestrator 8080:80
```

Building locally is still possible and is now the fallback, not the default:

```bash
REPO=$(cd infra/terraform && terraform output -raw artifact_registry_repo)
docker login -u oauth2accesstoken \
  -p "$(gcloud auth print-access-token)" "${REPO%%/*}"
docker build --platform linux/amd64 -t "$REPO/agent:demo-1" .
docker push "$REPO/agent:demo-1"
```

**If you build locally, always pass `--platform linux/amd64`.** The GKE Autopilot
nodes these manifests target are **amd64** (nothing sets a
`kubernetes.io/arch: arm64` nodeSelector), so on an arm64 workstation a native
build produces an arm64 image whose pods fail on the cluster with `exec format
error` — which looks like an app bug, not a build bug. The base image
(`python:3.14-slim`) is multi-arch, so the cross-build works fine; it is just
slower under emulation. `make image` sidesteps the whole question.

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
5. **Deploy** — `make image TAG=...`, then apply the Kustomize overlay ("Deploy
   to GKE"). **Requires explicit human approval**; only after the user confirms.
   A build is not a deploy, but it does write to a registry and cost money — ask
   before running that too.

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
| Infrastructure | `infra/**`, `Dockerfile`, `cloudbuild.yaml`, `.dockerignore`, `.gcloudignore` | Terraform, Kustomize, the container image and how it is built |
| Tooling config | `pyproject.toml`, `ruff.toml`, `ty.toml`, `pyrightconfig.json` | See "Code style" |

Three rules worth stating outright:

- **`app/shared/**` sits at the bottom of the dependency graph.** It must not
  import from `app.agents` or `app.cluster`, and it stays portable to Python
  3.11 so it can be reused by other services. Project-specific logic belongs in
  `app/agents/` or `app/cluster/`.
- **Cluster services come from the injector, not module globals.** `Database`,
  the session/memory/artifact services, the A2A task store and the
  `CaseStore` are all `@singleton` providers in `app/cluster/di.py`;
  `app/agent.py` is the composition root that resolves them and hands them to
  the serving layer and the capture plugin. Do not reintroduce a cached
  `get_*()` accessor: for the case store in particular, a second instance is a
  second dict under `DB_BACKEND=none`, and approvals split between them with no
  error. Two module-level flags remain on purpose — `_configured` in
  `shared/telemetry.py` and `shared/logging.py` — because they guard idempotent
  bootstrap rather than holding a dependency.

- **`pyproject.toml` is for dependencies and packaging only.** Lint and
  type-check policy lives in the standalone config files. One thing there is
  easy to mistake for cruft: `basedpyright` in the `lint` extra. It is not run
  by `agents-cli lint`, but it must be installed for the editor LSP to start —
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
- **Adding, renaming or removing an environment variable means updating
  [`docs/environment-variables.md`](docs/environment-variables.md) in the same
  change** — it is the only inventory, and nothing validates it.
- `GKE.md` and `README.md` are the human-facing docs; keep them in sync when
  changing architecture or commands. `docs/environment-variables.md` is the
  configuration reference; `docs/human-in-the-loop.md` is the approval guide;
  `docs/design-decisions.md` records why the architecture is what it is, with
  the measurements and the rejected alternatives behind each choice;
  `docs/a2a-v1-migration.md` is the A2A v0.3 → v1.0 upgrade record — what
  changed here, and the two ways that upgrade fails quietly.
