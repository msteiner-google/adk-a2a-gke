# Input variables for the GKE multi-agent cluster.

variable "project_id" {
  description = "The GCP project ID to deploy into."
  type        = string
}

variable "region" {
  description = "Region for the Autopilot cluster and Artifact Registry repo."
  type        = string
  default     = "us-central1"
}

variable "cluster_name" {
  description = "Name of the GKE Autopilot cluster."
  type        = string
  default     = "agents-cluster"
}

variable "artifact_repo_id" {
  description = "Artifact Registry (Docker) repository ID for agent images."
  type        = string
  default     = "agents"
}

variable "namespace" {
  description = "Kubernetes namespace the agents run in (must match kustomize)."
  type        = string
  default     = "agents"
}

variable "kubernetes_service_account" {
  description = "Kubernetes ServiceAccount the agent pods use (must match kustomize)."
  type        = string
  default     = "agent"
}

variable "google_service_account_id" {
  description = "ID of the Google Service Account the agents impersonate via Workload Identity."
  type        = string
  default     = "agents-runtime"
}

variable "deletion_protection" {
  description = "Guard against accidental cluster deletion. Set false for throwaway/dev clusters."
  type        = bool
  default     = true
}

variable "agent_iam_roles" {
  description = "Project IAM roles granted to the agents' Google Service Account."
  type        = list(string)
  default = [
    "roles/aiplatform.user",         # call Vertex AI / Gemini
    "roles/logging.logWriter",       # write logs (structured loguru -> Cloud Logging)
    "roles/monitoring.metricWriter", # write metrics
    # ADK / shared telemetry export traces via the Telemetry (OTLP) API at
    # telemetry.googleapis.com, which requires telemetry.tracesWriter.
    "roles/telemetry.tracesWriter", # write traces (OTLP -> Cloud Trace)
    "roles/cloudtrace.agent",       # write traces (direct Cloud Trace API)
  ]
}
