"""Configuration regressions: no credential-bearing URLs may reach deployment."""

import copy
import json
import os
import shutil
import zipfile
from unittest.mock import Mock

from test_ephemeral_env import Fixture, eph


class SnapshotSecurityTests(Fixture):
    def test_internal_symlink_to_excluded_credential_fails_closed_in_both_modes(self):
        secret = b"synthetic excluded credential bytes\n"
        (self.repo / ".env").write_bytes(secret)
        (self.repo / "credential-alias").symlink_to(".env")
        self.git("add", "-f", ".env", "credential-alias")
        self.git("commit", "-qm", "synthetic credential symlink fixture")
        for revision in (None, "HEAD"):
            with self.subTest(revision=revision):
                target = self.base / ("working-tree" if revision is None else "commit")
                with self.assertRaisesRegex(eph.Error, "dangling symlink"):
                    eph.snapshot(self.repo, target, revision)
                self.assertFalse((target / ".env").exists())
                for path in target.rglob("*"):
                    if not path.is_symlink() and path.is_file():
                        self.assertNotIn(secret, path.read_bytes())
                self.assertEqual((self.repo / ".env").read_bytes(), secret)

    def test_explicit_credential_exclusions_apply_even_to_tracked_ignored_files(self):
        names = (
            ".env", ".env.local", ".aws/credentials", ".kube/config",
            ".vault-token", ".netrc", "nested/.aws/credentials",
            "deploy/private.tfvars", "deploy/private.tfvars.json",
            "deploy/private.tfstate", "deploy/private.tfstate.backup",
            "build/keys/private.pem", "build/keys/private.key",
            "build/keys/private.p12", "build/keys/private.pfx",
        )
        (self.repo / ".gitignore").write_text(
            ".env*\n.aws/\n.kube/\n.vault-token\n.netrc\ndeploy/\nkeys/\n")
        for name in names:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic credential fixture\n")
        self.git("add", ".gitignore")
        self.git("add", "-f", *names)
        self.git("commit", "-qm", "synthetic tracked credential fixtures")
        for revision in (None, "HEAD"):
            target = self.base / ("working-tree" if revision is None else "commit")
            eph.snapshot(self.repo, target, revision)
            for name in names:
                with self.subTest(revision=revision, path=name):
                    self.assertFalse((target / name).exists())
            self.assertEqual((target / "tracked").read_text(), "original")

    def test_build_readability_does_not_widen_private_state_under_restrictive_umask(self):
        self.fake_source()
        executable = self.repo / "lambda/create-investigation/helper.sh"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        handler = self.repo / "lambda/create-investigation/handler.py"
        handler.chmod(0o600)
        self.git("add", "Makefile", "Containerfile", "deploy", "components", "lambda")
        self.git("commit", "-qm", "deployment source with executable and nonexecutable inputs")
        for revision in (None, "HEAD"):
            with self.subTest(revision=revision):
                self.config["state_dir"] = str(
                    self.base / ("working-tree-state" if revision is None else "commit-state"))
                previous = os.umask(0o077)
                try:
                    env = self.env()
                    env.create(revision)
                    before = eph.tree_manifest(env.source)
                    env.prepare()
                finally:
                    os.umask(previous)
                for root, directory_mode, file_mode, executable_mode in (
                    (env.source, 0o555, 0o444, 0o555),
                    (env.work, 0o755, 0o644, 0o755),
                ):
                    # Inspect source-selected paths only: generated overrides
                    # and credential/state files must remain private.
                    for path in [env.source, *env.source.rglob("*")]:
                        relative = path.relative_to(env.source)
                        actual = root / relative
                        expected = (directory_mode if path.is_dir() else
                                    executable_mode if relative == executable.relative_to(self.repo)
                                    else file_mode)
                        with self.subTest(root=root.name, path=str(relative)):
                            self.assertEqual(actual.stat().st_mode & 0o777, expected)
                for directory in (env.home.parent, env.home, env.source.parent.parent,
                                  env.source.parent):
                    with self.subTest(private_directory=directory):
                        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                for path in (env.home / "environment.json", env.home / "terraform.rc",
                             *(env.home / f"{key}.tfstate" for key in eph.ROOTS)):
                    with self.subTest(private_file=path.name):
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(eph.tree_manifest(env.source), before)
                copied_handler = env.work / handler.relative_to(self.repo)
                copied_handler.write_text("# changed only in writable build copy\n")
                self.assertEqual((env.source / handler.relative_to(self.repo)).read_bytes(),
                                 handler.read_bytes())
                self.assertEqual(eph.tree_manifest(env.source), before)


