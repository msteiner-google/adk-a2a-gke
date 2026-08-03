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

output "google_service_account_email" {
  description = "GSA the pods impersonate. Put this in the kustomize ServiceAccount annotation (iam.gke.io/gcp-service-account)."
  value       = google_service_account.agents.email
}
