# GKE Autopilot cluster + Workload Identity + Artifact Registry for the
# multi-agent system.
#
# What this provisions:
#   1. The Google Cloud APIs the stack needs.
#   2. An Artifact Registry Docker repo to hold the agent image.
#   3. A GKE Autopilot cluster (Workload Identity is on by default in Autopilot).
#   4. One Google Service Account (GSA) PER AGENT, plus a privileged migrator
#      GSA, each with the IAM roles it needs.
#   5. Workload Identity bindings so each agent's Kubernetes ServiceAccount
#      (created by the kustomize manifests) acts as its own GSA — no exported
#      keys.
#
# AlloyDB lives in alloydb.tf and reuses the per-agent GSAs created here.
#
# The Kubernetes namespace, ServiceAccounts, and Deployments themselves are
# applied with kustomize (see ../kustomize). Keep var.namespace and var.agents
# here in sync with those manifests and with app/agents/.
#
# IDENTITY MODEL — why one GSA per agent rather than one shared GSA.
#
# The agents are not equally trusted. `research` calls web_search, so it ingests
# untrusted text from the open internet and is the natural target for prompt
# injection. Under a single shared identity a hijacked worker inherits every
# permission every agent has, and — once AlloyDB is in the picture — read/write
# access to every other agent's session and task rows.
#
# Splitting the identity is what makes the schema-per-agent database layout an
# enforced boundary rather than a naming convention: each GSA maps to exactly
# one AlloyDB IAM database role, granted on exactly one PostgreSQL schema (see
# alloydb.tf and app/migrations/versions/*_grant_agent_role.py). It also gives
# per-agent audit trails, and it is the piece that is genuinely painful to
# retrofit later, because it changes IAM, database grants, and audit history all
# at once.
#
# Namespaces are deliberately NOT split. A namespace is a policy/blast-radius
# boundary, not an identity boundary, and splitting it here is expensive: A2A
# peer discovery resolves every peer inside a single A2A_NAMESPACE (see
# app/cluster/config.py), so per-agent namespaces would need fully-qualified
# peer URLs plus a ConfigMap and a Workload Identity binding per namespace. The
# topology enforcement usually wanted from namespaces is provided instead by
# ../kustomize/base/networkpolicy.yaml.

locals {
  # Enable exactly the APIs this stack depends on.
  services = [
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudtrace.googleapis.com",
    "telemetry.googleapis.com",         # Telemetry (OTLP) API: ADK/shared trace export
    "alloydb.googleapis.com",           # AlloyDB cluster + instance
    "servicenetworking.googleapis.com", # Private services access peering for AlloyDB
  ]

  agent_identities = toset(var.agents)

  # GSA account ids. Keep these short: the AlloyDB IAM database role is derived
  # from the service account email and PostgreSQL truncates identifiers at 63
  # bytes (see the validation in alloydb.tf).
  agent_account_ids = {
    for name in local.agent_identities : name => "${var.service_account_prefix}-${name}"
  }

  # Kubernetes ServiceAccount per agent. Must match
  # ../kustomize/base/serviceaccounts.yaml and each Deployment's
  # serviceAccountName.
  agent_ksa_names = {
    for name in local.agent_identities : name => "${var.service_account_prefix}-${name}"
  }

  # Roles required to open an IAM-authenticated AlloyDB connection. All three
  # are needed and they cover disjoint things — the official docs are
  # inconsistent on this, so the reasoning is recorded here:
  #   - alloydb.client            : alloydb.instances.connect and
  #                                 clusters.generateClientCertificate, which
  #                                 the connector uses to build the mTLS tunnel.
  #   - alloydb.databaseUser      : carries alloydb.users.login, the actual IAM
  #                                 database login.
  #   - serviceUsageConsumer      : the permission-check API the connector calls.
  alloydb_client_roles = [
    "roles/alloydb.client",
    "roles/alloydb.databaseUser",
    "roles/serviceusage.serviceUsageConsumer",
  ]

  all_agent_roles = concat(var.agent_iam_roles, local.alloydb_client_roles)
}

