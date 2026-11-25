"""Offline consumer-cleanup and destroy regressions; every AWS call is mocked."""

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from ephemeral_environment import cleanup
from ephemeral_environment.runtime import CommandError
from test_ephemeral_env import Fixture, eph


class CleanupFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.events = []
        self.runner = Mock()
        self.runner.json.side_effect = self.command
        self.runner.run.side_effect = self.command
        self.environment = self.env(self.runner)
        self.environment.meta = {
            "owner": "synthetic-owner", "roots": {}, "status": "deployed",
            "active_generation": None,
        }
        self.environment.load = Mock()
        self.environment.save = Mock()
        self.environment.check_work = Mock()
        self.environment.plan_apply = Mock(
            side_effect=lambda key, **kwargs: self.events.append(("destroy", key)))
        self.prefix = "arn:aws:ecs:us-west-2:123456789012:"
        self.cluster = "eph-example"
        self.cluster_arn = self.prefix + "cluster/" + self.cluster
        self.filesystem = "fs-owned"
        self.managed_point = "fsap-terraform"
        self.point = "fsap-investigation"
        self.definition = self.prefix + "task-definition/eph-example-user-20261124T120000:1"
        self.account = self.config["account_id"]
        self.identity_account = self.account
        self.cluster_owner = self.filesystem_owner = "synthetic-owner"
        self.tasks = [self.task("one")]
        self.points = [self.access_point(self.managed_point), self.access_point(self.point)]
        self.definitions = [self.definition]
        self.efs_mounts = [{"efsVolumeConfiguration": {"fileSystemId": self.filesystem}}]
        self.write_state()

    def task(self, identifier, stopping=False):
        return {
            "taskArn": self.prefix + "task/" + self.cluster + "/" + identifier,
            "clusterArn": self.cluster_arn,
            "lastStatus": "STOPPING" if stopping else "RUNNING",
            "desiredStatus": "STOPPED" if stopping else "RUNNING",
            "tags": [{"key": "investigation_id", "value": "investigation-one"}],
        }

    def access_point(self, identifier, deleting=False):
        return {
            "AccessPointId": identifier, "FileSystemId": self.filesystem,
            "LifeCycleState": "deleting" if deleting else "available",
            "Tags": [{"Key": "ClusterID", "Value": "cluster-one"},
                     {"Key": "InvestigationID", "Value": "investigation-one"}],
        }

    def write_state(self):
        resources = [
            ("aws_ecs_cluster", {"name": self.cluster}),
            ("aws_efs_file_system", {"id": self.filesystem}),
            ("aws_efs_access_point", {"id": self.managed_point}),
            ("aws_ecs_task_definition", {"family": "eph-example"}),
        ]
        path = self.environment.home / "boundary-regional.tfstate"
        path.write_text(json.dumps({"resources": [
            {"mode": "managed", "type": kind, "instances": [{"attributes": attributes}]}
            for kind, attributes in resources
        ]}))
        self.environment.meta["roots"]["boundary-regional"] = {
            "state_hash": eph.digest(path.read_bytes()),
        }

    def command(self, command, **kwargs):
        """Dispatch the real Cloud.aws wrapper to an in-memory AWS model."""
        self.assertEqual(command[0], "aws", "No external commands are permitted")
        args = command[5:]
        if args[-2:] == ["--output", "json"]:
            args = args[:-2]
        self.events.append(tuple(args))
        service, operation, *options = args
        if (service, operation) == ("sts", "get-caller-identity"):
            return {"Account": self.identity_account,
                    "Arn": f"arn:aws:iam::{self.identity_account}:user/dev"}
        if (service, operation) == ("ecs", "describe-clusters"):
            return {"clusters": [{"clusterArn": self.cluster_arn, "tags": [
                {"key": "EphemeralOwner", "value": self.cluster_owner}]}]}
        if (service, operation) == ("ecs", "list-tasks"):
            desired = options[options.index("--desired-status") + 1]
            return {"taskArns": [t["taskArn"] for t in self.tasks
                                 if t["desiredStatus"] == desired]}
        if (service, operation) == ("ecs", "describe-tasks"):
            requested = options[options.index("--tasks") + 1:options.index("--include")]
            self.assertLessEqual(len(requested), 100)
            return {"tasks": [t for t in self.tasks if t["taskArn"] in requested]}
        if (service, operation) == ("efs", "describe-file-systems"):
            return {"FileSystems": [{
                "FileSystemId": self.filesystem, "OwnerId": self.account,
                "Tags": [{"Key": "EphemeralOwner", "Value": self.filesystem_owner}],
            }]}
        if (service, operation) == ("efs", "describe-access-points"):
            return {"AccessPoints": copy.deepcopy(self.points)}
        if (service, operation) == ("ecs", "list-task-definitions"):
            definitions = self.definitions[:]
            if "--family-prefix" in options:
                # Despite the option name, AWS selects an exact family here.
                family = options[options.index("--family-prefix") + 1]
                definitions = [arn for arn in definitions
                               if arn.rsplit("/", 1)[-1].rsplit(":", 1)[0] == family]
            return {"taskDefinitionArns": definitions}
        if (service, operation) == ("ecs", "describe-task-definition"):
            return {"taskDefinition": {
                "taskDefinitionArn": options[1], "status": "ACTIVE",
                "volumes": self.efs_mounts,
            }}
        if (service, operation) == ("ecs", "stop-task"):
            return {}
        if (service, operation) == ("ecs", "wait"):
            self.assertEqual(options[0], "tasks-stopped")
            arns = options[options.index("--tasks") + 1:]
            self.assertLessEqual(len(arns), 100)
            self.tasks = [t for t in self.tasks if t["taskArn"] not in arns]
            return {}
        if (service, operation) == ("ecs", "deregister-task-definition"):
            self.definitions.remove(options[1])
            return {}
        if (service, operation) == ("efs", "delete-access-point"):
            self.points = [p for p in self.points if p["AccessPointId"] != options[1]]
            return {}
        self.fail(f"Unexpected AWS call: {args}")

    def inventory(self):
        return {
            "tasks": [{"cluster": self.cluster, "arn": self.tasks[0]["taskArn"],
                       "stopping": False, "investigation": "investigation-one"}],
            "access_points": [{"filesystem": self.filesystem, "id": self.point,
                               "deleting": False, "cluster_id": "cluster-one",
                               "investigation": "investigation-one"}],
            "task_definitions": [{"arn": self.definition}],
        }

    def mutations(self):
        return [event for event in self.events if event[0] == "destroy" or event[1] in {
            "stop-task", "wait", "deregister-task-definition", "delete-access-point",
        }]

    def destroy(self, answer="example", yes=False):
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value=answer) as prompt, \
                redirect_stdout(io.StringIO()) as output:
            self.environment.destroy(yes=yes, account=self.config["account_id"])
        return prompt, output.getvalue()


