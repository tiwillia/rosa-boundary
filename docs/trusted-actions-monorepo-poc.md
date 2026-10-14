# Combined Boundary + Trusted Actions Terraform PoC (2026-10-14)

## Revised decision

The first PoC merely colocated Git source while keeping Trusted Actions in a
separate workload state. That was not the requested deployment model. **This
revision places both workloads in a single regional Terraform root/state**:
`deploy/regional` calls `../trusted-actions` as a local module and passes the
Boundary VPC/subnets, task security group, account/region, and provider. HCP's
existing `rosa-boundary-stage-regional` workspace is the intended owner of both
workloads. Trusted Actions source, Go module, and image remain separate because
they are distinct binaries; that does not mean they deploy independently.

| Root/state | Owned resources |
|---|---|
| `deploy/account` | Account-wide Boundary IAM/OIDC identities, budget, model agreements |
| `deploy/network` | One VPC, NAT, subnets and S3 endpoint for the account/region |
| `deploy/regional` | Boundary Fargate, audit and investigations **and** Trusted Actions EC2/EBS, ECS service, internal ALB, logs, S3 and service IAM through `module.trusted_actions` |

Existing account/network states stay separate: account-wide singleton
lifecycles differ from per-region workload lifecycles. `deploy/trusted-actions`
is now a **child module, not an independently executable root**. It inherits
Boundary's AWS 6.x provider and its `allowed_account_ids` and tagging controls;
the earlier Trusted Actions AWS 5.x provider block was removed. `config.tf`
still reads the service YAML from the same checkout. The module gets Boundary's
actual task SG ID directly, not a second-state name lookup. The imported
standalone VPC/NAT/subnet definitions were removed; verify the former state
does not own any of those resources before any old-workspace apply.

## Safety gate and private API

`enable_trusted_actions` is **false by default and explicitly false in the
stage regional workspace variables**. A merge must not create duplicates of
resources still managed by the old `rosa-trusted-actions-stage` workspace.
The module has a private ALB in two private subnets, HTTPS listener only,
mandatory ACM certificate, and SG ingress only from Boundary task SG on 443.
The EC2 host receives 8080 only from that ALB. This is a network path, **not
proof of caller identity**: IAM on SG attachment/ENIs and application JWT
authorization still matter. No certificate or identity-scoped service credential
strategy has been approved; do not enable the module or deploy it as-is.

The old Trusted Actions meta/workload workspaces are **not** managed or
destroyed by this branch. The earlier proposal to move their VCS to the new
repo was withdrawn (`hcp-terraform/rosa-trusted-actions/` has a README only).
No HCP, AWS or Konflux registration was changed here.

## Non-destructive state migration required before enabling

1. Inventory live AWS resources, old Trusted Actions workload/meta state,
   Boundary regional state, credentials, tags, provider versions, image digest
   and names. Confirm the old workspace actually owns the existing resources;
   source code and workspace definitions do not prove that. Freeze both
   workspaces and preserve backups with access-controlled state handling.
2. Resolve provider 5→6 differences and all old-to-new resource addresses,
   e.g. `aws_lb.main` → `module.trusted_actions[0].aws_lb.main`. Coordinate a
   **non-destructive release/import** (or controlled state transfer) for every
   object; never apply a second state that tries to create the same ALB, S3
   bucket, secret or IAM roles. Distinguish existing objects from deliberately
   replaced ones (public ALB → private ALB, HTTP → HTTPS). Review the separate
   deletion-protection/OPA gate and any unavoidable outage.
3. Replace inherited `secrets.tf` behavior: it places supplied service-secret
   values in Terraform state, which is not a suitable shared/long-lived
   credential design. Require a reviewed rotation/identity-scoped approach,
   an ACM certificate/private DNS name, explicit allowed accounts, and a
   verifiable application audit pipeline before enabling.
4. Through a PR update **only** the existing Boundary regional workspace's
   Git-managed variables in `hcp-terraform/rosa-boundary/main.tf`, after the
   handoff is staged and reviewed. Add the approved Trusted Actions image
   digest, cert, bucket, endpoint/client settings, and enable gate. Keep the
   manual-apply settings, compare speculative/final plans and verify one owner
   per resource before approving any HCP apply. Retire the old workload/meta
   workspaces only after the new state is correct and live evidence is saved.
5. Verify private reachability *from a Boundary ECS task*, denial from an
   unauthorized source, TLS/JWT, logs and cleanup. No such test has run.

**Multi-region blocker:** inherited Trusted Actions IAM roles, ECS cluster,
ALB and some bucket names use account-global or fixed `app_name` values.
For each additional region choose an account-owned shared identity with
region-qualified resource policies, or adopt unique region-qualified names
without changing existing stage identities. Do not deploy a second regional
module until these collisions are resolved. The EC2/EBS/SQLite singleton also
requires an HA and recovery decision before GA.

## Image terminology

The current stage Boundary task image is a Quay **tag named after a Git commit**,
e.g. `rosa-boundary:<commit-sha>`. This records which source commit was built,
but a tag is a mutable registry pointer and could later reference different
bytes. The old Trusted Actions workspace uses `rosa-trusted-actions@sha256:…`,
an OCI **manifest digest** that addresses specific published image content.
These were inherited deployment configuration choices, not a technical
requirement of a monorepo. Both components can use verified digests, with Git
SHA recorded separately for provenance; architecture/platform of each digest
must be checked. The revised regional module input requires a digest when
enabled. CI/Konflux must register the moved Trusted Actions pipeline against
the combined repo and release the second image independently.

## Contributor experience and local evidence

- Two Go modules, build contexts, image release streams and test suites remain.
  Root `go test ./...` does **not** test the Trusted Actions module. Its old Go
  module path is retained for compatibility and needs a deliberate rename if
  the packages are published from the combined repository.
- Regional changes can affect both workloads in one plan. Always inspect the
  `module.trusted_actions[0]` addresses, generated image/config and secret
  exposure; the regional Makefile is for local dev only, while staging HCP
  executes Terraform directly. Upstream Trusted Actions Aurora/Fargate docs
  describe a future target, not the inherited EC2/SQLite implementation.
- `terraform init -backend=false -input=false` and `terraform validate` in
  `deploy/regional` passed with AWS provider 6.58.0 (module included), as did
  formatting of changed files. Mocked `terraform test` exercises both the
  disabled default and an enabled combined-module plan; mocks do not prove
  provider/API compatibility or live network isolation. Prior PoC
  `go test ./...` and `go build` in
  `services/trusted-actions` passed; no Go behavior changed in this revision.
  **No Terraform plan, AWS/HCP apply, image build, or live network/security
  assertion** was performed. The prior separate meta-root registry validation
  failed with 401, but that meta root is no longer part of this design.
