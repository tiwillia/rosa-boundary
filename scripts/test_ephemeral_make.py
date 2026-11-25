"""Exercise deployment and image Make contracts with local command shims."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_ephemeral_env import Fixture, eph


ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_TARGETS = ("ephemeral-env", "ephemeral-env-status", "ephemeral-env-destroy",
                     "ephemeral-env-reset")


class MakeContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ephemeral-make-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.tools = self.base / "tools"
        self.tools.mkdir()
        self.log = self.base / "calls.jsonl"
        for command in ("podman", "python3"):
            shim = self.tools / command
            shim.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "with open(os.environ['MAKE_CONTRACT_LOG'], 'a') as stream:\n"
                "    stream.write(json.dumps({'args': sys.argv[1:], "
                "'cwd': os.getcwd()}) + '\\n')\n"
            )
            shim.chmod(0o700)
        identity = self.tools / "id"
        identity.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "assert sys.argv[1:] == ['-un']\n"
            "value = os.environ['MAKE_CONTRACT_USERNAME']\n"
            "if not value:\n"
            "    sys.exit(1)\n"
            "print(value)\n"
        )
        identity.chmod(0o700)
        self.process_env = {
            key: value for key, value in os.environ.items()
            if key not in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "EPHEMERAL_CONFIG",
                           "EPHEMERAL_NAME", "EPHEMERAL_COMMIT", "EPHEMERAL_ARGS",
                           "XDG_CONFIG_HOME")
            and not key.startswith(("GITHUB_", "GIT_"))
        }
        self.process_env.update(
            PATH=str(self.tools) + os.pathsep + os.environ["PATH"],
            MAKE_CONTRACT_LOG=str(self.log),
            MAKE_CONTRACT_USERNAME="reviewuser",
        )

    def run_make(self, relative, *args):
        directory = self.base / ("boundary" if relative == "." else "trusted-actions")
        directory.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / relative / "Makefile", directory / "Makefile")
        result = subprocess.run(
            ["make", "--no-print-directory", *args],
            cwd=directory, env=self.process_env, text=True, capture_output=True,
        )
        return result, directory

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_image_builds_share_target_inputs_platform_and_tag_suffix(self):
        for relative in (".", "components/trusted-actions"):
            with self.subTest(component=relative):
                result, directory = self.run_make(
                    relative, "build-amd64",
                    "IMAGE_NAME=localhost/contract/server", "TAG=review",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                call = self.calls()[-1]
                args = call["args"]
                self.assertEqual(args[0], "build")
                self.assertEqual(args[args.index("--platform") + 1], "linux/amd64")
                self.assertEqual(args[args.index("-t") + 1], "localhost/contract/server:review-amd64")
                self.assertEqual(args[args.index("-f") + 1], "Containerfile")
                self.assertEqual(args[-1], ".")
                self.assertEqual(call["cwd"], str(directory))

    def test_image_build_defaults_match_component_names(self):
        for relative, name in ((".", "rosa-boundary"), ("components/trusted-actions", "rosa-trusted-actions")):
            with self.subTest(component=relative):
                result, _ = self.run_make(relative, "build-amd64")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                args = self.calls()[-1]["args"]
                self.assertEqual(args[args.index("-t") + 1], name + ":latest-amd64")

    def test_retained_lifecycle_targets_forward_explicit_configuration(self):
        for target, command in (
            ("ephemeral-env", "deploy"),
            ("ephemeral-env-status", "status"),
            ("ephemeral-env-destroy", "destroy"),
            ("ephemeral-env-reset", "reset"),
        ):
            with self.subTest(target=target):
                result, _ = self.run_make(
                    ".", target, "EPHEMERAL_CONFIG=/private/config with spaces.json",
                    "EPHEMERAL_NAME=review", "EPHEMERAL_COMMIT=abc123",
                    "EPHEMERAL_ARGS=--yes",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("python3 scripts/ephemeral_env.py", result.stdout)
                expected = [
                    "scripts/ephemeral_env.py", command,
                    "--config", "/private/config with spaces.json", "--name", "review",
                ]
                if command == "deploy":
                    expected += ["--commit", "abc123"]
                self.assertEqual(self.calls()[-1]["args"], expected + ["--yes"])

    def test_lifecycle_targets_require_configuration_and_name(self):
        for target in LIFECYCLE_TARGETS:
            for parameters in ([], ["EPHEMERAL_CONFIG=/private/config.json"], ["EPHEMERAL_NAME=review"]):
                with self.subTest(target=target, parameters=parameters):
                    result, _ = self.run_make(
                        ".", target, "EPHEMERAL_CONFIG=", "EPHEMERAL_NAME=", *parameters,
                    )
                    self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists(), "Invalid inputs must not invoke the orchestrator")

    def test_lifecycle_targets_default_config_path(self):
        home = self.base / "home with spaces"
        self.process_env["HOME"] = str(home)
        for xdg in (None, "", str(self.base / "xdg with spaces")):
            if xdg is None:
                self.process_env.pop("XDG_CONFIG_HOME", None)
            else:
                self.process_env["XDG_CONFIG_HOME"] = xdg
            expected = str((Path(xdg) if xdg else home / ".config")
                           / "rosa-boundary/ephemeral-dev.json")
            for target in LIFECYCLE_TARGETS:
                with self.subTest(target=target, xdg=xdg):
                    result, _ = self.run_make(".", target, "EPHEMERAL_NAME=review")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    args = self.calls()[-1]["args"]
                    self.assertEqual(args[args.index("--config") + 1], expected)
                    self.assertEqual(args[args.index("--name") + 1], "review")

    def test_default_environment_name_uses_current_account_not_user_environment(self):
        self.process_env["USER"] = "stale-user"
        for target in LIFECYCLE_TARGETS:
            with self.subTest(target=target):
                result, _ = self.run_make(".", target)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                args = self.calls()[-1]["args"]
                self.assertEqual(args[args.index("--name") + 1], "reviewuser")
        self.process_env.pop("USER", None)
        result, _ = self.run_make(".", "ephemeral-env")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.calls()[-1]["args"]
        self.assertEqual(args[args.index("--name") + 1], "reviewuser")

    def test_failed_identity_lookup_requires_explicit_environment_name(self):
        self.process_env["MAKE_CONTRACT_USERNAME"] = ""
        for target in LIFECYCLE_TARGETS:
            with self.subTest(target=target):
                result, _ = self.run_make(".", target)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Set EPHEMERAL_NAME", result.stdout)
        self.assertFalse(self.log.exists())

    def test_environment_name_override_does_not_require_identity_lookup(self):
        self.process_env.update(MAKE_CONTRACT_USERNAME="", EPHEMERAL_NAME="customdev")
        for target in LIFECYCLE_TARGETS:
            with self.subTest(target=target):
                result, _ = self.run_make(".", target)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                args = self.calls()[-1]["args"]
                self.assertEqual(args[args.index("--name") + 1], "customdev")

    def test_environment_config_path_overrides_default(self):
        self.process_env.update(XDG_CONFIG_HOME=str(self.base / "xdg"),
                                EPHEMERAL_CONFIG="/private/custom config.json")
        result, _ = self.run_make(".", "ephemeral-env", "EPHEMERAL_NAME=review")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = self.calls()[-1]["args"]
        self.assertEqual(args[args.index("--config") + 1], "/private/custom config.json")

    def test_recovery_is_not_a_make_shortcut(self):
        result, _ = self.run_make(".", "-n", "ephemeral-env-recover")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No rule to make target", result.stderr)


class RegionalMakeTests(Fixture):
    def test_real_make_plan_apply_and_destroy_plan_with_empty_dotenv(self):
        runner = eph.Runner(self.config)
        env = self.env(runner)
        regional = env.root("boundary-regional")
        regional.mkdir(parents=True)
        original = Path(__file__).resolve().parents[1] / "deploy/regional/Makefile"
        shutil.copyfile(original, regional / "Makefile")
        (env.work / ".env").write_text("")

        tools = self.base / "tools"
        tools.mkdir()
        log = self.base / "terraform-calls.jsonl"
        terraform = tools / "terraform"
        terraform.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['STUB_TERRAFORM_LOG'], 'a') as stream:\n"
            "    stream.write(json.dumps({'args': sys.argv[1:],\n"
            "        'issuer': os.environ.get('TF_VAR_keycloak_issuer_url'),\n"
            "        'plan_flags': os.environ.get('TF_CLI_ARGS_plan'),\n"
            "        'apply_flags': os.environ.get('TF_CLI_ARGS_apply')}) + '\\n')\n"
        )
        terraform.chmod(0o700)
        runner.env.update(
            PATH=str(tools) + os.pathsep + runner.env["PATH"],
            STUB_TERRAFORM_LOG=str(log),
        )
        plan = self.base / "approved.plan"
        env.tf("boundary-regional", "plan", "-input=false", "-out=" + str(plan))
        env.tf("boundary-regional", "apply", "-input=false", str(plan))
        env.tf("boundary-regional", "plan", "-input=false", "-destroy", "-out=" + str(plan))
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([item["args"] for item in calls], [["plan"], ["apply"], ["plan"]])
        for item in calls:
            self.assertEqual(item["issuer"], self.config["keycloak"]["issuer_url"])
        self.assertIn("-out=" + str(plan), calls[0]["plan_flags"])
        self.assertIn(str(plan), calls[1]["apply_flags"])
        self.assertIn("-destroy", calls[2]["plan_flags"])
        # No Lambda build was attempted between review and saved-plan apply:
        # that dependency has no implementation in this fixture checkout.
