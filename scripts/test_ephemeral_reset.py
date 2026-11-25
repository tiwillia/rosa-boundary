"""Offline local-reset guards; no AWS credentials, calls or resource deletion."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from test_ephemeral_env import Fixture, cli, eph


class ResetTests(Fixture):
    def setUp(self):
        super().setUp()
        # The production CLI creates this privately before taking the lock.
        # Fixture.env() otherwise creates parent directories with the host umask.
        eph.private_directory(Path(self.config["state_dir"]))

    def unused(self):
        env = self.env()
        env.create()
        env.aws = Mock(side_effect=AssertionError("Reset must not call AWS"))
        env.runner.run = Mock(side_effect=AssertionError("Reset must not execute commands"))
        return env

    def reset(self, env):
        with env.locked():
            return env.reset(yes=True)

    def test_changed_config_can_reset_unused_record_and_reuse_name(self):
        env = self.unused()
        original_metadata = (env.home / "environment.json").read_bytes()
        original_owner = env.meta["owner"]
        env.config = copy.deepcopy(env.config)
        env.config["region"] = "eu-west-1"
        env.config["ta_app_config"]["roles"] = [
            {"id": "SREP", "amsResource": "BackplaneOsdSrepResource"},
        ]
        with env.locked():
            lock_inode = (env.home / "lock").stat().st_ino
            result = env.reset(yes=True)
            self.assertEqual((env.home / "lock").stat().st_ino, lock_inode)
        archive = Path(result["archive"])
        archived_record = archive / "record"
        self.assertEqual(result["status"], "reset")
        self.assertEqual(result["aws_operations"], "none")
        self.assertEqual((archived_record / "environment.json").read_bytes(), original_metadata)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
        self.assertEqual(set(env.home.iterdir()), {env.home / "lock"})
        for key in eph.ROOTS:
            self.assertTrue((archived_record / f"{key}.tfstate").is_file())
        self.assertTrue((archived_record / "source").is_symlink())
        env.aws.assert_not_called()
        env.runner.run.assert_not_called()
        fresh = eph.Environment(self.repo, env.config, env.name)
        with fresh.locked():
            fresh.create()
            fresh.load()
        self.assertNotEqual(fresh.meta["owner"], original_owner)
        self.assertEqual(fresh.meta["config_hash"], eph.digest(eph.canonical(env.config)))

    def test_reset_is_idempotent_without_a_record(self):
        env = self.unused()
        self.reset(env)
        self.assertEqual(self.reset(env)["status"], "absent")

    def test_prepared_or_refreshed_unused_record_can_reset(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.prepare()
        env.refresh_source()
        env.aws = Mock(side_effect=AssertionError("Reset must not call AWS"))
        self.assertEqual(self.reset(env)["status"], "reset")
        env.aws.assert_not_called()

    def test_reset_requires_lock_and_respects_other_operation(self):
        env = self.unused()
        with self.assertRaisesRegex(eph.Error, "requires the environment lock"):
            env.reset(yes=True)
        other = eph.Environment(self.repo, self.config, env.name)
        with env.locked():
            with self.assertRaisesRegex(eph.Error, "Another operation"):
                with other.locked():
                    other.reset(yes=True)

    def test_possible_cloud_activity_always_refuses_even_with_empty_states(self):
        env = self.unused()
        original = copy.deepcopy(env.meta)
        changes = [
            ("status", "deploying"), ("status", "deployed"), ("status", "destroyed"),
            ("status", "recovered"), ("repositories", {"boundary": "eph-example/boundary"}),
            ("pending_ecr", {"boundary": {}}), ("pending_publications", {"ta": {}}),
            ("pending_ecr_deletions", {"ta": {}}), ("secret_version_id", "version"),
            ("expected_ta_deployment", "ecs-deployment"), ("outputs", {}),
            ("abandoned_publications", []), ("deleted_repositories", []),
            ("verification", {}), ("images", {"ta": {"uri": "image@sha256:test"}}),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                env.meta = {**copy.deepcopy(original), key: value}
                eph.write_json(env.home / "environment.json", env.meta)
                with self.assertRaisesRegex(eph.Error, "not provably unused"):
                    self.reset(env)
                self.assertTrue((env.home / "environment.json").exists())
        env.aws.assert_not_called()
        env.runner.run.assert_not_called()

    def test_any_root_apply_or_journal_refuses(self):
        env = self.unused()
        original = copy.deepcopy(env.meta)
        for root in eph.ROOTS:
            for key, value in (("touched", True), ("pending_plan", "hash"),
                               ("pending_generation", "generation"), ("generation", "generation"),
                               ("destroyed", True), ("recovered_from_plan", "hash")):
                with self.subTest(root=root, key=key):
                    env.meta = copy.deepcopy(original)
                    env.meta["roots"][root][key] = value
                    eph.write_json(env.home / "environment.json", env.meta)
                    with self.assertRaisesRegex(eph.Error, "not provably unused"):
                        self.reset(env)

    def test_missing_modified_and_nonempty_states_refuse(self):
        env = self.unused()
        for root in eph.ROOTS:
            path = env.home / f"{root}.tfstate"
            original = path.read_bytes()
            for alteration in ("missing", "hash", "resources", "serial", "outputs"):
                with self.subTest(root=root, alteration=alteration):
                    state = json.loads(original)
                    if alteration == "missing":
                        path.unlink()
                    elif alteration == "hash":
                        path.write_bytes(original + b"\n")
                    else:
                        state[alteration] = (
                            [{"type": "aws_s3_bucket"}] if alteration == "resources"
                            else 1 if alteration == "serial" else {"unexpected": "output"}
                        )
                        eph.write_json(path, state)
                    with self.assertRaises(eph.Error):
                        self.reset(env)
                    path.write_bytes(original)

    def test_unverified_ownership_and_original_config_refuse(self):
        env = self.unused()
        for filename in ("config.json", "environment.json"):
            path = env.home / filename
            original = path.read_bytes()
            for data in (b"not JSON", b"[]", b"{}"):
                with self.subTest(filename=filename, data=data):
                    path.write_bytes(data)
                    with self.assertRaisesRegex(eph.Error, "not provably unused"):
                        self.reset(env)
            path.write_bytes(original)

    def test_nonempty_state_with_matching_metadata_hash_still_refuses(self):
        env = self.unused()
        path = env.home / "ta-global.tfstate"
        state = json.loads(path.read_text())
        state["resources"] = [{"type": "aws_s3_bucket"}]
        eph.write_json(path, state)
        env.meta["roots"]["ta-global"]["state_hash"] = eph.digest(path.read_bytes())
        eph.write_json(env.home / "environment.json", env.meta)
        with self.assertRaisesRegex(eph.Error, "not provably unused"):
            self.reset(env)

    def test_symlinked_metadata_config_or_state_refuses(self):
        env = self.unused()
        for name in ("environment.json", "config.json", "ta-global.tfstate"):
            with self.subTest(name=name):
                path = env.home / name
                original = path.read_bytes()
                target = self.base / "external-file"
                target.write_bytes(original)
                path.unlink()
                path.symlink_to(target)
                with self.assertRaisesRegex(eph.Error, "not provably unused"):
                    self.reset(env)
                self.assertEqual(target.read_bytes(), original)
                path.unlink()
                path.write_bytes(original)

    def test_locks_backups_or_generation_image_history_refuse(self):
        env = self.unused()
        for filename in (".ta-global.tfstate.lock.info", "ta-global.tfstate.backup",
                         "unexpected.tfstate"):
            with self.subTest(filename=filename):
                path = env.home / filename
                path.write_text("{}")
                with self.assertRaisesRegex(eph.Error, "not provably unused"):
                    self.reset(env)
                path.unlink()
        env.meta["generations"][env.generation]["images"] = {"ta": {"uri": "image"}}
        eph.write_json(env.home / "environment.json", env.meta)
        with self.assertRaisesRegex(eph.Error, "not provably unused"):
            self.reset(env)

    def test_incomplete_record_is_preserved(self):
        env = self.unused()
        (env.home / "environment.json").unlink()
        with self.assertRaisesRegex(eph.Error, "incomplete local record"):
            self.reset(env)
        self.assertTrue((env.home / "config.json").is_file())

    def test_approval_decline_does_not_move_record(self):
        env = self.unused()
        env.approve = Mock(side_effect=eph.Error("Approval declined"))
        with env.locked(), self.assertRaisesRegex(eph.Error, "Approval declined"):
            env.reset()
        self.assertTrue((env.home / "environment.json").is_file())
        self.assertFalse((env.home.parent / ".reset-archives").exists())

    def test_changed_evidence_during_approval_refuses(self):
        env = self.unused()
        def change(*args):
            (env.home / "ta-global.tfstate").write_text("{}")
        env.approve = Mock(side_effect=change)
        with env.locked(), self.assertRaises(eph.Error):
            env.reset()
        self.assertTrue((env.home / "environment.json").is_file())

    def test_interrupted_archive_retains_active_metadata_and_fails_closed(self):
        env = self.unused()
        rename = os.rename
        calls = []
        def interrupt(source, destination):
            calls.append(source)
            if len(calls) == 2:
                raise OSError("synthetic interrupted archive")
            rename(source, destination)
        with patch("ephemeral_environment.reset.os.rename", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.reset(env)
        self.assertTrue((env.home / "environment.json").is_file())
        with self.assertRaises(eph.Error):
            self.reset(env)
        self.assertTrue(any((env.home.parent / ".reset-archives").iterdir()))

    def test_cli_reset_returns_local_result_without_loading_original_config(self):
        env = self.unused()
        config_file = self.config_file()
        output = io.StringIO()
        with patch.object(cli, "Environment", return_value=env), \
                patch.object(cli.os, "umask"), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main([
                "reset", "--config", str(config_file), "--name", env.name, "--yes", "--json",
            ]), 0)
        self.assertIn('"status": "reset"', output.getvalue())
        self.assertIn('"aws_operations": "none"', output.getvalue())
        env.aws.assert_not_called()

    def test_cli_reset_refuses_to_move_the_supplied_config_file(self):
        env = self.unused()
        output = io.StringIO()
        with patch.object(cli, "Environment", return_value=env), \
                patch.object(cli.os, "umask"), contextlib.redirect_stderr(output):
            self.assertEqual(cli.main([
                "reset", "--config", str(env.home / "config.json"),
                "--name", env.name, "--yes",
            ]), 1)
        self.assertIn("config must live outside", output.getvalue())
        self.assertTrue((env.home / "environment.json").is_file())