class CredentialURLTests(Fixture):
    def test_proxy_credentials_rejected_before_configuration_is_persisted(self):
        urls = (
            "https://example-user:not-a-real-password@proxy.example.invalid",
            "http://example-user@proxy.example.invalid",
            "socks5h://example-user:not-a-real-password@proxy.example.invalid",
            "//example-user:not-a-real-password@proxy.example.invalid",
            "https://proxy.example.invalid?token=not-a-real-token",
            "https://proxy.example.invalid#not-a-real-token",
            "https://[malformed",
            "https:example-user:not-a-real-password@proxy.example.invalid",
        )
        for section in ("aws", "backplane"):
            for url in urls:
                with self.subTest(section=section, url=url):
                    config = copy.deepcopy(self.config)
                    config["ta_app_config"].setdefault(section, {})["proxy_url"] = url
                    with self.assertRaises(eph.Error) as error:
                        eph.load_config(self.config_file(config), self.repo)
                    self.assertNotIn("not-a-real", str(error.exception))
                    self.assertFalse((self.base / "environments").exists())

    def test_nested_url_credentials_are_rejected(self):
        with self.assertRaises(eph.Error):
            eph.reject_credentials({
                "workers": [{"endpoint": "https://u:synthetic-fixture@proxy.example.invalid"}],
            })

    def test_public_proxy_urls_and_authorization_policy_remain_supported(self):
        config = copy.deepcopy(self.config)
        config["ta_app_config"]["aws"] = {"proxy_url": "http://proxy.example.invalid:3128"}
        config["ta_app_config"]["backplane"]["proxy_url"] = "https://proxy.example.invalid"
        loaded = eph.load_config(self.config_file(config), self.repo)
        self.assertEqual(loaded["ta_app_config"]["aws"], config["ta_app_config"]["aws"])
        eph.reject_credentials({
            "roles": ["arn:aws:iam::123456789012:role/example-*"],
            "actions": {"allowed_secrets": ["example-policy-name"], "pattern": "name?"},
        })


