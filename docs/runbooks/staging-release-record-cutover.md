# Staging release records: cutover boundary

**Not active.** The [existing pin workflow](../staging-deployment.md#container-image-updates) still selects images. Do not remove its inputs as part of bootstrapping the publisher. Production is not part of this change.

## Artifact contract

The release job needs the reviewed `main` commit **and the approved source revision and manifest digest for each component**. These are not necessarily three equal Git SHAs. Use the runtime repository actually configured for ECS (`quay.io/redhat-user-workloads/rosa-tenant/rosa-boundary`), not the release admission's other Quay destination. A Lambda build on Quay is not sufficient: verify the exact expected manifest in the staging account's ordinary `rosa-boundary/create-investigation-lambda` repository in **both** `us-east-1` and `us-west-2`. The west repository receives new images asynchronously through ECR replication. Never fall back to east for a west deployment or treat a `digest_sha` *tag* as an OCI digest.

For a component not built at the candidate commit, compare the candidate Git tree with the approved component's build inputs. A missing build does not establish equivalence. In particular, the currently configured staging SHA pair cannot simply seed a release record for a newer `main`: check the actual revisions, build and release evidence, and compatibility of the pair. Record the separately verified current deployment and retain its artifacts for recovery.

## Rollout ownership

Treat storage bootstrap, publication, and switching image selection as separate changes. Before enabling record-based selection in either regional workspace, the release-system owner must provide:

- A trusted job and short-lived staging publisher identity. The job must bind compatibility and build evidence to the exact candidate; PR code must not be able to publish accepted records. Keep write access out of the regional Terraform roles.
- Delivery of image-only **and** infrastructure-only candidates. Queue or retry an HCP run for the precise merged configuration version *after* publication; serialize promotions across both staging regions and reject superseded retries. Do not replan `main` by name or assume two workspace queues are a global lock.
- An explicit policy for speculative PRs, which do not have merged release records. A preview may use the current stage deployment only if labelled accordingly and isolated from every apply-capable run. Do not add a missing-record fallback to the deployment path.

## Switch and recovery

First gate both staging regional workspaces through reviewed Git configuration (`auto_apply` and `auto_apply_run_trigger`), reconcile the settings and check outstanding runs. Bootstrap the store and publish records without changing consumers. In a **separate, staging-only** change enable the digest consumer in both workspaces. Review the regional plans, not just the meta-workspace variable plan. Resume automatic applies only after the exact-revision delivery and stale-run controls are effective. Do not mutate HCP variables directly.

Digest selection prevents selecting the wrong artifacts; it does not make AWS updates atomic. The running Lambda reads an ECS task-definition family and can see a new base revision before its own update. Require compatible intermediate states or stage an explicit versioned transition. Recover a partial rollout with a new reviewed plan and retained known-good images in each target region, not an unconditional replay of old Terraform. Staging ECR retains only the most recent 25 tagged images by current lifecycle policy.
