# Terraform: GKE multi-agent cluster

Provisions the infrastructure for the multi-agent system:

- **GKE Autopilot** cluster (Workload Identity enabled by default)
- **Artifact Registry** Docker repo for the agent image
- **Google Service Account** the agents impersonate, with Vertex AI + telemetry
  IAM roles
- **Workload Identity** binding for the Kubernetes ServiceAccount used by the
  pods (created via kustomize — keep `namespace` / `kubernetes_service_account`
  in sync with `../kustomize`)
- The required **Google Cloud APIs**

## Usage

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # then edit project_id
terraform init
terraform apply
```

Then wire kubectl and grab the values kustomize needs:

```bash
eval "$(terraform output -raw get_credentials_command)"
terraform output google_service_account_email   # -> kustomize SA annotation
terraform output artifact_registry_repo         # -> image path prefix
```

See the repo's `GKE.md` for the full build → provision → deploy walkthrough.

> Tip: for a throwaway cluster set `deletion_protection = false` so
> `terraform destroy` can tear it down.
