"""Command-response diagnostics must identify operations without disclosing data."""

import os
import unittest
from unittest.mock import Mock, patch

from ephemeral_environment.common import Error
from ephemeral_environment.runtime import CommandError, Runner
from test_ephemeral_env import Fixture


class JSONDiagnosticsTests(unittest.TestCase):
    def test_explicit_empty_response_allowance_is_not_forwarded_to_subprocess(self):
        for output in (b"", b" \n"):
            with self.subTest(output=output):
                runner = Runner()
                runner.run = Mock(return_value=output)
                self.assertEqual(runner.json(["cli"], allow_empty_object=True), {})
                runner.run.assert_called_once_with(["cli"])

    def test_empty_response_allowance_does_not_hide_errors_or_malformed_json(self):
        runner = Runner()
        runner.run = Mock(side_effect=CommandError("synthetic failure"))
        with self.assertRaisesRegex(CommandError, "synthetic failure"):
            runner.json(["cli"], allow_empty_object=True)
        runner.run = Mock(return_value=b"not JSON")
        with self.assertRaisesRegex(Error, "returned invalid JSON"):
            runner.json(["cli"], allow_empty_object=True)

    def test_empty_and_malformed_responses_identify_command_without_response(self):
        for output in (b"", b" \n", b"synthetic-sensitive-response", b"\xff"):
            with self.subTest(output=output):
                runner = Runner()
                runner.run = Mock(return_value=output)
                with self.assertRaises(Error) as caught:
                    runner.json(
                        ["vault", "read", "-format=json", "private/vault/reference"],
                        label="vault read",
                    )
                message = str(caught.exception)
                self.assertIn("vault read returned", message)
                self.assertIn("empty output" if not output.strip() else "invalid JSON", message)
                self.assertIn("Response contents withheld", message)
                self.assertNotIn("synthetic-sensitive-response", message)
                self.assertNotIn("private/vault/reference", message)

    def test_valid_json_is_preserved_and_label_not_forwarded_to_subprocess(self):
        runner = Runner()
        runner.run = Mock(return_value=b'{"ok": true}')
        self.assertEqual(runner.json(["cli"], label="safe operation", build=True), {"ok": True})
        runner.run.assert_called_once_with(["cli"], build=True)

    def test_default_label_does_not_include_arguments(self):
        runner = Runner()
        runner.run = Mock(return_value=b"not JSON")
        with self.assertRaisesRegex(Error, "vault returned invalid JSON") as caught:
            runner.json(["vault", "read", "private/reference"])
        self.assertNotIn("private/reference", str(caught.exception))