class DestroyCleanupTests(CleanupFixture):
    def test_decline_lists_consumers_without_mutations_or_destroy_plans(self):
        with patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value="no") as prompt, \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(eph.Error, "Approval declined"):
                self.environment.destroy(account=self.account)
        prompt.assert_called_once()
        for value in (self.tasks[0]["taskArn"], "investigation-one", "cluster-one",
                      self.point, self.definition, self.account, self.config["region"],
                      "Audit sync is best-effort"):
            self.assertIn(value, output.getvalue())
        self.assertEqual(self.mutations(), [])
        self.environment.plan_apply.assert_not_called()
        self.assertEqual(self.environment.meta["status"], "deployed")

    def test_no_runtime_consumers_needs_no_cleanup_prompt(self):
        self.tasks = []
        self.points = [self.access_point(self.managed_point)]
        self.definitions = []
        prompt, output = self.destroy()
        prompt.assert_not_called()
        self.assertNotIn("Boundary consumers remain", output)
        self.assertEqual(self.mutations(), [("destroy", "boundary-regional")])
        self.assertEqual(self.environment.meta["status"], "destroyed")

    def test_approval_cleans_in_order_then_checks_quiescence_before_destroy(self):
        original = self.environment.boundary_consumers
        inventories = []

        def quiescent():
            inventories.append(True)
            if len(inventories) == 3:
                self.events.append(("quiescent", "check"))
            return original()

        self.environment.boundary_consumers = Mock(side_effect=quiescent)
        prompt, _ = self.destroy()
        prompt.assert_called_once()
        self.assertEqual([event[:2] for event in self.mutations()], [
            ("ecs", "stop-task"), ("ecs", "wait"),
            ("ecs", "deregister-task-definition"), ("efs", "delete-access-point"),
            ("destroy", "boundary-regional"),
        ])
        quiescent_index = self.events.index(("quiescent", "check"))
        self.assertGreater(quiescent_index, next(
            i for i, event in enumerate(self.events) if event[:2] == ("efs", "delete-access-point")))
        self.assertLess(quiescent_index, self.events.index(("destroy", "boundary-regional")))
        self.assertEqual([p["AccessPointId"] for p in self.points], [self.managed_point])
        self.assertEqual(self.environment.meta["status"], "destroyed")

    def test_account_mismatch_blocks_destroy_with_real_guard(self):
        self.identity_account = "999999999999"
        with self.assertRaisesRegex(eph.Error, "identity does not match"):
            self.destroy()
        self.assertEqual(self.mutations(), [])
        self.environment.plan_apply.assert_not_called()

    def test_changed_identity_after_approval_blocks_cleanup(self):
        def approve(*args):
            self.identity_account = "999999999999"

        self.environment.approve = Mock(side_effect=approve)
        with self.assertRaisesRegex(eph.Error, "identity does not match"):
            self.destroy()
        self.environment.approve.assert_called_once()
        self.assertEqual(self.mutations(), [])
        self.environment.plan_apply.assert_not_called()

    def test_yes_still_requires_matching_explicit_account(self):
        for account in (None, "999999999999"):
            with self.subTest(account=account), self.assertRaisesRegex(eph.Error, "--account-id"):
                self.environment.destroy(yes=True, account=account)
        self.assertEqual(self.events, [])

    def test_new_consumer_during_approval_rejected_before_any_mutation(self):
        inventory = self.inventory()
        for kind in inventory:
            with self.subTest(kind=kind):
                changed = copy.deepcopy(inventory)
                new = copy.deepcopy(changed[kind][0])
                field = "id" if kind == "access_points" else "arn"
                new[field] += "-new"
                changed[kind].append(new)
                self.environment.boundary_consumers = Mock(side_effect=[inventory, changed])
                with self.assertRaisesRegex(eph.Error, "New Boundary consumers"), \
                        redirect_stdout(io.StringIO()):
                    self.environment.destroy(yes=True, account=self.account)
                self.assertEqual(self.mutations(), [])
                self.environment.plan_apply.assert_not_called()

    def test_stopping_task_is_waited_for_not_stopped_again(self):
        self.tasks = [self.task("stopping", stopping=True)]
        self.destroy()
        self.assertFalse(any(event[:2] == ("ecs", "stop-task") for event in self.events))
        wait = next(event for event in self.events if event[:2] == ("ecs", "wait"))
        self.assertIn(self.prefix + "task/" + self.cluster + "/stopping", wait)
        self.environment.plan_apply.assert_called_once()

    def test_more_than_100_tasks_chunk_describe_and_wait(self):
        self.tasks = [self.task(f"{i:03d}") for i in range(205)]
        self.destroy(yes=True)
        for operation in ("describe-tasks", "wait"):
            batches = [event for event in self.events if event[:2] == ("ecs", operation)]
            sizes = []
            for event in batches:
                end = event.index("--include") if "--include" in event else len(event)
                sizes.append(end - event.index("--tasks") - 1)
            self.assertEqual(sizes, [100, 100, 5] * (2 if operation == "describe-tasks" else 1))
        self.assertEqual(sum(event[:2] == ("ecs", "stop-task") for event in self.events), 205)

    def test_final_quiescence_rejects_a_new_session_before_destroy(self):
        inventory = self.inventory()
        for kind in inventory:
            with self.subTest(kind=kind):
                final = {key: [] for key in inventory}
                final[kind] = [copy.deepcopy(inventory[kind][0])]
                field = "id" if kind == "access_points" else "arn"
                final[kind][0][field] += "-new"
                self.environment.boundary_consumers = Mock(side_effect=[
                    {key: [] for key in inventory} | {"tasks": inventory["tasks"]},
                    {key: [] for key in inventory} | {"tasks": inventory["tasks"]},
                    final,
                ])
                with self.assertRaisesRegex(eph.Error, "remain or appeared during cleanup"):
                    self.destroy()
                self.environment.plan_apply.assert_not_called()
                self.assertEqual(self.environment.meta["status"], "deployed")


