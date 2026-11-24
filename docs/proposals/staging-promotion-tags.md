# Staging commit tags

**Status:** Proposed, not implemented or enabled. Updated 2026-11-24 to remove cross-region coordination. **Scope:** Boundary runtime, create-investigation Lambda, and staging regional workspaces. [ROSAENG-67168](https://redhat.atlassian.net/browse/ROSAENG-67168).

## Goal

Keep the existing Konflux builds, promotion/release pipelines and HCP deployment flow. Add missing full-commit-SHA tags so Terraform can select the right images for its Git revision without rebuilding unchanged components or requiring image-pin PRs. Promotion already works; changing it is out of scope.

Each regional workspace deploys independently. Regions may apply concurrently; a failure or replication delay in one region does not block another.

## Select images on merge

For each `main` revision `C`, ensure both runtime and Lambda images have a tag equal to the full commit SHA, with no prefix. Reuse an existing SHA tag when it already identifies the selected image. Compare `C` with its first parent `P`:

| Component | Image selection |
|---|---|
| Build inputs changed | Wait for the successful, policy-accepted build for that component at `C`. |
| Build inputs unchanged | Reuse the digest from that component's full-SHA tag for `P`. |

Use the actual build inputs, including bundled helpers, rather than treating a missing or skipped build as proof that nothing changed. A missing required build or parent tag blocks tagging; retry after resolving it. Bootstrap the first pair with verified images for a known revision.

A SHA tag identifies the image selected for that revision, not necessarily an image built at that revision. Preserve the original build tags, signatures, attestations and provenance. Adding a tag must not rebuild or change the image.

Publish both tags even for docs-only revisions so later revisions can inherit them. This does not require deploying every revision. Repeating tagging is harmless if the existing tag has the expected digest; a conflicting digest is an error, not permission to overwrite it. Use registry protection or isolated writers to prevent conflicting writes.

## Use the existing registries

- **Runtime:** tag the existing manifest or index in the repository ECS consumes: `quay.io/redhat-user-workloads/rosa-tenant/rosa-boundary`. Verify `linux/amd64` is available.
- **Lambda:** tag the existing single-platform manifest in staging ECR in the configured **primary replication region**. Keep the existing publisher and ECR replication. Each regional workspace uses that digest from its own region's repository; replica repositories do not need the commit tag itself.

Builds and releases to these destinations already run through the Konflux build/release pipelines. That is the expected place to add tagging for unchanged components, not a new promotion service or publishing path. Verify the tagging behavior within those pipelines without changing existing promotion or production publishing.

## Resolve images in each HCP run

Use the commit SHA of the run's `main` configuration to resolve its image tags. There is no separate historical-revision deployment mechanism.

Before a region can apply:

1. Resolve both full-commit-SHA tags to digests.
2. Verify the runtime is pullable and the Lambda digest is available in that region's ECR repository.
3. Put those digests in the Terraform plan for both ECS container references and the regional Lambda URI.

Wait with bounded retries for publication or replication. If an image remains unavailable, fail that region's run rather than use an older image. The primary replication region does not wait for replica regions.

Attach this readiness check to the existing HCP planning flow. Use HCP's existing per-workspace run queue and apply settings; do not add an apply coordinator, cross-region lock, paired-plan approval, saved-plan inspector, or custom deployment-ordering service. No run-scoped input API is prescribed: prefer resolving the tags directly during planning. The exact HCP hook and commit-SHA source remain implementation details to verify.

Image-only changes must trigger regional runs too. PR speculative plans remain non-deployable and may use clearly identified existing pins when commit tags do not exist; apply-capable staging runs must not fall back to those pins.

## Scope and implementation

1. Extend tagging in the existing Konflux build/release pipelines and test changed/unchanged components, missing images, retries and tag conflicts. The [helper build-trigger fix](https://github.com/openshift-online/rosa-boundary/pull/657) is already upstream.
2. Add an opt-in regional digest resolver and wire it into existing HCP runs. Test missing images and delayed regional replication, including that one region does not gate another.
3. Enable staging in a separate reviewed change after verifying the real registry and HCP integration. Keep explicit pins as the default outside this staging opt-in.

No S3 release record, release bucket, deployment branch, shared per-release workspace-variable edits, second Lambda publisher, or production changes. Retention and tag cleanup are deferred, not an implementation prerequisite.

Brief staging interruption while Lambda and ECS configuration update is acceptable; compatibility testing and versioned traffic switching are out of scope. Handle apply failures through the affected workspace's normal HCP recovery flow, without holding other regions.
