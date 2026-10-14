# Boundary + Trusted Actions monorepo PoC (2026-10-13)

## Decision and scope

**Recommendation: colocate source and delivery definitions, not Terraform state.**
This branch imports `openshift-online/rosa-trusted-actions` upstream `1d50516`
into `services/trusted-actions/` (independent Go module), copies its AWS root to
`deploy/trusted-actions/`, and places its HCP meta root in
`hcp-terraform/rosa-trusted-actions/`. Boundary `main` was at upstream `c52a477`
when this branch was created. The original Trusted Actions checkout was left
intact; its extra local test commit is saved on
`backup/local-main-before-upstream-sync`. No PR or AWS/HCP changes were made.

| Owner / state | Resources | Dependency |
|---|---|---|
| `deploy/network` (Boundary stage network workspace) | One VPC, NAT, public/private subnets | Foundation |
| `deploy/account` (Boundary stage account workspace) | Shared Boundary IAM/OIDC identities, budget, Bedrock agreements | Account-wide; **no copy in Trusted Actions** |
| `deploy/regional` (Boundary stage regional workspace) | Fargate tasks and task SG, audit and investigation infrastructure | Network and account |
| `deploy/trusted-actions` (proposed existing Trusted Actions stage workspace) | API ECS EC2 service, internal ALB, EBS/SQLite, S3, audit delivery, service IAM/Secrets Manager | Network and Boundary task SG |

The Trusted Actions root still contains legacy *conditional* standalone-VPC
resource declarations, but `vpc_id` is now required and the existing-network
branch is selected. Before applying to a workspace with any previously managed
VPC, inspect state: this change would propose deleting that VPC and needs a
separate ownership transfer. Region-specific service IAM roles remain in the
service root because their names are service-scoped. Do not duplicate Boundary
account-wide roles, OIDC providers, budgets, or credential variable sets there.
Separate roots avoid merging incompatible AWS provider major versions (Trusted
Actions 5.x, Boundary 6.x) and prevent a single service edit from reconciling
the other's state. A monorepo is **not** an automatic Terraform dependency graph.

## Private API proposal and limitations

The Trusted Actions ALB is changed from internet-facing on public subnets to
**internal on two private subnets**; its sole listener is HTTPS/443 and requires
an ACM certificate (no HTTP fallback). The ALB SG only accepts TCP 443 from the
Boundary regional task SG looked up by name in the same VPC. The API's EC2 host
accepts 8080 only from the ALB. The Boundary tasks' current broad egress allows
them to reach the ALB without new egress rules. The ALB DNS name resolves to
private addresses; clients outside the VPC cannot reach it by default.

This restricts the *network path*, not callers: anyone who can attach the
Boundary SG to another ENI, enter a task, or run inside the VPC with equivalent
permissions may reach the API. Verify EC2/ENI/SG-attachment IAM, ECS Exec
isolation, and JWT authorization separately. Trusted Actions still uses OCM/
Backplane service credentials and its application auditor is a mock; it is not
safe to claim task-specific identity or audited actions merely because the API
is private. Do not put service credentials into Boundary EFS or S3 escrow.
An ACM certificate with a DNS name usable **inside** the VPC must be provisioned
before deployment; stage currently supplies none, so the proposed plan should
fail closed at the listener precondition. Private DNS, client configuration,
certificate rotation, JWT audience/issuer and allowed account/namespace policy
need explicit design and end-to-end tests. No VPC peering/PrivateLink is needed
for tasks in the same VPC. A future ALB replacement could interrupt service.

## HCP/state handoff, **not executed**

The copied Trusted Actions meta root keeps the separate `rosa-trusted-actions`
HCP project and existing `rosa-trusted-actions-stage` workspace with manual
apply. Its proposed VCS repo and working directory now point to
`openshift-online/rosa-boundary` / `deploy/trusted-actions`, and its credential
variable set name matches the Boundary-owned dynamic-credentials set. The
Boundary AWS-credentials trust allowlist already lists the Trusted Actions
project/workspace; **verify the actual attachment and trust in HCP**. Do not add
this existing workspace to Boundary's meta module: two meta states would both
manage it. Instead coordinate switching the existing meta-workspace VCS source
once, keeping its HCP state and the workload workspace's state; do not create a
second workspace. Ensure the moved meta-root module's own provider version and
its VCS registration are reviewed before reconciling it. Copying files does not
transfer Terraform state, Konflux component configuration, or ownership.

