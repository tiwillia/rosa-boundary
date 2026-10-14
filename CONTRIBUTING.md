# Contributing to the Boundary / Trusted Actions PoC

Read [AGENTS.md](AGENTS.md) and [development standards](docs/development-standards.md)
for Boundary requirements. The [Trusted Actions contributor guide](services/trusted-actions/CONTRIBUTING.md)
and [testing guide](services/trusted-actions/TESTING.md) cover its separate Go module.

| Change | Directory | Local checks |
|---|---|---|
| Boundary CLI / image / Lambda | repository root | `make test-cli`, `make test-lambda`, scoped checks in `docs/testing.md` |
| Trusted Actions API / CLI | `services/trusted-actions` | `make trusted-actions-test`; `make trusted-actions-build` when Node/npm codegen is available |
| Trusted Actions image | `services/trusted-actions/Containerfile` | `make trusted-actions-image` and inspect runtime |
| Terraform | `deploy/{account,network,regional}` with child module `deploy/trusted-actions` | `terraform fmt -check -recursive`, `terraform validate` in regional root; review speculative plans before deployment |

The applications share a repository but have **separate** Go modules, container
images and dependencies. The workloads **share the regional Terraform state**
when enabled. Run service-specific tooling from the service directory; never
use `go test ./...` at the repository root to claim both are covered. Root
`.tekton` builds the images independently; check Konflux component repository
and context configuration before expecting the migrated pipeline to run.

Changes to HCP staging workspace variables must go through
`hcp-terraform/rosa-boundary/main.tf`. The former Trusted Actions workspace
must release its resources non-destructively before regional state imports
them; never run two active states against the same resources. Coordinate a
reviewed handoff and inventory prior to apply. No staging or production
deployment is authorized merely by landing this PoC. Record unsupported workflows and
security gaps in the [PoC report](docs/trusted-actions-monorepo-poc.md).
