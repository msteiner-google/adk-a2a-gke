# Useful values after `terraform apply`.

output "cluster_name" {
  description = "Name of the GKE Autopilot cluster."
  value       = google_container_cluster.autopilot.name
}

output "cluster_location" {
  description = "Location (region) of the cluster."
  value       = google_container_cluster.autopilot.location
}

output "get_credentials_command" {
  description = "Run this to point kubectl at the new cluster."
  value = join(" ", [
    "gcloud container clusters get-credentials",
    google_container_cluster.autopilot.name,
    "--region", google_container_cluster.autopilot.location,
    "--project", var.project_id,
  ])
}

output "artifact_registry_repo" {
  description = "Docker image path prefix for pushing agent images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.agents.repository_id}"
}

output "builder_service_account_email" {
  description = "GSA that Cloud Build runs as. Pass it to `gcloud builds submit --service-account` (which `make image` does)."
  value       = google_service_account.builder.email
}

output "build_command" {
  description = "Build and push the agent image with Cloud Build. Set the tag to match newTag in the kustomize overlay."
  value = join(" ", [
    "gcloud builds submit --config cloudbuild.yaml",
    "--project", var.project_id,
    "--service-account", "projects/${var.project_id}/serviceAccounts/${google_service_account.builder.email}",
    "--substitutions", "_REGION=${var.region},_REPO=${google_artifact_registry_repository.agents.repository_id},_TAG=<tag>",
  ])
}

output "agent_service_account_emails" {
  description = "Per-agent GSA emails. Each goes in that agent's kustomize ServiceAccount annotation (iam.gke.io/gcp-service-account)."
  value       = { for name, sa in google_service_account.agents : name => sa.email }
}

output "migrator_service_account_email" {
  description = "GSA for the Alembic migration Job's ServiceAccount annotation."
  value       = google_service_account.migrator.email
}

output "agent_database_roles" {
  description = "Per-agent AlloyDB IAM database role. This is the DB_AGENT_ROLE the migration Job grants on each agent's schema, and the ALLOYDB_IAM_USER each agent connects as."
  value       = local.agent_db_roles
}

output "alloydb_instance_uri" {
  description = "ALLOYDB_INSTANCE_URI for the agent ConfigMap. This is what the AlloyDB connector dials."
  value       = "projects/${var.project_id}/locations/${var.region}/clusters/${var.alloydb_cluster_id}/instances/${var.alloydb_instance_id}"
}

output "alloydb_private_ip" {
  description = "Private IP of the AlloyDB primary. Informational only; the connector resolves the instance itself."
  value       = google_alloydb_instance.primary.ip_address
}

output "alloydb_database" {
  description = "Database holding every agent's schema (created by the migration Job's bootstrap step)."
  value       = var.alloydb_database
}

output "artifact_storage_uri" {
  description = "ARTIFACT_STORAGE_URI for the agent ConfigMap: where app/shared/artifacts.py's cloudpathlib-backed service stores artifacts. Null when var.enable_artifact_storage is false."
  value       = var.enable_artifact_storage ? local.artifact_storage_uri : null
}

output "artifact_bucket_name" {
  description = "Name of the GCS bucket holding agent artifacts."
  value       = var.enable_artifact_storage ? google_storage_bucket.artifacts[0].name : null
}

output "kustomize_values" {
  description = "Everything the kustomize manifests need after an apply. Fill these into infra/kustomize/base/configmap.yaml, serviceaccounts.yaml and migrate-job.yaml."
  value = {
    configmap = {
      ALLOYDB_INSTANCE_URI = "projects/${var.project_id}/locations/${var.region}/clusters/${var.alloydb_cluster_id}/instances/${var.alloydb_instance_id}"
      DB_NAME              = var.alloydb_database
      DB_BACKEND           = "alloydb"
      SESSION_BACKEND      = "alloydb"
      TASK_STORE_BACKEND   = "database"
      ARTIFACT_STORAGE_URI = var.enable_artifact_storage ? local.artifact_storage_uri : null
    }
    service_account_annotations = merge(
      { for name, sa in google_service_account.agents : "${var.service_account_prefix}-${name}" => sa.email },
      { (var.migrator_service_account_id) = google_service_account.migrator.email },
    )
    agent_iam_users = local.agent_db_roles
  }
}
