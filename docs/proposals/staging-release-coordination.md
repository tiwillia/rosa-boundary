# Staging infrastructure and image release coordination

**Status:** Proposal; no solution selected.

**Tracking:** [ROSAENG-67168](https://redhat.atlassian.net/browse/ROSAENG-67168)

**Scope:** Boundary staging infrastructure, runtime image, and create-investigation Lambda image. Production is out of scope.

**Recommendation:** Pursue option 3 using an immutable release record and mandatory readiness checks. Final selection remains subject to review.

## Problem

Merges to `main` independently trigger applicable Konflux builds and HCP Terraform runs. Staging regional workspaces auto-apply successful plans, while image versions remain explicitly pinned in `hcp-terraform/rosa-boundary/main.tf` until a separate update merges.

Infrastructure can therefore deploy before its required application artifacts are selected, leaving a functionally broken system despite a successful Terraform apply. After [PR #280](https://github.com/openshift-online/rosa-boundary/pull/280), task-definition changes deployed with an older Lambda image, causing `start-task` to fail with `Unknown volume 'ocm-config'`.

The goal is to coordinate compatible infrastructure and artifacts, not merely automate image updates.

## Option 1: Konflux image-pin nudging

Under this option, Konflux would automatically trigger upstream PRs updating staging image pins after successful builds, preserving review and HCP speculative plans. The Jira discussion proposes native component nudging, a no-build target Component, and custom Renovate matching for Terraform references. Lambda images already have an ECR publication path; promotion must observe its results and regional replication readiness.

**Advantages:** Removes manual pin-update work and retains the existing Git-based promotion model.

**Limitations:** A follow-up PR leaves the original unsafe interval: dependent infrastructure can deploy before image promotion. Independent nudges do not coordinate runtime/Lambda compatibility or ECR replication. This option needs an additional rollout gate to solve the full problem.

## Option 2: Dedicated staging deployment branch

Staging follows a protected deployment branch instead of `main`. After artifacts are published, a release PR promotes the selected infrastructure revision and compatible image pins together. Workspace-variable reconciliation must precede dependent regional applies.

**Advantages:** Separates source integration from deployment and records the intended release combination in Git.

**Limitations:** Deployment branches are atypical in ROSA, where deployment coordination normally lives in separate systems. This adds branch-management automation and requires separating staging's release boundary from the shared staging/production meta-workspace configuration.

## Option 3: Revision-bound release record and readiness gate

Replace manually maintained staging image pins with an immutable release record identifying the infrastructure commit, compatible runtime and Lambda image digests, registry destinations, and build/release evidence. Terraform resolves the record for its exact source revision and includes those image identities in the saved plan. Commit-SHA tags establish provenance; selection must not depend on publication time or mutable "latest" references.

Prefer initiating deployment after artifact publication. A mandatory HCP run task or equivalent readiness gate verifies the record and artifact availability before planning, with bounded waiting and fail-closed behavior. Check the actual Quay repository staging consumes and every target regional ECR repository, not release success alone. Failed early runs require an exact-revision retry, never a retry against an independently resolved "latest."

The gate verifies readiness; it does not select images or mutate workspace variables. Revision and artifact handoff to Terraform require design. An apply-only gate is possible if the plan already fixes image identities; it must not reselect versions.

**Advantages:** Keeps the existing branch model and lets one plan include infrastructure changes and their required images.

**Limitations:** Requires an operational owner for the release-record publisher and verifier, plus registry access. Path-filtered builds require explicit artifact inheritance based on component inputs/provenance or rebuilding; image-only changes must also trigger staging deployment. Speculative plans need a separate, clearly labelled artifact policy. Concurrent and cross-region runs must not allow stale candidates to overwrite newer deployments.

## Shared considerations

Artifact availability does not prove compatibility, and one Terraform apply is not an atomic deployment. The Lambda currently reads an ECS task-definition family, allowing old code to observe a newly registered definition during rollout. Changes must preserve compatibility during that transition or introduce explicit task-definition revision and Lambda-version switching with suitable retention.

Any selected approach needs recovery procedures and retained known-good artifacts. Meta-workspace speculative plans show workspace-variable changes, not downstream AWS resource plans. Production's existing pins and deployment behavior remain unchanged.
