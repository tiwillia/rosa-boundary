"""Offline Bedrock payload teardown regressions; no real AWS calls are permitted."""

import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from ephemeral_environment.runtime import CommandError
from test_ephemeral_env import Fixture, eph


class PayloadFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.events = []
        self.requests = []
        self.runner = Mock()
        self.runner.json.side_effect = self.command
        self.runner.run.side_effect = self.command
        self.environment = self.env(self.runner)
        self.environment.meta = {
            "owner": "payload-owner", "roots": {}, "status": "deployed",
            "active_generation": None,
        }
        self.environment.load = Mock()
        self.environment.save = Mock()
        self.environment.check_work = Mock()
        self.environment.plan_apply = Mock(
            side_effect=lambda key, **kwargs: self.events.append(("destroy", key)))
        self.account = self.config["account_id"]
        self.identity_account = self.account
        self.owner = self.environment.meta["owner"]
        self.location = self.config["region"]
        self.bucket = self.bucket_name()
        self.logging = {
            "cloudWatchConfig": {
                "logGroupName": "/eph/example/bedrock",
                "roleArn": f"arn:aws:iam::{self.account}:role/eph-example-bedrock",
                "largeDataDeliveryS3Config": {
                    "bucketName": self.bucket, "keyPrefix": "invocations/",
                },
            },
            "textDataDeliveryEnabled": True,
        }
        self.version_pages = [{"IsTruncated": False}]
        self.upload_pages = [{"IsTruncated": False}]
        self.delete_result = None
        self.tag_error = None
        self.write_state()

    def bucket_name(self):
        return (f"{self.account}-eph-example-dev-{self.config['region']}"
                "-bedrock-invocations")

    def resources(self):
        cloudwatch = self.logging["cloudWatchConfig"]
        return [
            {"mode": "managed", "type": "aws_s3_bucket", "name": "bedrock_large_payloads",
             "instances": [{"attributes": {"id": self.bucket}}]},
            {"mode": "managed", "type": "aws_bedrock_model_invocation_logging_configuration",
             "name": "boundary", "instances": [{"attributes": {"logging_config": [{
                 "cloudwatch_config": [{
                     "log_group_name": cloudwatch["logGroupName"],
                     "role_arn": cloudwatch["roleArn"],
                     "large_data_delivery_s3_config": [{
                         "bucket_name": self.bucket, "key_prefix": "invocations/",
                     }],
                 }],
             }]}}]},
        ]

    def write_state(self, resources=None):
        path = self.environment.home / "boundary-regional.tfstate"
        path.write_text(json.dumps({
            "lineage": "payload-lineage", "serial": 1,
            "resources": self.resources() if resources is None else resources,
        }))
        self.environment.meta["roots"]["boundary-regional"] = {
            "state_hash": eph.digest(path.read_bytes()),
        }
        return path

    def command(self, command, **kwargs):
        self.assertEqual(command[:5], [
            "aws", "--profile", self.config["profile"], "--region", self.config["region"],
        ], "Only the mocked AWS runner may be used")
        args = command[5:]
        if args[-2:] == ["--output", "json"]:
            args = args[:-2]
        self.events.append(tuple(args))
        self.requests.append((list(command), kwargs))
        service, operation, *options = args
        if service == "sts" and operation == "get-caller-identity":
            return {"Account": self.identity_account,
                    "Arn": f"arn:aws:iam::{self.identity_account}:user/dev"}
        if service == "bedrock":
            if operation == "get-model-invocation-logging-configuration":
                return {"loggingConfig": copy.deepcopy(self.logging)} if self.logging else {}
            if operation == "delete-model-invocation-logging-configuration":
                self.logging = None
                return b""
        if service == "s3api":
            self.assertEqual(options[options.index("--bucket") + 1], self.bucket)
            self.assertEqual(options[options.index("--expected-bucket-owner") + 1], self.account)
            if operation == "get-bucket-tagging":
                if self.tag_error:
                    raise self.tag_error
                return {"TagSet": [{"Key": "EphemeralOwner", "Value": self.owner}]}
            if operation == "get-bucket-location":
                return {"LocationConstraint": self.location}
            if operation == "list-object-versions":
                return self.version_pages.pop(0)
            if operation == "list-multipart-uploads":
                return self.upload_pages.pop(0)
            if operation == "delete-objects":
                return (self.delete_result if self.delete_result is not None
                        else {"Deleted": json.loads(kwargs["input"])["Objects"]})
            if operation == "abort-multipart-upload":
                return b""
        self.fail(f"Unexpected AWS call: {args}")

    def mutations(self):
        return [event for event in self.events if event[0] == "destroy" or (len(event) > 1 and event[1] in {
            "delete-objects", "abort-multipart-upload",
            "delete-model-invocation-logging-configuration",
        })]

    def cleanup(self, yes=True):
        with redirect_stdout(io.StringIO()) as output:
            self.environment.cleanup_bedrock_payloads(yes=yes, account=self.account)
        return output.getvalue()

    def destroy(self, yes=True):
        with redirect_stdout(io.StringIO()) as output:
            self.environment.destroy(yes=yes, account=self.account)
        return output.getvalue()