class PreparedInputTests(Fixture):
    """Reject added executable inputs, not just changes to preselected paths."""

    def setUp(self):
        super().setUp()
        self.fake_source()
        self.environment = self.env()
        self.environment.create()
        self.environment.prepare()

    def test_lambda_archive_preserves_runtime_readable_source_permissions(self):
        env = self.environment
        relative = "lambda/create-investigation/handler.py"
        archive = self.base / "lambda.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.write(env.work / relative, "handler.py")
        with zipfile.ZipFile(archive) as package:
            info = package.getinfo("handler.py")
            self.assertEqual((info.external_attr >> 16) & 0o777, 0o644)
            self.assertEqual(package.read("handler.py"), (env.source / relative).read_bytes())

    def test_unexpected_terraform_module_and_build_inputs_are_rejected(self):
        env = self.environment
        names = (
            "deploy/account/injected.tf",
            "deploy/account/injected.tf.json",
            "deploy/account/injected.auto.tfvars",
            "deploy/account/injected.auto.tfvars.json",
            "deploy/account/override.tf",
            "deploy/account/override.tf.json",
            "deploy/account/new_override.tf",
            "deploy/account/new_override.tf.json",
            "deploy/account/modules/shared-iam/injected.tf",
            "deploy/account/modules/shared-iam/injected.tf.json",
            "GNUmakefile", "included.mk", "build/injected.sh",
            ".containerignore", ".dockerignore",
            "components/trusted-actions/.containerignore",
            "components/trusted-actions/GNUmakefile",
            "components/trusted-actions/internal/injected.go",
            "lambda/create-investigation/injected.py",
            "terraform.rc",
        )
        for name in names:
            with self.subTest(path=name):
                path = env.work / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# synthetic unexpected deployment input\n")
                try:
                    with self.assertRaisesRegex(eph.Error, "changed"):
                        env.check_work()
                finally:
                    path.unlink()
                env.check_work()

    def test_generated_tfvars_are_verified_after_reload_and_before_destroy(self):
        env = self.environment
        path = env.root("ta-global") / "ephemeral.auto.tfvars.json"
        env.generated_input(path, {"backplane_client_secret": "nonsecret-placeholder"})
        env.check_work()
        self.assertEqual(env.generation_record()["generated_inputs"][path.relative_to(env.work).as_posix()],
                         eph.digest(path.read_bytes()))
        env.meta["roots"]["ta-global"].update(touched=True, generation=env.generation)
        env.save()
        original = path.read_bytes()
        for remove in (False, True):
            with self.subTest(remove=remove):
                if remove:
                    path.unlink()
                else:
                    path.write_text('{"backplane_client_secret":"unexpected-value"}')
                env.guard_account = Mock()
                env.ensure_quiescent = Mock()
                env.runner.run = Mock()
                try:
                    # destroy loads the journal and uses retained root inputs.
                    with self.assertRaisesRegex(eph.Error, "Generated deployment input"):
                        env.destroy(yes=True, account=self.config["account_id"])
                    env.runner.run.assert_not_called()
                finally:
                    path.write_bytes(original)
                env.check_work()

    def test_generated_tfvars_cannot_be_replaced_by_a_symlink(self):
        env = self.environment
        path = env.root("ta-global") / "ephemeral.auto.tfvars.json"
        env.generated_input(path, {"backplane_client_secret": "nonsecret-placeholder"})
        other = self.base / "same-input.json"
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
        with self.assertRaisesRegex(eph.Error, "Generated deployment input"):
            env.check_work()

    def test_authorized_tfvars_do_not_authorize_other_tfvars(self):
        env = self.environment
        path = env.root("ta-global") / "ephemeral.auto.tfvars.json"
        env.generated_input(path, {"backplane_client_secret": "nonsecret-placeholder"})
        env.check_work()
        (path.parent / "later.auto.tfvars.json").write_text("{}")
        with self.assertRaisesRegex(eph.Error, "changed"):
            env.check_work()

    def test_generated_terraform_outputs_are_allowed_only_with_verified_contents(self):
        env = self.environment
        root = env.root("boundary-regional")
        provider = root / ".terraform/providers/provider"
        archive = root / ".terraform/lambda/create-investigation.zip"
        lock = root / ".terraform.lock.hcl"
        provider.parent.mkdir(parents=True)
        archive.parent.mkdir(parents=True)
        provider.write_bytes(b"synthetic provider executable")
        provider.chmod(0o700)
        archive.write_bytes(b"synthetic Lambda ZIP")
        lock.write_text("# generated dependency lock\n")
        env.record_terraform_outputs("boundary-regional")
        env.check_work()
        env.load()
        env.check_work()
        for path in (provider, archive, lock):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(b"modified generated output")
                try:
                    with self.assertRaisesRegex(eph.Error, "changed"):
                        env.check_work()
                finally:
                    path.write_bytes(original)
                env.check_work()
        added = root / ".terraform/modules/injected/main.tf"
        added.parent.mkdir(parents=True)
        added.write_text("# unexpected executable module")
        with self.assertRaisesRegex(eph.Error, "cache changed"):
            env.check_work()

    def test_unrecorded_cache_cannot_hide_new_terraform_inputs(self):
        env = self.environment
        path = env.root("boundary-account") / ".terraform/modules/evil/main.tf"
        path.parent.mkdir(parents=True)
        path.write_text("# unexpected module")
        with self.assertRaisesRegex(eph.Error, "unrecorded"):
            env.check_work()

    def test_generated_lambda_dependencies_are_allowed_but_not_subsequent_tampering(self):
        env = self.environment
        dependency = env.work / "lambda/create-investigation/jwt/__init__.py"
        def build(*args, **kwargs):
            dependency.parent.mkdir()
            dependency.write_text("# synthetic installed dependency\n")
        env.runner.run = Mock(side_effect=build)
        env.build_lambda()
        env.check_work()
        env.build_lambda()
        env.runner.run.assert_called_once()
        dependency.write_text("# tampered dependency\n")
        with self.assertRaisesRegex(eph.Error, "Lambda deployment package changed"):
            env.check_work()
        with self.assertRaisesRegex(eph.Error, "Lambda deployment package changed"):
            env.build_lambda()
        env.runner.run.assert_called_once()

    def test_new_lambda_input_cannot_be_blessed_by_rebuilding(self):
        env = self.environment
        (env.work / "lambda/create-investigation/injected.py").write_text("# unreviewed input\n")
        env.runner.run = Mock()
        with self.assertRaisesRegex(eph.Error, "changed"):
            env.build_lambda()
        env.runner.run.assert_not_called()

    def test_unexpected_build_inputs_fail_before_publication(self):
        env = self.environment
        (env.work / "GNUmakefile").write_text("# unreviewed Make input\n")
        env.aws = Mock()
        env.runner.run = Mock()
        with self.assertRaisesRegex(eph.Error, "changed"):
            env.publish_images()
        env.aws.assert_not_called()
        env.runner.run.assert_not_called()

    def test_failed_lambda_build_checkpoints_partial_outputs_but_must_rebuild(self):
        env = self.environment
        dependency = env.work / "lambda/create-investigation/jwt/__init__.py"
        def fail_build(*args, **kwargs):
            dependency.parent.mkdir()
            dependency.write_text("# partial installed dependency\n")
            raise eph.Error("synthetic installer failure")
        env.runner.run = Mock(side_effect=fail_build)
        with self.assertRaisesRegex(eph.Error, "synthetic installer failure"):
            env.build_lambda()
        self.assertNotIn("lambda_package_fingerprint", env.generation_record())
        self.assertIn("lambda_build_fingerprint", env.generation_record())
        env.load()
        env.check_work()
        env.runner.run.side_effect = lambda *a, **kw: dependency.write_text("# complete dependency\n")
        env.build_lambda()
        self.assertEqual(env.runner.run.call_count, 2)
        self.assertNotIn("lambda_build_fingerprint", env.generation_record())
        self.assertIn("lambda_package_fingerprint", env.generation_record())
        env.check_work()
        env.build_lambda()
        self.assertEqual(env.runner.run.call_count, 2)

    def assert_failed_terraform_output_checkpoint(self, stage):
        env = self.environment
        env.guard_account = Mock()
        generated = env.root("ta-global") / ".terraform" / "partial-output"
        def tf(key, *args):
            if args[0] == stage:
                generated.parent.mkdir(exist_ok=True)
                generated.write_bytes(b"partial Terraform output")
                raise eph.Error("synthetic Terraform failure")
            return b""
        env.tf = Mock(side_effect=tf)
        with self.assertRaisesRegex(eph.Error, "synthetic Terraform failure"):
            env.plan_apply("ta-global", yes=True, account=self.config["account_id"])
        env.load()
        env.check_work()
        self.assertNotIn("pending_plan", env.meta["roots"]["ta-global"])
        self.assertFalse(any(call.args[1] == "apply" for call in env.tf.call_args_list))
        generated.write_bytes(b"tampered after failure")
        with self.assertRaisesRegex(eph.Error, "cache changed"):
            env.check_work()

    def test_failed_init_checkpoints_partial_cache_without_allowing_tampering(self):
        self.assert_failed_terraform_output_checkpoint("init")

    def test_failed_plan_checkpoints_generated_outputs_without_claiming_apply(self):
        self.assert_failed_terraform_output_checkpoint("plan")

    def test_file_modes_and_source_symlink_types_are_inputs(self):
        env = self.environment
        path = env.work / "tracked"
        path.chmod(path.stat().st_mode | 0o100)
        with self.assertRaisesRegex(eph.Error, "changed"):
            env.check_work()
        path.chmod(0o600)
        other = self.base / "same-source"
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
        with self.assertRaisesRegex(eph.Error, "changed"):
            env.check_work()

    def test_private_cli_config_cannot_enable_unreviewed_providers_or_credentials(self):
        env = self.environment
        (env.home / "terraform.rc").write_text('credentials "example.invalid" {}')
        with self.assertRaisesRegex(eph.Error, "CLI configuration changed"):
            env.check_work()

    def test_generated_input_journals_are_generation_specific(self):
        env = self.environment
        first = env.generation
        path = env.root("ta-global") / "ephemeral.auto.tfvars.json"
        env.generated_input(path, {"backplane_client_secret": "first-placeholder"})
        (self.repo / "tracked").write_text("second source generation")
        env.refresh_source()
        env.prepare()
        second = env.generation
        self.assertNotEqual(first, second)
        self.assertNotIn("generated_inputs", env.generation_record())
        env.generated_input(env.root("ta-global") / path.name,
                            {"backplane_client_secret": "second-placeholder"})
        env.check_work()
        env._generation = first
        try:
            env.check_work()
            path.write_text("{}")
            with self.assertRaisesRegex(eph.Error, "Generated deployment input changed"):
                env.check_work()
        finally:
            env._generation = None
        env.check_work()


