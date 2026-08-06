# Environment variables

Every environment variable this project reads or sets, what it defaults to, and
what it actually does. This file is the **single source of truth** — when you
add, rename or remove a variable, update it here (see
[Maintaining this file](#maintaining-this-file)).

Audited against the code at `d20f1ea`. Behaviour marked "silently" was confirmed
by executing the parsers, not just by reading them.

- [How configuration is resolved](#how-configuration-is-resolved)
- [Quick reference](#quick-reference) — every variable, one line each
- Detail by area: [agent identity & A2A](#agent-identity--a2a) ·
  [models & Google Cloud](#models--google-cloud) ·
  [database](#database) ·
  [session, memory & tasks](#session-memory--tasks) ·
  [artifacts](#artifacts) ·
  [HITL approvals](#hitl-approvals) ·
  [observability](#observability) ·
  [serving](#serving) ·
  [migrations & scripts](#migrations--scripts)
- [Set by the deployment, read by a library](#set-by-the-deployment-read-by-a-library)
- [Legacy variables — do not use](#legacy-variables--do-not-use)
- [Minimum viable configurations](#minimum-viable-configurations)
- [Traps](#traps)
- [Maintaining this file](#maintaining-this-file)

## How configuration is resolved

**Precedence.** For most settings: an explicit constructor argument (e.g.
`ModelModule(project=…)`) beats the environment variable, which beats the coded
default. In Kubernetes, a container's own `env:` entry beats a key arriving via
`envFrom: configMapRef` — that is how the `planner` Deployment overrides three
ConfigMap values.

**When values are read.** Almost everything is read **once, at import time**:
`app/__init__.py` imports `app.agent`, which calls `configure_observability()`
and builds the injector. Changing the environment after startup has no effect.
Two consequences worth knowing:

- `load_dotenv()` in `app/fast_api_app.py` runs *after* the injector already
  exists, so a `.env` file does **not** reach most configuration. Export the
  variables in your shell for local runs.
- `HITL_LEASE_TTL_SECONDS` is the exception — it is re-read on every claim,
  sweep and heartbeat.

**Failure style is deliberately inconsistent, so know which you are dealing
with.** Backend *selectors* (`DB_BACKEND`, `SESSION_BACKEND`, …) raise
`ValueError` at startup on an unknown value. Numeric *tuning* knobs
(`DB_POOL_SIZE`, `HITL_LEASE_TTL_SECONDS`, `A2A_PEER_PORT`) never raise — a typo
silently falls back to the default, because a bad ConfigMap value must not take
the pod down. Anything that is only validated by a remote service
(`ALLOYDB_IP_TYPE`, model ids, `DB_SCHEMA`) fails later, on first use.

## Quick reference

Legend: **R** = raises at startup if invalid · **S** = silently falls back ·
**L** = fails later, on first use.

### Agent identity & A2A — `app/cluster/config.py`

| Variable | Default | Effect | Bad value |
| --- | --- | --- | --- |
| `AGENT_NAME` | `orchestrator` | Which registered agent this process becomes; also the default `DB_SCHEMA` and the fallback trace service name | L |
| `A2A_PEERS` | the agent's `AgentSpec.peers` | Comma-separated `name` or `name=url` | S |
| `A2A_NAMESPACE` | `agents` | Namespace in derived peer DNS names | S |
| `A2A_CLUSTER_DOMAIN` | `svc.cluster.local` | Cluster DNS suffix | S |
| `A2A_PEER_SCHEME` | `http` | Scheme for derived peer URLs | S |
| `A2A_PEER_PORT` | `80` | Port for derived peer URLs; omitted from the URL when it is the scheme's default | S |
| `A2A_RPC_PATH` | `/a2a/app` | Where the serving layer mounts JSON-RPC and the agent card | S |
| `APP_URL` | `http://0.0.0.0:8000` | Base URL this agent advertises in its own card. **Must be set per agent** | S |

### Models & Google Cloud — `app/shared/config.py`

| Variable | Default | Effect | Bad value |
| --- | --- | --- | --- |
| `GOOGLE_CLOUD_PROJECT` | *(ADC discovery)* | Project for Vertex, logging, tracing, secrets | L |
| `GOOGLE_CLOUD_LOCATION` | `global` | Vertex AI endpoint region | L |
| `GEMINI_FAST_MODEL` | latest `flash-lite` from the **live** catalog | Pins the `fast` tier | L |
| `GEMINI_BALANCED_MODEL` | latest `flash` from the live catalog | Pins the `balanced` tier | L |
| `GEMINI_CAPABLE_MODEL` | latest `pro` from the live catalog | Pins the `capable` tier | L |
| `GEMINI_EMBEDDING_MODEL` | best embedding model from the live catalog | Pins the embedding model | L |

### Database — `app/cluster/db.py`

| Variable | Default | Effect | Bad value |
| --- | --- | --- | --- |
| `DB_BACKEND` | `none` | `none` \| `alloydb` \| `url` | R |
| `ALLOYDB_INSTANCE_URI` | — | `projects/P/locations/L/clusters/C/instances/I`. Required by `alloydb` | R |
| `ALLOYDB_IAM_USER` | — | GSA email **minus** `.gserviceaccount.com`. Required by `alloydb` | R |
| `ALLOYDB_IP_TYPE` | `PRIVATE` | `PRIVATE` \| `PUBLIC` \| `PSC` | L |
| `DB_URL` | — | Async SQLAlchemy DSN. Required by `url` | R |
| `DB_NAME` | `agents` | Database name (`alloydb` backend only) | L |
| `DB_SCHEMA` | *(`AGENT_NAME`)* | Per-agent schema, applied as `search_path`. Leave unset | L |
| `DB_POOL_SIZE` | `5` | Persistent connections per pod | S |
| `DB_MAX_OVERFLOW` | `2` | Burst connections above the pool | S |
| `DB_POOL_RECYCLE` | `1800` | Seconds before a pooled connection is replaced | S |

### Session, memory & tasks — `app/cluster/session.py`, `tasks.py`

| Variable | Default | Effect | Bad value |
| --- | --- | --- | --- |
| `SESSION_BACKEND` | `in_memory` | `in_memory` \| `alloydb` \| `database` \| `vertex_ai` | R |
| `SESSION_DB_URL` | — | Async DSN. Required by `SESSION_BACKEND=database` | R |
| `MEMORY_BACKEND` | `in_memory` | `in_memory` \| `vertex_ai`. **Currently a no-op** — see [Traps](#traps) | R |
| `AGENT_ENGINE_ID` | — | Bare reasoning-engine id. Required by the `vertex_ai` backends | R / L |
| `TASK_STORE_BACKEND` | `in_memory` | `in_memory` \| `database` | R |

### Artifacts, HITL, observability, serving

| Variable | Default | Effect | Bad value |
| --- | --- | --- | --- |
| `ARTIFACT_STORAGE_URI` | *(unset → in-memory)* | `gs://` / `s3://` / `az://` / local path. Same value for every agent | L |
| `HITL_LEASE_TTL_SECONDS` | `30` | Approval lease liveness timeout, heartbeat interval (TTL/3) and sweep interval | S |
| `HOSTNAME` | `socket.gethostname()` | HITL lease-owner identity. Kubernetes sets it to the pod name | S |
| `LOG_LEVEL` | `INFO` | loguru + stdlib level | R |
| `LOG_FORMAT` | *(auto: `json` when stderr is not a TTY)* | `json` \| `console` | S |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)* | Send traces to an OTLP collector instead of Cloud Trace | L |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | *(unset)* | As above, but a full `/v1/traces` URL | L |
| `OTEL_SERVICE_NAME` | `AGENT_NAME`, then `adk-agent` | Trace service name | S |
| `ALLOW_ORIGINS` | *(unset → no CORS middleware)* | Comma-separated CORS origins | S |
| `AGENT_VERSION` | `0.1.0` in code, `0.0.0` in the image | `version` field of the A2A card | S |

### Migrations & scripts

| Variable | Default | Effect | Consumer |
| --- | --- | --- | --- |
| `DB_AGENT_ROLE` | `""` → **silent no-op** | Role that migration `0003` grants USAGE + DML to | Alembic |
| `DB_READERS` | `""` → nothing granted | Comma-separated principals to grant read-only access | `scripts/grant_readers.py` |
| `DB_READER_SCHEMAS` | `""` → error if `DB_READERS` is set | Schemas to grant on; commas or whitespace | `scripts/grant_readers.py` |
| `CHECK_SCHEMAS` | `orchestrator research math` | Whitespace-separated schemas to report on | `scripts/dbcheck.py` |
| `MIGRATE_AGENTS` | — | Space-separated agents the migration Job loops over | Job shell script |
| `AGENT_ROLE_PREFIX` / `AGENT_ROLE_SUFFIX` | — | Compose `DB_AGENT_ROLE` as `<prefix>-<agent>@<suffix>` | Job shell script |

---

## Agent identity & A2A

**`AGENT_NAME`** is the keystone. One value must be true in five places at once:
the folder `app/agents/<name>/`, the `AgentSpec.name` in the registry, the
Kubernetes Service name, an entry in Terraform's `var.agents`, and the
PostgreSQL schema. A name not in the registry raises `KeyError: Unknown
AGENT_NAME` at startup.

**`A2A_PEERS`** parsing, in full:

| Value | Result |
| --- | --- |
| unset, or `""` | falls back to the agent's declared `AgentSpec.peers` |
| `"   "` or `","` | **zero peers** — this is the only way to override declared peers with none |
| `"research,math"` | bare names → derived DNS URLs |
| `"math=http://127.0.0.1:8091"` | explicit URL, trailing slashes stripped |
| `"=http://x"` | item silently dropped (empty name) |
| `"a;b"` or `"a b"` | **one** peer literally named `a;b` / `a b` — comma is the only separator |
| `"a=http://one,b,a=http://two"` | de-duplicated by name: last URL wins, first position kept |

Nothing here ever raises; every malformed form degrades silently.

A bare name becomes `<scheme>://<name>.<namespace>.<domain>[:<port>]`, with the
port omitted when it equals the scheme's default (80 for `http`, 443 for
`https`). Note the scheme comparison is **case-sensitive**:
`A2A_PEER_SCHEME=HTTPS` with port 443 yields `HTTPS://…:443`, port and all.

**`A2A_RPC_PATH`** is normalized to exactly one leading slash and no trailing
slash: `a2a/app`, `/a2a/app/` and `///a2a/app///` all become `/a2a/app`.

**`APP_URL`** is the one A2A variable with a dangerous default. The value goes
into the agent card that *peers fetch and then dial*, and `0.0.0.0` is a bind
address, not a routable destination — a peer reading it connects to itself, on a
port the container is not even listening on. The failure mode is the nasty kind:
pods are healthy, probes pass, and delegation just hangs. Set it per agent to
`http://<service>.<namespace>.svc.cluster.local`.

## Models & Google Cloud

**Leaving the four `GEMINI_*_MODEL` variables unset is a network call, not a
default.** There are no hardcoded model names: the code lists the live Vertex AI
catalog and picks the newest model in the family. The listing is eager (the
first page is fetched immediately), it needs credentials, and its answer changes
as Google ships models. A failed lookup **raises** rather than falling back:

```
ValueError: No Gemini model found for family 'flash' in the Vertex AI catalog.
Set the corresponding GEMINI_*_MODEL env var to pin one.
```

Because `app/agent.py` resolves the whole injector at import time, and unit
tests import `app.*`, **pinning all four is mandatory for a hermetic test run**:

```bash
GEMINI_FAST_MODEL=gemini-2.5-flash-lite \
GEMINI_BALANCED_MODEL=gemini-2.5-flash \
GEMINI_CAPABLE_MODEL=gemini-2.5-pro \
GEMINI_EMBEDDING_MODEL=gemini-embedding-001 \
  uv run pytest tests/unit app/shared/tests -q
```

Pinning short-circuits the resolver before the catalog is ever touched, so the
run is offline, credential-free and deterministic.

`GOOGLE_CLOUD_PROJECT` unset is a supported mode, not an oversight — in the
cluster the project comes from Workload Identity / ADC. The internal "absent"
value is the empty string (a `NewType` base must be a real class, so the binding
cannot be `str | None`), converted back to `None` at the client boundary.

A **404 on a model call is a location problem, not a model-name problem.** Fix
`GOOGLE_CLOUD_LOCATION` (`global` usually works); never change the model.

## Database

`DB_BACKEND` decides which companions become mandatory:

| `DB_BACKEND` | Requires | Notes |
| --- | --- | --- |
| `none` *(default)* | — | No engine. `Database.engine()` raises if something asks for one |
| `alloydb` | `ALLOYDB_INSTANCE_URI` **and** `ALLOYDB_IAM_USER` | IAM auth via the connector, no password anywhere |
| `url` | `DB_URL` | Plain async DSN — local Postgres, out-of-cluster Alembic, SQLite in tests |

Missing companions raise at startup with an explicit message, e.g.
`DB_BACKEND=alloydb requires ALLOYDB_INSTANCE_URI and ALLOYDB_IAM_USER to be
set.` An unknown backend raises `Unknown DB_BACKEND='postgres'; expected one of
'none', 'alloydb', 'url'.`

Asymmetry worth remembering: `DB_BACKEND=""` falls back to `none`, but
`SESSION_BACKEND=""`, `MEMORY_BACKEND=""` and `TASK_STORE_BACKEND=""` all
**raise**. Only `DB_BACKEND` tolerates an empty value.

`DB_SCHEMA` defaults to `AGENT_NAME`, which is what gives each agent its own
schema with zero per-agent config — leave it unset. An explicitly empty value
means "do not pin `search_path`" and is not the same as unset. The schema is
only applied when the backend is `alloydb` or the DSN starts with `postgresql`;
SQLite is deliberately excluded.

The three pool knobs are parsed leniently: `DB_POOL_SIZE=many` yields `5` with
no warning. All three are skipped entirely for SQLite URLs.

`ALLOYDB_IP_TYPE` is upper-cased but **not validated here** — a bogus value
survives startup and fails on first connect with `ValueError: Incorrect value
for ip_type, got 'INTERNAL'. Want one of: 'PUBLIC', 'PRIVATE', 'PSC'.`

## Session, memory & tasks

| `SESSION_BACKEND` | Requires | Result |
| --- | --- | --- |
| `in_memory` *(default)* | — | Per-pod, lost on restart |
| `alloydb` | a configured `DB_BACKEND` | ADK sessions on the **shared** engine — preferred in the cluster |
| `database` | `SESSION_DB_URL` | ADK sessions on a **second**, private pool |
| `vertex_ai` | `AGENT_ENGINE_ID` | Managed Agent Engine sessions |

`MEMORY_BACKEND` accepts `in_memory` and `vertex_ai`; there is deliberately no
database option. See [Traps](#traps) — it is currently wired to nothing.

`TASK_STORE_BACKEND=in_memory` is per-pod, so a task created by the pod that
answered `message/send` is invisible to the pod that later receives `tasks/get`.
Scaling an agent past one replica silently breaks task polling and
resubscription; `database` fixes both. The database store is constructed with
`create_table=False` because Alembic owns the schema, so if the migration has
not run you get "relation does not exist" rather than an auto-created table.

## Artifacts

`ARTIFACT_STORAGE_URI` has no companion `ARTIFACT_BACKEND` switch — **the URI
scheme is the backend**. `gs://`, `s3://` and `az://` are cloud paths; anything
else, including a bare or relative local path, becomes a local directory.

That fallback is silent and is the trap here:

| Value | Result |
| --- | --- |
| `gs://bucket/prefix` | Cloud Storage |
| `/var/lib/agent/artifacts` | local directory (parents created on write) |
| `gcs://bucket/x` *(typo)* | local directory `gcs:/bucket/x` — **no error** |
| `~/artifacts` | local directory literally named `~` — tilde is **not** expanded |
| `az://…` | raises: the Azure SDK is not installed in this project |

Set the **same** URI for every agent. Artifacts are keyed by the ADK app name
(`app`), not `AGENT_NAME`, which is exactly what lets an artifact cross an A2A
hop.

## HITL approvals

`HITL_LEASE_TTL_SECONDS` is a **liveness timeout, not a resume budget** — a
running resume heartbeats its own lease every TTL/3, so a long resume is never
reclaimed. One number drives three things: the staleness cutoff for reclaiming a
`deciding` row, the heartbeat interval (TTL/3), and the recovery sweep interval
(TTL). Non-numeric values fall back to 30 silently; `0` and negatives are
clamped to 1 second.

**The approval store has no backend switch of its own** — it follows
`DB_BACKEND`. With `DB_BACKEND=none` it is a per-pod dict, so a restart loses
every pending approval and only the pod that took a decision can act on it.
Scaling an agent past one replica therefore requires a database.

`HOSTNAME` is read **once at import** to build the lease-owner token
`<host>/<uuid12>`. The UUID matters: a restarted pod reuses its name, and
without the suffix a dead predecessor's lease would look like our own.

## Observability

`LOG_FORMAT`'s real default is **TTY auto-detection**, not `json` — stderr is
not a TTY in a container, so pods get JSON anyway, and an interactive shell gets
the console format. Only `json` and `console` are recognized; any other value
silently falls back to auto-detection.

`LOG_LEVEL` is `.upper()`ed but **not** stripped, so `" info"` fails. Note
`TRACE` and `SUCCESS` are loguru-only levels and will raise from
`logging.basicConfig` with `ValueError: Unknown level: 'TRACE'`.

Exporter selection (`select_exporter_kind`) is a pure decision:

1. Either `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
   set → **OTLP**, and it wins even when `GOOGLE_CLOUD_PROJECT` is also set.
2. Otherwise `GOOGLE_CLOUD_PROJECT` set, or ADC available → **Cloud Trace**.
3. Otherwise → **none**, but a real `TracerProvider` is still installed, so
   spans record and context still propagates across A2A hops.

The two OTLP variables are equivalent inside this repo (they are OR-ed); the
difference is downstream in the OTel SDK, where the base endpoint gets
`v1/traces` appended and the traces-specific one is used verbatim.

## Serving

`ALLOW_ORIGINS` is split on commas with **no per-item strip and no empty-item
filtering**: `"a, b"` yields `["a", " b"]` and the second entry can never match
a real `Origin` header. An explicitly empty value is treated as unset, which
means no CORS middleware at all — cross-origin state-changing requests that
carry an `Origin` header then get a 403.

`AGENT_VERSION` has two different defaults: `0.1.0` in the code and `0.0.0`
baked into the image by the Dockerfile `ARG`. Since no documented build passes
`--build-arg AGENT_VERSION=…`, **every deployed agent card advertises `0.0.0`**.
Cosmetic — nothing branches on it — but useless for identifying a build.

## Migrations & scripts

The migration Job composes two variables per loop iteration rather than setting
them globally:

```sh
for agent in ${MIGRATE_AGENTS}; do          # deliberately unquoted: word-splitting
  DB_SCHEMA="${agent}" \
  DB_AGENT_ROLE="${AGENT_ROLE_PREFIX}-${agent}@${AGENT_ROLE_SUFFIX}" \
    uv run alembic -c app/migrations/alembic.ini upgrade head
done
```

`MIGRATE_AGENTS` is therefore **space**-separated, not comma-separated.

`DB_SCHEMA` selects the Alembic target (precedence: `-x schema=` → `DB_SCHEMA` →
`AGENT_NAME`), is validated against `^[a-z_][a-z0-9_]*$`, is created if absent,
and gets its own `alembic_version` table — which is what makes each agent
independently versioned.

**`DB_AGENT_ROLE` unset is the dangerous one.** Migration `0003` resolves it to
`""` and returns immediately: no grant, no warning, no error. The migration
"succeeds", and the agent then crash-loops with `permission denied for schema
<name>`. Worse, once the schema is at head the revision will not re-run.

Script variables parse differently from each other, which is easy to get wrong:

| Variable | Separator | Validated? |
| --- | --- | --- |
| `MIGRATE_AGENTS` | whitespace | no (shell only) |
| `CHECK_SCHEMAS` | whitespace | **no** — interpolated into SQL; trusted input only |
| `DB_READERS` | comma | yes — `^[A-Za-z0-9._%+@-]+$`, ≤ 63 bytes |
| `DB_READER_SCHEMAS` | comma **or** whitespace | yes — `^[a-z_][a-z0-9_]*$`, ≤ 63 bytes |

## Set by the deployment, read by a library

These are set in `infra/kustomize/base/` and never read by this repo's code, but
they are load-bearing — do not remove them.

| Variable | Set in | Read by | Removing it |
| --- | --- | --- | --- |
| `GOOGLE_GENAI_USE_ENTERPRISE=true` | ConfigMap | ADK + google-genai | Breaks **every** A2A delegation with `part_metadata parameter is only supported in Gemini Developer API mode` |
| `OTEL_RESOURCE_ATTRIBUTES=gcp.project_id=<project>` | ConfigMap | OpenTelemetry SDK | Every span batch is rejected with `400 Bad Request` |
| `OTEL_SDK_DISABLED=true` | migration Job | OpenTelemetry SDK | Floods the Job's logs with 403s — the migrator GSA has no trace-writer role |

And one that is read by **nothing at all**:

**`PORT=8080` is inert.** The Dockerfile `CMD` hardcodes `--port 8080`; the app
never reads `PORT`, and uvicorn would only honour `UVICORN_PORT` (and the CLI
flag beats it anyway). Changing the ConfigMap key is *silently ignored*. To
actually move the port you must edit six places: the `CMD --port`, `EXPOSE`,
each Deployment's `containerPort`, each probe's `tcpSocket.port`, each Service's
`targetPort`, and the NetworkPolicy ports — plus this key, to keep the
documentation honest.

Values that must be filled per project come from
`terraform output -json kustomize_values`: `ALLOYDB_INSTANCE_URI`, `DB_NAME`,
`ARTIFACT_STORAGE_URI`, and each agent's `ALLOYDB_IAM_USER`. Three have no
Terraform output and are derived by hand: `OTEL_RESOURCE_ATTRIBUTES`
(`gcp.project_id=<var.project_id>`), `AGENT_ROLE_SUFFIX` (`<project_id>.iam`)
and `MIGRATE_AGENTS`.

## Legacy variables — do not use

`app/app_utils/services.py` reads four variables that look like configuration
and are not. The serving Runner always takes the **injector's** session and
artifact services, and `app/fast_api_app.py` re-registers the `shared://` scheme
whenever a database or artifact URI is configured, so these never take effect in
a configured cluster:

| Variable | Superseded by |
| --- | --- |
| `SESSION_SERVICE_URI` | `SESSION_BACKEND` (+ `DB_BACKEND`) |
| `GOOGLE_CLOUD_AGENT_ENGINE_ID` | `AGENT_ENGINE_ID` |
| `GOOGLE_CLOUD_AGENT_ENGINE_LOCATION` | `GOOGLE_CLOUD_LOCATION` |
| `LOGS_BUCKET_NAME` | `ARTIFACT_STORAGE_URI` |

They can only ever reach the ADK web/dev routes, and only when no database and
no artifact URI are configured. Setting one expecting it to change cluster
behaviour is a silent no-op.

## Minimum viable configurations

**Hermetic unit tests** — the four model pins, nothing else. See
[Models & Google Cloud](#models--google-cloud).

**Local single agent:**

```bash
export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT=<your-project>
export GOOGLE_CLOUD_LOCATION=global
uv run uvicorn app.fast_api_app:app --port 8000
```

**Two agents locally, exercising A2A** — `APP_URL` is required on the callee, and
the caller needs an explicit peer URL:

```bash
# Terminal 1 — the specialist
AGENT_NAME=math APP_URL=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8091

# Terminal 2 — the orchestrator delegating to it
AGENT_NAME=orchestrator A2A_PEERS=math=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8090
```

**Durable cluster agent** — the minimum beyond the ConfigMap defaults:
`AGENT_NAME`, `APP_URL`, `ALLOYDB_IAM_USER`, plus `DB_BACKEND=alloydb`,
`ALLOYDB_INSTANCE_URI`, `SESSION_BACKEND=alloydb`, `TASK_STORE_BACKEND=database`
and `ARTIFACT_STORAGE_URI`.

## Traps

Ranked by how much time they cost when you hit them.

1. **`APP_URL` left at its default** — delegation hangs, pods look healthy.
2. **`GEMINI_*_MODEL` unset in tests** — the suite hits the network and is not
   reproducible.
3. **`DB_AGENT_ROLE` unset in the migration Job** — migration succeeds, agents
   crash-loop on `permission denied`, and the revision will not re-run.
4. **A typo'd `ARTIFACT_STORAGE_URI` scheme** — writes land silently on the pod's
   local filesystem and vanish with the pod.
5. **`MEMORY_BACKEND` is currently a no-op.** The Runner is constructed without a
   memory service, so setting `vertex_ai` changes nothing. Verify before relying
   on it.
6. **`PORT` is inert** — see above.
7. **Empty string is not "unset"** for `SESSION_BACKEND`, `MEMORY_BACKEND` and
   `TASK_STORE_BACKEND`: they raise. `DB_BACKEND` and `A2A_PEERS` do not.
8. **`.env` is read too late** to reach most configuration; export instead.

## Maintaining this file

**When you add, rename or remove an environment variable, update this file in
the same change.** It is the only inventory — nothing generates or validates it,
so an omission here is invisible until someone goes scavenging through the
source.

Checklist for a new variable:

1. Add it to the right [Quick reference](#quick-reference) table with its
   default, effect, and failure style (**R** / **S** / **L**).
2. If it has non-obvious parsing, a required companion, or a dangerous default,
   add a paragraph in the matching detail section.
3. If a manifest sets it, cover it in
   [Set by the deployment](#set-by-the-deployment-read-by-a-library) and say
   where the value comes from.
4. If it replaces an old variable, move the old one to
   [Legacy variables](#legacy-variables--do-not-use) rather than deleting the
   row — a silently-ignored variable is worth documenting.

To find drift, every read site in the codebase:

```bash
rg -n 'os\.environ|os\.getenv' -g '*.py' app scripts
rg -n '_ENV\s*=\s*"' -g '*.py' app scripts        # the name constants
rg -n '^\s{2,}[A-Z_]+:' infra/kustomize/base/     # what the manifests set
```