class PayloadOwnershipTests(PayloadFixture):
    def test_state_hash_is_verified_before_any_aws_calls(self):
        path = self.environment.home / "boundary-regional.tfstate"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(eph.Error, "ownership/hash mismatch"):
            self.cleanup()
        self.assertEqual(self.events, [])

    def test_only_current_managed_root_named_bucket_is_selected(self):
        original = self.resources()
        ignored = []
        for update in (
            {"mode": "data"}, {"module": "module.foreign"},
            {"name": "other"}, {"type": "aws_s3_bucket_policy"},
            {"instances": [{"deposed": "old", "attributes": {"id": self.bucket}}]},
        ):
            ignored.append(original[0] | update)
        self.write_state(ignored)
        self.cleanup()
        self.assertEqual(self.events, [])
        self.write_state(ignored + original)
        self.cleanup()
        self.assertEqual(len(self.mutations()), 1)

    def test_missing_or_destroyed_state_needs_no_new_aws_calls(self):
        path = self.environment.home / "boundary-regional.tfstate"
        path.unlink()
        self.environment.meta["roots"] = {}
        self.cleanup()
        self.assertEqual(self.events, [])
        self.write_state()
        self.environment.meta["roots"]["boundary-regional"]["destroyed"] = True
        self.cleanup()
        self.assertEqual(self.events, [])

    def test_exact_environment_bucket_required_even_with_verified_hash(self):
        for identifier in (
            self.bucket.replace("example", "another"),
            self.bucket.replace(self.account, "999999999999"),
            self.bucket.replace("us-west-2", "us-east-1"),
            self.bucket + "-extra", None,
        ):
            with self.subTest(identifier=identifier):
                resources = self.resources()
                resources[0]["instances"][0]["attributes"]["id"] = identifier
                self.write_state(resources)
                with self.assertRaisesRegex(eph.Error, "does not match this disposable environment"):
                    self.cleanup()
                self.assertEqual(self.events, [])

    def test_duplicate_current_bucket_instances_refused(self):
        resources = self.resources()
        resources[0]["instances"] *= 2
        self.write_state(resources)
        with self.assertRaisesRegex(eph.Error, "does not match"):
            self.cleanup()
        self.assertEqual(self.events, [])

    def test_live_owner_and_region_mismatch_refuse_before_approval(self):
        for field, value, message in (
            ("owner", "foreign-owner", "matching live ownership"),
            ("owner", None, "matching live ownership"),
            ("location", "us-east-1", "outside the approved region"),
        ):
            with self.subTest(field=field, value=value):
                original = getattr(self, field)
                setattr(self, field, value)
                with patch("builtins.input") as prompt, self.assertRaisesRegex(eph.Error, message):
                    self.cleanup(yes=False)
                prompt.assert_not_called()
                self.assertEqual(self.mutations(), [])
                setattr(self, field, original)

    def test_legacy_location_constraints_are_normalized(self):
        logging = copy.deepcopy(self.logging)
        for region, location in (("us-east-1", None), ("eu-west-1", "EU")):
            with self.subTest(region=region):
                self.logging = copy.deepcopy(logging)
                self.config["region"] = region
                self.bucket = self.bucket_name()
                self.logging["cloudWatchConfig"]["largeDataDeliveryS3Config"]["bucketName"] = self.bucket
                self.location = location
                self.write_state()
                self.version_pages = [{"IsTruncated": False}]
                self.upload_pages = [{"IsTruncated": False}]
                self.cleanup()

    def test_only_precise_no_such_bucket_allows_retry(self):
        self.tag_error = CommandError("missing", stderr=(
            b"An error occurred (NoSuchBucket) when calling the GetBucketTagging operation: absent"))
        self.cleanup()
        self.assertEqual([event[:2] for event in self.events], [("s3api", "get-bucket-tagging")])
        self.assertEqual(self.mutations(), [])

    def test_already_removed_bucket_allows_remaining_terraform_destroy(self):
        self.tag_error = CommandError("missing", stderr=(
            b"(NoSuchBucket) when calling the GetBucketTagging operation: absent"))
        self.destroy()
        self.assertEqual(self.mutations(), [("destroy", "boundary-regional")])
        self.assertFalse(any(event[0] == "bedrock" for event in self.events))
        self.assertEqual(self.environment.meta["status"], "destroyed")

    def test_bucket_removed_during_approval_skips_logging_and_payload_mutations(self):
        def disappear(*args):
            self.tag_error = CommandError("missing", stderr=(
                b"(NoSuchBucket) when calling the GetBucketTagging operation: absent"))

        self.environment.approve = Mock(side_effect=disappear)
        self.cleanup()
        self.environment.approve.assert_called_once()
        self.assertEqual(self.mutations(), [])
        self.assertFalse(any(event[:2] == (
            "bedrock", "delete-model-invocation-logging-configuration") for event in self.events))

    def test_access_denied_and_lookalike_missing_errors_are_not_ignored(self):
        for stderr in (
            b"(AccessDenied) when calling the GetBucketTagging operation",
            b"NoSuchBucket",
            b"(NoSuchBucket) when calling the GetBucketLocation operation",
            b"(NotNoSuchBucket) when calling the GetBucketTagging operation",
            b"unrecognized response",
        ):
            with self.subTest(stderr=stderr):
                self.tag_error = CommandError("mock AWS failure", stderr=stderr)
                with self.assertRaises(CommandError) as caught:
                    self.cleanup()
                self.assertIs(caught.exception, self.tag_error)
                self.assertEqual(self.mutations(), [])

    def test_live_owner_region_and_state_are_rechecked_after_approval(self):
        path = self.environment.home / "boundary-regional.tfstate"
        for change, message in (
            (lambda: setattr(self, "owner", "foreign"), "matching live ownership"),
            (lambda: setattr(self, "location", "us-east-1"), "outside the approved region"),
            (lambda: path.write_text(path.read_text() + "\n"), "ownership/hash mismatch"),
        ):
            with self.subTest(message=message):
                self.owner = self.environment.meta["owner"]
                self.location = self.config["region"]
                self.write_state()
                self.environment.approve = Mock(side_effect=lambda *a: change())
                with self.assertRaisesRegex(eph.Error, message):
                    self.cleanup()
                self.assertEqual(self.mutations(), [])


