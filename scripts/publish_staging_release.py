#!/usr/bin/env python3
"""Verify an approved staging candidate and create its immutable S3 record.

See scripts/staging-release-publisher.md for the candidate and trust contract.
"""

import argparse
import hashlib
import json
import re
import subprocess
from contextlib import closing
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

ACCOUNT = "150100906299"
REGIONS = ["us-east-1", "us-west-2"]
BUCKET = "150100906299-rosa-boundary-stage-us-east-1-release-records"
RUNTIME = "quay.io/redhat-user-workloads/rosa-tenant/rosa-boundary"
LAMBDA = "rosa-boundary/create-investigation-lambda"
MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
SHA = r"[0-9a-f]{40}"
DIGEST = r"sha256:[0-9a-f]{64}"


class ReleaseError(ValueError):
    pass


def fields(obj, names, label):
    if not isinstance(obj, dict) or set(obj) != set(names.split()):
        raise ReleaseError(f"invalid fields in {label}; expected {names}")


def matches(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ReleaseError("duplicate JSON field")
    return result


def candidate_record(raw, candidate_sha256, revision):
    if not matches(revision, SHA):
        raise ReleaseError("expected a full lowercase infrastructure commit SHA")
    if hashlib.sha256(raw).hexdigest() != candidate_sha256:
        raise ReleaseError("candidate differs from the approved artifact checksum")
    record = json.loads(raw, object_pairs_hook=unique_keys)
    fields(record, "approval schema_version infrastructure_revision runtime lambda "
           "staging_account_id regions release_evidence", "candidate")
    if record.pop("approval") != "approved":
        raise ReleaseError("candidate lacks approval")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ReleaseError("expected schema_version 1")
    if record["infrastructure_revision"] != revision:
        raise ReleaseError("candidate is for a different infrastructure revision")
    if record["staging_account_id"] != ACCOUNT or record["regions"] != REGIONS:
        raise ReleaseError("candidate is not for the expected staging account and regions")
    for name, repository in (("runtime", RUNTIME), ("lambda", LAMBDA)):
        image = record[name]
        fields(image, "repository source_revision digest", name)
        if (image["repository"] != repository or not matches(image["source_revision"], SHA)
                or not matches(image["digest"], DIGEST)):
            raise ReleaseError(f"invalid {name} repository, source revision or manifest digest")
    evidence = record["release_evidence"]
    fields(evidence, "release_id compatibility_id runtime_build_id lambda_build_id", "release_evidence")
    for name, value in evidence.items():
        if not matches(value, r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}"):
            raise ReleaseError(f"expected a stable evidence identifier for {name}")
    return record


def inspect(option, ref):
    try:
        result = subprocess.run(
            ["skopeo", "--override-os", "linux", "--override-arch", "amd64",
             "inspect", "--tls-verify=true", "--retry-times", "2", "--retry-delay", "2s",
             option, f"docker://{ref}"],
            check=True, capture_output=True, timeout=45,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Registry errors may include signed URLs; log only the public reference.
        raise ReleaseError(f"registry inspection failed for {ref} ({type(exc).__name__})") from None
    return result.stdout


def verify_image(repository, image, *, allow_index):
    tagged = f"{repository}:{image['source_revision']}"
    manifest = inspect("--raw", tagged)
    digest = image["digest"]
    if "sha256:" + hashlib.sha256(manifest).hexdigest() != digest:
        raise ReleaseError(f"{tagged} does not match approved manifest digest")
    parsed = json.loads(manifest)
    accepted = MANIFEST_TYPES | INDEX_TYPES if allow_index else MANIFEST_TYPES
    if (not isinstance(parsed, dict) or parsed.get("mediaType") not in accepted
            or parsed.get("schemaVersion") != 2):
        raise ReleaseError(f"unsupported manifest type for {tagged}")
    # Inspect by digest: tag movement between these calls cannot change the
    # approved image. For an index skopeo selects the requested platform.
    config = json.loads(inspect("--config", f"{repository}@{digest}"))
    if not isinstance(config, dict) or (config.get("os"), config.get("architecture")) != ("linux", "amd64"):
        raise ReleaseError(f"{tagged} is not usable on linux/amd64")


def put_record(s3, record):
    key = f"releases/v1/{record['infrastructure_revision']}.json"
    target = dict(Bucket=BUCKET, Key=key, ExpectedBucketOwner=ACCOUNT)
    body = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        s3.put_object(**target, Body=body, IfNoneMatch="*", ContentType="application/json",
                      ServerSideEncryption="AES256")
        return f"published s3://{BUCKET}/{key}"
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "PreconditionFailed":
            # A 409 race or an ambiguous timeout can be retried by running
            # the exact approved candidate again. Never fall back to overwrite.
            raise
    with closing(s3.get_object(**target)["Body"]) as existing:
        if existing.read(len(body) + 1) != body:
            raise ReleaseError(f"immutable release conflicts at s3://{BUCKET}/{key}")
    return f"already published s3://{BUCKET}/{key}"


def publish(record):
    config = Config(connect_timeout=5, read_timeout=15, ignore_configured_endpoint_urls=True,
                    retries={"mode": "standard", "total_max_attempts": 3})
    session = boto3.Session(region_name=REGIONS[0])
    if session.client("sts", config=config).get_caller_identity()["Account"] != ACCOUNT:
        raise ReleaseError("AWS caller is outside the staging account")
    verify_image(RUNTIME, record["runtime"], allow_index=True)
    for region in REGIONS:
        repository = f"{ACCOUNT}.dkr.ecr.{region}.amazonaws.com/{LAMBDA}"
        verify_image(repository, record["lambda"], allow_index=False)
    return put_record(session.client("s3", config=config), record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1],
                        help="trusted Boundary checkout with complete commit history (default: script repository)")
    parser.add_argument("--candidate", required=True, type=Path, help="approved candidate JSON")
    parser.add_argument("--candidate-sha256", required=True, help="authenticated artifact SHA-256 (hex)")
    parser.add_argument("--infrastructure-revision", required=True, help="target full Git commit SHA")
    args = parser.parse_args()
    if __package__:
        from .staging_release_inputs import verify_component_inputs
    else:
        from staging_release_inputs import verify_component_inputs

    try:
        with args.candidate.open("rb") as source:
            raw = source.read(16385)
        if len(raw) > 16384:
            raise ReleaseError("candidate exceeds 16 KiB")
        record = candidate_record(raw, args.candidate_sha256, args.infrastructure_revision)
        verify_component_inputs(args.repo, args.infrastructure_revision, record["runtime"]["source_revision"],
                                record["lambda"]["source_revision"])
        print(publish(record))
    except ClientError as exc:
        parser.exit(1, f"AWS {exc.operation_name} failed: {exc.response['Error']['Code']}\n")
    except BotoCoreError as exc:
        parser.exit(1, f"AWS request failed ({type(exc).__name__})\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
