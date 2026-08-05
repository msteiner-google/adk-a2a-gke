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

variable "agents" {
  description = <<-EOT
    The agents in this system. Each name must match, exactly:
      - the folder app/agents/<name>/ and its AgentSpec.name,
      - the Kubernetes Service name (A2A peer DNS depends on it),
      - the AGENT_NAME value in its Deployment,
      - its PostgreSQL schema in AlloyDB.
    Use a single lowercase word: ADK needs a valid Python identifier (no
    hyphens) and Kubernetes DNS labels forbid underscores.
  EOT
  type        = list(string)
  default     = ["orchestrator", "research", "math", "planner"]

  validation {
    condition     = alltrue([for name in var.agents : can(regex("^[a-z][a-z0-9]*$", name))])
    error_message = "Agent names must be a single lowercase word matching ^[a-z][a-z0-9]*$."
  }
}

variable "service_account_prefix" {
  description = <<-EOT
    Prefix for each agent's Google Service Account id and Kubernetes
    ServiceAccount name, giving e.g. "agent-research".

    Keep it short. The AlloyDB IAM database role is the service account email
    minus the ".gserviceaccount.com" suffix, and PostgreSQL truncates
    identifiers at 63 bytes; alloydb.tf asserts the resulting names fit.
  EOT
  type        = string
  default     = "agent"
}

variable "migrator_service_account_id" {
  description = <<-EOT
    Google Service Account id (and Kubernetes ServiceAccount name) for the
    Alembic migration Job. This identity owns every agent schema and is the only
    one with DDL rights, so schema changes cannot originate from a running agent.
  EOT
  type        = string
  default     = "agent-migrator"
}

variable "agent_extra_iam_roles" {
  description = <<-EOT
    Additional project IAM roles per agent, e.g. { research = ["roles/..."] }.

    This is the payoff of per-agent identities: widen one agent without widening
    the rest. Keep var.agent_iam_roles as the minimal shared baseline.
  EOT
  type        = map(list(string))
  default     = {}

  validation {
    condition     = alltrue([for name in keys(var.agent_extra_iam_roles) : contains(var.agents, name)])
    error_message = "Keys of agent_extra_iam_roles must be names listed in var.agents."
  }
}

variable "deletion_protection" {
  description = "Guard against accidental cluster deletion. Set false for throwaway/dev clusters."
  type        = bool
  default     = true
}