class PayloadCleanupTests(PayloadFixture):
    def test_logging_is_disabled_only_after_approval_and_before_bucket_mutations(self):
        self.environment.approve = Mock(side_effect=lambda *a: self.events.append(("approved",)))
        self.version_pages = [
            {"Versions": [{"Key": "sensitive", "VersionId": "v1"}], "IsTruncated": False},
            {"IsTruncated": False},
        ]
        check = self.environment.check_bedrock_logging
        self.environment.check_bedrock_logging = Mock(wraps=check)
        self.cleanup()
        self.assertEqual(self.environment.check_bedrock_logging.call_count, 3)
        approved = self.events.index(("approved",))
        disabled = next(i for i, e in enumerate(self.events)
                        if e[:2] == ("bedrock", "delete-model-invocation-logging-configuration"))
        deleted = next(i for i, e in enumerate(self.events) if e[:2] == ("s3api", "delete-objects"))
        self.assertLess(approved, disabled)
        self.assertLess(disabled, deleted)
        self.assertEqual(sum(e[:2] == ("s3api", "get-bucket-tagging") for e in self.events), 2)

    def test_absent_logging_is_not_deleted(self):
        self.logging = None
        self.cleanup()
        self.assertEqual(self.mutations(), [])

    def test_foreign_or_changed_logging_configuration_is_never_disabled(self):
        original = copy.deepcopy(self.logging)
        for change in (
            {"s3Config": {"bucketName": "foreign"}},
            {"textDataDeliveryEnabled": False},
            {"imageDataDeliveryEnabled": True},
            {"cloudWatchConfig": {"logGroupName": "foreign"}},
        ):
            for after_approval in (False, True):
                with self.subTest(change=change, after_approval=after_approval):
                    self.logging = copy.deepcopy(original)
                    if after_approval:
                        self.environment.approve = Mock(
                            side_effect=lambda *a: self.logging.update(change))
                    else:
                        self.logging.update(change)
                    with self.assertRaisesRegex(eph.Error, "differs from"):
                        self.cleanup()
                    self.assertEqual(self.mutations(), [])
        self.logging = original
        self.write_state(self.resources()[:1])
        with self.assertRaisesRegex(eph.Error, "not owned"):
            self.cleanup()
        self.assertEqual(self.mutations(), [])

    def test_versions_markers_and_uploads_drain_service_pages_using_stdin(self):
        secrets = ['private/model "prompt"\nname', "private-version-1",
                   "private-marker", "private-upload-key", "private-upload-id"]
        version = {"Key": secrets[0], "VersionId": secrets[1]}
        marker = {"Key": secrets[0], "VersionId": secrets[2]}
        self.version_pages = [
            {"Versions": [version], "IsTruncated": True, "NextKeyMarker": "ignore-me"},
            {"DeleteMarkers": [marker], "IsTruncated": True},
            {"IsTruncated": False},
        ]
        self.upload_pages = [
            {"Uploads": [{"Key": secrets[3], "UploadId": secrets[4]}], "IsTruncated": True},
            {"Uploads": [{"Key": "second-private-key", "UploadId": "second-private-id"}],
             "IsTruncated": False},
            {"IsTruncated": False},
        ]
        with redirect_stderr(io.StringIO()) as stderr:
            output = self.cleanup()
        deletes = [kw for cmd, kw in self.requests if "delete-objects" in cmd]
        self.assertEqual([json.loads(kw["input"]) for kw in deletes], [
            {"Objects": [version]}, {"Objects": [marker]},
        ])
        for kwargs in deletes:
            self.assertNotIn("Quiet", json.loads(kwargs["input"]))
        aborts = [kw for cmd, kw in self.requests if "abort-multipart-upload" in cmd]
        self.assertEqual([json.loads(kw["input"]) for kw in aborts], [
            {"Key": secrets[3], "UploadId": secrets[4]},
            {"Key": "second-private-key", "UploadId": "second-private-id"},
        ])
        listings = [e for e in self.events if e[:2] in {
            ("s3api", "list-object-versions"), ("s3api", "list-multipart-uploads"),
        }]
        self.assertEqual(len(listings), 6)
        abort_indices = [i for i, e in enumerate(self.events)
                         if e[:2] == ("s3api", "abort-multipart-upload")]
        delete_indices = [i for i, e in enumerate(self.events)
                          if e[:2] == ("s3api", "delete-objects")]
        self.assertLess(max(abort_indices), min(delete_indices))
        for event in listings:
            self.assertIn("--no-paginate", event)
            limit = "--max-keys" if event[1] == "list-object-versions" else "--max-uploads"
            self.assertEqual(event[event.index(limit) + 1], "1000")
            for forbidden in ("--starting-token", "--max-items", "--page-size",
                              "--key-marker", "--version-id-marker", "--upload-id-marker"):
                self.assertNotIn(forbidden, event)
        for command, kwargs in self.requests:
            if "delete-objects" in command:
                self.assertEqual(command[command.index("--delete") + 1], "file:///dev/stdin")
            if "abort-multipart-upload" in command:
                self.assertEqual(command[command.index("--cli-input-json") + 1], "file:///dev/stdin")
            if any(op in command for op in ("delete-objects", "abort-multipart-upload")):
                self.assertFalse(kwargs.get("log"))
            for secret in secrets:
                self.assertNotIn(secret, " ".join(command))
                self.assertNotIn(secret, output + stderr.getvalue())
        self.assertIn("removed 2 object versions/delete markers", output)
        self.assertEqual(self.version_pages, [])
        self.assertEqual(self.upload_pages, [])

    def test_response_errors_fail_without_retention_bypass_or_terraform_destroy(self):
        for code in ("AccessDenied", "InvalidRequest", "InternalError"):
            with self.subTest(code=code):
                self.version_pages = [{"Versions": [{"Key": "private", "VersionId": "held"}],
                                       "IsTruncated": False}]
                self.upload_pages = [{"IsTruncated": False}]
                self.delete_result = {"Errors": [{"Code": code, "Key": "private",
                                                  "Message": "private retention detail"}]}
                with self.assertRaisesRegex(eph.Error, "not bypassed") as caught:
                    self.destroy()
                self.assertNotIn("private", str(caught.exception))
                self.environment.plan_apply.assert_not_called()
                self.assertEqual(self.environment.meta["status"], "deployed")
                self.assertFalse(any(e[:2] == ("s3api", "abort-multipart-upload")
                                     for e in self.events))
                self.assertFalse(any("bypass" in str(e).lower() for e in self.events))

    def test_truncated_empty_service_pages_refuse_incomplete_cleanup(self):
        for phase in ("versions", "uploads"):
            with self.subTest(phase=phase):
                self.version_pages = [{"IsTruncated": True}]
                self.upload_pages = [{"IsTruncated": phase == "uploads"}]
                with self.assertRaisesRegex(eph.Error, "truncated without entries"):
                    self.destroy()
                self.environment.plan_apply.assert_not_called()

    def test_malformed_service_pages_are_not_assumed_empty(self):
        for field, identifier in (("Uploads", "UploadId"), ("Versions", "VersionId"),
                                  ("DeleteMarkers", "VersionId")):
            pages = [
                {}, None, [], {"IsTruncated": None}, {"IsTruncated": 0},
                {"IsTruncated": "false"},
                {"IsTruncated": False, field: None},
                {"IsTruncated": False, field: {}},
                {"IsTruncated": False, field: ["not-an-entry"]},
            ]
            for entry in (
                {}, {"Key": "private"}, {identifier: "private"},
                {"Key": "", identifier: "private"},
                {"Key": "private", identifier: ""},
                {"Key": 12, identifier: "private"},
                {"Key": "private", identifier: 12},
            ):
                pages.append({"IsTruncated": False, field: [entry]})
            for page in pages:
                with self.subTest(field=field, page=page):
                    self.version_pages = [page] if field != "Uploads" else [{"IsTruncated": False}]
                    self.upload_pages = [page] if field == "Uploads" else [{"IsTruncated": False}]
                    with self.assertRaisesRegex(eph.Error, "Malformed Bedrock"):
                        self.destroy()
                    self.environment.plan_apply.assert_not_called()
                    self.assertFalse(any(e[:2] in {
                        ("s3api", "delete-objects"), ("s3api", "abort-multipart-upload"),
                    } for e in self.events))

    def test_missing_or_incomplete_deleted_acknowledgements_block_destroy(self):
        entries = [{"Key": "private-a", "VersionId": "a"},
                   {"Key": "private-b", "VersionId": "b"}]
        for result in ({}, {"Deleted": []}, {"Deleted": entries[:1]}):
            with self.subTest(result=result):
                self.delete_result = result
                self.version_pages = [{"Versions": entries, "IsTruncated": False}]
                self.upload_pages = [{"IsTruncated": False}]
                with self.assertRaisesRegex(eph.Error, "refused or incomplete"):
                    self.destroy()
                self.environment.plan_apply.assert_not_called()

    def test_declining_payload_cleanup_has_no_mutations_or_terraform_destroy(self):
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value="no") as prompt, \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(eph.Error, "Approval declined"):
                self.environment.destroy(account=self.account)
        prompt.assert_called_once()
        self.assertIn("outside Boundary", output.getvalue())
        self.assertIn("Object retention and legal holds will NOT be bypassed", output.getvalue())
        self.assertEqual(self.mutations(), [])
        self.environment.plan_apply.assert_not_called()

    def test_initial_destroy_scope_warns_before_input_and_needs_no_extra_prompt(self):
        output = io.StringIO()

        def approve(prompt):
            initial = output.getvalue()
            for text in ("account/region Bedrock logging", "outside Boundary",
                         "model inputs/outputs", "Retention/holds will not be bypassed"):
                self.assertIn(text, initial)
            self.assertEqual(self.mutations(), [])
            return "example"

        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", side_effect=approve) as prompt, redirect_stdout(output):
            with self.environment.approval_scope("destroy"):
                self.environment.approve("An earlier owned-resource plan")
                self.environment.destroy(account=self.account)
                self.environment.approve("A later owned-resource plan")
        prompt.assert_called_once()
        self.environment.plan_apply.assert_called_once_with(
            "boundary-regional", destroy=True, yes=False, account=self.account)
        self.assertEqual(self.environment.meta["status"], "destroyed")
        self.assertIsNone(self.environment._operation_approval)

    def test_changed_identity_after_approval_prevents_all_mutations(self):
        self.environment.approve = Mock(
            side_effect=lambda *a: setattr(self, "identity_account", "999999999999"))
        with self.assertRaisesRegex(eph.Error, "identity does not match"):
            self.destroy()
        self.assertEqual(self.mutations(), [])
        self.environment.plan_apply.assert_not_called()

    def test_partial_destroy_retries_failed_root_with_its_applied_generation(self):
        env = self.environment
        env.meta["active_generation"] = "new-unapplied-generation"
        env.meta["roots"]["boundary-regional"]["generation"] = "previous-applied-generation"
        env.record_terraform_outputs = Mock()
        env.plan_apply = eph.Environment.plan_apply.__get__(env)
        attempts = []
        path = env.home / "boundary-regional.tfstate"

        def terraform(key, *args):
            self.assertEqual(key, "boundary-regional")
            self.assertEqual(env.generation, "previous-applied-generation")
            if args[0] == "plan":
                self.assertIn("-destroy", args)
                (env.home / f"{key}.plan").write_bytes(b"saved destroy plan")
            if args[:2] == ("show", "-json"):
                return json.dumps({"resource_changes": [{
                    "address": "aws_s3_bucket.bedrock_large_payloads", "type": "aws_s3_bucket",
                    "change": {"actions": ["delete"]},
                }]}).encode()
            if args[:2] == ("show", "-no-color"):
                return b"delete owned payload bucket"
            if args[0] == "apply":
                state = json.loads(path.read_text())
                state["serial"] += 1
                attempts.append(env.generation)
                if len(attempts) == 1:
                    path.write_text(json.dumps(state))
                    raise eph.Error("partial destroy")
                state["resources"] = []
                path.write_text(json.dumps(state))
            return b""

        env.tf = Mock(side_effect=terraform)
        with self.assertRaisesRegex(eph.Error, "partial destroy"):
            self.destroy()
        record = env.meta["roots"]["boundary-regional"]
        self.assertEqual(record["state_hash"], eph.digest(path.read_bytes()))
        self.assertIn("pending_plan", record)
        self.assertEqual(record["pending_generation"], "previous-applied-generation")
        self.assertEqual(env.meta["status"], "deployed")
        self.assertIsNone(env._generation)
        # The first attempt disabled logging and drained data, but left owned
        # Terraform state behind. Retry must neither adopt new state nor switch
        # to the newer, never-applied source generation.
        self.version_pages = [{"IsTruncated": False}]
        self.upload_pages = [{"IsTruncated": False}]
        self.destroy()
        self.assertEqual(attempts, ["previous-applied-generation"] * 2)
        self.assertEqual(sum(e[:2] == ("bedrock", "delete-model-invocation-logging-configuration")
                             for e in self.events), 1)
        self.assertTrue(record["destroyed"])
        self.assertNotIn("pending_plan", record)
        self.assertEqual(env.meta["status"], "destroyed")