class CleanupInventoryTests(CleanupFixture):
    def test_absent_parents_allow_retry_and_orphan_definition_cleanup(self):
        original = self.command
        missing_cluster = self.cluster

        def command(args, **kwargs):
            if "describe-file-systems" in args:
                raise CommandError("Mock missing filesystem", stderr=(
                    b"An error occurred (FileSystemNotFound) when calling the "
                    b"DescribeFileSystems operation: filesystem does not exist"))
            result = original(args, **kwargs)
            if "describe-clusters" in args:
                return {"clusters": [], "failures": [
                    {"arn": missing_cluster, "reason": "MISSING"}]}
            return result

        self.runner.json.side_effect = command
        for missing_cluster in (self.cluster, self.cluster_arn):
            with self.subTest(cluster=missing_cluster):
                self.assertEqual(self.environment.boundary_consumers(), {
                    "tasks": [], "access_points": [],
                    "task_definitions": [{"arn": self.definition}],
                })
        self.destroy()
        self.assertEqual([event[:2] for event in self.mutations()], [
            ("ecs", "deregister-task-definition"), ("destroy", "boundary-regional"),
        ])
        self.assertFalse(any(event[:2] in {
            ("ecs", "list-tasks"), ("efs", "describe-access-points"),
        } for event in self.events))

    def test_only_exact_matching_missing_cluster_failure_is_accepted(self):
        original = self.command
        responses = [
            {},
            {"clusters": [], "failures": []},
            {"failures": [{"arn": self.cluster, "reason": "AccessDenied"}]},
            {"failures": [{"arn": self.cluster, "reason": "MISSING extra"}]},
            {"failures": [{"arn": "foreign", "reason": "MISSING"}]},
            {"failures": [{"arn": self.cluster}]},
            {"failures": [{"arn": self.cluster, "reason": "MISSING"}] * 2},
        ]
        for response in responses:
            with self.subTest(response=response):
                def command(args, **kwargs):
                    result = original(args, **kwargs)
                    return response if "describe-clusters" in args else result

                self.runner.json.side_effect = command
                with self.assertRaisesRegex(eph.Error, "matching live ownership"):
                    self.environment.destroy(account=self.account)
                self.assertEqual(self.mutations(), [])
                self.environment.plan_apply.assert_not_called()

    def test_filesystem_access_denied_and_notfound_substrings_are_not_missing(self):
        original = self.command
        for stderr in (
            b"(AccessDeniedException) when calling the DescribeFileSystems operation",
            b"FileSystemNotFound",
            b"(FileSystemNotFound) when calling the DescribeAccessPoints operation",
            b"(NotFileSystemNotFound) when calling the DescribeFileSystems operation",
            b"unrelated malformed error",
        ):
            with self.subTest(stderr=stderr):
                error = CommandError("Mock AWS failure", stderr=stderr)

                def command(args, **kwargs):
                    if "describe-file-systems" in args:
                        raise error
                    return original(args, **kwargs)

                self.runner.json.side_effect = command
                with self.assertRaises(CommandError) as caught:
                    self.environment.destroy(account=self.account)
                self.assertIs(caught.exception, error)
                self.assertEqual(self.mutations(), [])
                self.environment.plan_apply.assert_not_called()

    def test_malformed_filesystem_inventory_is_not_treated_as_missing(self):
        original = self.command
        for response in ({}, {"FileSystems": []}, {"FileSystems": [{"FileSystemId": "foreign"}]}):
            with self.subTest(response=response):
                def command(args, **kwargs):
                    result = original(args, **kwargs)
                    return response if "describe-file-systems" in args else result

                self.runner.json.side_effect = command
                with self.assertRaisesRegex(eph.Error, "matching live ownership"):
                    self.environment.destroy(account=self.account)
                self.assertEqual(self.mutations(), [])
                self.environment.plan_apply.assert_not_called()

    def test_live_foreign_parent_tags_refuse_without_prompt_or_mutation(self):
        for attribute in ("cluster_owner", "filesystem_owner"):
            with self.subTest(parent=attribute):
                setattr(self, attribute, "another-environment")
                with patch("builtins.input") as prompt, \
                        self.assertRaisesRegex(eph.Error, "matching live ownership"):
                    self.environment.destroy(account=self.account)
                prompt.assert_not_called()
                self.assertEqual(self.mutations(), [])
                self.environment.plan_apply.assert_not_called()
                setattr(self, attribute, "synthetic-owner")

    def test_task_in_wrong_cluster_or_account_refuses(self):
        original = self.tasks[0]
        for field, value in (
            ("clusterArn", self.prefix + "cluster/foreign"),
            ("taskArn", "arn:aws:ecs:us-west-2:999999999999:task/foreign/task"),
        ):
            with self.subTest(field=field):
                self.tasks = [{**original, field: value}]
                with self.assertRaisesRegex(eph.Error, "outside the owned cluster/account"):
                    self.environment.boundary_consumers()
                self.assertEqual(self.mutations(), [])

    def test_hash_tampering_rejected_before_aws_inventory(self):
        path = self.environment.home / "boundary-regional.tfstate"
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(eph.Error, "ownership/hash mismatch"):
            self.environment.boundary_consumers()
        self.assertEqual(self.events, [])

    def test_terraform_access_point_is_not_a_runtime_consumer(self):
        inventory = self.environment.boundary_consumers()
        self.assertEqual([point["id"] for point in inventory["access_points"]], [self.point])

    def test_generated_definition_requires_owned_mount_not_only_family_prefix(self):
        for mounts in ([], [{"efsVolumeConfiguration": {"fileSystemId": "fs-foreign"}}],
                       self.efs_mounts + [{"efsVolumeConfiguration": {"fileSystemId": "fs-foreign"}}]):
            with self.subTest(mounts=mounts):
                self.efs_mounts = mounts
                self.assertEqual(self.environment.boundary_consumers()["task_definitions"], [])
        self.efs_mounts = [{"efsVolumeConfiguration": {"fileSystemId": self.filesystem}}]
        self.definitions = [
            self.prefix + "task-definition/eph-example:1",
            self.prefix + "task-definition/eph-example-unrelated:1",
            self.definition.replace("123456789012", "999999999999"),
            self.definition,
        ]
        self.assertEqual(self.environment.boundary_consumers()["task_definitions"],
                         [{"arn": self.definition}])

    def test_dynamic_definitions_listed_once_without_exact_family_filter(self):
        foreign_mount = self.definition.replace("120000", "130000")
        self.definitions = [
            self.definition,
            self.prefix + "task-definition/unrelated-user-20261124T120000:1",
            self.definition.replace("123456789012", "999999999999"),
            self.definition.replace("us-west-2", "us-east-1"),
            foreign_mount,
        ]
        # Multiple Terraform base families still require only one account listing.
        path = self.environment.home / "boundary-regional.tfstate"
        state = json.loads(path.read_text())
        state["resources"].append({
            "mode": "managed", "type": "aws_ecs_task_definition",
            "instances": [{"attributes": {"family": "another-base"}}],
        })
        path.write_text(json.dumps(state))
        self.environment.meta["roots"]["boundary-regional"]["state_hash"] = eph.digest(
            path.read_bytes())
        original = self.command

        def command(args, **kwargs):
            result = original(args, **kwargs)
            if "describe-task-definition" in args and foreign_mount in args:
                result["taskDefinition"]["volumes"] = [
                    {"efsVolumeConfiguration": {"fileSystemId": "fs-foreign"}}]
            return result

        self.runner.json.side_effect = command
        self.assertEqual(self.environment.boundary_consumers()["task_definitions"],
                         [{"arn": self.definition}])
        listings = [event for event in self.events
                    if event[:2] == ("ecs", "list-task-definitions")]
        self.assertEqual(listings, [("ecs", "list-task-definitions", "--status", "ACTIVE")])
        descriptions = [event[-1] for event in self.events
                        if event[:2] == ("ecs", "describe-task-definition")]
        self.assertEqual(descriptions, [self.definition, foreign_mount])

    def test_existing_quiescence_check_remains_refusal_not_cleanup_for_redeploy(self):
        with self.assertRaisesRegex(eph.Error, "Boundary tasks remain"):
            self.environment.ensure_quiescent()
        self.tasks = []
        with self.assertRaisesRegex(eph.Error, "Unclosed Boundary investigations"):
            self.environment.ensure_quiescent()
        self.assertEqual(self.mutations(), [])


