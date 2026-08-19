# Adding an agent

How to add a new specialist agent to the multi-agent system, from code to a
running pod. The worked example throughout is a fictional `weather` agent.

## Before you start

**What this system is.** A cluster of small agents that call each other over
A2A, an HTTP protocol for agent-to-agent calls. An *orchestrator* agent receives
the user's request and delegates sub-tasks to specialists; each agent is its own
Kubernetes Deployment and Service. [README.md](../README.md) has the overview
and [`../GKE.md`](../GKE.md) the architecture in depth. Everything below assumes
only that much.

**What an agent is here.** Not a class you subclass. An agent is a declarative
`AgentSpec` — a name, a description, an instruction, a model tier and a tuple of
tools — placed in a registry and turned into a running agent by one shared
factory, `build_agent()`. There is no orchestrator base class and no per-role
code path: an agent that coordinates others is simply one whose `peers` is
non-empty. So adding an agent is mostly declaration, not implementation.

**What you will write:** three small files under `app/agents/<name>/`, one line
in a registry, **one request contract** (optional, but expected here), one entry
in the delegating agent's `peers`, and two test updates. That is a complete,
working agent you can exercise locally.

**How far you need to go.** Steps 0–5 are the whole job if you run locally —
stop there. Steps 6–8 are cluster-only: five infrastructure files, a Terraform
apply and a deploy. The [checklist](#checklist) at the end is split the same way.

**The one rule that is not obvious.** Your agent will be called with an
explicit payload and **nothing else** — no conversation history, no shared
state. The default payload is a single free-text task; declaring a contract in
`app/agents/contracts.py` (step 2b) upgrades that to named, validated fields,
and is optional in the mechanism but expected of every agent this repo owns.
Either way, everything your agent needs has to arrive in that payload. If your agent needs to gate an action behind
a human, see [human-in-the-loop.md](human-in-the-loop.md#writing-a-gated-action);
it is two ordinary functions, not a framework feature.

For standing the whole system up in a fresh Google Cloud project, read
[`deploy-to-another-project.md`](deploy-to-another-project.md) instead.

The steps below were verified by adding a throwaway agent to this repo and
running the tests and `kubectl kustomize`.

---

## 0. Pick the name first

The agent name is load-bearing in **six** places at once, and they must all be
byte-identical:

| Where | What |
| --- | --- |
| `app/agents/<name>/` | the package directory |
| `AgentSpec.name` | the ADK agent name |
| `AGENTS` key | registry key in `app/agents/__init__.py` |
| `AGENT_NAME` | env value in its Deployment |
| Kubernetes `Service` name | A2A peer DNS resolves `<name>.<ns>.svc.cluster.local` |
| PostgreSQL schema | derived from `AGENT_NAME`; see [step 7](#7-database-nothing-to-do) |

Constraints, which come from two directions at once:

- ADK requires a valid **Python identifier** → no hyphens.
- Kubernetes DNS labels forbid **underscores**.

So: **a single lowercase word**, `^[a-z][a-z0-9]*$`. `weather`, `billing`,
`legal`. Not `web_search`, not `data-analyst`. Terraform enforces this with a
`validation` block on `var.agents`, but you want to get it right before you
write any code — renaming later means touching all six places.

Also keep it short. The agent's AlloyDB role is derived from its service account
email (`agent-<name>@<project>.iam`) and PostgreSQL truncates identifiers at 63
bytes; `alloydb.tf` has a `precondition` that fails the apply rather than
silently granting to the wrong role.

---

## 1. Write the agent

Create `app/agents/<name>/` with three files. Copy `app/agents/math/` — it is
the smallest complete example.

**`app/agents/<name>/__init__.py`** — must have a docstring, or ruff's `D104`
fails the lint:

```python
"""The weather agent package."""
```

**`app/agents/<name>/tools.py`** — agent-specific tools. ADK derives each tool's
function declaration from the **signature and docstring**, so the Google-style
`Args:`/`Returns:` sections are part of the contract, not decoration:

```python
"""Tools specific to the weather agent."""

from __future__ import annotations


def forecast(location: str) -> dict[str, str]:
    """Look up the current forecast for a location.

    Args:
        location: City or region to report on, e.g. "Amsterdam".

    Returns:
        A mapping with the forecast, or an ``error`` status for invalid input.
    """
    ...
```

Return a `dict[str, str]` with a `status` key rather than raising: the LLM sees
the return value and can react to `{"status": "error", ...}`, whereas an
exception becomes an opaque failure.

> ⚠️ If a tool takes `tool_context: ToolContext`, import `ToolContext` **at
> runtime** — `from google.adk.tools.tool_context import ToolContext` — never
> under `TYPE_CHECKING`. With `from __future__ import annotations`, ADK
> evaluates the annotation via `typing.get_type_hints()`, so a
> `TYPE_CHECKING`-only import raises `NameError` at request time and breaks
> *every* tool in the module. See the comment block in `app/agents/common.py`.

**`app/agents/<name>/agent.py`** — the spec:

```python
"""The weather agent.

A focused leaf agent (no peers) that reports weather conditions.
"""

from __future__ import annotations

from app.agents.base import AgentSpec
from app.agents.weather.tools import forecast

SPEC = AgentSpec(
    name="weather",
    description="Reports current conditions and forecasts for a location.",
    instruction=(
        "You are a weather specialist. You receive a JSON request with a "
        "`location`, an optional `when` field, and a `case_id`.\n\n"
        "- Use the `forecast` tool rather than answering from memory, then "
        "summarize briefly.\n"
        "- The request is all the context you have: you cannot see the "
        "conversation it came from."
    ),
    tier="balanced",
    tools=(forecast,),
)
```

Notes on the fields:

- **`description` is not documentation.** It is published in this agent's A2A
  agent card, and it is what the orchestrator's planner LLM reads to decide
  whether to delegate here. A vague description is the most common reason a new
  agent never gets called. Write it as a capability statement.
- **`tier`** is one of `fast` / `balanced` / `capable`, resolved to a live Gemini
  model at startup. **Every agent in this repo currently uses `balanced`**, after
  `fast` was measured proposing a gated action only 3/5 times when asked
  ([`design-decisions.md`](design-decisions.md)). Drop to `fast` only for an agent
  that calls no tools, and verify it still does what you asked.
- **`tools`** — this agent's own tools only. There is deliberately no
  shared-context tool: context arrives in the payload.
- **`instruction`** — say what the payload fields are and that the agent cannot
  see the caller's conversation. A specialist that asks the caller to clarify
  something it was not sent will simply stall.
- **`peers`** — leave unset for a leaf agent. Set it only if *this* agent
  delegates onward (see [step 3](#3-wire-up-delegation)).

Absolute imports only: `from app.agents.base import ...`, never `from ..base`.
Ruff's `TID252` rejects parent-relative imports across subpackages.

---

## 2. Register it

`app/agents/__init__.py` is the single source of truth for which agents exist:

```python
from app.agents.weather.agent import SPEC as WEATHER

AGENTS: dict[str, AgentSpec] = {
    spec.name: spec for spec in (ORCHESTRATOR, RESEARCH, MATH, PLANNER, WEATHER)
}
```

`AGENT_NAME` selects the agent at startup, and the same container image already
runs all of them.

---

## 2b. Declare its request contract (optional, but do it)

**Optional in the mechanism, required by this repo's policy.** The default
contract between two agents here is *one free-text task plus a `case_id`*: an
agent with no entry in `PAYLOADS` is still reachable and still answers, its tool
falling back to `UnknownPeerRequest`. Declaring a model is how you opt into
something stricter — named parameters, validation before the call leaves the
pod, and a JSON Schema published in the agent's card.

Do it for an agent you own. A single free-text field is where a caller quietly
starts pasting conversation context back in — the transcript returning a
paragraph at a time, which is the thing explicit delegation exists to prevent.
Leave it undeclared only for a peer another squad owns, whose real schema lives
in its own agent card. `app/agents/contracts.py`'s module docstring lays out all
three tiers.

**It also fails quietly**, which is why it is the step people forget: nothing
errors, the caller just sends prose.

`app/agents/contracts.py`:

```python
class WeatherRequest(PeerRequest):
    """Ask the weather specialist about conditions at a location."""

    location: str = Field(
        description=(
            "The place to report on, e.g. 'Dublin, IE'. Be specific: the "
            "specialist cannot ask which Dublin you meant."
        )
    )
    when: str = Field(
        default="",
        description="Optional day or range, e.g. 'tomorrow' or '2026-08-20'.",
    )


PAYLOADS: dict[str, type[PeerRequest]] = {
    ...,
    "weather": WeatherRequest,
}
```

Two things matter here:

- **Every field needs a `description`.** It is compiled into the tool
  declaration the *calling* model sees, and published as JSON Schema in this
  agent's A2A card. It is the only instruction a caller gets about how to call
  you, so an undescribed field is a silently mis-filled one.
- **`case_id` and `document_refs` come free** from `PeerRequest`. Do not
  redeclare them, and never add a field for a document's *contents* — pass the
  reference and read it with `read_document`.

`test_agents.py::test_every_delegatable_agent_declares_a_contract` fails if you
skip this — the test is what makes a repo policy out of an optional mechanism.

---

## 3. Wire up delegation

A new agent is reachable but never *reached* until something lists it as a peer.
Add it to the delegating agent's spec — normally the orchestrator, in
`app/agents/orchestrator/agent.py`:

```python
SPEC = AgentSpec(
    ...,
    peers=("research", "math", "planner", "trades", "weather"),
)
```

This is the **default** topology; `A2A_PEERS` overrides it at deploy time
without a rebuild, which is handy for testing an agent in isolation.

**The orchestrator is not the only agent that may list peers.** `math` declares
`peers=("currency",)` and delegates conversions on, so
`orchestrator -> math -> currency` is an ordinary chain of A2A calls. If your
agent belongs behind a specialist rather than beside one, add it to that
specialist's spec instead — nothing in `build_agent` treats depth specially, and
the rules do not loosen further down. Two things then differ from the common
case: the calling agent needs its own `A2A_PEERS` locally (see the Makefile's
`MATH_PEERS`), and the NetworkPolicy needs a rule naming *that* caller rather
than the orchestrator (step 6d).

---

## 4. Update the tests

Exactly two tests assert the registry contents, and both fail until updated
(verified):

- `tests/unit/test_agents.py::test_registry_lists_expected_agents`
- `tests/unit/test_agents.py::test_declared_peer_topology`

```python
def test_registry_lists_expected_agents():
    assert set(AGENTS) == {
        "orchestrator",
        "research",
        "math",
        "planner",
        "trades",
        "currency",
        "weather",
    }


def test_declared_peer_topology():
    assert AGENTS["orchestrator"].peers == (
        "research",
        "math",
        "planner",
        "trades",
        "weather",
    )
    assert AGENTS["math"].peers == ("currency",)
    assert AGENTS["weather"].peers == ()
```

Add tests for your tool's behaviour alongside them — `test_agents.py` has
examples for `calculate` covering both the success and error paths.

Then:

```bash
GEMINI_FAST_MODEL=gemini-2.5-flash-lite \
GEMINI_BALANCED_MODEL=gemini-2.5-flash \
GEMINI_CAPABLE_MODEL=gemini-2.5-pro \
GEMINI_EMBEDDING_MODEL=gemini-embedding-001 \
  uv run pytest tests/unit app/shared/tests -q

agents-cli lint
```

> The `GEMINI_*_MODEL` pins are **mandatory**. Importing `app` resolves the
> model tiers against the live Vertex catalog and *raises* on failure, so
> unpinned tests are neither hermetic nor offline-safe.

---

## 5. Try it locally before touching the cluster

Run the new agent and the orchestrator as two processes and exercise the real
A2A hop:

```bash
# Terminal 1 — the new specialist
AGENT_NAME=weather APP_URL=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8091

# Terminal 2 — an orchestrator that can reach only it
AGENT_NAME=orchestrator A2A_PEERS=weather=http://127.0.0.1:8091 \
  uv run uvicorn app.fast_api_app:app --port 8090
```

`APP_URL` matters: it is the URL the agent advertises in its own agent card. It
defaults to `http://0.0.0.0:8000`, which the caller cannot reach.

Confirm the card is being served, which is what peer discovery actually fetches:

```bash
curl -s http://127.0.0.1:8091/a2a/app/.well-known/agent-card.json | jq '.name, .description'
```

Both default to in-memory session/task storage, so no database is needed here.

---

## 6. Cluster resources

Five files, all in `infra/`. Skip this section entirely if you only run locally.

### 6a. Terraform — the agent's identity

`infra/terraform/variables.tf`, the `agents` default:

```hcl
default = ["orchestrator", "research", "math", "planner", "trades", "currency", "weather"]
```

Then `terraform apply`. This creates a **Google service account** (GSA)
`agent-weather@<project>`, its IAM roles, a **Workload Identity** binding — the
mechanism that lets a Kubernetes pod authenticate to Google Cloud as that
service account, with no key file — and its AlloyDB IAM database user. All of it
is driven by `for_each` over that list, so there is nothing else to write.

Each agent gets its own identity so that a compromised or misbehaving agent has
only the permissions it needs. That is why this step is per-agent rather than
shared.

If this agent needs cloud permissions the others should not have, that is the
whole point of separate identities:

```hcl
agent_extra_iam_roles = { weather = ["roles/..."] }
```

Grab the generated values:

```bash
terraform output -json kustomize_values
```

### 6b. `serviceaccounts.yaml` — the Kubernetes identity

```yaml
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agent-weather
  labels:
    app.kubernetes.io/component: specialist
  annotations:
    iam.gke.io/gcp-service-account: agent-weather@PROJECT.iam.gserviceaccount.com
```

This is the **Kubernetes service account** (KSA) the pod runs as; the annotation
is what pairs it with the Google service account from the previous step. The KSA
name must be `agent-<name>`, because Terraform's Workload Identity binding names
the pair `<namespace>/<ksa>` explicitly — a mismatch means the pod silently falls
back to the node's identity and Vertex calls fail with `403`.

### 6c. `workers.yaml` — Deployment + Service

Copy an existing pair and change four values. The Service name **must** equal the
agent name.

```yaml
    spec:
      serviceAccountName: agent-weather
      containers:
        - name: agent
          image: agent-image
          # ...
          env:
            - name: AGENT_NAME
              value: weather
            - name: APP_URL
              value: http://weather.agents.svc.cluster.local
            - name: ALLOYDB_IAM_USER
              value: agent-weather@PROJECT.iam    # no .gserviceaccount.com
```

Leave `DB_SCHEMA` unset — it defaults to `AGENT_NAME`.

### 6d. `networkpolicy.yaml` — otherwise it gets no traffic at all

This is the easiest step to miss and the failure mode is a **connection
timeout**, not an error message. `default-deny-ingress` denies everything, so an
agent absent from the allow-list is simply unreachable:

```yaml
  podSelector:
    matchExpressions:
      - key: app
        operator: In
        values: ["research", "math", "planner", "trades", "weather"]
```

If the new agent is *called by* something other than the orchestrator, add a
rule rather than widening this one — the policy is meant to mirror
`AgentSpec.peers`, so keep the two in sync. `currency-ingress-from-math` in the
same file is the worked example: `currency` accepts traffic from `math` and from
nobody else, the orchestrator included.

### 6e. `migrate-job.yaml` — give it a schema

Append your agent to the space-separated list — whatever it currently holds:

```yaml
            - name: MIGRATE_AGENTS
              value: "orchestrator research math trades currency weather"
```

The Job loops over this list, creating each schema, running every migration in
it, and granting that agent's IAM role access to its own schema and nothing
else.

This list is *agents that need a database schema*, which is not necessarily
every registered agent — one deployed with `DB_BACKEND=none` has no schema and
no database role, so it does not belong here. Everything else does.

> The `replicas:` list in `infra/kustomize/overlays/dev/kustomization.yaml` is
> **optional** — an agent not listed there just uses the base's replica count
> (verified). Add an entry only if you want a different number.

---

## 7. Database: nothing to do

Worth stating explicitly, because it looks like it should need work:

- **No new migration.** Every agent gets the *same* tables in its *own* schema.
  The existing revisions run once per schema, each with its own
  `alembic_version`, so a new agent starts at `head` on first migration.
- **No new database.** One database, one schema per agent — see the reasoning in
  [`../GKE.md`](../GKE.md#durable-storage-on-alloydb).
- **No new grants to write.** Revision `0003` reads `DB_AGENT_ROLE`, which the
  Job derives from the agent name.

Write a migration only if your agent needs tables of *its own*, beyond sessions
and A2A tasks.

---

## 8. Deploy

```bash
REPO=$(cd infra/terraform && terraform output -raw artifact_registry_repo)

# --platform linux/amd64 is required: the Autopilot nodes are amd64, so an
# arm64 workstation's native build fails on the cluster with "exec format
# error". (podman accepts the same arguments as docker.)
docker build --platform linux/amd64 -t "$REPO/agent:latest" .
docker push "$REPO/agent:latest"

# The Job's spec is immutable, so clear the previous run.
kubectl -n agents delete job/agent-migrate --ignore-not-found
kubectl apply -k infra/kustomize/overlays/dev

# Let the schema land first: agents have no CREATE privilege by design and
# crash-loop until their schema exists.
kubectl -n agents wait --for=condition=complete job/agent-migrate --timeout=10m
kubectl -n agents rollout status deploy/weather
```

Verify the agent is actually discoverable, not just Running:

```bash
kubectl -n agents port-forward svc/weather 8080:80
curl -s localhost:8080/a2a/app/.well-known/agent-card.json | jq .name
```

Then send the orchestrator a request that should route to it, and confirm the
delegation in Cloud Trace — one trace spans every A2A hop.

---

## Checklist

```
--- code; enough to run locally (steps 0-5) ---
[ ] Name is a single lowercase word, identical in all six places
[ ] app/agents/<name>/__init__.py has a docstring          (ruff D104)
[ ] tools.py: Google-style docstrings; ToolContext imported at runtime
[ ] agent.py: SPEC with a capability-style description
[ ] Registered in app/agents/__init__.py
[ ] Request contract added to app/agents/contracts.py + PAYLOADS
[ ] Every contract field has a Field(description=...)
[ ] Added to the delegating agent's AgentSpec.peers
[ ] test_registry_lists_expected_agents updated
[ ] test_declared_peer_topology updated
[ ] Instruction tells the agent it cannot see the caller's conversation
[ ] Tool tests added
[ ] pytest + agents-cli lint green
[ ] Exercised locally over a real A2A hop
--- cluster only (steps 6-8) ---
[ ] var.agents updated, terraform apply run
[ ] serviceaccounts.yaml: KSA agent-<name> + GSA annotation
[ ] workers.yaml: Deployment + Service (AGENT_NAME, APP_URL, ALLOYDB_IAM_USER)
[ ] networkpolicy.yaml: allowed ingress from whichever agents call it
[ ] migrate-job.yaml: MIGRATE_AGENTS updated
[ ] Image rebuilt --platform linux/amd64 and pushed
[ ] Migration Job completed BEFORE judging the pods
[ ] Agent card served; delegated request returns a correct answer
```

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `KeyError: Unknown AGENT_NAME 'weather'` at startup | Not registered | Add it to `AGENTS` in `app/agents/__init__.py` |
| Orchestrator never delegates to it | `description` too vague, or not in `peers` | Rewrite as a capability statement; check `AgentSpec.peers` / `A2A_PEERS` |
| Caller sends one vague `request` string | No entry in `PAYLOADS` | Add the contract (step 2b); the tool fell back to `UnknownPeerRequest` |
| Agent replies asking for context it was never sent | Its instruction assumes a conversation | It only ever sees the payload — add the missing field to its contract |
| `Invalid payload for peer '<name>'` | Caller omitted a required field | Give the field a default, or make its `description` clearer |
| Delegation hangs, then times out | Missing from `networkpolicy.yaml`, or allowed only from the orchestrator when the caller is another specialist | Add a rule naming the actual caller (step 6d) |
| Delegation hangs, agent looks healthy | `APP_URL` wrong | Must equal `http://<service>.<namespace>.svc.cluster.local` |
| Peer agent card `404` | Card is at `<svc>/a2a/app/.well-known/agent-card.json`, not the service root | Check `A2A_RPC_PATH` and that `App(name=...)` is still `"app"` |
| DNS `NXDOMAIN` for the peer | Service name ≠ agent name | They must be identical |
| `403` on Vertex calls | KSA/GSA mismatch | `serviceAccountName` must be `agent-<name>` and the annotation must match Terraform |
| `NameError: name 'ToolContext' is not defined` on any tool call | `ToolContext` imported under `TYPE_CHECKING` | Import it at runtime |
| Pod crash-loops on a missing relation | Migration Job has not run for this agent | Add it to `MIGRATE_AGENTS` and re-run the Job |
| `permission denied for schema weather` | `DB_AGENT_ROLE` wrong at migration time | Must be `agent-<name>@<project>.iam`, no `.gserviceaccount.com` |
| Terraform: role exceeds 63 bytes | Name plus prefix too long | Shorten the agent name or `service_account_prefix` |
| `exec format error` | arm64 image on amd64 nodes | Rebuild with `--platform linux/amd64` |

---

## Removing an agent

Reverse the same list. Two things do not clean themselves up:

- Its **PostgreSQL schema** persists. Dropping it is a manual
  `DROP SCHEMA "<name>" CASCADE`, deliberately — it holds conversation history.
- Its **GSA and AlloyDB user** are removed by `terraform apply` once the name
  leaves `var.agents`, which also revokes its database access.

Remove it from every other agent's `peers` first, or the survivors will try to
resolve an agent card that no longer exists.
