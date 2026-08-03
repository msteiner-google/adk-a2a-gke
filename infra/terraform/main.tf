# GKE Autopilot cluster + Workload Identity + Artifact Registry for the
# multi-agent system.
#
# What this provisions:
#   1. The Google Cloud APIs the stack needs.
#   2. An Artifact Registry Docker repo to hold the agent image.
#   3. A GKE Autopilot cluster (Workload Identity is on by default in Autopilot).
#   4. A Google Service Account (GSA) the agents impersonate, with the IAM roles
#      needed to call Vertex AI and emit telemetry.
#   5. A Workload Identity binding so the Kubernetes ServiceAccount used by the
#      agent pods (created by the kustomize manifests) acts as that GSA — no
#      exported keys.
#
# The Kubernetes namespace, ServiceAccount, and Deployments themselves are
# applied with kustomize (see ../kustomize). Keep var.namespace and
# var.kubernetes_service_account here in sync with those manifests.

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
    "telemetry.googleapis.com", # Telemetry (OTLP) API: ADK/shared trace export
  ]

  # The Workload Identity principal for the pods' Kubernetes ServiceAccount.
  wi_member = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.kubernetes_service_account}]"
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

# --- Workload Identity: GSA the agents impersonate ---------------------------

resource "google_service_account" "agents" {
  project      = var.project_id
  account_id   = var.google_service_account_id
  display_name = "Multi-agent runtime service account"

  depends_on = [google_project_service.services]
}

# Project roles the agents need at runtime (Vertex AI + telemetry).
resource "google_project_iam_member" "agents" {
  for_each = toset(var.agent_iam_roles)

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agents.email}"
}

# Let the Kubernetes ServiceAccount (namespace/ksa) act as the GSA. The kustomize
# ServiceAccount must carry the annotation:
#   iam.gke.io/gcp-service-account: <this GSA email>
resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.agents.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.wi_member
}
