"""Offline invocation-wide approval tests; no AWS or external tools are run."""

import io
import os
from pathlib import Path
from unittest.mock import Mock, patch

from test_ephemeral_env import Fixture, cli, eph


class ApprovalFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.enterContext(patch("sys.stdout", self.stdout))
        self.enterContext(patch("sys.stderr", self.stderr))
        self.terminal = self.enterContext(patch(
            "ephemeral_environment.cloud.sys.stdin.isatty", return_value=True))
        self.answer = self.enterContext(patch("builtins.input", return_value="example"))
        eph.private_directory(Path(self.config["state_dir"]))
        self.environment = self.env()
        # Fail loudly if a test accidentally reaches any real cloud/tool path.
        self.environment.aws = Mock(side_effect=AssertionError("AWS is forbidden"))
        self.environment.runner.run = Mock(side_effect=AssertionError("Tools are forbidden"))
        self.environment.runner.json = Mock(side_effect=AssertionError("Tools are forbidden"))

    def tearDown(self):
        self.environment.aws.assert_not_called()
        self.environment.runner.run.assert_not_called()
        self.environment.runner.json.assert_not_called()
        super().tearDown()

    def assert_descriptions(self, descriptions, output=None):
        lines = (self.stdout.getvalue() if output is None else output).splitlines()
        positions = []
        for description in descriptions:
            self.assertEqual(lines.count(description), 1, description)
            positions.append(lines.index(description))
        self.assertEqual(positions, sorted(positions))


class ApprovalScopeTests(ApprovalFixture):
    def test_destroy_approval_separates_scope_warnings_and_details(self):
        env = self.environment
        with env.approval_scope("destroy"):
            env.approve("Bedrock payload cleanup\n  Bucket: synthetic-owned-bucket")
            env.approve("Terraform destroy: boundary-regional\n  Delete: 1")
        output = self.stdout.getvalue()
        self.assertIn("Destroy environment: example\nAWS: 123456789012/us-west-2\n\nDelete\n", output)
        self.assertIn("\n\nWarnings\n", output)
        self.assertIn("\n\nBedrock payload cleanup\n  Bucket:", output)
        self.assertIn("\n\nTerraform destroy: boundary-regional\n  Delete: 1", output)
        self.assertEqual(output.count("\nWarnings\n"), 1)
        self.assertIn("outside Boundary", output)
        self.assertIn("Retention/holds will not be bypassed", output)
        self.assertLess(len(output.split()), 140)
        self.assertTrue(all(len(line) <= 88 for line in output.splitlines()))
        self.answer.assert_called_once()

    def test_multiple_approvals_prompt_once_and_display_every_plan(self):
        env = self.environment
        descriptions = ["Clean up owned sessions"] + [
            f"Reviewed plan for {root}" for root in eph.ROOTS
        ] + ["Delete owned ECR repository boundary", "Delete owned ECR repository ta"]
        with env.approval_scope("destroy"):
            for description in descriptions:
                env.approve(description)
        self.answer.assert_called_once_with("Type example to approve: ")
        self.assert_descriptions(descriptions)
        output = self.stdout.getvalue()
        self.assertEqual(output.count("Destroy environment: example"), 1)
        self.assertIn("123456789012/us-west-2", output)
        self.assertIn("Workspace and dev audit data will be lost", output)
        self.assertIsNone(env._operation_approval)

    def test_decline_aborts_subsequent_actions_without_caching_approval(self):
        env = self.environment
        later_action = Mock()
        self.answer.side_effect = ["no", env.name]
        with env.approval_scope("destroy"):
            with self.assertRaisesRegex(eph.Error, "Approval declined"):
                env.approve("First plan")
                later_action()
            self.assertFalse(env._operation_approval["approved"])
            env.approve("Retry first plan")
            self.assertTrue(env._operation_approval["approved"])
        later_action.assert_not_called()
        self.assertEqual(self.answer.call_count, 2)
        self.assertIsNone(env._operation_approval)

    def test_success_and_exception_both_require_fresh_approval_next_scope(self):
        env = self.environment
        for fails in (False, True):
            with self.subTest(fails=fails):
                self.answer.reset_mock()
                try:
                    with env.approval_scope("deploy"):
                        env.approve("First invocation")
                        if fails:
                            raise eph.Error("Synthetic operation failure")
                except eph.Error as error:
                    self.assertEqual(str(error), "Synthetic operation failure")
                self.assertIsNone(env._operation_approval)
                with env.approval_scope("deploy"):
                    env.approve("Next invocation")
                self.assertEqual(self.answer.call_count, 2)
                self.assertIsNone(env._operation_approval)

    def test_changed_name_account_or_region_rejected_before_or_after_approval(self):
        env = self.environment
        for field in ("name", "account_id", "region"):
            for approved in (False, True):
                with self.subTest(field=field, approved=approved):
                    target = env.__dict__ if field == "name" else env.config
                    original = target[field]
                    self.answer.reset_mock()
                    with env.approval_scope("destroy"):
                        if approved:
                            env.approve("Original target")
                        target[field] = "changed"
                        try:
                            with self.assertRaisesRegex(eph.Error, "Operation target changed"):
                                env.approve("Must not authorize changed target", yes=True)
                        finally:
                            target[field] = original
                    self.assertEqual(self.answer.call_count, int(approved))
                    self.assertIsNone(env._operation_approval)
        self.assertNotIn("Must not authorize changed target", self.stdout.getvalue())

    def test_yes_never_reads_terminal_input_and_still_displays_plans(self):
        env = self.environment
        self.terminal.return_value = False
        with env.approval_scope("deploy"):
            env.approve("First noninteractive plan", yes=True)
            env.approve("Second noninteractive plan", yes=True)
        self.answer.assert_not_called()
        self.assert_descriptions(["First noninteractive plan", "Second noninteractive plan"])

    def test_missing_terminal_fails_closed_without_yes(self):
        env = self.environment
        self.terminal.return_value = False
        later_action = Mock()
        with self.assertRaisesRegex(eph.Error, "Approval declined"):
            with env.approval_scope("deploy"):
                env.approve("Noninteractive plan")
                later_action()
        self.answer.assert_not_called()
        later_action.assert_not_called()
        self.assertIsNone(env._operation_approval)

    def test_nested_scope_cannot_replace_outer_approval(self):
        env = self.environment
        with env.approval_scope("deploy"):
            env.approve("Outer first plan")
            with self.assertRaisesRegex(eph.Error, "scope is already active"):
                with env.approval_scope("destroy"):
                    self.fail("Nested scope must not run")
            env.approve("Outer second plan")
        self.answer.assert_called_once()
        self.assertIsNone(env._operation_approval)


