# Retired proposed meta root (PoC)

This directory deliberately contains **no Terraform configuration**. The
initial PoC proposed re-pointing the old Trusted Actions workspace at a new
workload root; the revised design instead puts Trusted Actions in Boundary's
`deploy/regional` state as a gated module. Boundary's existing regional
workspace is configured by `hcp-terraform/rosa-boundary/main.tf`.

The old Trusted Actions meta/workload workspaces still exist independently
outside this branch. **Do not delete their state or remove their workspaces
automatically.** Their resources must be inventoried, released without
destruction, imported into Boundary regional state, and only then retired in
an explicitly reviewed migration. See `docs/trusted-actions-monorepo-poc.md`.
