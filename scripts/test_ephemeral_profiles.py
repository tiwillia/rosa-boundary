"""Named profile registration uses the real CLI schema, not a second config."""

import os
from pathlib import Path
from unittest.mock import Mock, patch

from ephemeral_environment.runtime import CommandError
from test_ephemeral_env import Fixture, eph


class ProfileRegistrationTests(Fixture):
    def prepared(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.prepare()
        env.runner.run = Mock(side_effect=self.register)
        self.installed = self.enterContext(patch(
            "ephemeral_environment.readiness.shutil.which",
            return_value="/installed/rosa-boundary",
        ))
        return env

    def register(self, args, **kwargs):
        if args[1:3] == ["config", "init"]:
            path = Path(kwargs["extra_env"]["XDG_CONFIG_HOME"]) / "rosa-boundary/config.yaml"
            previous = path.read_bytes() if path.exists() else b""
            path.write_bytes(previous + b"# source CLI profile with ocm_url\n")
        return b""

    def connection(self, env):
        env.meta["outputs"] = {
            "trusted_actions": {"internal_fqdn": "https://ta.example.org"},
            "boundary": {
                "lambda_function_name": "create-dev",
                "lambda_invoker_role_arn": "arn:aws:iam::123456789012:role/invoker",
                "sre_shared_role_arn": "arn:aws:iam::123456789012:role/sre",
                "ecs_cluster_name": "boundary-dev", "efs_filesystem_id": "fs-test",
            },
        }

    def test_standard_xdg_profile_uses_source_built_cli_and_preserves_selection(self):
        env = self.prepared()
        self.connection(env)
        config_home = self.base / "xdg config"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
            env.write_connection()
        calls = env.runner.run.call_args_list
        build, registration, validation = calls
        helper = env.source.parent / "profile-cli"
        self.assertEqual(build.args[0], ["make", "build-cli", f"CLI_BIN={helper}"])
        self.assertEqual(build.kwargs["cwd"], env.work)
        args = registration.args[0]
        self.assertEqual(args[:3], [str(helper), "config", "init"])
        for flag in ("--overwrite", "--yes", "--auto-discover=false", "--use-device-code=false"):
            self.assertIn(flag, args)
        self.assertNotIn("--use", args)
        self.assertNotIn("--profile", args)
        self.assertEqual(args[args.index("--name") + 1], env.name)
        self.assertEqual(args[args.index("--region") + 1], "us-west-2")
        staged_home = Path(registration.kwargs["extra_env"]["XDG_CONFIG_HOME"])
        self.assertEqual(staged_home.parent, config_home / "rosa-boundary")
        self.assertFalse(staged_home.exists())
        self.assertTrue(registration.kwargs["build"])  # No runtime AWS/Vault credentials.
        self.assertEqual(validation.args[0], ["/installed/rosa-boundary", "config", "list-profiles"])
        self.assertEqual(validation.kwargs["extra_env"], registration.kwargs["extra_env"])
        self.assertTrue(validation.kwargs["build"])
        target = config_home / "rosa-boundary/config.yaml"
        self.assertIn(b"ocm_url", target.read_bytes())
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        connection = env.meta["connection"]
        self.assertEqual(connection["cli_profile"], env.name)
        self.assertEqual(connection["cli_config_path"], str(config_home / "rosa-boundary/config.yaml"))
        self.assertEqual(connection["cli_command"], "rosa-boundary --profile example")
        self.assertNotIn("XDG_CONFIG_HOME", connection["client_environment"])
        self.assertNotIn("XDG_CACHE_HOME", connection["client_environment"])
        self.assertFalse((env.home / "client-config").exists())
        self.assertFalse((env.home / "client-cache").exists())
        env.check_work()

    def test_missing_or_empty_xdg_uses_normal_home_config(self):
        env = self.prepared()
        self.connection(env)
        with patch("ephemeral_environment.readiness.Path.home", return_value=self.base):
            for xdg in (None, ""):
                with self.subTest(xdg=xdg), patch.dict(os.environ):
                    if xdg is None:
                        os.environ.pop("XDG_CONFIG_HOME", None)
                    else:
                        os.environ["XDG_CONFIG_HOME"] = xdg
                    env.write_connection()
                    call = env.runner.run.call_args
                    self.assertEqual(Path(call.kwargs["extra_env"]["XDG_CONFIG_HOME"]).parent,
                                     self.base / ".config/rosa-boundary")

    def test_older_installed_cli_cannot_break_existing_profiles(self):
        env = self.prepared()
        self.connection(env)
        config_home = self.base / "config"
        target = config_home / "rosa-boundary/config.yaml"
        target.parent.mkdir(parents=True)
        original = b"active_profile: stage\nprofiles:\n  stage: {}\n"
        target.write_bytes(original)

        def old_cli(args, **kwargs):
            if args[1:3] == ["config", "list-profiles"]:
                staged = Path(kwargs["extra_env"]["XDG_CONFIG_HOME"]) / "rosa-boundary/config.yaml"
                self.assertIn(b"ocm_url", staged.read_bytes())
                raise CommandError("old CLI", b"field ocm_url not found; sensitive details")
            return self.register(args, **kwargs)

        env.runner.run.side_effect = old_cli
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
            with self.assertRaisesRegex(eph.Error, "installed rosa-boundary cannot read") as caught:
                env.write_connection()
        self.assertIn("make install-cli", str(caught.exception))
        self.assertNotIn("sensitive details", str(caught.exception))
        self.assertEqual(target.read_bytes(), original)
        self.assertNotIn("connection", env.meta)
        self.assertFalse((env.home / "connection.json").exists())
        self.assertEqual(list(target.parent.glob(".ephemeral-profile-*")), [])

    def test_without_installed_cli_source_helper_can_register(self):
        env = self.prepared()
        self.installed.return_value = None
        self.connection(env)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base / "config")}):
            env.write_connection()
        self.assertEqual(env.runner.run.call_count, 2)

    def test_incompatible_cli_does_not_create_first_config(self):
        env = self.prepared()
        self.connection(env)
        config_home = self.base / "config"

        def reject(args, **kwargs):
            if args[1:3] == ["config", "list-profiles"]:
                raise CommandError("incompatible CLI")
            return self.register(args, **kwargs)

        env.runner.run.side_effect = reject
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
            with self.assertRaisesRegex(eph.Error, "existing config was not changed"):
                env.write_connection()
        self.assertFalse((config_home / "rosa-boundary/config.yaml").exists())
        self.assertNotIn("connection", env.meta)

    def test_concurrent_config_edit_is_not_overwritten(self):
        env = self.prepared()
        self.connection(env)
        config_home = self.base / "config"
        target = config_home / "rosa-boundary/config.yaml"

        def edit_during_validation(args, **kwargs):
            if args[1:3] == ["config", "list-profiles"]:
                target.write_bytes(b"# concurrent edit\n")
            return self.register(args, **kwargs)

        env.runner.run.side_effect = edit_during_validation
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
            with self.assertRaisesRegex(eph.Error, "changed during registration"):
                env.write_connection()
        self.assertEqual(target.read_bytes(), b"# concurrent edit\n")

    def test_config_symlink_is_not_followed(self):
        env = self.prepared()
        self.connection(env)
        config_home = self.base / "config"
        directory = config_home / "rosa-boundary"
        directory.mkdir(parents=True)
        target = self.base / "unrelated"
        target.write_text("preserve")
        (directory / "config.yaml").symlink_to(target)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
            with self.assertRaisesRegex(eph.Error, "not a symlink"):
                env.write_connection()
        self.assertEqual(target.read_text(), "preserve")

    def test_cli_registration_failure_has_local_repair_guidance_and_no_connection_success(self):
        env = self.prepared()
        self.connection(env)
        env.runner.run.side_effect = [b"", CommandError("synthetic failure", b"sensitive-config")]
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base / "config")}):
            with self.assertRaisesRegex(eph.Error, "Could not register CLI profile") as caught:
                env.write_connection()
        self.assertNotIn("sensitive-config", str(caught.exception))
        self.assertIn("No AWS redeployment is needed", str(caught.exception))
        self.assertNotIn("connection", env.meta)

    def test_relative_config_home_is_rejected_before_building(self):
        env = self.prepared()
        self.connection(env)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative-config"}):
            with self.assertRaisesRegex(eph.Error, "XDG_CONFIG_HOME must be absolute"):
                env.write_connection()
        env.runner.run.assert_not_called()

    def test_profile_lock_symlink_is_not_followed(self):
        env = self.prepared()
        self.connection(env)
        config_home = self.base / "config"
        directory = config_home / "rosa-boundary"
        directory.mkdir(parents=True)
        target = self.base / "unrelated"
        target.write_text("preserve")
        (directory / ".ephemeral-profiles.lock").symlink_to(target)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config_home)}):
            with self.assertRaisesRegex(eph.Error, "must not be a symlink"):
                env.write_connection()
        self.assertEqual(target.read_text(), "preserve")