class CLIApprovalTests(ApprovalFixture):
    def setUp(self):
        super().setUp()
        env = self.environment
        env.meta = {"status": "created", "source": {}, "images": {}}
        self.config_path = self.config_file()
        self.enterContext(patch.object(cli, "Environment", return_value=env))
        self.enterContext(patch.object(cli, "load_config", return_value=self.config))
        self.enterContext(patch.object(cli.os, "umask"))
        for method in ("create", "refresh_source", "deploy", "destroy", "recover", "reset", "load"):
            self.enterContext(patch.object(env, method))

    def invoke(self, action, *extra):
        result = cli.main([
            action, "--config", str(self.config_path), "--name", self.environment.name, *extra,
        ])
        self.assertIsNone(self.environment._operation_approval)
        self.assertIsNone(self.environment.runner.lock_fd)
        return result

    def assert_command_scope(self, action):
        env = self.environment
        self.assertEqual(env._operation_approval["action"], action)
        self.assertIsNotNone(env.runner.lock_fd)
        self.assertEqual(
            os.fstat(env.runner.lock_fd).st_ino, (env.home / "lock").stat().st_ino)

    def approval_steps(self, action, descriptions, completed, yes=False):
        self.assert_command_scope(action)
        for description in descriptions:
            self.environment.approve(description, yes=yes)
            completed.append(description)

    def test_deploy_refresh_and_all_roots_ecr_share_one_approval_per_invocation(self):
        env = self.environment
        (env.home / "environment.json").write_text("{}")
        refresh = "Replace owned deployment source generation"
        plans = [f"Apply reviewed plan for {root}" for root in eph.ROOTS]
        publications = ["Publish owned ECR boundary image", "Publish owned ECR ta image"]
        completed = []
        env.refresh_source.side_effect = lambda commit, yes, account: self.approval_steps(
            "deploy", [refresh], completed, yes)
        env.deploy.side_effect = lambda yes, account: self.approval_steps(
            "deploy", publications + plans, completed, yes)
        for invocation in range(2):
            with self.subTest(invocation=invocation):
                self.stdout.seek(0)
                self.stdout.truncate()
                self.answer.reset_mock()
                completed.clear()
                self.assertEqual(self.invoke("deploy"), 0)
                self.answer.assert_called_once_with("Type example to approve: ")
                self.assertEqual(completed, [refresh] + publications + plans)
                self.assert_descriptions(completed)
        self.assertEqual(env.refresh_source.call_count, 2)
        self.assertEqual(env.deploy.call_count, 2)

    def test_destroy_cleanup_all_roots_and_ecr_share_one_approval(self):
        env = self.environment
        descriptions = ["Stop owned Boundary sessions and remove investigation resources"] + [
            f"Destroy reviewed plan for {root}" for root in reversed(eph.ROOTS)
        ] + ["Delete owned ECR repository boundary", "Delete owned ECR repository ta"]
        completed = []
        env.destroy.side_effect = lambda yes, account: self.approval_steps(
            "destroy", descriptions, completed, yes)
        self.assertEqual(self.invoke("destroy"), 0)
        self.answer.assert_called_once_with("Type example to approve: ")
        self.assertEqual(completed, descriptions)
        self.assert_descriptions(descriptions)

    def test_every_command_runs_inside_real_lock_and_its_own_approval_scope(self):
        env = self.environment
        for action in ("create", "deploy", "destroy", "recover", "reset", "status"):
            with self.subTest(action=action):
                self.answer.reset_mock()
                method = getattr(env, "load" if action == "status" else action)
                completed = []

                def operation(*args, **kwargs):
                    self.approval_steps(action, [f"{action} first", f"{action} second"], completed)
                    return {"name": env.name, "status": "reset"} if action == "reset" else None

                method.side_effect = operation
                self.assertEqual(self.invoke(action), 0)
                self.answer.assert_called_once_with("Type example to approve: ")
                self.assertEqual(completed, [f"{action} first", f"{action} second"])
                method.side_effect = None

    def test_declined_refresh_does_not_reach_deploy_or_cache_next_invocation(self):
        env = self.environment
        (env.home / "environment.json").write_text("{}")
        completed = []
        env.refresh_source.side_effect = lambda commit, yes, account: self.approval_steps(
            "deploy", ["Approve source refresh"], completed, yes)
        env.deploy.side_effect = lambda yes, account: self.approval_steps(
            "deploy", ["Apply subsequent plan"], completed, yes)
        self.answer.return_value = "no"
        self.assertEqual(self.invoke("deploy"), 1)
        self.assertEqual(completed, [])
        env.deploy.assert_not_called()
        self.assertIn("Approval declined", self.stderr.getvalue())
        self.assertNotIn("Apply subsequent plan", self.stdout.getvalue())
        self.answer.return_value = env.name
        self.assertEqual(self.invoke("deploy"), 0)
        self.assertEqual(self.answer.call_count, 2)
        self.assertEqual(completed, ["Approve source refresh", "Apply subsequent plan"])

    def test_failed_command_does_not_approve_retry_or_another_command(self):
        env = self.environment
        completed = []

        def fail_after_approval(yes, account):
            self.approval_steps("destroy", ["Cleanup before failure"], completed, yes)
            raise eph.Error("Synthetic cleanup failure")

        env.destroy.side_effect = fail_after_approval
        self.assertEqual(self.invoke("destroy"), 1)
        self.assertIn("Synthetic cleanup failure", self.stderr.getvalue())
        env.destroy.side_effect = lambda yes, account: self.approval_steps(
            "destroy", ["Retry cleanup", "Retry plan"], completed, yes)
        self.assertEqual(self.invoke("destroy"), 0)
        env.recover.side_effect = lambda yes, account: self.approval_steps(
            "recover", ["Reconcile interrupted ECR publication"], completed, yes)
        self.assertEqual(self.invoke("recover"), 0)
        self.assertEqual(self.answer.call_count, 3)

    def test_cli_yes_is_forwarded_without_input_and_missing_terminal_fails(self):
        env = self.environment
        completed = []
        descriptions = ["Cleanup sessions", "Destroy root plan", "Delete ECR repository"]
        env.destroy.side_effect = lambda yes, account: self.approval_steps(
            "destroy", descriptions, completed, yes)
        self.terminal.return_value = False
        self.assertEqual(self.invoke("destroy"), 1)
        self.assertEqual(completed, [])
        self.answer.assert_not_called()
        self.assertEqual(self.invoke(
            "destroy", "--yes", "--account-id", self.config["account_id"]), 0)
        env.destroy.assert_called_with(True, self.config["account_id"])
        self.assertEqual(completed, descriptions)
        self.answer.assert_not_called()
