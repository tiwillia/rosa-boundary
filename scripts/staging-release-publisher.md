# Publish an approved staging release

`publish_staging_release.py` is a release-job entry point. The release system
must authenticate the approval, successful component builds/releases, and
compatibility decision for **this exact tuple**: infrastructure commit, both
component commits, both destination manifest digests, account, and regions.
The compatibility decision must cover the old/new rollout transition as well
as the final combination. Stable evidence IDs below must resolve in that
system. Registry presence and unchanged build inputs alone do not prove this.

Supply the candidate file and its expected **file SHA-256** from the protected
release job. The job obtains the checksum from authenticated approval metadata;
computing it from an untrusted download or accepting it from a PR parameter is
not verification. The CLI checks this binding, but does not authenticate a
signer, contact an approval service, or interpret its evidence. Only the
trusted job may assume the publisher identity.

Candidate shape (placeholders must be replaced; revisions are 40 lowercase
hex characters and image digests use `sha256:` plus 64 lowercase hex):

```json
{
  "approval": "approved",
  "schema_version": 1,
  "infrastructure_revision": "<target infrastructure SHA>",
  "runtime": {
    "repository": "quay.io/redhat-user-workloads/rosa-tenant/rosa-boundary",
    "source_revision": "<runtime build SHA>",
    "digest": "<approved Quay manifest or index digest>"
  },
  "lambda": {
    "repository": "rosa-boundary/create-investigation-lambda",
    "source_revision": "<Lambda build SHA>",
    "digest": "<approved regional ECR single-image manifest digest>"
  },
  "staging_account_id": "150100906299",
  "regions": ["us-east-1", "us-west-2"],
  "release_evidence": {
    "release_id": "releases/123",
    "compatibility_id": "compatibility/456",
    "runtime_build_id": "builds/runtime/789",
    "lambda_build_id": "builds/lambda/234"
  }
}
```

Use only non-secret evidence identifiers. The S3 JSON record has these fields
minus `approval`. It contains no local timestamps, credentials, or presigned URLs.

**Runner prerequisites**

- Trusted Boundary checkout with all three commits and the required ancestry,
  trees, and blobs fetched before invocation (use full history). Run the
  publisher and helper from trusted release tooling, not from unreviewed
  candidate code. `--repo` defaults to the checkout containing the script.
- Python 3.11+ with current boto3/botocore supporting conditional S3
  `PutObject`; skopeo supporting `inspect --raw`, `--config`, and bounded retries.
- Staging-scoped short-lived AWS credentials. Use trusted registry configuration
  with no mirrors or rewrites for these destinations. Configure skopeo's read access
  to Quay and both staging ECR registries, e.g. via the ECR credential helper
  on the trusted runner. No registry credential is a candidate or CLI field.
- A dedicated release publisher role with read access to the images, and
  `s3:PutObject` and `s3:GetObject` with SSE-S3 for staging
  `releases/v1/` only. Enforce conditional writes and prevent deletion or
  expiry for the release prefix in AWS; the CLI cannot enforce retention or
  protect against other writers. Do not reuse a content sync/delete role.

```sh
python3 scripts/publish_staging_release.py \
  --repo "$TRUSTED_BOUNDARY_CHECKOUT" \
  --candidate "$APPROVED_CANDIDATE_FILE" \
  --candidate-sha256 "$AUTHENTICATED_CANDIDATE_SHA256" \
  --infrastructure-revision "$TARGET_INFRASTRUCTURE_COMMIT"
```

The publisher checks the Git build inputs, resolves the exact component SHA
tags in Quay and both ECR regions, hashes raw manifests, and inspects the
approved digests for `linux/amd64`. An OCI/Docker index is allowed for the
runtime; Lambda needs a single-image manifest. A digest-shaped tag, image
config digest, or digest from a different registry is not a manifest check.

The only write is a conditional PUT to
`s3://150100906299-rosa-boundary-stage-us-east-1-release-records/releases/v1/<infrastructure_revision>.json`.
An identical existing record succeeds; a different one fails. On replication
lag, a concurrent-write conflict, or an ambiguous timeout, the job can retry
the **same** approved inputs with bounded backoff. Resolve a conflicting
release through a new approved infrastructure revision. Publication does not
queue HCP or prove a deployment.

Offline tests: `python3 -m unittest scripts.test_publish_staging_release`.
