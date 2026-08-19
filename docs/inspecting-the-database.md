# Inspecting the database

How to look at what the agents have actually stored in AlloyDB — sessions,
events, A2A tasks and pending approval cases.

## What is in there

The agents share **one PostgreSQL database** (`agents`) on AlloyDB, and each
agent owns **its own schema** inside it, named after the agent. So `math`'s
conversations live in the `math` schema, `research`'s in `research`, and so on.
Nothing is in `public`. [`../GKE.md`](../GKE.md#durable-storage-on-alloydb)
explains why it is arranged that way; the practical consequence is that you must
qualify every table (`math.sessions`) or set a `search_path`.

Each schema holds the same four tables:

| Table | What it holds | Owned by |
| --- | --- | --- |
| `sessions` | One row per conversation, including its ADK session state | ADK |
| `events` | Every turn, tool call and tool result within a session | ADK |
| `tasks` | A2A tasks — the unit of work when one agent calls another | the a2a SDK |
| `approval_cases` | Proposed actions awaiting, or having received, a [human decision](human-in-the-loop.md) | this repo |

Plus `alembic_version`, which is per-schema: each agent is migrated
independently.

Two things follow that are worth knowing before you go looking for data:

- **A request that crosses agents leaves rows in several schemas**, with no
  shared id column. [Tracing a request](#tracing-a-request-across-agents) shows
  how to join them.
- **An approval raised inside a delegated agent is stored on the *caller*.** If
  the orchestrator asked `math` to do something gated, the row is in
  `orchestrator.approval_cases`, not `math`'s.

An agent deployed without a database (`DB_BACKEND=none`) has no schema at all —
its state is per-pod and nothing here applies to it.

---

## The short version

Use **AlloyDB Studio** in the Cloud console:

**Console → AlloyDB → `agents-db` → AlloyDB Studio → IAM authentication → Authenticate**

(`agents-db` is the default `alloydb_cluster_id`; if you changed that variable,
use your own name. The same applies to `--region` below, which is `var.region`.)

Then pick database `agents` and query. The tables are **not** in `public` — each
agent owns a schema, so either qualify the table or set the search path:

```sql
SET search_path TO math;
SELECT id, state, update_time FROM sessions;

-- or, without changing the search path:
SELECT id, context_id, status->>'state' AS state, updated_at
FROM orchestrator.tasks
ORDER BY updated_at DESC;
```

---

## Why Studio works even though the instance has no public IP

This surprises people. The instance is private-IP-only, reachable from the GKE
pods over the private-services-access peering and from nowhere else — yet Studio
works from a browser with no proxy, no bastion, and no VPN.

Studio does not open a network connection from your machine. It executes
statements server-side through the AlloyDB Admin API
(`alloydb.instances.executeSql`). So the network posture is irrelevant to it;
only IAM is.

Studio's limits are worth knowing before you rely on it: responses over **10 MB**
are truncated, statements running longer than **five minutes** are cancelled, and
each editor tab opens a **new session** — so `SET search_path` does not persist
across tabs or across separate runs.

---

## Access model

Two things are needed, and they are easy to confuse:

| Layer | What it controls | How it is granted |
| --- | --- | --- |
| **Cloud IAM** | May you open Studio and call `executeSql`? | `roles/alloydb.databaseUser` + `roles/serviceusage.serviceUsageConsumer` (Terraform) |
| **PostgreSQL** | Which schemas and tables can you see? | `GRANT USAGE` / `GRANT SELECT` (script, below) |

Having `roles/owner` covers the first and **none** of the second. A new IAM
database user has no privileges on anything, so without the grants Studio
authenticates fine and then shows you an empty Explorer pane.

### Adding a reader

Two steps. First, Terraform — add the principal to `database_readers`:

```hcl
# infra/terraform/terraform.tfvars
database_readers = ["someone@example.com", "someone-else@example.com"]
```

```bash
cd infra/terraform && terraform apply -var-file=terraform.tfvars
```

That creates the AlloyDB cluster user and grants the two project IAM roles. It
cannot create the in-database grants — those need a SQL connection, which is not
expressible as a Terraform resource.

Second, apply the grants with `scripts/grant_readers.py`, run as the migrator
(the only identity that owns every schema). The image copies only `./app`, so
inject the script:

```bash
cd infra/terraform
REPO=$(terraform output -raw artifact_registry_repo)
# The database role is the migrator's service account email minus the
# ".gserviceaccount.com" suffix.
MIGRATOR=$(terraform output -raw migrator_service_account_email)
MIGRATOR=${MIGRATOR%.gserviceaccount.com}
cd -

READERS="someone@example.com"                              # comma-separated
SCHEMAS="orchestrator research math trades currency"       # every agent with a schema
B64=$(base64 < scripts/grant_readers.py | tr -d '\n')

# Unquoted heredoc: the shell substitutes the variables above.
cat > /tmp/ov.json <<JSON
{"apiVersion":"v1","spec":{"serviceAccountName":"agent-migrator","restartPolicy":"Never",
 "containers":[{"name":"g","image":"$REPO/agent:latest",
  "envFrom":[{"configMapRef":{"name":"agent-config"}}],
  "env":[{"name":"ALLOYDB_IAM_USER","value":"$MIGRATOR"},
         {"name":"DB_READERS","value":"$READERS"},
         {"name":"DB_READER_SCHEMAS","value":"$SCHEMAS"},
         {"name":"OTEL_SDK_DISABLED","value":"true"},
         {"name":"LOG_LEVEL","value":"WARNING"}],
  "command":["sh","-c","echo $B64 | base64 -d > /tmp/g.py && python /tmp/g.py"],
  "resources":{"requests":{"cpu":"500m","memory":"1Gi"}}}]}}
JSON

kubectl -n agents run grant-readers --restart=Never \
  --image="$REPO/agent:latest" --overrides="$(cat /tmp/ov.json)"
kubectl -n agents logs -f grant-readers
kubectl -n agents delete pod grant-readers      # it does not clean up after itself
```

`DB_READER_SCHEMAS` accepts commas or whitespace; `DB_READERS` is
**comma-separated only**. List every agent that has a schema — an agent running
with `DB_BACKEND=none` has none and must be left out, or the script fails on a
schema that does not exist.

It is idempotent, and it prints the *effective* privileges afterwards rather
than assuming the grants took:

```
someone@example.com on orchestrator  usage=True select=True insert=False create=False
someone@example.com on research      usage=True select=True insert=False create=False
someone@example.com on math          usage=True select=True insert=False create=False
```

`insert=False` and `create=False` are the point: a human reader can look at agent
state but cannot mutate it or add objects. `ALTER DEFAULT PRIVILEGES` is included,
so tables added by future migrations are covered without re-running this.

> **Why this is a script and not an Alembic revision.** Revision `0003` grants the
> *agent* role on its own schema, because that is tied to schema creation and runs
> once per schema. Who may *read* is a people-lifecycle concern — readers come and
> go — and adding one should not require bumping a migration.

---

## Tracing a request across agents

Three ready-made queries in [`../scripts/sql/`](../scripts/sql/). Paste them into
Studio as-is; each has a single `params` block at the top to edit.

| File | Answers |
| --- | --- |
| `recent_requests.sql` | "What happened recently?" One row per request, with fan-out and duration. **Start here** to get a `session_id`. |
| `request_path.sql` | "What did request X actually do?" The full event stream across every agent, depth-indented. |
| `request_hops.sql` | "What was the shape and cost?" One row per delegation hop. |

### Why this needs a join at all

There is no single "trace id" column. Each agent mints its **own** session: the
A2A callee generates a `context_id` and ADK uses it as the session id
(`adk/a2a/converters/request_converter.py:115`). So one user request produces:

```
orchestrator.sessions.id = 4be80423…      math.sessions.id = dc56a725…
orchestrator.tasks.id    = 3a226bc1…      math.tasks.id    = 484a092e…
```

...four rows, two schemas, no shared key on the sessions table.

The link lives in the **caller's event metadata**. When a `RemoteA2aAgent` gets a
response it writes the *remote* identifiers onto its own event:

```sql
event_data -> 'custom_metadata' ->> 'a2a:context_id'  -- = callee's session id
event_data -> 'custom_metadata' ->> 'a2a:task_id'     -- = callee's task id
event_data -> 'custom_metadata' -> 'a2a:response' -> 'metadata'
    ->> 'adk_invocation_id'                           -- callee's invocation id
```

`request_path.sql` walks that edge with a recursive CTE, so it handles chains of
any depth (`orchestrator → research → …`), not just one hop.

**This is the payoff of one-database-with-schemas.** The walk is a plain join
across `orchestrator.events` and `math.sessions`. With a database per agent it
would be impossible without FDW or dblink.

### What the queries return

`request_path.sql` gives one row per event, depth-indented by hop — `depth`,
`agent`, `timestamp`, `author`, `session_id`, `invocation_id`, and a `what`
column rendering whichever kind of event it is:

| Event | `what` renders as |
| --- | --- |
| user or agent text | the first 110 characters |
| tool call | `-> call math({"case_id": …, "expression": …})` |
| tool result | `<- calculate returned {"result": "391.0", …}` |
| A2A reply, on the caller's side | `<= A2A response from math (task 484a092e)` |

Delegation appears as a call to the peer's **own name**, because a peer is a
tool here — there is no `transfer_to_agent` row to look for.

`request_hops.sql` gives one row per delegation: `hop` (`orchestrator -> math`),
`callee_session`, `callee_task_state`, `callee_duration`, `callee_tokens`,
`callee_events` and `callee_session_state`.

That last column is worth reading once, because it makes the share-nothing
property concrete: whatever it holds lives in the **callee's** session and never
crossed the hop in either direction. Only message content does
([`design-decisions.md`](design-decisions.md), D3).

### Adding an agent

Each query has three `UNION ALL` blocks (`all_events`, `all_sessions`,
`all_tasks`). Add one line per new agent to each. There is no way to make this
dynamic in plain SQL; if the list grows unwieldy, create a set of views once:

```sql
CREATE VIEW all_events AS
    SELECT 'orchestrator'::text AS agent, * FROM orchestrator.events
    UNION ALL SELECT 'research', * FROM research.events
    UNION ALL SELECT 'math',     * FROM math.events;
    -- ...one line per agent that has a schema
```

...and delete the CTEs from the queries. Views need `CREATE` on a schema, which
only the migrator has, so that is a migration-time change rather than something
you can do from Studio as a reader.

### Two SQL traps these queries already work around

Both bit during development; the fixes are commented in place.

- **Recursive CTE type mismatch.** `sessions.id` is `varchar(128)` but `->>`
  yields `text`, and PostgreSQL rejects a recursive CTE whose two terms disagree.
  Hence the `::text` casts in the anchor term.
- **`NOT IN` with NULLs.** `recent_requests.sql` excludes sessions that were
  reached over A2A. With `NOT IN`, a single NULL from the subquery makes the
  predicate NULL for *every* row and returns nothing — silently. It uses
  `NOT EXISTS` instead.

---

## Useful queries

```sql
-- Which schemas exist (one per agent, plus AlloyDB's own ai/google_ml).
SELECT nspname FROM pg_namespace
WHERE nspname NOT LIKE 'pg_%'
  AND nspname NOT IN ('information_schema', 'public');

-- Row counts across every agent at once. This cross-schema view is exactly what
-- a database-per-agent layout would have made impossible.
SELECT 'orchestrator' AS agent, count(*) FROM orchestrator.sessions
UNION ALL SELECT 'research', count(*) FROM research.sessions
UNION ALL SELECT 'math',     count(*) FROM math.sessions;

-- Recent sessions and their ADK session state.
SELECT id, user_id, state, update_time
FROM math.sessions ORDER BY update_time DESC LIMIT 10;

-- The event stream for one session, newest first. This is the access pattern
-- idx_events_app_user_session_ts is built for.
SELECT timestamp, author, invocation_id
FROM math.events
WHERE session_id = '<session-id>'
ORDER BY timestamp DESC;

-- A2A tasks, with the timestamps this repo adds on top of the a2a schema.
SELECT id, context_id, status->>'state' AS state, created_at, updated_at
FROM orchestrator.tasks ORDER BY updated_at DESC LIMIT 10;

-- Retention candidates -- served by idx_sessions_update_time.
SELECT count(*) FROM math.sessions WHERE update_time < now() - interval '30 days';
```

### Approval cases

These live in the schema of the agent that asked the human — normally the
orchestrator — not the specialist that proposed the action.

```sql
-- What is waiting for a human right now?
SELECT proposal_id, agent, action, summary, created_at
FROM orchestrator.approval_cases
WHERE status = 'pending'
ORDER BY created_at;

-- THE reconciliation query: approved, but the action never completed. Served by
-- idx_approval_cases_unexecuted. Every row here is actionable -- re-drive it
-- with POST /cases/{proposal_id}.
SELECT proposal_id, agent, action, proposal, decided_by, decided_at
FROM orchestrator.approval_cases
WHERE status = 'approved'
ORDER BY decided_at;

-- The audit trail for one case: what was proposed, what was signed off, by
-- whom, and what actually ran. `proposal` is the anchor -- it is exactly what
-- the human was shown, and `result` is what came back when it ran.
SELECT proposal_id, action, proposal, status,
       decided_by, note, decided_at, result, executed_at
FROM orchestrator.approval_cases
WHERE case_id = '<case-id>'
ORDER BY created_at;

-- Anything whose execution did not confirm the approved proposal.
SELECT proposal_id, agent, action, result->>'reason' AS reason, executed_at
FROM orchestrator.approval_cases
WHERE status = 'failed'
ORDER BY executed_at DESC;
```

Note `sessions.state` is `JSONB` (ADK) while `tasks.status` is plain `json`
(a2a), so `->>` works on both but JSONB containment operators only on the former.
That difference is deliberate — see the migration comments.

### Verifying isolation

```sql
-- Each agent role should have USAGE on its own schema and nothing else,
-- and CREATE nowhere.
SELECT r.rolname, n.nspname,
       has_schema_privilege(r.rolname, n.nspname, 'USAGE')  AS usage,
       has_schema_privilege(r.rolname, n.nspname, 'CREATE') AS create_priv
FROM pg_roles r
CROSS JOIN pg_namespace n
WHERE r.rolname LIKE 'agent-%'
  -- Every agent schema, discovered rather than hardcoded, so this stays correct
  -- as agents are added or removed.
  AND n.nspname NOT LIKE 'pg_%'
  AND n.nspname NOT IN ('information_schema', 'public', 'ai', 'google_ml')
ORDER BY 1, 2;
```

---

## Alternatives to Studio

**`scripts/dbcheck.py`** — a read-only summary of every schema (row counts,
alembic revision, recent sessions and tasks). Same injection pattern as above but
with no `DB_READERS`. Good for a quick "did anything land?" check from the
terminal. It reports on `CHECK_SCHEMAS` (whitespace-separated, default
`orchestrator research math` — it predates `trades` and `currency`, so pass
them explicitly), so set that variable if your set of agents differs. Note it summarises sessions and tasks only — for approval cases, use the
queries above.

**psql from a pod** — for anything Studio's 10 MB / five-minute limits get in the
way of. Run a pod as the migrator and connect through the AlloyDB connector; the
agent image already has everything needed.

**Cloud Logging / Cloud Trace** — often the better first stop for *behavioural*
questions. Trace context propagates across A2A hops, so one trace shows the whole
orchestrator → worker delegation.

---

## Group-based access (the better pattern, later)

Granting per person does not scale, and AlloyDB supports **IAM group
authentication**: add a group to the cluster, and every member — users and
service accounts — inherits access. AlloyDB creates each member's database
account automatically on first sign-in, so access becomes purely a matter of
group membership, with no `gcloud alloydb users create` and no re-running grants.

This repo does **not** use it yet, for two documented reasons:

1. It is **Preview**, and the docs state it is available *for new clusters only*:
   > To enable this feature on an existing cluster, contact your Google Cloud
   > account team.

   Our cluster already exists, so enabling it is not a self-service change.
2. It needs a **second instance flag**, `alloydb.iam_group_authentication=on`,
   alongside the `alloydb.iam_authentication` we already set. Changing instance
   flags may restart the database.

When you do switch, the shape is:

```bash
# 1. Grant the group the same two project roles (Terraform: member = "group:...").
# 2. Add the group to the cluster.
gcloud beta alloydb users create agents-db-readers@example.com \
  --cluster=agents-db --region="$REGION" --type=IAM_GROUP
# 3. Grant it read-only, exactly as for an individual:
#    DB_READERS=agents-db-readers@example.com scripts/grant_readers.py
```

Two traps worth knowing in advance:

- **IAM group names are capped at 63 characters.** Nest a longer group under a
  shorter parent, and add the *parent* to the cluster.
- **Migrating an existing individual reader is not automatic.** You must delete
  their individual cluster user (`gcloud alloydb users delete`) — otherwise they
  keep authenticating as `ALLOYDB_IAM_USER` and silently do **not** inherit the
  group's privileges. The account is recreated as a group user on next sign-in.

---

## Break-glass: the `postgres` superuser

There is deliberately **no password** on any account — `initial_user` is omitted
from `alloydb.tf` so nothing sensitive lands in Terraform state. If IAM auth is
broken and you need in:

```bash
gcloud alloydb users set-password postgres \
  --cluster=agents-db --region="$REGION" --password=<temp>
```

Then sign in to Studio with **built-in authentication** as `postgres`. Clear the
password again afterwards — it is a shared credential with no per-person
attribution, which is exactly what the IAM setup exists to avoid.
