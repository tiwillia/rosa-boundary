import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, call, patch

from botocore.exceptions import ClientError

from scripts import publish_staging_release as publisher


def manifest(media_type):
    return json.dumps({"schemaVersion": 2, "mediaType": media_type}).encode()


RUNTIME_BYTES = manifest("application/vnd.oci.image.index.v1+json")
LAMBDA_BYTES = manifest("application/vnd.oci.image.manifest.v1+json")
INFRA = "a" * 40


def component(repo, sha, blob):
    return {"repository": repo, "source_revision": sha, "digest": "sha256:" + hashlib.sha256(blob).hexdigest()}


def candidate():
    return {
        "approval": "approved", "schema_version": 1, "infrastructure_revision": INFRA,
        "runtime": component(publisher.RUNTIME, "b" * 40, RUNTIME_BYTES),
        "lambda": component(publisher.LAMBDA, "c" * 40, LAMBDA_BYTES),
        "staging_account_id": "150100906299", "regions": ["us-east-1", "us-west-2"],
        "release_evidence": {"release_id": "releases/42", "compatibility_id": "gates/50",
                             "runtime_build_id": "build/15", "lambda_build_id": "build/21"},
    }


def parse(obj, revision=INFRA):
    raw = json.dumps(obj).encode()
    return publisher.candidate_record(raw, hashlib.sha256(raw).hexdigest(), revision)


def aws_error(code):
    return ClientError({"Error": {"Code": code}}, "PutObject")


