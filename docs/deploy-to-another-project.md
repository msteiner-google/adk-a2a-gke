# Deploying this repo to a different Google Cloud project

How to take **this repository** (not the upstream template) and stand it up in a
Google Cloud project of your own.

The repo currently contains a working deployment targeting project `msteiner` in
`europe-west4`. Five values are specific to that project; everything else is
portable. This guide covers what to change, in what order, and how to verify it.

For *what* the system is and how it works, read [`../GKE.md`](../GKE.md). This
document is only about getting it running somewhere else.

---

## 1. Prerequisites

### Tools

| Tool | Notes |
| --- | --- |
| `gcloud` | Authenticated: `gcloud auth login` **and** `gcloud auth application-default login`. |
| `kubectl` | Installed via `gcloud components install kubectl` or your package manager. |
| `uv` | Python runner. All Python goes through `uv run` — never bare `python`. |
| OpenTofu `>= 1.6` **or** Terraform `>= 1.6` | See the version note below. |
| `podman` or `docker` | To build and push the container image. |

**Terraform version note.** `infra/terraform/versions.tf` sets
`required_version = ">= 1.6"`. Homebrew's `terraform` formula is frozen at
**1.5.7** (the last MPL-licensed release), which **fails this constraint**:

```
Error: Unsupported Terraform Core version
```

This deployment was performed with **OpenTofu 1.12.3** (`brew install opentofu`),
which runs the configuration unmodified. Substitute `tofu` for `terraform` in
every command below, or install Terraform `>= 1.6` from `hashicorp/tap`. Do not
lower the constraint.

### Google Cloud

