# Object storage for agent artifacts.
#
# An artifact is a blob an agent produces or consumes -- a generated report, a
# fetched page, an image -- kept out of the conversation history and referenced
# by filename and version. The app side is app/cluster/artifacts.py, which binds
# app/shared/artifacts.py's cloudpathlib-backed ADK artifact service to whatever
# ARTIFACT_STORAGE_URI names. This file provisions the bucket that URI points at
# when the deployment runs on GCS.
#
# Nothing here is GCS-specific on the application side: the same image runs
# against s3:// or az:// by changing the ConfigMap value, because cloudpathlib
# resolves the scheme. GCS is provisioned because that is where this cluster
# runs and Workload Identity makes the credentials free.
#
# WHY ONE SHARED BUCKET AND NOT ONE PER AGENT.
#
# Unlike AlloyDB -- where each agent gets its own PostgreSQL schema, granted to
# its own IAM role -- artifacts are deliberately a shared namespace. ADK keys
# them by {app_name}/{user_id}/{session_id}/{filename}, and app_name is the ADK
# App name ("app") for every agent in this cluster, not AGENT_NAME. A shared
# location is therefore what lets `research` save a document that the
# orchestrator can load back on the same session, mirroring how `shared:`
# session state propagates across an A2A hop.
#
# The cost is that a prompt-injected `research` can read and overwrite artifacts
# belonging to any session, since roles/storage.objectUser is bucket-wide. To
# tighten that without giving up cross-agent sharing, add an IAM condition on
# resource.name.startsWith(...) per agent, or move to per-agent buckets and
# accept that agents can no longer exchange artifacts by reference.

locals {
  # Default to a project-scoped name; GCS bucket names are globally unique.
  artifact_bucket_name = coalesce(
    var.artifact_bucket_name,
    "${var.project_id}-agent-artifacts",
  )

  # The value that goes in the ConfigMap as ARTIFACT_STORAGE_URI. The prefix
  # keeps the artifact tree from colliding with anything else in the bucket.
  artifact_storage_uri = "gs://${local.artifact_bucket_name}/${var.artifact_storage_prefix}"
}

resource "google_storage_bucket" "artifacts" {
  count = var.enable_artifact_storage ? 1 : 0

  project  = var.project_id
  name     = local.artifact_bucket_name
  location = var.region

  # Uniform access: per-object ACLs cannot express "this service account may
  # read the whole prefix" and are the usual source of accidental exposure.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Object versioning is off on purpose: the artifact service versions
  # explicitly (each save writes a new `<filename>/<n>` object and never
  # mutates an existing one), so bucket versioning would only duplicate that at
  # extra cost.
  versioning {
    enabled = false
  }

  # Optional TTL. Artifacts are conversation-scoped working data, so leaving
  # them forever is rarely what you want; null disables the rule entirely.
  dynamic "lifecycle_rule" {
    for_each = var.artifact_retention_days == null ? [] : [var.artifact_retention_days]
    content {
      condition {
        age = lifecycle_rule.value
      }
      action {
        type = "Delete"
      }
    }
  }

  # Guard real data; set true for throwaway/dev projects so `terraform destroy`
  # can remove a non-empty bucket.
  force_destroy = var.artifact_bucket_force_destroy

  depends_on = [google_project_service.services]
}

# Each agent reads and writes its own artifacts under Workload Identity. Granted
# on the bucket rather than the project so the agents get no access to any other
# bucket (Terraform state, logs, ...) in this project.
#
# roles/storage.objectUser, not objectAdmin: it carries get/list/create/delete
# on objects, which is exactly what the artifact service does, without the
# bucket-level setIamPolicy that objectAdmin implies.
resource "google_storage_bucket_iam_member" "agents_artifacts" {
  for_each = toset(var.enable_artifact_storage ? var.agents : [])

  bucket = google_storage_bucket.artifacts[0].name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.agents[each.key].email}"
}