class CandidateTests(unittest.TestCase):
    def test_cli_requires_input_history_check_before_publication(self):
        raw = json.dumps(candidate()).encode()
        helper = MagicMock()
        helper.verify_component_inputs.side_effect = ValueError("incompatible build inputs")
        argv = ["publish", "--repo", "/trusted/repo", "--candidate", "approved.json",
                "--candidate-sha256", hashlib.sha256(raw).hexdigest(), "--infrastructure-revision", INFRA]
        with (patch.dict(sys.modules, {"scripts.staging_release_inputs": helper}),
              patch.object(sys, "argv", argv), patch.object(Path, "open", return_value=io.BytesIO(raw)),
              patch.object(publisher, "publish") as publish, patch("sys.stderr", new_callable=io.StringIO),
              self.assertRaises(SystemExit) as exit_status):
            publisher.main()
        self.assertEqual(exit_status.exception.code, 1)
        helper.verify_component_inputs.assert_called_once_with(Path("/trusted/repo"), INFRA, "b" * 40, "c" * 40)
        publish.assert_not_called()

    def test_registry_timeout_fails_without_printing_tool_output(self):
        ref = publisher.RUNTIME + ":" + INFRA
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(
                "skopeo", 45, stderr=b"private registry error")) as run:
            with self.assertRaises(publisher.ReleaseError) as err:
                publisher.inspect("--raw", ref)
        self.assertNotIn("private registry error", str(err.exception))
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        self.assertEqual(run.call_args.args[0][-2:], ["--raw", "docker://" + ref])

    def test_rejects_unbound_or_ambiguous_input(self):
        for path, value in [
            (("approval",), "pending"), (("infrastructure_revision",), "f" * 40),
            (("schema_version",), True), (("staging_account_id",), "514903006123"),
            (("regions",), ["us-east-1"]), (("runtime", "source_revision"), "latest"),
            (("lambda", "source_revision"), "b62d9ee"),
            (("runtime", "repository"), "quay.io/unapproved/repo"),
            (("lambda", "repository"), "rosa-boundary/other"),
            (("runtime", "digest"), "sha256-" + "0" * 64),
            (("release_evidence", "compatibility_id"), ""),
            (("release_evidence", "unexpected"), "extra"),
        ]:
            with self.subTest(path=path), self.assertRaises(publisher.ReleaseError):
                obj = candidate()
                target = obj if len(path) == 1 else obj[path[0]]
                target[path[-1]] = value
                parse(obj)
        with self.assertRaisesRegex(publisher.ReleaseError, "checksum"):
            publisher.candidate_record(json.dumps(candidate()).encode(), "0" * 64, INFRA)
        raw = b'{"approval":"pending","approval":"approved"}'
        with self.assertRaisesRegex(publisher.ReleaseError, "duplicate"):
            publisher.candidate_record(raw, hashlib.sha256(raw).hexdigest(), INFRA)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.record = parse(candidate())
        self.session_factory = self.enterContext(patch.object(publisher.boto3, "Session"))
        self.clients = {"sts": MagicMock(), "s3": MagicMock()}
        self.session_factory.return_value.client.side_effect = lambda name, **_: self.clients[name]
        self.clients["sts"].get_caller_identity.return_value = {"Account": publisher.ACCOUNT}
        self.inspect = self.enterContext(patch.object(publisher, "inspect", side_effect=self.registry))

    @staticmethod
    def registry(option, ref):
        if option == "--raw":
            return RUNTIME_BYTES if ref.startswith("quay.io/") else LAMBDA_BYTES
        return b'{"os":"linux","architecture":"amd64"}'

    def test_publish_binds_tags_and_digests_in_every_registry(self):
        result = publisher.publish(self.record)
        refs = [("quay.io/redhat-user-workloads/rosa-tenant/rosa-boundary", self.record["runtime"])]
        for region in ["us-east-1", "us-west-2"]:
            refs.append((f"150100906299.dkr.ecr.{region}.amazonaws.com/rosa-boundary/create-investigation-lambda",
                         self.record["lambda"]))
        self.assertEqual(self.inspect.call_args_list, [
            c for repo, item in refs for c in (
                call("--raw", repo + ":" + item["source_revision"]), call("--config", repo + "@" + item["digest"])
            )
        ])
        self.session_factory.assert_called_once_with(region_name="us-east-1")
        put = self.clients["s3"].put_object.call_args.kwargs
        self.assertEqual(put["Bucket"], "150100906299-rosa-boundary-stage-us-east-1-release-records")
        self.assertEqual(put["ServerSideEncryption"], "AES256")
        self.assertEqual(put["ExpectedBucketOwner"], "150100906299")
        self.assertEqual(put["Key"], f"releases/v1/{INFRA}.json")
        self.assertEqual(put["IfNoneMatch"], "*")
        expected = candidate()
        del expected["approval"]
        self.assertEqual(json.loads(put["Body"]), expected)
        self.assertIn("published s3://", result)

    def test_rejects_nonstage_caller_before_registry_or_write(self):
        self.clients["sts"].get_caller_identity.return_value = {"Account": "514903006123"}
        with self.assertRaisesRegex(publisher.ReleaseError, "staging account"):
            publisher.publish(self.record)
        self.inspect.assert_not_called()
        self.clients["s3"].put_object.assert_not_called()

    def test_missing_or_mismatched_image_never_publishes(self):
        for registry in ["quay.io", "us-east-1", "us-west-2"]:
            for missing in [False, True]:
                def lookup(option, ref):
                    if registry in ref and option == "--raw":
                        if missing:
                            raise publisher.ReleaseError("not replicated")
                        return b"other manifest"
                    return self.registry(option, ref)
                with self.subTest(registry=registry, missing=missing):
                    self.inspect.side_effect = lookup
                    with self.assertRaises(publisher.ReleaseError):
                        publisher.publish(self.record)
                    self.clients["s3"].put_object.assert_not_called()

    def test_wrong_platform_or_lambda_index_never_publishes(self):
        self.inspect.side_effect = lambda opt, ref: (
            b'{"os":"linux","architecture":"arm64"}' if opt == "--config" else self.registry(opt, ref))
        with self.assertRaisesRegex(publisher.ReleaseError, "linux/amd64"):
            publisher.publish(self.record)
        self.record["lambda"] = component(publisher.LAMBDA, "d" * 40, RUNTIME_BYTES)
        self.inspect.side_effect = lambda opt, ref: RUNTIME_BYTES if opt == "--raw" else self.registry(opt, ref)
        with self.assertRaisesRegex(publisher.ReleaseError, "manifest type"):
            publisher.publish(self.record)
        self.clients["s3"].put_object.assert_not_called()

    def test_retry_accepts_only_identical_bytes(self):
        s3 = self.clients["s3"]
        publisher.put_record(s3, self.record)
        body = s3.put_object.call_args.kwargs["Body"]
        s3.put_object.side_effect = aws_error("PreconditionFailed")
        for existing in [body, body + b"extra", b'{"different":true}']:
            with self.subTest(existing_matches=existing == body):
                s3.get_object.return_value = {"Body": io.BytesIO(existing)}
                if existing == body:
                    self.assertIn("already published", publisher.put_record(s3, self.record))
                else:
                    with self.assertRaisesRegex(publisher.ReleaseError, "conflicts"):
                        publisher.put_record(s3, self.record)
        self.assertTrue(all(c.kwargs["IfNoneMatch"] == "*" for c in s3.put_object.call_args_list))

    def test_access_error_is_not_idempotent_success(self):
        s3 = self.clients["s3"]
        s3.put_object.side_effect = aws_error("AccessDenied")
        with self.assertRaises(ClientError):
            publisher.put_record(s3, self.record)
        s3.get_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