class PayloadMutationGuardTests(Fixture):
    ACTIONS = (
        ("s3api", "delete-objects"),
        ("s3api", "abort-multipart-upload"),
        ("bedrock", "delete-model-invocation-logging-configuration"),
    )

    def test_each_new_mutation_guards_before_dispatch_in_both_output_modes(self):
        runner = Mock()
        env = self.env(runner)
        events = []
        env.guard_account = Mock(side_effect=lambda: events.append("guard"))
        runner.json.side_effect = lambda *a, **kw: events.append("json")
        runner.run.side_effect = lambda *a, **kw: events.append("run")
        for service, operation in self.ACTIONS:
            for json_output in (True, False):
                with self.subTest(operation=operation, json_output=json_output):
                    events.clear()
                    env.guard_account.reset_mock()
                    env.aws(service, operation, json_output=json_output, input=b"private")
                    self.assertEqual(events, ["guard", "json" if json_output else "run"])
                    env.guard_account.assert_called_once_with()
                    dispatch = runner.json if json_output else runner.run
                    self.assertEqual(dispatch.call_args.kwargs["input"], b"private")

    def test_identity_change_blocks_each_new_mutation_before_runner_dispatch(self):
        runner = Mock()
        env = self.env(runner)
        for service, operation in self.ACTIONS:
            for json_output in (True, False):
                for identity in (
                    {"Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/dev"},
                    {"Account": self.config["account_id"],
                     "Arn": f"arn:aws-us-gov:iam::{self.config['account_id']}:user/dev"},
                ):
                    with self.subTest(operation=operation, json_output=json_output, identity=identity):
                        runner.reset_mock()
                        runner.json.return_value = identity
                        with self.assertRaises(eph.Error):
                            env.aws(service, operation, json_output=json_output)
                        runner.json.assert_called_once()
                        self.assertIn("get-caller-identity", runner.json.call_args.args[0])
                        runner.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
