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
#   cluster_name               = "agents-cluster"
#   artifact_repo_id           = "agents"
#   namespace                  = "agents"          # must match kustomize
#   kubernetes_service_account = "agent"           # must match kustomize

# Throwaway dev cluster: allow `terraform destroy` to tear it down.
deletion_protection = false
