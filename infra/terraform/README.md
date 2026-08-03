# Terraform: GKE multi-agent cluster

Provisions the infrastructure for the multi-agent system:

- **GKE Autopilot** cluster (Workload Identity enabled by default)
- **Artifact Registry** Docker repo for the agent image
- **One Google Service Account per agent**, plus a migrator account, each with
  Vertex AI + telemetry + AlloyDB IAM roles (see the IDENTITY MODEL comment at
  the top of `main.tf` for why they are not shared)
- **Workload Identity** bindings for each agent's Kubernetes ServiceAccount
  (created via kustomize — keep `namespace` and `agents` in sync with
  `../kustomize`)
- **AlloyDB** cluster + primary instance for durable session and A2A task
  storage, with private-services-access peering and IAM database users
  (`alloydb.tf`)
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
terraform output artifact_registry_repo         # -> image path prefix
terraform output -json kustomize_values         # -> every manifest placeholder
```

See the repo's `GKE.md` for the full build → provision → deploy walkthrough.

> Tip: for a throwaway cluster set `deletion_protection = false` and
> `alloydb_deletion_policy = "FORCE"` so `terraform destroy` can tear it down.

> **Upgrading an existing deployment:** the per-agent service accounts replace
> the former single `agents-runtime` account, which `terraform apply` will
> destroy. Re-apply any out-of-band IAM grants to the new accounts.