- A project with **billing enabled**. GKE Autopilot is not free; see
  [Teardown](#8-teardown).
- Quota for a regional Autopilot cluster in your chosen region.
- Vertex AI (Gemini) available to the project.

Terraform enables the required APIs itself (`container`, `artifactregistry`,
`aiplatform`, `compute`, `iam`, `cloudresourcemanager`, `logging`, `monitoring`,
`cloudtrace`, `telemetry`), so you don't need to pre-enable them — but you need
the permissions to do so.

### IAM for the person running Terraform

`roles/owner` covers everything. Granular equivalent:

| Role | Needed for |
| --- | --- |
| `roles/serviceusage.serviceUsageAdmin` | Enabling the APIs above |
| `roles/container.admin` | Creating the Autopilot cluster |
| `roles/artifactregistry.admin` | Creating the Docker repo |
| `roles/iam.serviceAccountAdmin` | Creating the runtime GSA |
| `roles/resourcemanager.projectIamAdmin` | Granting the GSA its project roles |

The **runtime** service account's roles are managed by Terraform
(`var.agent_iam_roles`) and need no manual action.

---

## 2. The five project-specific values

Everything you must change. Nothing else in the repo hardcodes a project.

| # | File | What | Current value |
| --- | --- | --- | --- |
| 1 | `infra/terraform/<name>.tfvars` | `project_id`, `region` | `msteiner`, `europe-west4` |
| 2 | `infra/kustomize/base/serviceaccounts.yaml` | Workload Identity GSA email on **each** of the 4 ServiceAccounts | `agent-<name>@msteiner.iam.gserviceaccount.com` |
| 3 | `infra/kustomize/overlays/dev/kustomization.yaml` | Image path | `europe-west4-docker.pkg.dev/msteiner/agents/agent` |
| 4 | `infra/kustomize/base/configmap.yaml` | `OTEL_RESOURCE_ATTRIBUTES` | `gcp.project_id=msteiner` |
| 5 | `infra/kustomize/base/configmap.yaml` | `ALLOYDB_INSTANCE_URI` | `projects/msteiner/locations/europe-west4/...` |
| 6 | `orchestrator.yaml`, `workers.yaml` | `ALLOYDB_IAM_USER` per agent | `agent-<name>@msteiner.iam` |
| 7 | `infra/kustomize/base/migrate-job.yaml` | `ALLOYDB_IAM_USER`, `AGENT_ROLE_SUFFIX` | `agent-migrator@msteiner.iam`, `msteiner.iam` |
| 8 | `.env` (local dev only) | `GOOGLE_CLOUD_PROJECT` | `msteiner-kubeflow` |

Items 2 and 5–7 all come from one command: `tofu output -json kustomize_values`.

Items 2–4 are all derivable from Terraform outputs after `apply` — see step 4.

**#4 is easy to miss.** It is not a cosmetic label: the Cloud Trace ingest
endpoint rejects any span batch whose OpenTelemetry resource lacks
`gcp.project_id`, so leaving another project's id there means **all tracing
silently fails** with a repeating `400 Bad Request` in the pod logs. Nothing else
breaks, so it's easy to ship without noticing.

### Not project-specific — leave alone

- `infra/kustomize/base/configmap.yaml` → `GOOGLE_GENAI_USE_ENTERPRISE: "true"`.
  Required for **all** deployments. Without it, ADK reports Developer-API mode
  while the client is Vertex, and every A2A delegation fails with
  `ValueError: part_metadata parameter is only supported in Gemini Developer API mode`.
  Filed upstream as `personal/adk-template#11`.
- `GOOGLE_CLOUD_LOCATION: "global"` is the **Vertex AI** endpoint and is
  independent of your cluster region. Changing the cluster region does not
  require changing this.
- `GOOGLE_CLOUD_PROJECT` is deliberately **not** set in the ConfigMap — the pods
  resolve it from Workload Identity (ADC), so Vertex calls bill to the cluster's
  project automatically. Only set it if you want Vertex in a *different* project
  from the cluster.

### Unrelated leftover

`deployment/terraform/single-project/vars/env.tfvars` contains
`project_id = "msteiner-kubeflow"`. That is the **base template's Cloud Run
CI/CD** path and is *not used* by this GKE deployment. Ignore it, or update it if
you intend to use that path. `infra/` and `deployment/` are separate on purpose.

---

## 3. Provision infrastructure

```bash
cd infra/terraform
```

Create a tfvars file named for your project (the repo keeps one per target):

```hcl
# infra/terraform/my-project.tfvars
project_id = "my-project"
region     = "europe-west1"

# Optional overrides (defaults shown):
#   cluster_name               = "agents-cluster"
#   artifact_repo_id           = "agents"
#   namespace                  = "agents"    # must match kustomize
#   service_account_prefix     = "agent"     # must match kustomize
#   agents                     = ["orchestrator", "research", "math"]

# true blocks `destroy` on the cluster. Use false for a throwaway environment.
deletion_protection = false
```

> A **named** tfvars file is not auto-loaded. You must pass `-var-file` on
> *every* `plan` / `apply` / `destroy`, or you silently get the defaults from
> `variables.tf` (region `us-central1`) and an interactive prompt for
> `project_id`. Only a file named exactly `terraform.tfvars` is picked up
> automatically.

> If you change `namespace`, `service_account_prefix`, or `agents`, you must
> make the same change in `infra/kustomize/base/` — the Workload Identity
> binding is `serviceAccount:<project>.svc.id.goog[<namespace>/<ksa>]` and will
> not match otherwise.

> **Region matters for AlloyDB.** The default `c4a-highmem-1` machine type is
> Arm (Axion) and is not offered in every region. `europe-west4` and
> `us-central1` are covered; check
> <https://cloud.google.com/alloydb/docs/choose-machine-type> first.

```bash
tofu init
tofu plan  -var-file=my-project.tfvars      # expect ~19 resources to add
tofu apply -var-file=my-project.tfvars
```

Autopilot cluster creation takes **6–10 minutes**.

This creates: the Autopilot cluster, an Artifact Registry Docker repo, one GSA
per agent (`agent-<name>@<project>.iam.gserviceaccount.com`) plus
`agent-migrator`, their IAM roles and Workload Identity bindings, and the
AlloyDB cluster/instance with its private-services-access peering. AlloyDB adds
several minutes on top of the cluster.

### State is local

There is **no remote backend configured**. State lands in
`infra/terraform/terraform.tfstate` on the machine that ran `apply`, so only that
machine can manage or destroy the deployment. For anything shared or long-lived,
add a GCS backend before the first `apply`.

`.gitignore` does **not** currently cover `terraform.tfstate` or `*.tfvars` (it
only lists `.terraform*` and `my_env.tfvars`). State can contain sensitive
values — add these before committing:

```gitignore
*.tfstate
*.tfstate.*
*.tfvars
!*.tfvars.example
```

---

## 4. Point kubectl and the manifests at the new project

```bash
eval "$(tofu output -raw get_credentials_command)"
kubectl config current-context      # gke_<project>_<region>_agents-cluster
```

Read the values you need:

```bash
tofu output -raw artifact_registry_repo   # <region>-docker.pkg.dev/<project>/agents
tofu output -json kustomize_values        # every manifest placeholder
```

Apply them to items 2-7 from the table:

- `infra/kustomize/base/serviceaccounts.yaml` → the
  `iam.gke.io/gcp-service-account` annotation on each ServiceAccount, from
  `.service_account_annotations`
- `infra/kustomize/overlays/dev/kustomization.yaml` →
  `newName: <artifact_registry_repo>/agent`
- `infra/kustomize/base/configmap.yaml` →
  `OTEL_RESOURCE_ATTRIBUTES: "gcp.project_id=<project>"` and
  `ALLOYDB_INSTANCE_URI` from `.configmap`
- `orchestrator.yaml` / `workers.yaml` / `migrate-job.yaml` →
  `ALLOYDB_IAM_USER`, from `.agent_iam_users`

Verify the render before applying anything:

```bash
cd ../..                                  # repo root
kubectl kustomize infra/kustomize/overlays/dev | rg 'image:|gcp-service-account|gcp.project_id'
```

No `PROJECT_ID` or `REGION` placeholder should survive.

---

## 5. Build and push the image

One image serves all three agents; `AGENT_NAME` selects the role at startup.

```bash
REPO=$(cd infra/terraform && tofu output -raw artifact_registry_repo)

# Auth the builder to Artifact Registry (avoids the gcloud credential helper).
podman login -u oauth2accesstoken -p "$(gcloud auth print-access-token)" "${REPO%%/*}"

podman build --platform linux/amd64 -t "$REPO/agent:latest" .
podman push "$REPO/agent:latest"
```

**`--platform linux/amd64` is mandatory on Apple Silicon.** GKE Autopilot nodes
are amd64 and nothing in these manifests requests arm64. A native arm64 build
pushes fine and then fails at runtime with `exec format error`, which reads like
an application bug rather than a build one. The cross-build works (the
`python:3.12-slim` base is multi-arch); it's just slower under emulation.

Confirm before pushing:

```bash
podman image inspect "$REPO/agent:latest" --format '{{.Os}}/{{.Architecture}}'   # linux/amd64
```

Substitute `docker` freely — the flags are identical.

---

## 6. Deploy

```bash
kubectl apply -k infra/kustomize/overlays/dev

for d in orchestrator research math; do
  kubectl -n agents rollout status deploy/$d --timeout=420s
done
```

On a cold Autopilot cluster the first rollout can take a few minutes while nodes
scale up; transient `FailedScheduling` / `Too many pods` events during that
window are normal and resolve themselves.

```bash
kubectl -n agents get pods,svc
```

All three pods should be `1/1 Running`.

---

## 7. Verify it actually works

**Pods reaching `Running` is not sufficient.** The readiness probe is a bare TCP
check on port 8080, and a leaf agent answers correctly even when delegation is
broken. Test a *delegated* request.

```bash
kubectl -n agents port-forward svc/orchestrator 8080:80
```

In another shell:

```bash
# 1. Agent card is served (note the /a2a/app prefix — not the service root)
curl -s http://127.0.0.1:8080/a2a/app/.well-known/agent-card.json | head

# 2. End-to-end delegation through a specialist
curl -s -X POST http://127.0.0.1:8080/apps/app/users/u1/sessions/s1 \
  -H 'Content-Type: application/json' -d '{}'

curl -s -X POST http://127.0.0.1:8080/run \
  -H 'Content-Type: application/json' \
  -d '{"appName":"app","userId":"u1","sessionId":"s1",
       "newMessage":{"role":"user","parts":[{"text":"What is 1234 * 5678 + 42? Use your math specialist."}]}}'
```

A correct run shows `transfer_to_agent` from `orchestrator`, then an answer
authored by `math` (`7006694`).

Check the logs are clean — in particular that tracing is exporting:

```bash
kubectl -n agents logs deploy/orchestrator --tail=100 | rg 'Failed to export span batch' || echo "tracing OK"
```

Some `UserWarning: [EXPERIMENTAL] RemoteA2aAgent / A2aAgentExecutor` noise at
startup is expected and harmless.

The orchestrator Service is `ClusterIP`, so `port-forward` is the only access
path until you switch it to a `LoadBalancer` or put an Ingress/Gateway in front.

---

## 8. Teardown

Autopilot bills continuously. To remove everything:

```bash
kubectl delete -k infra/kustomize/overlays/dev      # optional; destroy covers the cluster
cd infra/terraform
tofu destroy -var-file=my-project.tfvars
```

`destroy` fails on the cluster if `deletion_protection = true` — set it to
`false` and re-apply first.

The enabled APIs are intentionally left on (`disable_on_destroy = false`), since
other workloads in the project may depend on them.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Error: Unsupported Terraform Core version` | Terraform 1.5.7 vs the `>= 1.6` floor | Use `tofu`, or Terraform ≥ 1.6 from `hashicorp/tap` |
| Terraform `Error 409: already exists` | Resource pre-exists outside state | `tofu import` it — do not retry creation |
| Pods `CrashLoopBackOff`, `exec format error` | arm64 image on amd64 nodes | Rebuild with `--platform linux/amd64` |
| `ValueError: part_metadata ... only supported in Gemini Developer API mode` | `GOOGLE_GENAI_USE_ENTERPRISE` missing from the ConfigMap | Restore it (see §2) |
| `Failed to export span batch code: 400` | Resource missing `gcp.project_id` | Fix `OTEL_RESOURCE_ATTRIBUTES` (item 4) |
| Model `404` at pod startup | Wrong Vertex location, not a bad model name | Keep `GOOGLE_CLOUD_LOCATION: "global"`. **Never** change the model to "fix" this |
| `403` on Vertex calls | Workload Identity not bound | Confirm each GSA annotation matches `tofu output -json kustomize_values` and that each pod uses `serviceAccountName: agent-<name>` |
| Pods `CrashLoopBackOff` with a missing-relation or permission error on `sessions`/`tasks` | The migration Job has not run; agents have no CREATE privilege by design | `kubectl -n agents wait --for=condition=complete job/agent-migrate --timeout=10m` |
| Migration Job: `Job ... field is immutable` | A previous Job of the same name exists | `kubectl -n agents delete job/agent-migrate` then re-apply |
| `Schema version not found in adk_internal_metadata` | Tables created outside Alembic | Migration `0001` seeds `schema_version='1'`; recreate the schema with Alembic |
| AlloyDB `403`/`PermissionDenied` on connect | Missing one of the three required roles | Needs `alloydb.client` **and** `alloydb.databaseUser` **and** `serviceusage.serviceUsageConsumer` |
| Peer agent card `404` | Card lives at `<svc>/a2a/app/.well-known/agent-card.json`, not the service root | Check `A2A_RPC_PATH` and `APP_URL` |
| Pods start but delegation hangs | `APP_URL` wrong for a Deployment | Must equal `http://<service>.<namespace>.svc.cluster.local` |

### Renaming or adding agents

The agent name is load-bearing in four places at once: the directory
`app/agents/<name>/`, `AgentSpec.name`, the Kubernetes Service name, and the
`AGENT_NAME` env value. They must be identical, and the name must be a valid
Python identifier *and* a valid DNS label — so a single lowercase word, no
hyphens, no underscores. See "Add an agent" in [`../AGENTS.md`](../AGENTS.md).

---

## 10. Checklist

```
[ ] Tooling installed; tofu/terraform >= 1.6 confirmed
[ ] gcloud auth login + application-default login
[ ] Target project has billing enabled
[ ] infra/terraform/<name>.tfvars created (project_id, region, deletion_protection)
[ ] tofu apply -var-file=<name>.tfvars succeeded (AlloyDB adds several minutes)
[ ] kubectl context points at the new cluster
[ ] serviceaccounts.yaml: all 4 GSA emails updated (item 2)
[ ] kustomization.yaml image path updated          (item 3)
[ ] configmap.yaml OTEL_RESOURCE_ATTRIBUTES updated(item 4)
[ ] configmap.yaml ALLOYDB_INSTANCE_URI updated    (item 5)
[ ] ALLOYDB_IAM_USER set per agent + migrate-job   (items 6-7)
[ ] kubectl kustomize shows no PROJECT_ID/REGION placeholders
[ ] Image built --platform linux/amd64, verified, pushed
[ ] job/agent-migrate completed BEFORE judging the agents
[ ] All three deployments rolled out 1/1
[ ] Delegated request returns a correct answer      (not just "pods Running")
[ ] No "Failed to export span batch" in logs
[ ] .gitignore covers *.tfstate and *.tfvars before committing
```