class PrivateOverrideTests(Fixture):
    def test_ta_client_ids_and_backplane_url_are_secret_selectors_not_persisted_values(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.prepare()
        env.meta["images"]["ta"] = {"uri": "example@sha256:ta"}
        env.save()
        network = {"vpc_id": "vpc", "private_subnet_ids": ["private"],
                   "public_subnet_ids": ["public"]}
        variables = env.variables("ta-regional", network)
        env.generated_input(env.root("ta-regional") / "ephemeral.auto.tfvars.json", variables)
        env.aws = Mock(side_effect=[
            {"Tags": [{"Key": "EphemeralOwner", "Value": env.meta["owner"]}]},
            {"VersionId": "synthetic-version"},
        ])
        env.put_credentials("arn:aws:secretsmanager:us-west-2:123456789012:secret:owned",
                            self.credentials)
        self.assertEqual(json.loads(env.aws.call_args.kwargs["input"]),
                         {"ocm_client_id": self.credentials["client_id"],
                          "backplane_client_id": self.credentials["client_id"],
                          "ocm_client_secret": self.credentials["client_secret"],
                          "backplane_client_secret": self.credentials["client_secret"],
                          "backplane_url": self.credentials["backplane_url"],
                          "ocm_token": ""})
        override = json.loads((env.root("ta-regional") / "ephemeral_override.tf.json").read_text())
        self.assertEqual(override["locals"]["container_secrets"], [
            {"name": name, "valueFrom": "${local.global.secretsmanager_secret_arn}:" + field + "::"}
            for name, field in (
                ("ROSA_TA_OCM_CLIENT_SECRET", "ocm_client_secret"),
                ("ROSA_TA_OCM_TOKEN", "ocm_token"),
                ("ROSA_TA_BACKPLANE_CLIENT_SECRET", "backplane_client_secret"),
                ("ROSA_TA_OCM_CLIENT_ID", "ocm_client_id"),
                ("ROSA_TA_BACKPLANE_CLIENT_ID", "backplane_client_id"),
                ("ROSA_TA_BACKPLANE_URL", "backplane_url"),
            )
        ])
        app = json.loads(variables["app_config"])
        self.assertNotIn("client_id", app["ocm"])
        self.assertNotIn("client_id", app["backplane"])
        self.assertNotIn("base_url", app["backplane"])
        for path in env.home.rglob("*"):
            if path.is_file():
                for value in self.credentials.values():
                    self.assertNotIn(value.encode(), path.read_bytes(), str(path))
        env.check_work()

    def test_automatic_thumbprint_is_only_an_owned_provider_create_default(self):
        self.fake_source()
        del self.config["keycloak"]["thumbprint"]
        env = self.env()
        env.create()
        env.prepare()
        override = json.loads((env.root("boundary-account") / "ephemeral_override.tf.json").read_text())
        self.assertEqual(override["resource"], {
            "aws_iam_openid_connect_provider": {"keycloak": {"thumbprint_list": None}},
        })
        self.assertNotIn("module", override)
        self.assertNotIn("output", override)
        self.assertIsNone(env.variables("boundary-account")["keycloak_thumbprint"])
        env.check_work()

    def test_referenced_provider_never_changes_its_thumbprints(self):
        self.fake_source()
        del self.config["keycloak"]["thumbprint"]
        self.config["keycloak"]["existing_provider_arn"] = (
            "arn:aws:iam::123456789012:oidc-provider/identity.example.org/realms/dev")
        env = self.env()
        env.create()
        env.prepare()
        override = json.loads((env.root("boundary-account") / "ephemeral_override.tf.json").read_text())
        self.assertEqual(override["resource"], {
            "aws_iam_openid_connect_provider": {"keycloak": {"count": 0}},
        })
        self.assertEqual(override["module"]["shared_iam"]["keycloak_provider_arn"],
                         self.config["keycloak"]["existing_provider_arn"])
        env.check_work()

    def test_original_ta_secret_version_is_disabled_only_in_private_work(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.prepare()
        relative = eph.ROOTS["ta-global"] + "/secrets.tf"
        self.assertEqual((env.work / relative).read_bytes(), (env.source / relative).read_bytes())
        override = json.loads((env.root("ta-global") / "ephemeral_override.tf.json").read_text())
        self.assertEqual(override["resource"],
                         {"aws_secretsmanager_secret_version": {"app": {"count": 0}}})
        variables = env.variables("ta-global")
        self.assertNotIn("manage_secret_version", variables)
        self.assertEqual(variables["backplane_client_secret"], "script-populates-secrets-manager")
        env.check_work()

    def test_existing_primary_oidc_override_replaces_both_original_references(self):
        self.fake_source()
        arn = ("arn:aws:iam::123456789012:oidc-provider/"
               "identity.example.org/realms/dev")
        self.config["keycloak"]["existing_provider_arn"] = arn
        env = self.env()
        env.create()
        env.prepare()
        override = json.loads((env.root("boundary-account") / "ephemeral_override.tf.json").read_text())
        self.assertEqual(override["resource"],
                         {"aws_iam_openid_connect_provider": {"keycloak": {"count": 0}}})
        self.assertEqual(override["module"], {"shared_iam": {"keycloak_provider_arn": arn}})
        self.assertEqual(override["output"], {"oidc_provider_arn": {"value": arn}})
        self.assertNotIn("create_keycloak_oidc_provider", env.variables("boundary-account"))
        env.check_work()

    def test_create_primary_oidc_keeps_original_resource_and_references(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.prepare()
        override = json.loads((env.root("boundary-account") / "ephemeral_override.tf.json").read_text())
        self.assertEqual(set(override), {"terraform"})
        self.assertNotIn("create_keycloak_oidc_provider", env.variables("boundary-account"))
        env.check_work()

    def test_literal_and_suffixed_overrides_in_roots_and_local_modules_are_rejected(self):
        self.fake_source()
        env = self.env()
        env.create()
        root = env.source / eph.ROOTS["boundary-account"]
        nested = root / "modules"
        # Source snapshots are immutable in production; temporarily change only
        # this fixture to exercise prepare's input rejection independently.
        root.chmod(0o700)
        nested.mkdir()
        for parent in (root, nested):
            for name in (
                "override.tf", "override.tf.json", "extra_override.tf", "extra_override.tf.json",
                "terraform.tfvars", "terraform.tfvars.json",
                "extra.auto.tfvars", "extra.auto.tfvars.json",
            ):
                with self.subTest(parent=parent, name=name):
                    path = parent / name
                    path.write_text("{}")
                    try:
                        # Each attempt needs a fresh work copy, not a retry of a
                        # partially prepared generation.
                        with self.assertRaisesRegex(eph.Error, "implicit Terraform"):
                            env.prepare()
                    finally:
                        path.unlink()
                        # Remove only the generated test work tree.
                        shutil.rmtree(env.work)
