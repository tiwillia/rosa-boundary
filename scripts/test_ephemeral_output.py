"""Offline CLI presentation tests using synthetic metadata and no cloud clients."""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ephemeral_environment import cli
from ephemeral_environment.common import Error, ROOTS
from ephemeral_environment.output import environment_payload, summary


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.config = {"account_id": "123456789012", "region": "us-west-2"}
        self.meta = {
            "status": "deployed",
            "source": {
                "selection": "working-tree", "commit": "a" * 40,
                "fingerprint": "b" * 64, "files": 321,
            },
            "images": {"boundary": {"uri": "image-details-not-for-summary"}},
            "outputs": {"boundary": {"nested": "output-details-not-for-summary"}},
            "connection": {
                "cli_command": "rosa-boundary --profile example",
                "trusted_actions_url": "https://ta.example.invalid",
                "client_environment": {"UNEXPECTED_SECRET": "synthetic-secret-marker"},
            },
            "verification": {"details": "verification-details-not-for-summary"},
            "roots": {key: {"touched": True, "state_hash": "c" * 64} for key in ROOTS},
            "active_generation": "d" * 32,
            "generations": {"d" * 32: {}},
            "source_requests": [{"selection": "working-tree", "generation": "d" * 32}],
            "outputs_generation": "d" * 32,
        }
        self.home = Path("/synthetic/environments/example")

    def payload(self):
        return environment_payload("example", self.config, self.home, self.meta)

    def invoke(self, action, *, json_output=False, failure=None, existing=False, reset=None):
        with tempfile.TemporaryDirectory() as temporary:
            env = Mock()
            env.home = Path(temporary) / "example"
            if existing:
                env.home.mkdir()
                (env.home / "environment.json").write_text("{}")
            env.meta = copy.deepcopy(self.meta)
            env.locked.side_effect = contextlib.nullcontext
            env.approval_scope.side_effect = lambda action: contextlib.nullcontext()
            env.reset.return_value = reset or {
                "name": "example", "status": "reset", "archive": "/synthetic/archive/example",
                "aws_operations": "none", "next_step": "make ephemeral-env",
            }
            if failure:
                getattr(env, action if action != "status" else "load").side_effect = failure
            stdout, stderr = io.StringIO(), io.StringIO()
            argv = [action, "--config", str(Path(temporary) / "config.json"), "--name", "example"]
            if json_output:
                argv.append("--json")
            with patch.object(cli, "Environment", return_value=env), \
                    patch.object(cli, "load_config", return_value=self.config), \
                    patch.object(cli.os, "umask"), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(argv)
            return code, stdout.getvalue(), stderr.getvalue(), env

    def test_all_actions_default_to_compact_human_output(self):
        for action in ("create", "deploy", "destroy", "status", "recover", "reset"):
            with self.subTest(action=action):
                code, text, error, env = self.invoke(action)
                self.assertEqual((code, error), (0, ""))
                self.assertTrue(text.startswith(f"Ephemeral environment: example ({action})\n"))
                self.assertLessEqual(len(text.splitlines()), 16)
                self.assertNotIn('"images"', text)
                self.assertNotIn("synthetic-secret-marker", text)
                if action != "reset":
                    self.assertIn("\n\nRoots (recorded)\n", text)
                    for root in ROOTS:
                        self.assertIn(f"\n  {root}: applied", text)
                    self.assertIn("Target: 123456789012 / us-west-2", text)
                    self.assertIn("Recorded status (not live): deployed", text)
                    self.assertIn(f"State directory: {env.home}", text)
                    self.assertIn("fingerprint bbbbbbbbbbbb", text)
                    self.assertNotIn("b" * 64, text)
                    self.assertNotIn("image-details", text)
                    self.assertNotIn("output-details", text)
                    self.assertNotIn("verification-details", text)

    def test_json_preserves_exact_existing_payload_schema_for_all_nonreset_actions(self):
        for action in ("create", "deploy", "destroy", "status", "recover"):
            with self.subTest(action=action):
                code, text, error, env = self.invoke(action, json_output=True)
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(json.loads(text), {
                    "name": "example", "status": self.meta["status"],
                    "account_id": "123456789012", "region": "us-west-2",
                    "source": self.meta["source"], "state_dir": str(env.home),
                    "images": self.meta["images"], "outputs": self.meta["outputs"],
                    "connection": self.meta["connection"],
                    "verification": self.meta["verification"], "roots": self.meta["roots"],
                    "active_generation": self.meta["active_generation"], "generation_count": 1,
                    "source_request": self.meta["source_requests"][-1],
                    "outputs_generation": self.meta["outputs_generation"],
                    "status_is_recorded_not_live": True,
                })

    def test_json_retains_defaults_for_initial_metadata(self):
        self.meta = {
            "status": "created", "source": self.meta["source"], "images": {},
        }
        payload = self.payload()
        for key in ("outputs", "connection", "verification", "roots", "source_request"):
            self.assertEqual(payload[key], {})
        self.assertEqual(payload["generation_count"], 0)
        self.assertIsNone(payload["active_generation"])
        self.assertIsNone(payload["outputs_generation"])
        self.assertIn("Recorded status (not live): created", summary("create", payload))

    def test_reset_json_is_unchanged_including_absent_result(self):
        for result in (
            {"name": "example", "status": "reset", "archive": "/synthetic/archive/example",
             "aws_operations": "none", "next_step": "make ephemeral-env"},
            {"name": "example", "status": "absent", "aws_operations": "none"},
        ):
            with self.subTest(status=result["status"]):
                code, text, error, _ = self.invoke("reset", json_output=True, reset=result)
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(json.loads(text), result)

    def test_reset_shows_archive_no_cloud_changes_and_next_step(self):
        _, text, _, env = self.invoke("reset")
        self.assertIn("Archived local path: /synthetic/archive/example", text)
        self.assertIn("AWS: no changes; no cloud checks", text)
        self.assertIn("Next: make ephemeral-env", text)
        env.load.assert_not_called()
        env.deploy.assert_not_called()
        env.destroy.assert_not_called()
        _, text, _, _ = self.invoke("reset", reset={
            "name": "example", "status": "absent", "aws_operations": "none",
        })
        self.assertIn("Local record: absent", text)
        self.assertIn("Next: make ephemeral-env", text)
        self.assertNotIn("Archived local path:", text)

    def test_status_is_cached_and_never_calls_cloud_operations(self):
        _, text, _, env = self.invoke("status")
        self.assertIn("Recorded status (not live)", text)
        self.assertIn("Cloud checks: none; cached local record only", text)
        env.load.assert_called_once_with()
        self.assertEqual([call[0] for call in env.method_calls],
                         ["locked", "approval_scope", "load"])

    def test_successful_deploy_shows_only_useful_connection_details(self):
        _, text, _, _ = self.invoke("deploy", existing=True)
        self.assertIn("CLI: rosa-boundary --profile example", text)
        self.assertIn("Trusted Actions (private, VPC only): https://ta.example.invalid", text)
        self.meta["connection"] = {}
        _, text, _, _ = self.invoke("deploy")
        self.assertNotIn("CLI:", text)
        self.assertNotIn("Trusted Actions (", text)

    def test_root_status_precedence_and_partial_destruction(self):
        keys = list(ROOTS)
        self.meta["status"] = "destroying"
        self.meta["roots"] = {
            keys[0]: {"touched": True, "destroyed": True, "pending_plan": "pending"},
            keys[1]: {"touched": True, "destroyed": True},
            keys[2]: {"touched": True},
            keys[3]: {"touched": False},
            keys[4]: {},
        }
        text = summary("status", self.payload())
        for key, expected in zip(keys, ("interrupted", "destroyed", "applied",
                                       "not applied", "not applied")):
            self.assertIn(f"{key}: {expected}", text)
        self.assertIn("Recorded status (not live): destroy interrupted", text)
        self.assertIn("retry or use recover", text)

    def test_pending_plan_never_presents_deployed_as_current_success(self):
        for status, display in (
            ("deployed", "interrupted operation"),
            ("deploying", "deploy interrupted"),
            ("destroying", "destroy interrupted"),
            ("source-prepared", "interrupted operation"),
        ):
            with self.subTest(status=status):
                self.meta["status"] = status
                self.meta["roots"][next(iter(ROOTS))]["pending_plan"] = "pending"
                payload = self.payload()
                text = summary("deploy", payload)
                self.assertIn(f"Recorded status (not live): {display}", text)
                self.assertNotIn("CLI:", text)
                self.assertNotIn("Trusted Actions (", text)
                self.assertEqual(payload["status"], status)
                self.assertEqual(self.meta["status"], status)

    def test_incomplete_operations_without_pending_plan_are_not_successes(self):
        for status in ("deploying", "destroying"):
            self.meta["status"] = status
            text = summary("status", self.payload())
            self.assertIn(f"{status[:-3]} incomplete", text)
            self.assertNotIn("CLI:", text)

    def test_source_uses_fingerprint_not_only_commit_or_arbitrary_selection(self):
        original = summary("status", self.payload())
        self.meta["source"]["fingerprint"] = "e" * 64
        changed = summary("status", self.payload())
        self.assertNotEqual(original, changed)
        self.assertIn("working tree aaaaaaaaaaaa", changed)
        self.meta["source"]["selection"] = "arbitrary-private-selection"
        changed = summary("status", self.payload())
        self.assertIn("Source: commit aaaaaaaaaaaa; fingerprint eeeeeeeeeeee", changed)
        self.assertNotIn("arbitrary-private-selection", changed)

    def test_destroyed_summary_does_not_recommend_old_connection(self):
        self.meta["status"] = "destroyed"
        for root in self.meta["roots"].values():
            root["destroyed"] = True
        _, text, _, _ = self.invoke("destroy")
        self.assertIn("Recorded status (not live): destroyed", text)
        self.assertEqual(text.count(": destroyed"), len(ROOTS) + 1)
        self.assertNotIn("CLI:", text)

    def test_failure_retains_nonzero_exit_and_error_without_success_summary(self):
        for action in ("create", "deploy", "destroy", "status", "recover", "reset"):
            for json_output in (False, True):
                with self.subTest(action=action, json=json_output):
                    code, text, error, _ = self.invoke(
                        action, failure=Error("synthetic operation failed"),
                        json_output=json_output,
                    )
                    self.assertEqual(code, 1)
                    self.assertEqual(text, "")
                    self.assertEqual(error, "ephemeral-env: synthetic operation failed\n")


if __name__ == "__main__":
    unittest.main()