class CloudJSONLabelsTests(Fixture):
    def test_vault_null_data_reports_versioned_kv_path_guidance_without_warning_contents(self):
        env = self.env()
        env.runner.run = Mock(return_value=(
            b'{"data":null,"warnings":["Invalid path for a versioned K/V secrets engine: '
            b'private-vault-path synthetic-sensitive-value"]}'
        ))
        with self.assertRaises(Error) as caught:
            env.vault_credentials()
        message = str(caught.exception)
        self.assertIn("Vault read returned no secret data", message)
        self.assertIn("invalid path for a versioned KV", message)
        self.assertIn("<mount>/data/<secret-path>", message)
        self.assertIn("make ephemeral-env-reset", message)
        self.assertNotIn("private-vault-path", message)
        self.assertNotIn("synthetic-sensitive-value", message)

    def test_vault_unexpected_response_shapes_fail_without_traceback(self):
        env = self.env()
        for output in (b"null", b"[]", b"{}", b'{"data":null}', b'{"data":[]}',
                       b'{"data":"synthetic-sensitive-value"}'):
            with self.subTest(output=output):
                env.runner.run = Mock(return_value=output)
                with self.assertRaises(Error) as caught:
                    env.vault_credentials()
                self.assertNotIn("synthetic-sensitive-value", str(caught.exception))

    def test_vault_read_failure_includes_safe_login_and_retry_instructions(self):
        env = self.env()
        env.runner.run = Mock(side_effect=CommandError(
            "synthetic failure", b"private/vault/reference synthetic-sensitive-token",
        ))
        with self.assertRaises(Error) as caught:
            env.vault_credentials()
        message = str(caught.exception)
        self.assertIn("Vault could not read the configured secret", message)
        self.assertIn(f"export VAULT_ADDR={self.config['vault']['address']}", message)
        self.assertIn("vault login -no-print -method=oidc", message)
        self.assertIn("vault login -no-print\n", message)
        self.assertIn("hidden prompt", message)
        self.assertIn("unset VAULT_TOKEN", message)
        self.assertIn("vault token lookup -format=json >/dev/null", message)
        self.assertIn("retry make ephemeral-env", message)
        self.assertIn("no reset is needed", message)
        self.assertIn("policy access", message)
        self.assertIn("#log-in-to-vault", message)
        self.assertNotIn("synthetic-sensitive-token", message)
        self.assertNotIn("private/vault/reference", message)
        self.assertNotIn(self.config["vault"]["path"], message)

    def test_vault_login_guidance_does_not_expose_exported_token(self):
        env = self.env()
        env.runner.run = Mock(side_effect=CommandError("synthetic failure"))
        with patch.dict(os.environ, {"VAULT_TOKEN": "synthetic-private-exported-token"}):
            with self.assertRaises(Error) as caught:
                env.vault_credentials()
        self.assertIn("overrides the CLI's saved login", str(caught.exception))
        self.assertNotIn("synthetic-private-exported-token", str(caught.exception))

    def test_empty_bedrock_logging_response_means_no_existing_configuration(self):
        env = self.env()
        env.state_resource = Mock(side_effect=AssertionError("No existing config to match"))
        for output in (b"", b"\n", b"{}"):
            with self.subTest(output=output):
                env.runner.run = Mock(return_value=output)
                env.check_bedrock_logging()
        env.state_resource.assert_not_called()

    def test_other_aws_and_vault_reads_still_reject_empty_responses(self):
        env = self.env()
        env.runner.run = Mock(return_value=b"")
        for service, operation in (
            ("sts", "get-caller-identity"), ("iam", "list-open-id-connect-providers"),
            ("bedrock", "list-foundation-models"),
        ):
            with self.subTest(service=service, operation=operation):
                with self.assertRaisesRegex(Error, "returned empty output"):
                    env.aws(service, operation)
        with self.assertRaisesRegex(Error, "vault read returned empty output"):
            env.vault_credentials()

    def test_bedrock_logging_errors_and_existing_ownership_guard_are_preserved(self):
        env = self.env()
        env.runner.run = Mock(side_effect=CommandError("synthetic access denied"))
        with self.assertRaisesRegex(CommandError, "synthetic access denied"):
            env.check_bedrock_logging()
        env.runner.run = Mock(return_value=b"not JSON")
        with self.assertRaisesRegex(Error, "returned invalid JSON"):
            env.check_bedrock_logging()
        env.runner.run = Mock(return_value=b'{"loggingConfig":{"textDataDeliveryEnabled":true}}')
        env.state_resource = Mock(side_effect=Error("not owned"))
        with self.assertRaisesRegex(Error, "not owned by this environment"):
            env.check_bedrock_logging()

    def test_aws_response_error_identifies_service_and_operation_only(self):
        env = self.env()
        env.runner.run = Mock(return_value=b"not JSON")
        with self.assertRaisesRegex(Error, "aws sts get-caller-identity returned invalid JSON") as caught:
            env.guard_account()
        message = str(caught.exception)
        self.assertNotIn(self.config["profile"], message)
        self.assertNotIn(self.config["region"], message)

    def test_vault_response_error_does_not_disclose_path(self):
        env = self.env()
        env.runner.run = Mock(return_value=b"synthetic-sensitive-response")
        with self.assertRaisesRegex(Error, "vault read returned invalid JSON") as caught:
            env.vault_credentials()
        self.assertNotIn(self.config["vault"]["path"], str(caught.exception))
        self.assertNotIn("synthetic-sensitive-response", str(caught.exception))