variable "agent_iam_roles" {
  description = <<-EOT
    Baseline project IAM roles granted to EVERY agent's service account.

    Keep this minimal — it is the floor for the least-trusted agent. The three
    AlloyDB roles are appended automatically in main.tf; per-agent additions go
    in var.agent_extra_iam_roles.
  EOT
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

# --- Artifact storage ---------------------------------------------------------

variable "enable_artifact_storage" {
  description = <<-EOT
    Create the GCS bucket agents store artifacts in (see artifacts.tf).

    Set false to skip it — e.g. when pointing ARTIFACT_STORAGE_URI at an
    existing bucket, at S3/Azure (cloudpathlib speaks both), or when running
    with the in-memory default. Leaving ARTIFACT_STORAGE_URI unset in the
    ConfigMap is what actually makes the agents ignore the bucket.
  EOT
  type        = bool
  default     = true
}

variable "artifact_bucket_name" {
  description = <<-EOT
    Name of the artifact bucket. GCS names are globally unique, so this defaults
    to "<project_id>-agent-artifacts" when null.
  EOT
  type        = string
  default     = null
}

variable "artifact_storage_prefix" {
  description = <<-EOT
    Object prefix under the bucket that holds the artifact tree, giving
    ARTIFACT_STORAGE_URI = "gs://<bucket>/<prefix>". Keeps artifacts from
    colliding with anything else stored in the same bucket.
  EOT
  type        = string
  default     = "artifacts"
}

variable "artifact_retention_days" {
  description = <<-EOT
    Delete artifact objects older than this many days. Artifacts are
    conversation-scoped working data, so an unbounded bucket is usually not what
    you want; null disables the lifecycle rule.
  EOT
  type        = number
  default     = 30
}

variable "artifact_bucket_force_destroy" {
  description = "Allow `terraform destroy` to delete the artifact bucket while it still holds objects."
  type        = bool
  default     = false
}

# --- AlloyDB -----------------------------------------------------------------

variable "network" {
  description = <<-EOT
    VPC network for the private-services-access peering that AlloyDB uses.

    Must be the same network the GKE cluster runs in: the cluster is VPC-native,
    so pod IPs come from a subnet secondary range, and subnet routes are
    exchanged over the peering automatically. That is what lets pods reach the
    instance's private IP directly, with no Auth Proxy sidecar.
  EOT
  type        = string
  default     = "default"
}

variable "alloydb_cluster_id" {
  description = "AlloyDB cluster id."
  type        = string
  default     = "agents-db"
}

variable "alloydb_instance_id" {
  description = "AlloyDB primary instance id."
  type        = string
  default     = "primary"
}

variable "alloydb_machine_type" {
  description = <<-EOT
    AlloyDB machine type for the primary instance.

    Default c4a-highmem-1 is the Axion (Arm) prototype shape: 1 vCPU / 8 GB.
    Google documents it as sandbox/dev only — it carries NO uptime SLA even with
    high availability configured, and no local SSD cache. Move to
    c4a-highmem-2-lssd or larger before production.

    C4A is not available in every region. As of writing it covers us-central1,
    us-east1, us-east4, europe-west1..4, asia-east1 and asia-southeast1; check
    https://cloud.google.com/alloydb/docs/choose-machine-type before changing
    var.region.
  EOT
  type        = string
  default     = "c4a-highmem-1"
}

variable "alloydb_cpu_count" {
  description = <<-EOT
    vCPU count. Must agree with var.alloydb_machine_type or the API rejects the
    request (c4a-highmem-1 => 1).
  EOT
  type        = number
  default     = 1
}

variable "alloydb_database" {
  description = <<-EOT
    Database that holds every agent's schema.

    One database, one schema per agent — not a database per agent. PostgreSQL
    cannot query across databases without FDW/dblink, so separate databases
    would make any cross-agent question (tracing one request through
    orchestrator -> research -> math) impossible, while multiplying connection
    pools against a single-vCPU instance. See app/cluster/db.py.

    The provider has no google_alloydb_database resource, so this database is
    created by the migration Job's bootstrap step (app/cluster/bootstrap.py).
  EOT
  type        = string
  default     = "agents"
}

variable "alloydb_psa_prefix_length" {
  description = <<-EOT
    Prefix length of the private services access range reserved for AlloyDB.
    /16 is Google's recommendation; shrink only if the VPC address space is
    tight, and never below /24.
  EOT
  type        = number
  default     = 16
}

variable "database_readers" {
  description = <<-EOT
    Human IAM principals granted READ-ONLY access to every agent schema, for
    inspecting the database from AlloyDB Studio in the Cloud console.

    Use full email addresses for user accounts ("someone@example.com"). Service
    accounts would need the ".gserviceaccount.com" suffix stripped, but they
    should normally get their own per-agent identity instead of being a reader.

    Terraform creates the cluster user and grants the project-level IAM roles.
    The in-database GRANTs are applied separately by scripts/grant_readers.py,
    because who may read is a people-lifecycle concern and should not require
    bumping an Alembic revision (unlike the per-agent grants in revision 0003,
    which are tied to schema creation).

    GROUPS: AlloyDB also supports IAM *group* authentication, which is the
    better pattern once more than one person needs access -- membership alone
    then controls access, with no per-person cluster user. It is currently
    Preview and documented as new-clusters-only, and needs a second instance
    flag (alloydb.iam_group_authentication=on). See docs/inspecting-the-database.md.
  EOT
  type        = list(string)
  default     = []
}

variable "alloydb_deletion_policy" {
  description = <<-EOT
    Set to "FORCE" to allow `terraform destroy` to delete a cluster that still
    has instances. Leave null for real data.
  EOT
  type        = string
  default     = null
}