resource "google_project_service" "services" {
  for_each = toset(local.services)

  project = var.project_id
  service = each.value

  # Keep APIs enabled if this config is destroyed; other workloads may need them.
  disable_on_destroy = false
}

# --- Artifact Registry -------------------------------------------------------

resource "google_artifact_registry_repository" "agents" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_repo_id
  description   = "Container images for the multi-agent system."
  format        = "DOCKER"

  depends_on = [google_project_service.services]
}

# --- GKE Autopilot cluster ---------------------------------------------------

resource "google_container_cluster" "autopilot" {
  name     = var.cluster_name
  project  = var.project_id
  location = var.region

  # Autopilot manages nodes, scaling, and security posture for you. Workload
  # Identity is enabled automatically on Autopilot clusters.
  enable_autopilot = true

  # Required for Autopilot / VPC-native clusters. Empty block => GKE-managed
  # secondary ranges on the default network.
  ip_allocation_policy {}

  # Guard real clusters; set var.deletion_protection = false for throwaway ones.
  deletion_protection = var.deletion_protection

  depends_on = [google_project_service.services]
}

# --- Workload Identity: one GSA per agent ------------------------------------
#
# NOTE ON UPGRADING AN EXISTING DEPLOYMENT: this replaces the former single
# `agents-runtime` service account. `terraform apply` will DESTROY that account
# and create one per agent. Any IAM grant made to `agents-runtime` outside this
# config must be re-applied to the new accounts.

resource "google_service_account" "agents" {
  for_each = local.agent_identities

  project      = var.project_id
  account_id   = local.agent_account_ids[each.key]
  display_name = "Agent runtime: ${each.key}"
  description  = "Workload Identity for the ${each.key} agent. Owns the '${each.key}' AlloyDB schema."

  depends_on = [google_project_service.services]
}

# Baseline project roles every agent needs (Vertex AI, telemetry, and the three
# roles required to open an IAM-authenticated AlloyDB connection).
#
# The flattened for_each key is "<agent>|<role>" so adding an agent or a role
# does not renumber the others in state.
resource "google_project_iam_member" "agents" {
  for_each = {
    for pair in setproduct(var.agents, local.all_agent_roles) :
    "${pair[0]}|${pair[1]}" => { agent = pair[0], role = pair[1] }
  }

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.agents[each.value.agent].email}"
}

# Per-agent additional roles. This is the point of splitting the identities:
# grant `research` only what `research` needs, without widening the others.
resource "google_project_iam_member" "agents_extra" {
  for_each = {
    for item in flatten([
      for agent, roles in var.agent_extra_iam_roles : [
        for role in roles : { key = "${agent}|${role}", agent = agent, role = role }
      ]
    ]) : item.key => item
  }

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.agents[each.value.agent].email}"
}

# Let each agent's Kubernetes ServiceAccount act as its own GSA. The kustomize
# ServiceAccount must carry the annotation:
#   iam.gke.io/gcp-service-account: <that agent's GSA email>
resource "google_service_account_iam_member" "workload_identity" {
  for_each = local.agent_identities

  service_account_id = google_service_account.agents[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${local.agent_ksa_names[each.key]}]"
}

# --- Workload Identity: the schema migrator ----------------------------------
#
# Runs Alembic as a Kubernetes Job. It owns every agent schema and is the only
# identity with DDL rights, so no long-running, model-driven process can alter
# the schema. Deliberately a separate identity from every agent.

resource "google_service_account" "migrator" {
  project      = var.project_id
  account_id   = var.migrator_service_account_id
  display_name = "Agent schema migrator"
  description  = "Runs Alembic migrations against AlloyDB. Owns all agent schemas."

  depends_on = [google_project_service.services]
}

resource "google_project_iam_member" "migrator" {
  for_each = toset(local.alloydb_client_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.migrator.email}"
}

resource "google_service_account_iam_member" "migrator_workload_identity" {
  service_account_id = google_service_account.migrator.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.migrator_service_account_id}]"
}