Suggested gated sequence for maintainers:

1. Inventory both live HCP meta/workload states and AWS account/region, including
   ALB, target groups/listeners, S3 bucket, Secrets Manager, service roles,
   public/private subnet IDs, SG names and IAM trust. Resolve any duplicate
   ownership and preserve resource addresses in the *existing* workload state.
2. Review infrastructure and security plan **before** switching VCS. Verify the
   selected repository/working directory exists on `main`; suspend competing
   runs and move the existing meta-workspace VCS pointer only after the code is
   published. Keep manual applies; do not create a new service state.
3. In Git-managed meta variables, set a valid ACM cert/DNS, any required secret
   references and allowed-account policy. Confirm the actual credential set and
   account guard, and compare proposed replaces/deletes (especially ALB/public
   listener) to the inventory. Never put long-lived credentials in Git or
   Terraform secret-version state; replace the inherited credential provisioning
   path with a reviewed identity-scoped/rotation design before production.
4. Configure the Konflux component to use the Boundary repository and migrated
   `.tekton` path-context. Verify build architectures, image digest, SBOM/policy
   checks and working image; update the Git-managed image pin in the service
   meta root. Test TLS routing from a Boundary task and denial from unauthorized
   VPC sources; inspect ALB and app audit logs. Roll back VCS/infra in a reviewed
   sequence if necessary; ALB replacement is not a no-downtime migration.

## Contributor costs and open blockers

- Two Go modules/toolchains: root Boundary targets do not test the Trusted
  Actions tree. Trusted Actions `go.mod` requires Go 1.26; its old module path is
  retained to avoid an unrelated import/API migration. If publishing module
  packages from this repository, plan an import-path rename and downstream
  migration. Its `make build` and `make test` also run npm/OpenAPI generation;
  committed generated Go lets direct `go test ./...` work without npm.
- Two images and release cadences: root `.tekton` has a Trusted Actions pipeline
  with service-specific build context, but **Konflux registration is external**.
  Commit-SHA pinning for Boundary and image-digest pinning for Trusted Actions
  must be coordinated; sharing Git does not imply a single deploy or release.
  Trusted Actions' inherited image uses `hi/go:latest` and `hi/core-runtime:latest`
  and its pipeline builds x86_64 only; review base-image pinning and
  architecture/support policy before production use.
- Several HCP Terraform roots and meta roots require disciplined single-owner
  workspace/variable management, cross-root dependencies and ordering. The
  Trusted Actions root uses AWS provider 5.x; upgrading it to Boundary's 6.x
  needs separate migration review. `deploy/trusted-actions/config.tf` reads
  `services/trusted-actions/config/config.yaml` relative to the module, so HCP
  must fetch the whole repo. Service `docs/deployment.md` describes a later
  Aurora/Fargate design not implemented by the inherited EC2/SQLite Terraform.
- Stage's inherited `backplane_client_secret` input is required but not in its
  HCP variables; `secrets.tf` stores supplied secret material in Terraform state
  and enables immediate secret deletion. The default account allowlist is empty,
  Trusted Actions S3 WORM is off, and application audit integration is not
  complete. These are **deployment blockers**, not PoC endorsements.
- Legacy `vpc.tf` still includes conditional VPC resources; eventually remove
  them with an explicit state inventory and `removed`/import handoff if relevant.
  Stage private subnets must span two AZs. An internal ALB + cert requires DNS
  and a documented Boundary task API client path (and approved reachability for
  operational diagnostics). No live task/ALB/IAM/ABAC evidence was collected.

## Local evidence

- `cd services/trusted-actions && go test ./...`: passed (Go 1.27.1, upstream
  Trusted Actions module and committed generated API code).
- `cd services/trusted-actions && go build ./cmd/server ./cmd/action-cli`:
  passed (compile only, no image/runtime test).
- `cd deploy/trusted-actions && terraform init -backend=false -input=false &&
  terraform validate`: passed with AWS provider 5.100.0 and random 3.9.1.
- `terraform fmt -check -recursive` on both migrated roots: passed. The HCP
  meta-root `terraform init -backend=false` was attempted but the private
  workspace module registry returned 401 (no registry credentials here), so
  **meta-root validate is unverified**.
- No `terraform plan` (would require approved account/stage vars, certificate,
  secret strategy and state), no image build, no AWS or HCP mutation, and no
  validation of private network behavior. This document reports source design,
  **not** a live deployment result.
