# The identity that builds and pushes the agent image (see ../../cloudbuild.yaml).
#
# WHY A DEDICATED SERVICE ACCOUNT
#
# Cloud Build's legacy default service account
# (<PROJECT_NUMBER>@cloudbuild.gserviceaccount.com) carries roles/editor on the
# whole project and, in projects created after mid-2024, is not provisioned at
# all -- `gcloud builds submit` then fails with an error about a missing
# service account that reads like an API-enablement problem. Both reasons point
# the same way: name the identity, and give it the three things a build needs.
#
# Nothing here is granted to the agents, and none of the agents' roles are
# granted here. A build can push an image; it cannot call Vertex AI, read the
# artifact bucket or connect to AlloyDB.

resource "google_service_account" "builder" {
  project      = var.project_id
  account_id   = var.builder_service_account_id
  display_name = "Cloud Build — agent image"
  description  = "Builds and pushes the agent container image. See cloudbuild.yaml."

  depends_on = [google_project_service.services]
}

# Push access to the agent repository ONLY -- repository-scoped, not the
# project-wide roles/artifactregistry.writer. A build that is compromised can
# overwrite the image it was built to produce, and no other.
resource "google_artifact_registry_repository_iam_member" "builder_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.agents.location
  repository = google_artifact_registry_repository.agents.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.builder.email}"
}

# Required by `options.logging: CLOUD_LOGGING_ONLY` in cloudbuild.yaml. Without
# it the build starts and then fails at the first log write, which surfaces as a
# build that produced no logs -- the least debuggable failure mode available.
resource "google_project_iam_member" "builder_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.builder.email}"
}

# `gcloud builds submit` stages the source tarball in a bucket it creates
# (gs://<project>_cloudbuild) and the build worker reads it back as this
# service account.
resource "google_project_iam_member" "builder_source" {
  project = var.project_id
  role    = "roles/storage.objectUser"
  member  = "serviceAccount:${google_service_account.builder.email}"
}

# Whoever runs `make image` has to be allowed to act as this account. Left empty
# by default: a human with roles/owner already can, and listing a principal here
# is how you extend that to a CI runner without giving it owner.
resource "google_service_account_iam_member" "builder_users" {
  for_each = toset(var.builder_impersonators)

  service_account_id = google_service_account.builder.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}
