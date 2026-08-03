# Deployment target: project "msteiner", region europe-west4.
#
# Usage:
#   terraform plan  -var-file=msteiner.tfvars
#   terraform apply -var-file=msteiner.tfvars
#
# This is a named var-file, so Terraform does NOT auto-load it -- pass
# -var-file on every plan/apply/destroy or the run falls back to the defaults
# in variables.tf (region us-central1) and prompts for project_id.

project_id = "msteiner"
region     = "europe-west4"

# Defaults from variables.tf, left as-is:
#   cluster_name           = "agents-cluster"
#   artifact_repo_id       = "agents"
#   namespace              = "agents"                 # must match kustomize
#   service_account_prefix = "agent"                  # -> GSA/KSA "agent-<name>"
#   agents                 = ["orchestrator", "research", "math"]
#   network                = "default"                # AlloyDB peers into this VPC
#   alloydb_machine_type   = "c4a-highmem-1"          # 1 vCPU / 8 GB
#
# C4A (Arm/Axion) is available in europe-west4, so the default machine type
# works in this region. Verify before moving the deployment elsewhere:
# https://cloud.google.com/alloydb/docs/choose-machine-type

# Read-only AlloyDB access for inspecting data in the Cloud console (AlloyDB
# Studio). Terraform creates the cluster user and the project IAM roles; run
# scripts/grant_readers.py afterwards to apply the in-database SELECT grants.
database_readers = ["msteiner@google.com"]

# Throwaway dev cluster: allow `terraform destroy` to tear it down. The AlloyDB
# policy is separate -- without FORCE, destroy refuses a cluster that still has
# instances.
deletion_protection     = false
alloydb_deletion_policy = "FORCE"
