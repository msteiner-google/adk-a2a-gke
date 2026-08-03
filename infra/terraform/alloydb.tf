# AlloyDB for PostgreSQL: durable session and A2A task storage.
#
# What this provisions:
#   1. A private services access (PSA) range and VPC peering, so the instance
#      gets a private IP inside the same VPC as the GKE cluster.
#   2. An AlloyDB cluster and a single primary instance on the prototype
#      c4a-highmem-1 shape (1 vCPU / 8 GB).
#   3. IAM database users -- one per agent, plus the migrator -- so pods
#      authenticate with Workload Identity and no password exists anywhere.
#
# CONNECTIVITY. Pods reach the instance's private IP directly; there is no
# AlloyDB Auth Proxy sidecar. That works because the Autopilot cluster is
# VPC-native: pod IPs are alias IPs from a subnet secondary range, and subnet
# routes are exchanged across the PSA peering automatically. Keep var.network
# equal to the cluster's network or this breaks.
#
# AUTHENTICATION. IAM database authentication only. The AlloyDB Python connector
# (see app/cluster/db.py) mints a short-lived OAuth token per connection from the
# pod's Workload Identity credentials and wraps the socket in mTLS. No password
# is set on any user, and nothing sensitive lands in Terraform state -- which is
# also why no `initial_user` block appears below.
#
# BREAK-GLASS. With no built-in user there is no password login. To get a psql
# session for debugging:
#   gcloud alloydb users set-password postgres --cluster=<id> --region=<region> \
#     --password=<temp>
# ...then remove it again afterwards.

# --- Private services access -------------------------------------------------

data "google_compute_network" "vpc" {
  name    = var.network
  project = var.project_id

  depends_on = [google_project_service.services]
}

# Address block handed to the service producer network that hosts AlloyDB.
resource "google_compute_global_address" "alloydb_psa" {
  name          = "${var.alloydb_cluster_id}-psa"
  project       = var.project_id
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = var.alloydb_psa_prefix_length
  network       = data.google_compute_network.vpc.id
}

resource "google_service_networking_connection" "alloydb_psa" {
  network                 = data.google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.alloydb_psa.name]

  depends_on = [google_project_service.services]
}

# --- Cluster and instance ----------------------------------------------------

resource "google_alloydb_cluster" "agents" {
  cluster_id      = var.alloydb_cluster_id
  project         = var.project_id
  location        = var.region
  deletion_policy = var.alloydb_deletion_policy

  network_config {
    network = data.google_compute_network.vpc.id
  }

  # No initial_user: IAM authentication only, so there is no password to store
  # in state. See BREAK-GLASS in the header.

  depends_on = [google_service_networking_connection.alloydb_psa]
}

resource "google_alloydb_instance" "primary" {
  cluster       = google_alloydb_cluster.agents.name
  instance_id   = var.alloydb_instance_id
  instance_type = "PRIMARY"

  # ZONAL, matching the prototype machine shape. c4a-highmem-1 has no uptime
  # SLA even when configured for HA, so paying for REGIONAL would buy failover
  # without the guarantee. Move to a 2+ vCPU lssd shape and REGIONAL together.
  availability_type = "ZONAL"

  machine_config {
    machine_type = var.alloydb_machine_type
    # Must agree with machine_type; the API rejects a mismatch.
    cpu_count = var.alloydb_cpu_count
  }

  database_flags = {
    # Required for IAM database authentication. This is an INSTANCE flag, not a
    # cluster setting. Without it, every IAM login is rejected.
    "alloydb.iam_authentication" = "on"
  }

  depends_on = [google_service_networking_connection.alloydb_psa]
}

# --- IAM database users ------------------------------------------------------

locals {
  # An AlloyDB IAM database user for a service account is the account's email
  # with the ".gserviceaccount.com" suffix removed, e.g.
  # "agent-research@my-project.iam".
  agent_db_roles = {
    for name, sa in google_service_account.agents :
    name => trimsuffix(sa.email, ".gserviceaccount.com")
  }

  migrator_db_role = trimsuffix(
    google_service_account.migrator.email, ".gserviceaccount.com"
  )
}

# Agents: ordinary IAM users. They get no database-level roles here; their
# access is exactly the USAGE + DML grant that Alembic applies to their own
# schema (app/migrations/versions/*_grant_agent_role.py). No CREATE, and nothing
# at all on any other agent's schema.
resource "google_alloydb_user" "agents" {
  for_each = local.agent_identities

  cluster   = google_alloydb_cluster.agents.name
  user_id   = local.agent_db_roles[each.key]
  user_type = "ALLOYDB_IAM_USER"

  database_roles = ["alloydbiamuser"]

  lifecycle {
    precondition {
      # PostgreSQL silently truncates identifiers at 63 bytes, which would
      # grant to a role that is not the one intended. Fail loudly instead.
      condition     = length(local.agent_db_roles[each.key]) <= 63
      error_message = "Database role '${local.agent_db_roles[each.key]}' exceeds PostgreSQL's 63-byte identifier limit. Shorten var.service_account_prefix or the agent name."
    }
  }

  depends_on = [google_alloydb_instance.primary]
}

# Human read-only principals, for inspecting the data in AlloyDB Studio.
#
# No database_roles here on purpose: the default for a new IAM user is
# alloydbsuperuser, which would defeat the point. Listing an explicit (empty of
# superuser) role set keeps them to plain login rights, and the actual read
# access comes from the SELECT grants applied by scripts/grant_readers.py.
resource "google_alloydb_user" "readers" {
  for_each = toset(var.database_readers)

  cluster   = google_alloydb_cluster.agents.name
  user_id   = each.key
  user_type = "ALLOYDB_IAM_USER"

  database_roles = ["alloydbiamuser"]

  depends_on = [google_alloydb_instance.primary]
}

# Both roles are needed to authenticate: databaseUser carries alloydb.users.login,
# and serviceUsageConsumer covers the permission-check API. Granted explicitly
# rather than relying on roles/owner, because the basic roles do not reliably
# include newer service-specific permissions.
resource "google_project_iam_member" "database_readers" {
  for_each = {
    for pair in setproduct(var.database_readers, [
      "roles/alloydb.databaseUser",
      "roles/serviceusage.serviceUsageConsumer",
    ]) : "${pair[0]}|${pair[1]}" => { principal = pair[0], role = pair[1] }
  }

  project = var.project_id
  role    = each.value.role
  member  = "user:${each.value.principal}"
}

# Migrator: the only identity with DDL rights. alloydbsuperuser lets it create
# the database and own every agent schema.
resource "google_alloydb_user" "migrator" {
  cluster   = google_alloydb_cluster.agents.name
  user_id   = local.migrator_db_role
  user_type = "ALLOYDB_IAM_USER"

  database_roles = ["alloydbiamuser", "alloydbsuperuser"]

  lifecycle {
    precondition {
      condition     = length(local.migrator_db_role) <= 63
      error_message = "Database role '${local.migrator_db_role}' exceeds PostgreSQL's 63-byte identifier limit."
    }
  }

  depends_on = [google_alloydb_instance.primary]
}
