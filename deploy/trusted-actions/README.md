# Trusted Actions regional child module (PoC)

This directory was imported from `rosa-trusted-actions/terraform` at upstream
`1d50516` and is **not a standalone state or HCP workspace**. The Boundary
regional root calls it as `module.trusted_actions[0]` when the currently
disabled `enable_trusted_actions` gate is deliberately enabled. It inherits
the regional AWS provider's account guard, region, tags and AWS 6.x version,
and receives the Boundary VPC/subnets and task security group by direct
Terraform references. It creates no Boundary account identities or VPC.

The [migration report](../../docs/trusted-actions-monorepo-poc.md) explains the
old workspace release/import, private certificate and credential blockers.
Do **not** use `terraform apply` from this module directory or enable it while
the former Trusted Actions workspace owns its objects. Local checks use
`terraform -chdir=deploy/regional init -backend=false` and
`terraform -chdir=deploy/regional validate` from the repo root. Stage plans run
through HCP Terraform's Boundary regional workspace, never the dev Makefile.
