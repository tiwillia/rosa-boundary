# Contributing to the Boundary / Trusted Actions PoC

Read [AGENTS.md](AGENTS.md) and [development standards](docs/development-standards.md)
for Boundary requirements. The [Trusted Actions contributor guide](services/trusted-actions/CONTRIBUTING.md)
and [testing guide](services/trusted-actions/TESTING.md) cover its separate Go module.

| Change | Directory | Local checks |
|---|---|---|
| Boundary CLI / image / Lambda | repository root | `make test-cli`, `make test-lambda`, scoped checks in `docs/testing.md` |
| Trusted Actions API / CLI | `services/trusted-actions` | `make trusted-actions-test`; `make trusted-actions-build` when Node/npm codegen is available |
| Trusted Actions image | `services/trusted-actions/Containerfile` | `make trusted-actions-image` and inspect runtime |
| Terraform | `deploy/{account,network,regional,trusted-actions}` | `terraform fmt -check -recursive`, `terraform validate` in changed root; review speculative plans before deployment |

The applications share a repository but **not** a Go module, container image, or
Terraform state. Run service-specific tooling from the service directory; never
use `go test ./...` at the repository root to claim both are covered. Root
`.tekton` builds the images independently; check Konflux component repository
and context configuration before expecting the migrated pipeline to run.

Changes to HCP staging workspace variables must go through their Git-managed
meta root. For the proposed Trusted Actions workspace, see
`hcp-terraform/rosa-trusted-actions/`; **do not run it alongside its old VCS
source as another owner of the same workspace**. Coordinate a reviewed VCS/state
handoff and inventory prior to apply. No staging or production deployment is
authorized merely by landing this PoC. Record unsupported workflows and
security gaps in the [PoC report](docs/trusted-actions-monorepo-poc.md).