class CleanupPendingDeletionTests(CleanupFixture):
    def setUp(self):
        super().setUp()
        inventory = self.inventory()
        self.environment.boundary_consumers = Mock(side_effect=[
            inventory, inventory, {key: [] for key in inventory},
        ])
        self.environment.ensure_quiescent = Mock()
        self.environment.approve = Mock()

    def test_pending_efs_deletion_is_polled_until_gone(self):
        original = self.command
        polls = []

        def command(args, **kwargs):
            if "describe-access-points" in args:
                polls.append(True)
                return {"AccessPoints": ([self.access_point(self.point, deleting=True)]
                                         if len(polls) == 1 else [])}
            return original(args, **kwargs)

        self.runner.json.side_effect = command
        with patch.object(cleanup.time, "sleep") as sleep:
            self.destroy()
        self.assertEqual(len(polls), 2)
        sleep.assert_called_once_with(2)
        self.assertEqual(self.environment.boundary_consumers.call_count, 3)
        self.environment.plan_apply.assert_called_once()

    def test_efs_timeout_blocks_destroy_and_retry_waits_without_redeleting(self):
        inventory = self.inventory()
        inventory["tasks"] = []
        inventory["task_definitions"] = []
        inventory["access_points"][0]["deleting"] = True
        self.environment.boundary_consumers.side_effect = [inventory, inventory]
        self.points = [self.access_point(self.point, deleting=True)]
        with patch.object(cleanup.time, "sleep") as sleep:
            with self.assertRaisesRegex(eph.Error, "still pending; retry destroy"):
                self.destroy()
        self.assertEqual(sleep.call_count, 60)
        self.assertEqual(sum(event[:2] == ("efs", "describe-access-points")
                             for event in self.events), 60)
        self.assertEqual(self.mutations(), [])
        self.environment.ensure_quiescent.assert_not_called()
        self.environment.plan_apply.assert_not_called()
        self.assertEqual(self.environment.meta["status"], "deployed")
        self.points = []
        self.environment.boundary_consumers.side_effect = [
            inventory, inventory, {key: [] for key in inventory},
        ]
        self.destroy()
        self.assertFalse(any(event[:2] == ("efs", "delete-access-point") for event in self.events))
        self.assertEqual(self.environment.boundary_consumers.call_count, 5)
        self.environment.plan_apply.assert_called_once()
        self.assertEqual(self.environment.meta["status"], "destroyed")

    def test_aws_wait_failure_never_deregisters_deletes_or_destroys(self):
        original = self.command

        def command(args, **kwargs):
            if "tasks-stopped" in args:
                raise CommandError("Mock AWS task waiter timed out")
            return original(args, **kwargs)

        self.runner.run.side_effect = command
        with self.assertRaisesRegex(CommandError, "waiter timed out"):
            self.destroy()
        self.assertEqual([event[:2] for event in self.mutations()], [("ecs", "stop-task")])
        self.environment.ensure_quiescent.assert_not_called()
        self.environment.plan_apply.assert_not_called()


