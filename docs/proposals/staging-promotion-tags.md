# Staging release coordination with promotion tags

> Historical draft, archived 2026-11-24 with the retired S3 prototype. Its tag prefixes and cross-region coordination are superseded. Continue from the [current proposal on `staging-promotion-tags`](https://github.com/tiwillia/rosa-boundary/blob/staging-promotion-tags/docs/proposals/staging-promotion-tags.md), not this branch.

**Status:** Proposed for review, not implemented or enabled. **Scope:** Boundary runtime, create-investigation Lambda, and the two Boundary staging regional workspaces. [ROSAENG-67168](https://redhat.atlassian.net/browse/ROSAENG-67168).

## Decision

Keep path-filtered Konflux builds. Add a staging promotion job that gives each eligible `main` revision a pair of registry references: `runtime:stage-<full-commit>` and `lambda:stage-<full-commit>`. A build SHA says where an image came from; a promotion tag says which image staging selected for an infrastructure revision. Adding the latter changes neither the image bytes nor its build provenance. Terraform deploys **digests**, never a moving tag.

A digest is the content fingerprint of an image manifest (or multi-platform index), written `sha256:…`. Unlike a Git commit ID, it identifies the built artifact, not its source. Two tags can name the same digest without creating two images.

This replaces the proposed S3 release record. No release bucket, deployment branch, `latest` lookup, per-release Terraform variable edit, or human-ordered pin PR. Production pins, production registry destinations, and production HCP behavior stay unchanged. This prevents the stale-image failure behind [PR #280](https://github.com/openshift-online/rosa-boundary/pull/280); it is not a claim that available images are necessarily compatible.

## What happens on merge

A trusted job processes the first-parent sequence on `main`. For revision `C`, compare its tree with its first parent `P`, **not** with the branch tip at job execution time. Produce aliases for every mainline revision after bootstrap so ancestry is unambiguous; a docs-only merge needs two inexpensive aliases, not two builds or an infrastructure deployment. Reconcile missed events in ancestry order. Use one reviewed definition of component inputs for classification and contract tests against the actual build triggers, including the runtime's bundled helpers and both push build recipes.

| Component at `C` | Promotion rule |
|---|---|
| Build inputs changed | Require the successful, policy-accepted build for **that component at `C`**, its digest and publication in the registry staging consumes. Do not reuse an older green build if this build fails or is missing. |
| Build inputs unchanged | Resolve that component's exact `stage-P` alias and reuse its digest. A missing parent alias blocks inheritance. Never infer “unchanged” from a skipped or missing pipeline. |
| Inputs cannot be classified, or history is incomplete | Stop. An explicitly verified fresh build for `C` can establish a new baseline; a lookup by registry time cannot. |

For example, if `C` changes the Lambda and its ECS task definition but not the runtime, promote the parent's runtime digest and `C`'s Lambda digest. A failed candidate must not poison its descendants: retry the same revision for transient errors; to restart after an unreleasable ancestor, qualify fresh builds for **both** components at a later revision. Never silently skip a failed revision and inherit past it.

## Where images are checked

- **Runtime:** verify the digest is pullable for `linux/amd64` in the Quay repository ECS actually uses (`quay.io/redhat-user-workloads/rosa-tenant/rosa-boundary`). Add `stage-C` to that manifest or index without rebuilding. Preserve its original SHA tag, labels, signatures and attestations.
- **Lambda:** use the existing release to ordinary staging ECR in `us-east-1`. Add `stage-C` there by attaching the existing single-platform manifest to a new tag. Resolve the east tag to digest `D` and verify **the same manifest digest `D`** in `us-west-2`. Use the repository in each Lambda's own region with `@D`. The west alias is not required. [ECR retagging](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-retag.html) avoids layer uploads; [replication](https://docs.aws.amazon.com/AmazonECR/latest/userguide/replication.html) is asynchronous. Do not create a second Lambda publisher or assume source success backfills a missing replica.
- **Retries:** create once; an existing alias is success only if it resolves to the expected digest. A conflicting alias fails closed. Alias writes across repositories are not atomic: a lone alias is not a ready release. Use bounded retries for publication and replication, then fail with the candidate, component, region and missing or conflicting digest. Do not start either regional apply on timeout.

## How HCP enforces it

Keep release coordination in the existing delivery systems. The staging promotion job is the proposed **only automated apply coordinator** for the two regional workspaces. It takes the HCP configuration version for `C`, not “latest `main`”, and submits or resumes the exact-revision plans after both aliases and both ECR destinations are ready. Image-only changes must also reach this path. An ordinary VCS or meta-workspace trigger may produce a plan, but must not independently apply it.

The staging resolver supplies the two verified digests as **run-scoped inputs**, not shared workspace variables. Each regional plan must fix both ECS container image references and its local Lambda URI to those digests. Before allowing either apply, the coordinator checks both saved plans reference `C` and the expected images. It processes deployment candidates in order across both workspaces; a late retry cannot deploy an older candidate over a newer one. After a partial apply, hold the next rollout until both regions have been reconciled or recovered. Use existing job/run histories for diagnostics, not as a new release database.

PR speculative plans remain available and non-deployable. Where post-merge aliases do not yet exist, label the preview as using the currently deployed digests; that fallback must be impossible on an apply-capable run. Preserve normal Git review for source and infrastructure. Turning off auto-apply alone is not the gate: restrict apply permissions and cover VCS, meta-triggered, retried and manually queued runs.

## Staging scope, bootstrap and recovery

The gate prevents Terraform from deploying with missing or stale images. It does not test whether the old Lambda works with the new ECS task definition during the apply. Staging accepts that `start-task` may briefly fail until both updates finish; uninterrupted service and versioned traffic switching are out of scope. A failed or partial apply is still a deployment failure, not an assumed transient.

Bootstrap with an explicit `main` revision and verify both selected images' provenance and availability. Do not relabel today's staging pins as today's `main` without evidence. Shadow the publisher and compare candidate plans without granting it apply access. Put the two regional workspaces behind the gate **before** removing staging's explicit pins. Keep prior known-good digests available in Quay and both ECR regions; rollback is a new reviewed recovery plan against current infrastructure, not replay of an old queued run.

## Implementation order and stop conditions

1. **Prove the integration before enabling anything.** Confirm an approved staging-only Konflux task and short-lived identity can retag the exact Quay and east ECR repositories without bypassing release policy or publishing aliases to production. Confirm `stage-*` tags are protected from replacement, retention/tag quotas allow an alias per revision, and a trusted job can serialize promotions. If Quay cannot protect the aliases or safely isolate writers, stop; read-before-write alone is not immutability.
2. **Add the build-or-inherit promotion job and tests** separately from deployment. Include bootstrap, same-digest retry, conflicting alias, failed parent, partial alias pair and delayed west replication. Use the independent [helper build-trigger fix](https://github.com/openshift-online/rosa-boundary/pull/657); do not bundle it again.
3. **Add the default-off regional digest consumer and HCP integration** independently. Prove the actual HCP workspace/module supports exact configuration selection, per-run inputs, saved-plan inspection and non-bypassable ordering for both regions, including runs with no regional file diff. If the existing integration cannot do this, stop and present alternatives; do not ship a best-effort gate.
4. **Switch staging in its own reviewed change** only after the preceding pieces work. Start from current upstream `main`, reuse applicable digest-selection/tests from the prototype, and leave the prototype's S3 resources, record publisher and bucket-based resolver out. No production change or live rollout is implied by approving this proposal.