class CleanupMutationDispatchTests(Fixture):
    def test_each_cleanup_mutation_guards_account_before_dispatch(self):
        runner = Mock()
        env = self.env(runner)
        events = []
        env.guard_account = Mock(side_effect=lambda: events.append("guard"))
        runner.json.side_effect = lambda *args, **kwargs: events.append("json")
        runner.run.side_effect = lambda *args, **kwargs: events.append("run")
        for operation in ("stop-task", "delete-access-point", "deregister-task-definition"):
            service = "efs" if operation == "delete-access-point" else "ecs"
            for json_output in (True, False):
                with self.subTest(operation=operation, json_output=json_output):
                    events.clear()
                    env.guard_account.reset_mock()
                    runner.reset_mock()
                    env.aws(service, operation, json_output=json_output)
                    self.assertEqual(events, ["guard", "json" if json_output else "run"])
                    env.guard_account.assert_called_once_with()
                    dispatched = runner.json if json_output else runner.run
                    self.assertEqual(dispatched.call_args.args[0][:7], [
                        "aws", "--profile", self.config["profile"],
                        "--region", self.config["region"], service, operation,
                    ])
                    (runner.run if json_output else runner.json).assert_not_called()

    def test_each_cleanup_mutation_guard_error_prevents_runner_execution(self):
        runner = Mock()
        env = self.env(runner)
        env.guard_account = Mock(side_effect=eph.Error("AWS identity does not match approved account"))
        for operation in ("stop-task", "delete-access-point", "deregister-task-definition"):
            service = "efs" if operation == "delete-access-point" else "ecs"
            for json_output in (True, False):
                with self.subTest(operation=operation, json_output=json_output):
                    env.guard_account.reset_mock()
                    with self.assertRaisesRegex(eph.Error, "identity does not match"):
                        env.aws(service, operation, json_output=json_output)
                    env.guard_account.assert_called_once_with()
                    runner.json.assert_not_called()
                    runner.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
