"""Offline regression tests: no AWS, Vault, podman, or Terraform credentials needed."""

import copy
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


import ephemeral_environment as eph
from ephemeral_environment import cli, runtime
from ephemeral_environment.snapshot import writable_copy


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.org")
        self.git("config", "user.name", "Test")
        (self.repo / ".gitignore").write_text("ignored\n.env\n*.tfstate\n")
        (self.repo / "tracked").write_text("original")
        self.git("add", ".gitignore", "tracked")
        self.git("commit", "-qm", "initial")
        self.config = {
            "account_id": "123456789012", "profile": "test-dev", "region": "us-west-2",
            "state_dir": str(self.base / "environments"),
            "keycloak": {"issuer_url": "https://identity.example.org/realms/dev",
                         "client_id": "boundary-dev", "thumbprint": "a" * 40,
                         "required_groups": ["dev-sre"], "required_oidc_role": "dev-sre",
                         "abac_tag_key": "uuid"},
            "vault": {"address": "https://vault.example.org", "path": "kv/data/dev"},
            "ocm": {"base_url": eph.INTEGRATION_OCM,
                    "jwk_cert_url": "https://sso.example.org/certs"},
            "ta_app_config": {"roles": [{"id": "SREP", "amsResource": "SrepResource"}],
                              "backplane": {},
                              "actions": {"aws_allowed_regions": ["us-west-2"],
                                          "allowed_namespaces": ["test"],
                                          "allowed_secrets": []}},
            "boundary_account": {"bedrock_monthly_budget_usd": 10,
                                 "bedrock_budget_notification_email": "test@example.org"},
            "ta_tls": {"internal_fqdn": "ta.example.org",
                       "certificate_arn": "arn:aws:acm:us-west-2:123456789012:certificate/test"},
        }
        self.credentials = {
            "client_id": "synthetic-service-id", "client_secret": "synthetic-service-secret",
            "backplane_url": "https://private-backplane.example.invalid",
        }

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.PIPE)

    def env(self, runner=None):
        env = eph.Environment(self.repo, self.config, "example", runner=runner)
        env.home.mkdir(parents=True, mode=0o700)
        return env

    def config_file(self, value=None):
        path = self.base / "config.json"
        path.write_text(json.dumps(value or self.config))
        return path

    def fake_source(self):
        for key, relative in eph.ROOTS.items():
            root = self.repo / relative
            root.mkdir(parents=True)
            (root / "main.tf").write_text('terraform {}\nprovider "aws" {}\n')
            variables = (
                "aws_account_id", "backplane_client_secret",
                "create_github_actions_oidc_provider", "adopt_existing_resources",
                "ephemeral_environment",
                "rta_server_url",
                "enable_lambda_ecr_source", "lambda_ecr_replication_regions",
                "bedrock_model_agreements", "enable_s3_replication_role",
                "bedrock_logging_source_regions", "replication",
            )
            (root / "variables.tf").write_text("\n".join(
                f'variable "{name}" {{ default = null }}' for name in variables))
            if key.startswith("boundary"):
                (root / "Makefile").write_text("init:\n\tterraform init\n")
        (self.repo / "components/trusted-actions/Makefile").write_text("build-amd64:\n\ttrue\n")
        (self.repo / "Makefile").write_text("build-amd64:\n\ttrue\n")
        with (self.repo / "Makefile").open("a") as makefile:
            makefile.write("\nbuild-cli:\n")
        (self.repo / "Containerfile").write_text(
            "FROM builder AS rta-builder\n"
            "COPY --from=rta-builder /out/rta /usr/local/bin/rta\n")
        (self.repo / "deploy/regional/ecs.tf").write_text("# RTA_ALLOW_INSECURE_HTTP\n")
        command = self.repo / "components/trusted-actions/internal/rta/command.go"
        command.parent.mkdir(parents=True)
        command.write_text("// allow-insecure-http\n")
        package = self.repo / "lambda/create-investigation"
        package.mkdir(parents=True)
        (package / "handler.py").write_text("# fake Lambda fixture\n")
        (self.repo / eph.ROOTS["ta-global"] / "secrets.tf").write_text(
            'resource "aws_secretsmanager_secret" "app" { name = "example" }\n'
            'resource "aws_secretsmanager_secret_version" "app" {\n'
            ' secret_id = aws_secretsmanager_secret.app.id\n'
            ' secret_string = jsonencode({ key = var.secret })\n}\n')
        (self.repo / eph.ROOTS["ta-regional"] / "remote_state.tf").write_text(
            'data "terraform_remote_state" "global" { backend = "remote" }\n')


class ConfigTests(Fixture):
    def test_invalid_json_reports_location_without_contents(self):
        path = self.base / "invalid-config.json"
        path.write_text('{\n  "sensitive": "synthetic-private-value"\n  "missing-comma": true\n}\n')
        with self.assertRaisesRegex(eph.Error, "Invalid config JSON at line 3, column 3") as caught:
            eph.load_config(path, self.repo)
        self.assertNotIn("synthetic-private-value", str(caught.exception))
        self.assertNotIn("missing-comma", str(caught.exception))

    def test_missing_config_reports_file_error_without_contents(self):
        with self.assertRaisesRegex(eph.Error, r"Cannot read config JSON \(FileNotFoundError\)"):
            eph.load_config(self.base / "missing-config.json", self.repo)

    def test_invalid_config_encoding_reports_safe_error(self):
        path = self.base / "invalid-config.json"
        path.write_bytes(b'{"sensitive": "\xff"}')
        with self.assertRaisesRegex(eph.Error, "Config JSON must be UTF-8 text"):
            eph.load_config(path, self.repo)

    def test_cli_uses_package_environment_without_compatibility_aliases(self):
        environment = Mock()
        environment.locked.return_value.__enter__ = Mock()
        environment.locked.return_value.__exit__ = Mock(return_value=False)
        environment.approval_scope.return_value.__enter__ = Mock()
        environment.approval_scope.return_value.__exit__ = Mock(return_value=False)
        environment.meta = {
            "status": "created", "source": {}, "images": {},
        }
        with patch.object(cli, "Environment", return_value=environment) as constructor:
            with patch.object(cli, "load_config", return_value=self.config):
                with patch.object(cli.os, "umask"), patch("builtins.print"):
                    self.assertEqual(cli.main(["create", "--config", str(self.config_file()),
                                               "--name", "example"]), 0)
        self.assertEqual(constructor.call_args.args[0], Path(__file__).resolve().parents[1])
        environment.create.assert_called_once_with(None)
        environment.approval_scope.assert_called_once_with("create")
        environment.deploy.assert_not_called()

    def test_explicit_config(self):
        result = eph.load_config(self.config_file(), self.repo)
        self.assertEqual(result["account_id"], self.config["account_id"])
        self.assertEqual(result["network"], {})

    def test_ocm_jwks_defaults_to_production_sso(self):
        del self.config["ocm"]["jwk_cert_url"]
        result = eph.load_config(self.config_file(), self.repo)
        self.assertEqual(
            result["ocm"]["jwk_cert_url"],
            "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs",
        )
        self.assertEqual(result["ocm"]["base_url"], eph.INTEGRATION_OCM)
        self.config["ocm"]["jwk_cert_url"] = result["ocm"]["jwk_cert_url"]
        self.assertEqual(result, eph.load_config(self.config_file(), self.repo))

    def test_ocm_jwks_override_is_preserved(self):
        result = eph.load_config(self.config_file(), self.repo)
        self.assertEqual(result["ocm"]["jwk_cert_url"], self.config["ocm"]["jwk_cert_url"])

    def test_ocm_jwks_override_must_be_valid_https(self):
        for value in (None, "", "http://sso.example.org/certs",
                      "https://user:placeholder@sso.example.org/certs",
                      "https://sso.example.org/certs?token=placeholder"):
            with self.subTest(value=value):
                self.config["ocm"]["jwk_cert_url"] = value
                with self.assertRaisesRegex(eph.Error, "OCM jwk_cert_url"):
                    eph.load_config(self.config_file(), self.repo)

    def test_account_is_required_and_never_defaulted(self):
        for account in (None, "", "123", 123456789012):
            with self.subTest(account=account):
                config = copy.deepcopy(self.config)
                config["account_id"] = account
                with self.assertRaises(eph.Error):
                    eph.load_config(self.config_file(config), self.repo)

    def test_reject_unknown_field_and_production_ocm(self):
        for change in ({"unexpected": True}, {"ocm": {"base_url": "https://api.openshift.com",
                                                     "jwk_cert_url": "https://example.org/certs"}}):
            with self.subTest(change=change):
                config = copy.deepcopy(self.config)
                config.update(change)
                with self.assertRaises(eph.Error):
                    eph.load_config(self.config_file(config), self.repo)

    def test_reject_state_inside_repository(self):
        self.config["state_dir"] = str(self.repo / "state")
        with self.assertRaises(eph.Error):
            eph.load_config(self.config_file(), self.repo)

    def test_plaintext_credentials_rejected(self):
        self.config["ta_app_config"]["backplane"] = {"client_secret": "do-not-print"}
        with self.assertRaisesRegex(eph.Error, "Plaintext"):
            eph.load_config(self.config_file(), self.repo)

    def test_external_keycloak_urls_are_explicit(self):
        self.config["keycloak"]["issuer_url"] = "https://user:password@example.org/realm"
        with self.assertRaises(eph.Error):
            eph.load_config(self.config_file(), self.repo)

    def test_ta_scope_is_single_region(self):
        self.config["ta_app_config"]["actions"]["aws_allowed_regions"].append("eu-west-1")
        with self.assertRaises(eph.Error):
            eph.load_config(self.config_file(), self.repo)

    def test_tls_certificate_cannot_target_another_account(self):
        self.config["ta_tls"]["certificate_arn"] = "arn:aws:acm:us-west-2:999999999999:certificate/test"
        with self.assertRaisesRegex(eph.Error, "certificate"):
            eph.load_config(self.config_file(), self.repo)

    def test_backplane_url_must_not_be_in_config(self):
        for value in ("https://backplane.example.org", "http://backplane.example.org", None, ""):
            with self.subTest(value=value):
                self.config["ta_app_config"]["backplane"] = {"base_url": value}
                with self.assertRaisesRegex(eph.Error, "must come from Vault"):
                    eph.load_config(self.config_file(), self.repo)

    def test_backplane_config_can_be_omitted(self):
        del self.config["ta_app_config"]["backplane"]
        eph.load_config(self.config_file(), self.repo)

    def test_service_client_ids_must_come_from_vault(self):
        for key in ("client_id", "clientId", "client-id"):
            with self.subTest(key=key):
                config = copy.deepcopy(self.config)
                config["ta_app_config"]["backplane"][key] = "synthetic-private-id"
                with self.assertRaisesRegex(eph.Error, "Plaintext credentials"):
                    eph.load_config(self.config_file(config), self.repo)

    def test_separate_backplane_credential_selectors_are_rejected(self):
        for key in ("backplane_client_id_key", "backplane_secret_key"):
            with self.subTest(key=key):
                config = copy.deepcopy(self.config)
                config["vault"][key] = "separate-service-field"
                with self.assertRaisesRegex(eph.Error, "vault has missing or unsupported fields"):
                    eph.load_config(self.config_file(config), self.repo)

    def test_ambient_configuration_is_not_inherited(self):
        poisoned = {"TF_VAR_stage": "prod", "TF_CLI_ARGS": "-destroy", "MAKEFLAGS": "-e",
                    "AWS_ACCESS_KEY_ID": "wrong-account-key", "AWS_ENDPOINT_URL": "http://evil",
                    "VAULT_TOKEN": "vault-secret", "BASH_ENV": "/bad/script",
                    "GITHUB_TOKEN": "github-secret"}
        with patch.dict(os.environ, poisoned):
            env = eph.clean_environment(self.config)
        for key in poisoned:
            self.assertNotIn(key, env)
        self.assertEqual(env["AWS_PROFILE"], "test-dev")

    def test_build_processes_do_not_inherit_aws_or_vault_environment(self):
        runner = eph.Runner(self.config)
        result = subprocess.CompletedProcess([], 0, b"", b"")
        with patch.dict(os.environ, {"VAULT_TOKEN": "never-pass", "AWS_SECRET_ACCESS_KEY": "never-pass"}):
            with patch.object(runtime.subprocess, "run", return_value=result) as run:
                runner.run(["make", "image"], build=True)
        actual = run.call_args.kwargs["env"]
        self.assertNotIn("VAULT_TOKEN", actual)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", actual)
        self.assertNotIn("AWS_PROFILE", actual)
        self.assertNotIn("AWS_REGION", actual)


class ConfigDefaultTests(Fixture):
    def minimal_config(self):
        config = copy.deepcopy(self.config)
        for key in ("profile", "state_dir", "ocm", "ta_tls"):
            del config[key]
        for key in ("client_id", "abac_tag_key", "required_groups", "required_oidc_role", "thumbprint"):
            del config["keycloak"][key]
        config["vault"] = {"path": config["vault"]["path"]}
        del config["boundary_account"]["bedrock_monthly_budget_usd"]
        config["ta_app_config"]["actions"] = {"allowed_namespaces": ["test"]}
        return config

    def load(self, config):
        return eph.load_config(self.config_file(config), self.repo)

    def test_minimal_configuration_defaults(self):
        state = self.base / "xdg-state"
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(state),
                                     "VAULT_ADDR": "https://vault.example.org",
                                     "AWS_PROFILE": "never-inherit"}):
            loaded = self.load(self.minimal_config())
        self.assertEqual(loaded["profile"], "default")
        self.assertEqual(eph.clean_environment(loaded)["AWS_PROFILE"], "default")
        self.assertEqual(loaded["state_dir"], str(state / "rosa-ephemeral"))
        self.assertFalse(state.exists())
        self.assertNotIn("ta_tls", loaded)
        self.assertEqual(loaded["ocm"], {
            "base_url": eph.INTEGRATION_OCM,
            "jwk_cert_url": "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/certs",
        })
        self.assertEqual(loaded["keycloak"]["client_id"], "rosa-boundary-sre")
        self.assertEqual(loaded["keycloak"]["abac_tag_key"], "username")
        self.assertEqual(loaded["keycloak"]["required_groups"],
                         ["ai-sd-sre", "eng-openshift-enterprise", "sre-team"])
        self.assertEqual(loaded["keycloak"]["required_oidc_role"], "ai-sd-sre")
        self.assertNotIn("thumbprint", loaded["keycloak"])
        self.assertEqual(loaded["vault"], {
            "address": "https://vault.example.org", "path": "kv/data/dev",
        })
        self.assertEqual(loaded["boundary_account"], {
            "bedrock_monthly_budget_usd": 25,
            "bedrock_budget_notification_email": "test@example.org",
        })
        self.assertEqual(loaded["ta_app_config"]["actions"], {
            "allowed_namespaces": ["test"], "aws_allowed_regions": ["us-west-2"],
        })
        self.assertEqual(loaded["network"], {})
        for key in ("workers", "aws"):
            self.assertNotIn(key, loaded["ta_app_config"])

    def test_full_existing_configuration_preserves_canonical_hash(self):
        for optional_lists in (False, True):
            with self.subTest(optional_lists=optional_lists):
                config = copy.deepcopy(self.config)
                if optional_lists:
                    config["ta_app_config"]["actions"]["aws_allowed_operations"] = ["reboot"]
                else:
                    del config["ta_app_config"]["actions"]["allowed_secrets"]
                # The old loader already inserted network={} and missing JWKS.
                expected = copy.deepcopy(config)
                expected["network"] = {}
                with patch.dict(os.environ, {"VAULT_ADDR": "http://ignored.invalid",
                                             "AWS_PROFILE": "ignored",
                                             "XDG_STATE_HOME": "ignored-relative"}):
                    loaded = self.load(config)
                self.assertEqual(eph.canonical(loaded), eph.canonical(expected))
                self.assertEqual(eph.digest(eph.canonical(loaded)),
                                 eph.digest(eph.canonical(expected)))
                self.assertNotIn("reference_existing_github_oidc", loaded["boundary_account"])

    def test_defaults_reach_existing_terraform_and_vault_consumers(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"),
                                     "VAULT_ADDR": "https://vault.example.org"}):
            self.config = self.load(self.minimal_config())
        runner = Mock()
        runner.json.return_value = {"data": {"data": {
            "trusted_actions_sa_client_id": "synthetic-id",
            "trusted_actions_sa_client_secret": "synthetic-ocm",
            "trusted_actions_backplane_url": self.credentials["backplane_url"],
        }}}
        env = self.env(runner)
        self.assertEqual(env.vault_credentials(), {
            "client_id": "synthetic-id", "client_secret": "synthetic-ocm",
            "backplane_url": self.credentials["backplane_url"],
        })
        env.meta = {"owner": "test-owner", "images": {"ta": {"uri": "example@sha256:test"}}}
        account = env.variables("boundary-account")
        self.assertEqual(account["bedrock_monthly_budget_usd"], 25)
        self.assertEqual(account["oidc_client_id"], "rosa-boundary-sre")
        self.assertIsNone(account["keycloak_thumbprint"])
        self.assertEqual(account["required_oidc_role"], "ai-sd-sre")
        env.meta["images"]["boundary"] = {"uri": "example@sha256:boundary"}
        env.outputs = Mock(return_value={
            "internal_fqdn": None, "alb_dns_name": "internal-ta-123.us-west-2.elb.amazonaws.com",
        })
        regional = env.variables("boundary-regional", {
            "vpc_id": "vpc-test", "private_subnet_ids": ["private-a", "private-b"],
        })
        self.assertIsNone(regional["keycloak_thumbprint"])
        self.assertEqual(regional["abac_tag_key"], "username")
        self.assertEqual(regional["required_groups"],
                         ["ai-sd-sre", "eng-openshift-enterprise", "sre-team"])
        self.assertTrue(account["enable_oidc_group_enforcement"])
        self.assertTrue(regional["enable_oidc_group_enforcement"])
        ta = env.variables("ta-regional", {
            "vpc_id": "vpc-test", "public_subnet_ids": ["public-a", "public-b"],
            "private_subnet_ids": ["private-a", "private-b"],
        })
        app = json.loads(ta["app_config"])
        self.assertEqual(app["ocm"], {"base_url": eph.INTEGRATION_OCM})
        self.assertNotIn("client_id", app["backplane"])
        self.assertEqual(app["actions"]["aws_allowed_regions"], ["us-west-2"])
        self.assertNotIn("allowed_secrets", app["actions"])
        self.assertNotIn("aws_allowed_operations", app["actions"])

    def test_omitted_and_explicit_defaults_reuse_owned_snapshot(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state"),
                                     "VAULT_ADDR": "https://vault.example.org"}):
            omitted = self.minimal_config()
            self.config = self.load(omitted)
            explicit = copy.deepcopy(self.config)
            env = self.env()
            env.create()
            for config in (explicit, omitted):
                env.config = self.load(config)
                self.assertEqual(env.load()["config_hash"], eph.digest(eph.canonical(self.config)))
            # Required identity still participates in the ownership hash.
            env.config["keycloak"]["required_oidc_role"] = "different-role"
            with self.assertRaisesRegex(eph.Error, "ownership/configuration mismatch"):
                env.load()

    def test_state_home_unset_or_empty_uses_home(self):
        config = copy.deepcopy(self.config)
        del config["state_dir"]
        for xdg in (None, ""):
            with self.subTest(xdg=xdg), patch.dict(os.environ, {"HOME": str(self.base)}):
                os.environ.pop("XDG_STATE_HOME", None)
                if xdg is not None:
                    os.environ["XDG_STATE_HOME"] = xdg
                self.assertEqual(self.load(config)["state_dir"],
                                 str(self.base / ".local/state/rosa-ephemeral"))

    def test_partial_ocm_override_defaults_only_missing_fields(self):
        config = copy.deepcopy(self.config)
        config["ocm"] = {"jwk_cert_url": "https://custom.example.org/certs"}
        self.assertEqual(self.load(config)["ocm"], {
            "base_url": eph.INTEGRATION_OCM,
            "jwk_cert_url": "https://custom.example.org/certs",
        })

    def test_empty_ocm_object_matches_omitted_defaults(self):
        config = copy.deepcopy(self.config)
        config["ocm"] = {}
        explicit = self.load(config)
        del config["ocm"]
        self.assertEqual(self.load(config), explicit)

    def test_state_home_relative_or_inside_repository_rejected(self):
        config = copy.deepcopy(self.config)
        del config["state_dir"]
        for xdg in ("relative", "~/state", str(self.repo / "state")):
            with self.subTest(xdg=xdg), patch.dict(os.environ, {"XDG_STATE_HOME": xdg}):
                with self.assertRaisesRegex(eph.Error, "XDG_STATE_HOME|state_dir"):
                    self.load(config)

    def test_explicit_state_supports_tilde_but_not_relative_or_repository_paths(self):
        config = copy.deepcopy(self.config)
        with patch.dict(os.environ, {"HOME": str(self.base)}):
            config["state_dir"] = "~/private-state"
            self.assertEqual(self.load(config)["state_dir"], str(self.base / "private-state"))
        for state in ("relative", str(self.repo / "state")):
            config["state_dir"] = state
            with self.subTest(state=state), self.assertRaises(eph.Error):
                self.load(config)

    def test_vault_address_requires_explicit_value_or_valid_environment(self):
        config = copy.deepcopy(self.config)
        del config["vault"]["address"]
        for address in (None, "", "http://vault.example.org",
                        "https://user:synthetic@vault.example.org",
                        "https://vault.example.org?token=synthetic",
                        "https://[malformed"):
            with self.subTest(address=address), patch.dict(os.environ):
                os.environ.pop("VAULT_ADDR", None)
                if address is not None:
                    os.environ["VAULT_ADDR"] = address
                with self.assertRaisesRegex(eph.Error, "Vault address"):
                    self.load(config)

    def test_defaulted_values_reject_explicit_null_empty_and_wrong_types(self):
        paths = (
            ("profile",), ("state_dir",), ("ocm",), ("ocm", "base_url"),
            ("ocm", "jwk_cert_url"), ("keycloak", "client_id"),
            ("keycloak", "abac_tag_key"), ("vault", "address"),
            ("keycloak", "required_groups"), ("keycloak", "required_oidc_role"),
            ("keycloak", "thumbprint"),
            ("boundary_account", "bedrock_monthly_budget_usd"),
            ("ta_app_config", "actions", "aws_allowed_regions"),
        )
        for path in paths:
            invalid_values = (None, "", False, []) if path == ("ocm",) else (None, "", False, {}, [])
            for invalid in invalid_values:
                with self.subTest(path=path, invalid=invalid):
                    config = copy.deepcopy(self.config)
                    target = config
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = invalid
                    with patch.dict(os.environ, {"VAULT_ADDR": "https://valid.example.org"}):
                        with self.assertRaises(eph.Error):
                            self.load(config)

    def test_authentication_target_and_recipient_remain_explicit(self):
        paths = (
            ("account_id",), ("region",), ("keycloak",), ("vault",),
            ("boundary_account",),
            ("keycloak", "issuer_url"),
            ("vault", "path"), ("boundary_account", "bedrock_budget_notification_email"),
            ("ta_tls", "internal_fqdn"), ("ta_tls", "certificate_arn"),
        )
        for path in paths:
            with self.subTest(path=path):
                config = copy.deepcopy(self.config)
                target = config
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
                with self.assertRaises(eph.Error):
                    self.load(config)

    def test_action_lists_validate_without_inventing_policy(self):
        for key in ("allowed_namespaces", "allowed_secrets", "aws_allowed_operations"):
            for invalid in (None, "", "test", {}, [None], [""], ["  "], [123]):
                with self.subTest(key=key, invalid=invalid):
                    config = copy.deepcopy(self.config)
                    config["ta_app_config"]["actions"][key] = invalid
                    with self.assertRaises(eph.Error):
                        self.load(config)
        self.config["ta_app_config"]["actions"]["allowed_namespaces"] = []
        self.assertEqual(self.load(self.config)["ta_app_config"]["actions"]["allowed_namespaces"], [])

    def test_missing_namespace_policy_defaults_to_permissive(self):
        del self.config["ta_app_config"]["actions"]["allowed_namespaces"]
        self.assertEqual(self.load(self.config)["ta_app_config"]["actions"]["allowed_namespaces"], [])

    def test_actions_block_is_optional_with_stage_namespace_defaults(self):
        for actions in (None, {}):
            with self.subTest(actions=actions):
                config = copy.deepcopy(self.config)
                if actions is None:
                    del config["ta_app_config"]["actions"]
                else:
                    config["ta_app_config"]["actions"] = actions
                loaded = self.load(config)["ta_app_config"]["actions"]
                self.assertEqual(loaded["allowed_namespaces"], [])
                self.assertEqual(loaded.get("allowed_secrets", []), [])
                self.assertEqual(loaded["aws_allowed_regions"], ["us-west-2"])
                self.assertEqual(loaded.get("aws_allowed_operations", []), [])

    def test_explicit_invalid_actions_block_is_rejected(self):
        for actions in (None, "", [], False):
            with self.subTest(actions=actions):
                config = copy.deepcopy(self.config)
                config["ta_app_config"]["actions"] = actions
                with self.assertRaises(eph.Error):
                    self.load(config)

    def test_omitted_roles_default_to_srep_only(self):
        config = copy.deepcopy(self.config)
        del config["ta_app_config"]["roles"]
        loaded = self.load(config)
        self.assertEqual(loaded["ta_app_config"]["roles"], [
            {"id": "SREP", "amsResource": "BackplaneOsdSrepResource"},
        ])
        env = eph.Environment(self.repo, loaded, "roles")
        env.meta = {"owner": "test-owner", "images": {"ta": {"uri": "ta@sha256:test"}}}
        variables = env.variables("ta-regional", {
            "vpc_id": "vpc-test", "public_subnet_ids": ["public-a", "public-b"],
            "private_subnet_ids": ["private-a", "private-b"],
        })
        self.assertEqual(json.loads(variables["app_config"])["roles"],
                         loaded["ta_app_config"]["roles"])

    def test_ta_app_config_is_optional_and_matches_explicit_defaults(self):
        expected_roles = [{"id": "SREP", "amsResource": "BackplaneOsdSrepResource"}]
        results = []
        for app in (None, {}, {"roles": expected_roles}):
            config = copy.deepcopy(self.config)
            if app is None:
                del config["ta_app_config"]
            else:
                config["ta_app_config"] = app
            loaded = self.load(config)
            self.assertEqual(loaded["ta_app_config"]["roles"], expected_roles)
            self.assertEqual(loaded["ta_app_config"]["actions"]["allowed_namespaces"], [])
            results.append(eph.canonical(loaded))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])

    def test_explicit_roles_are_preserved_in_order(self):
        roles = [
            {"id": "ROSAAiAgent", "amsResource": "BackplaneOsdAiAgentResource"},
            {"id": "SREP", "amsResource": "BackplaneOsdSrepResource"},
        ]
        self.config["ta_app_config"]["roles"] = roles
        self.assertEqual(self.load(self.config)["ta_app_config"]["roles"], roles)

    def test_invalid_explicit_roles_and_app_config_do_not_use_defaults(self):
        for key in ("roles", "ta_app_config"):
            for invalid in (None, "", False, [], "SREP"):
                with self.subTest(key=key, invalid=invalid):
                    config = copy.deepcopy(self.config)
                    if key == "roles":
                        config["ta_app_config"]["roles"] = invalid
                    else:
                        config[key] = invalid
                    with self.assertRaises(eph.Error):
                        self.load(config)
        config = copy.deepcopy(self.config)
        config["ta_app_config"]["roles"] = {}
        with self.assertRaises(eph.Error):
            self.load(config)

    def test_defaultable_sections_reject_typo_fields(self):
        for section in (None, "keycloak", "vault", "ocm", "boundary_account", "network", "actions"):
            with self.subTest(section=section):
                config = copy.deepcopy(self.config)
                target = (config if section is None else config["ta_app_config"]["actions"]
                          if section == "actions" else config.setdefault(section, {}))
                target["typo_field"] = "not-a-default"
                with self.assertRaisesRegex(eph.Error, "unsupported"):
                    self.load(config)

    def test_auth_disabling_and_plaintext_credentials_remain_forbidden(self):
        for key, value in (("auth", "disabled"), ("enable_auth", False),
                           ("client_secret", "synthetic")):
            with self.subTest(key=key):
                config = copy.deepcopy(self.config)
                config["ta_app_config"][key] = value
                with self.assertRaises(eph.Error):
                    self.load(config)

    def test_budget_must_be_positive_finite_number(self):
        for budget in (True, False, 0, -1, "25", float("inf"), float("nan")):
            with self.subTest(budget=budget):
                config = copy.deepcopy(self.config)
                config["boundary_account"]["bedrock_monthly_budget_usd"] = budget
                with self.assertRaisesRegex(eph.Error, "positive Bedrock budget"):
                    self.load(config)

    def test_integer_budget_does_not_require_float_conversion(self):
        self.config["boundary_account"]["bedrock_monthly_budget_usd"] = 10 ** 400
        self.assertEqual(self.load(self.config)["boundary_account"]["bedrock_monthly_budget_usd"],
                         10 ** 400)


class SnapshotTests(Fixture):
    def test_dirty_snapshot_uses_worktree_not_index_and_untracked(self):
        (self.repo / "tracked").write_text("staged")
        self.git("add", "tracked")
        (self.repo / "tracked").write_text("unstaged")
        (self.repo / "untracked").write_text("untracked source")
        (self.repo / "ignored").write_text("credential")
        (self.repo / ".env").write_text("credential")
        target = self.base / "snapshot"
        result = eph.snapshot(self.repo, target)
        self.assertEqual((target / "tracked").read_text(), "unstaged")
        self.assertTrue((target / "untracked").is_file())
        self.assertFalse((target / "ignored").exists())
        self.assertFalse((target / ".env").exists())
        self.assertEqual(result["selection"], "working-tree")
        self.assertEqual(result["fingerprint"], eph.digest(eph.canonical(eph.tree_manifest(target))))

    def test_tracked_ignored_inputs_are_retained_from_working_tree(self):
        (self.repo / "ignored").write_text("staged build input")
        self.git("add", "-f", "ignored")
        (self.repo / "ignored").write_text("unstaged build input")
        target = self.base / "snapshot"
        eph.snapshot(self.repo, target)
        self.assertEqual((target / "ignored").read_text(), "unstaged build input")

    def test_commit_snapshot_ignores_dirty_tree(self):
        (self.repo / "tracked").write_text("dirty")
        (self.repo / "untracked").write_text("new")
        target = self.base / "snapshot"
        eph.snapshot(self.repo, target, "HEAD")
        self.assertEqual((target / "tracked").read_text(), "original")
        self.assertFalse((target / "untracked").exists())
        self.assertFalse((target / ".git").exists())

    def test_commit_snapshot_retains_force_added_ignored_inputs(self):
        (self.repo / "ignored").write_text("committed build input")
        self.git("add", "-f", "ignored")
        self.git("commit", "-qm", "force added build input")
        (self.repo / "ignored").write_text("dirty build input")
        target = self.base / "snapshot"
        eph.snapshot(self.repo, target, "HEAD")
        self.assertEqual((target / "ignored").read_text(), "committed build input")

    def test_tracked_ignored_signature_keys_retain_exact_bytes_in_both_modes(self):
        repository = Path(__file__).resolve().parents[1]
        names = ("build/keys/aws-cli.asc", "build/keys/redhat-release-2.asc")
        expected = {name: (repository / name).read_bytes() for name in names}
        (self.repo / ".gitignore").write_text("keys/\n")
        for name, data in expected.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.git("add", ".gitignore")
        self.git("add", "-f", *names)
        self.git("commit", "-qm", "tracked public signature verification keys")
        untracked = self.repo / "build/keys/untracked.asc"
        untracked.write_bytes(b"ignored untracked build output")
        for revision in (None, "HEAD"):
            with self.subTest(revision=revision):
                target = self.base / ("working-tree" if revision is None else "commit")
                eph.snapshot(self.repo, target, revision)
                for name, data in expected.items():
                    with self.subTest(key=name):
                        self.assertEqual((target / name).read_bytes(), data)
                self.assertFalse((target / untracked.relative_to(self.repo)).exists())

    def test_snapshot_preserves_internal_symlink(self):
        (self.repo / "link").symlink_to("tracked")
        target = self.base / "snapshot"
        eph.snapshot(self.repo, target)
        self.assertTrue((target / "link").is_symlink())
        self.assertEqual(os.readlink(target / "link"), "tracked")

    def test_writable_copy_normalizes_legacy_private_readonly_source(self):
        private = self.base / "private"
        private.mkdir(mode=0o700)
        source = private / "source"
        nested = source / "nested"
        nested.mkdir(parents=True)
        contents = {"input": b"legacy source input\n", "tool": b"#!/bin/sh\nexit 0\n"}
        for name, data in contents.items():
            path = nested / name
            path.write_bytes(data)
            path.chmod(0o500 if name == "tool" else 0o400)
        nested.chmod(0o500)
        source.chmod(0o500)
        target = private / "work"
        previous = os.umask(0o077)
        try:
            writable_copy(source, target)
        finally:
            os.umask(previous)
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        for original, copied in ((source, target), (nested, target / "nested")):
            self.assertEqual(original.stat().st_mode & 0o777, 0o500)
            self.assertEqual(copied.stat().st_mode & 0o777, 0o755)
        for name, data in contents.items():
            with self.subTest(path=name):
                original = nested / name
                copied = target / "nested" / name
                self.assertEqual(original.stat().st_mode & 0o777,
                                 0o500 if name == "tool" else 0o400)
                self.assertEqual(copied.stat().st_mode & 0o777,
                                 0o755 if name == "tool" else 0o644)
                self.assertEqual(copied.read_bytes(), data)
                copied.write_bytes(b"changed only in work copy\n")
                self.assertEqual(original.read_bytes(), data)

    def test_snapshot_rejects_escaping_symlink(self):
        (self.repo / "link").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(eph.Error, "escaping"):
            eph.snapshot(self.repo, self.base / "snapshot")

    def test_fingerprint_includes_deletions_modes_and_symlink_targets(self):
        fingerprints = []
        for number in range(4):
            target = self.base / f"snapshot-{number}"
            if number == 1:
                (self.repo / "tracked").chmod(0o755)
            elif number == 2:
                (self.repo / "link").symlink_to("tracked")
            elif number == 3:
                (self.repo / "tracked").unlink()
                (self.repo / "link").unlink()
            fingerprints.append(eph.snapshot(self.repo, target)["fingerprint"])
        self.assertEqual(len(set(fingerprints)), 4)

    def test_private_state_and_snapshot_tamper_detection(self):
        env = self.env()
        env.create()
        self.assertEqual((env.home / "environment.json").stat().st_mode & 0o777, 0o600)
        target = env.home / "source" / "tracked"
        target.chmod(0o600)
        target.write_text("tampered")
        with self.assertRaisesRegex(eph.Error, "modified"):
            env.load()

    def test_configuration_cannot_change_after_creation(self):
        env = self.env()
        env.create()
        env.config = copy.deepcopy(env.config)
        env.config["region"] = "eu-west-1"
        env.aws = Mock()
        with self.assertRaisesRegex(eph.Error, "mismatch") as caught:
            env.load()
        message = str(caught.exception)
        self.assertIn("Changed configuration fields/sections: region.", message)
        self.assertIn(str(env.home / "environment.json"), message)
        self.assertIn(str(env.home / "config.json"), message)
        self.assertIn("before AWS preflight", message)
        self.assertIn("even if deployment never created AWS resources", message)
        self.assertIn("EPHEMERAL_NAME=<new-name>", message)
        self.assertIn("Deleting AWS resources will not clear this local mismatch", message)
        self.assertNotIn("eu-west-1", message)
        self.assertNotIn("us-west-2", message)
        env.aws.assert_not_called()

    def test_config_mismatch_guidance_does_not_disclose_private_values(self):
        env = self.env()
        env.create()
        env.config = copy.deepcopy(env.config)
        env.config["vault"]["path"] = "private/changed-vault-reference"
        with self.assertRaises(eph.Error) as caught:
            env.load()
        message = str(caught.exception)
        self.assertIn("Changed configuration fields/sections: vault.", message)
        for value in ("private/changed-vault-reference", "kv/data/dev",
                      "https://vault.example.org", "https://identity.example.org"):
            self.assertNotIn(value, message)

    def test_config_mismatch_guidance_handles_missing_or_unverified_original_config(self):
        env = self.env()
        env.create()
        env.config = copy.deepcopy(env.config)
        env.config["region"] = "eu-west-1"
        original = env.home / "config.json"
        for contents in (None, "not JSON", "[]", '{"region":"untrusted-private-value"}'):
            with self.subTest(contents=contents):
                if contents is None:
                    original.unlink(missing_ok=True)
                else:
                    original.write_text(contents)
                with self.assertRaisesRegex(eph.Error, "mismatch") as caught:
                    env.load()
                self.assertNotIn("Changed configuration fields/sections:", str(caught.exception))
                self.assertNotIn("untrusted-private-value", str(caught.exception))
                self.assertIn("Deleting AWS resources will not clear", str(caught.exception))

    def test_no_automatic_adoption_or_resnapshot(self):
        env = self.env()
        with self.assertRaisesRegex(eph.Error, "refusing adoption"):
            env.load()
        env.create()
        with self.assertRaisesRegex(eph.Error, "already exists"):
            env.create()

    def test_state_directory_must_be_private(self):
        directory = self.base / "public"
        directory.mkdir(mode=0o755)
        with self.assertRaisesRegex(eph.Error, "0700"):
            eph.private_directory(directory)


class SafetyTests(Fixture):
    def test_account_guard_before_mutation(self):
        env = self.env()
        env.aws = Mock(return_value={"Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/dev"})
        with self.assertRaisesRegex(eph.Error, "does not match"):
            env.guard_account()

    def test_yes_requires_explicit_matching_account(self):
        env = self.env()
        env.aws = Mock()
        with self.assertRaisesRegex(eph.Error, "--account-id"):
            env.guard_account(yes=True)
        env.aws.assert_not_called()

    def test_noninteractive_execution_never_autoapproves(self):
        env = self.env()
        with patch("sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(eph.Error, "declined"):
                env.approve("review", False)

    def test_state_hash_prevents_adoption_and_tamper(self):
        env = self.env()
        env.meta = {"roots": {}}
        state = env.home / "ta-global.tfstate"
        state.write_text("{}")
        with self.assertRaisesRegex(eph.Error, "ownership"):
            env.state_check("ta-global")
        env.record_state("ta-global")
        env.state_check("ta-global")
        state.write_text('{"tamper":true}')
        with self.assertRaisesRegex(eph.Error, "ownership"):
            env.state_check("ta-global")

    def test_copied_roots_use_only_local_state_without_provider_overrides(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.prepare()
        env.check_work()
        for key in eph.ROOTS:
            override = json.loads((env.root(key) / "ephemeral_override.tf.json").read_text())
            self.assertNotIn("provider", override)
            self.assertEqual(set(override["terraform"]["backend"]), {"local"})
            expected = {"terraform"}
            if key == "ta-regional":
                expected.update({"data", "locals"})
            elif key == "ta-global":
                expected.add("resource")
            self.assertEqual(set(override), expected)
        override = json.loads((env.root("ta-regional") / "ephemeral_override.tf.json").read_text())
        remote = override["data"]["terraform_remote_state"]["global"]
        self.assertEqual(remote, {"backend": "local",
                                 "config": {"path": str(env.home / "ta-global.tfstate")}})
        secrets = (env.root("ta-global") / "secrets.tf").read_text()
        self.assertIn('aws_secretsmanager_secret"', secrets)
        self.assertIn("secret_version", secrets)
        override = json.loads((env.root("ta-global") / "ephemeral_override.tf.json").read_text())
        self.assertEqual(override["resource"]["aws_secretsmanager_secret_version"]["app"], {"count": 0})
        self.assertNotIn("manage_secret_version", env.variables("ta-global"))
        self.assertIn("secret_version", (env.home / "source" / eph.ROOTS["ta-global"] / "secrets.tf").read_text())
        self.assertEqual((env.work / ".env").read_text(), "")

    def test_added_remote_backend_is_rejected(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.prepare()
        (env.root("ta-global") / "injected.tf").write_text('terraform { backend "s3" {} }')
        with self.assertRaisesRegex(eph.Error, "changed"):
            env.check_work()

    def test_capabilities_require_standard_boundary_build_and_same_checkout_rta(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.check_capabilities()
        makefile = env.source / "Makefile"
        containerfile = env.source / "Containerfile"
        original_makefile = makefile.read_text()
        original_containerfile = containerfile.read_text()
        for path, content in (
            (makefile, "build-ephemeral-image:\n\ttrue\n"),
            (containerfile, "FROM runtime\n"),
        ):
            with self.subTest(path=path):
                path.chmod(0o600)
                path.write_text(content)
                env.aws = Mock()
                with self.assertRaisesRegex(eph.Error, "Unsupported snapshot"):
                    env.check_capabilities()
                env.aws.assert_not_called()
                makefile.chmod(0o600)
                makefile.write_text(original_makefile)
                containerfile.chmod(0o600)
                containerfile.write_text(original_containerfile)

    def test_deployment_source_cannot_change_between_runs(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.prepare()
        (env.work / "tracked").write_text("changed")
        with self.assertRaisesRegex(eph.Error, "changed"):
            env.check_work()

    def test_vault_kv2_values_are_captured_without_command_arguments(self):
        runner = Mock()
        runner.json.return_value = {"data": {"data": {
            "trusted_actions_sa_client_id": "test-id",
            "trusted_actions_sa_client_secret": "supersecret",
            "trusted_actions_backplane_url": self.credentials["backplane_url"],
        }}}
        env = self.env(runner)
        with patch.dict(os.environ, {"VAULT_TOKEN": "vault-session"}):
            self.assertEqual(env.vault_credentials(), {
                "client_id": "test-id", "client_secret": "supersecret",
                "backplane_url": self.credentials["backplane_url"],
            })
        args, kwargs = runner.json.call_args
        self.assertNotIn("supersecret", str(args))
        self.assertEqual(kwargs["extra_env"]["VAULT_TOKEN"], "vault-session")

    def test_vault_credential_selectors_are_rejected(self):
        for key in ("client_id_key", "client_secret_key"):
            for value in ("custom-field", "trusted_actions_sa_client_id", None, ""):
                with self.subTest(key=key, value=value):
                    config = copy.deepcopy(self.config)
                    config["vault"][key] = value
                    with self.assertRaisesRegex(eph.Error, "vault has missing or unsupported fields"):
                        eph.load_config(self.config_file(config), self.repo)

    def test_vault_kv1_uses_fixed_fields_and_ignores_alternative_names(self):
        runner = Mock()
        runner.json.return_value = {"data": {
            "trusted_actions_sa_client_id": "fixed-id",
            "trusted_actions_sa_client_secret": "fixed-secret",
            "trusted_actions_backplane_url": self.credentials["backplane_url"],
            "client_id": "other-id", "client_secret": "other-secret",
            "ams-service.clientId": "literal-id",
            "ams-service.clientSecret": "literal-secret",
        }}
        env = self.env(runner)
        self.assertEqual(env.vault_credentials(), {
            "client_id": "fixed-id", "client_secret": "fixed-secret",
            "backplane_url": self.credentials["backplane_url"],
        })
        runner.json.return_value = {"data": {
            "client_id": "other-id", "client_secret": "other-secret",
        }}
        with self.assertRaisesRegex(eph.Error, "lacks required"):
            env.vault_credentials()

    def test_vault_requires_complete_service_account_pair_and_backplane_url(self):
        fields = {"trusted_actions_sa_client_id": "service-id",
                  "trusted_actions_sa_client_secret": "service-secret",
                  "trusted_actions_backplane_url": self.credentials["backplane_url"]}
        runner = Mock()
        env = self.env(runner)
        env.aws = Mock()
        for key in fields:
            for invalid in (None, "", "  ", 123, []):
                with self.subTest(key=key, invalid=invalid):
                    values = {**fields, key: invalid}
                    runner.json.return_value = {"data": values}
                    with self.assertRaisesRegex(eph.Error, "fields must be nonempty"):
                        env.vault_credentials()
                    env.aws.assert_not_called()
            runner.json.return_value = {"data": {k: v for k, v in fields.items() if k != key}}
            with self.assertRaisesRegex(eph.Error, "lacks required"):
                env.vault_credentials()

    def test_vault_backplane_url_requires_https_without_disclosing_value(self):
        runner = Mock()
        env = self.env(runner)
        for url in ("http://private.example.invalid", "https://user:secret@private.example.invalid",
                    "https://private.example.invalid?token=secret", "https://[malformed",
                    "private.example.invalid", "https://private.example.invalid#secret"):
            with self.subTest(url=url):
                runner.json.return_value = {"data": {
                    "trusted_actions_sa_client_id": "service-id",
                    "trusted_actions_sa_client_secret": "service-secret",
                    "trusted_actions_backplane_url": url,
                }}
                with self.assertRaises(eph.Error) as caught:
                    env.vault_credentials()
                self.assertNotIn(url, str(caught.exception))

    def test_unselected_backplane_vault_fields_cannot_choose_another_account(self):
        runner = Mock()
        runner.json.return_value = {"data": {
            "trusted_actions_sa_client_id": "shared-service-id",
            "trusted_actions_sa_client_secret": "shared-service-secret",
            "trusted_actions_backplane_url": self.credentials["backplane_url"],
            "backplane_client_id": "other-id", "backplane_client_secret": "other-secret",
        }}
        env = self.env(runner)
        self.assertEqual(env.vault_credentials(), {
            "client_id": "shared-service-id", "client_secret": "shared-service-secret",
            "backplane_url": self.credentials["backplane_url"],
        })

    def test_secret_value_uses_stdin_never_state_or_arguments(self):
        env = self.env()
        env.meta = {"owner": "ownership-uuid"}
        env.aws = Mock(side_effect=[
            {"Tags": [{"Key": "EphemeralOwner", "Value": "ownership-uuid"}]}, {},
        ])
        arn = "arn:aws:secretsmanager:us-west-2:123456789012:secret:owned"
        env.put_credentials(arn, self.credentials)
        args, kwargs = env.aws.call_args
        for value in self.credentials.values():
            self.assertNotIn(value, str(args))
            self.assertNotIn(value, json.dumps(env.meta))
        self.assertIn("file:///dev/stdin", args)
        self.assertEqual(json.loads(kwargs["input"]), {
            "ocm_client_id": self.credentials["client_id"],
            "backplane_client_id": self.credentials["client_id"],
            "ocm_client_secret": self.credentials["client_secret"],
            "backplane_client_secret": self.credentials["client_secret"],
            "backplane_url": self.credentials["backplane_url"],
            "ocm_token": "",
        })
        self.assertFalse(list(env.home.glob("*.tfvars*")))

    def test_refuses_unowned_or_foreign_secret(self):
        env = self.env()
        env.meta = {"owner": "mine"}
        env.aws = Mock(return_value={"Tags": []})
        for arn in ("arn:aws:secretsmanager:eu-west-1:123456789012:secret:other",
                    "arn:aws:secretsmanager:us-west-2:123456789012:secret:unowned"):
            with self.subTest(arn=arn), self.assertRaises(eph.Error):
                env.put_credentials(arn, self.credentials)

    def test_subprocess_failure_does_not_expose_secret_output(self):
        runner = eph.Runner()
        result = subprocess.CompletedProcess([], 2, b"supersecret", b"supersecret")
        with patch.object(runtime.subprocess, "run", return_value=result):
            with self.assertRaises(eph.Error) as caught:
                runner.run(["vault", "read", "safe-reference"])
        self.assertNotIn("supersecret", str(caught.exception))

    def test_variable_contract_single_account_region_and_development(self):
        env = self.env()
        env.meta = {"owner": "mine", "images": {"boundary": {"uri": "boundary@sha256:abc"},
                                                "ta": {"uri": "ta@sha256:def"}}}
        env.outputs = Mock(return_value={"secretsmanager_secret_arn": "arn:owned"})
        env.outputs.return_value["internal_fqdn"] = "https://ta.example.org"
        self.config["network"] = {}
        network = {"vpc_id": "vpc-owned", "private_subnet_ids": ["subnet-a", "subnet-b"],
                   "public_subnet_ids": ["subnet-c", "subnet-d"]}
        for key in eph.ROOTS:
            variables = env.variables(key, network)
            if key in {"boundary-regional", "ta-regional"}:
                self.assertTrue(variables["ephemeral_environment"])
            self.assertEqual(variables["aws_region"], "us-west-2")
            if key.startswith("boundary-"):
                self.assertEqual(variables["aws_account_id"], self.config["account_id"])
            else:
                self.assertNotIn("aws_account_id", variables)
            self.assertEqual(variables.get("stage", variables.get("environment")), "dev")
            self.assertNotIn("supersecret", json.dumps(variables))
        ta = env.variables("ta-regional", network)
        self.assertNotIn("client_id", json.loads(ta["app_config"])["ocm"])
        self.assertNotIn("client_id", json.loads(ta["app_config"])["backplane"])
        self.assertEqual(ta["vpc_id"], network["vpc_id"])
        for name in ("log_retention_days", "firehose_log_retention_days", "global_outputs"):
            self.assertNotIn(name, ta)
        boundary = env.variables("boundary-regional", network)
        self.assertEqual(boundary["rta_server_url"], "https://ta.example.org")
        self.assertNotIn("trusted_actions_api_url", boundary)
        self.assertNotIn("enable_bedrock_invocation_logging", boundary)
        self.assertNotIn("retention_days", boundary)
        self.assertEqual(boundary["log_retention_days"], 1)

    def test_missing_vault_fields_fail_without_creating_cloud_resources(self):
        runner = Mock()
        runner.json.return_value = {"data": {"data": {}}}
        env = self.env(runner)
        env.aws = Mock()
        with self.assertRaisesRegex(eph.Error, "lacks required"):
            env.vault_credentials()
        env.aws.assert_not_called()

    def test_preflight_certificate_must_be_issued_and_match_hostname(self):
        env = self.env()
        env.meta = {"roots": {}, "owner": "mine"}
        for certificate in (
            {"Status": "PENDING_VALIDATION", "SubjectAlternativeNames": ["ta.example.org"]},
            {"Status": "ISSUED", "SubjectAlternativeNames": ["wrong.example.org"]},
            {"Status": "ISSUED", "SubjectAlternativeNames": ["*.other.example.org"]},
        ):
            env.aws = Mock(return_value={"Certificate": certificate})
            with self.subTest(certificate=certificate), self.assertRaisesRegex(eph.Error, "certificate"):
                env.preflight()
            env.aws.assert_called_once()

    def test_preflight_accepts_existing_certificate_but_never_adopts_oidc(self):
        env = self.env()
        env.meta = {"roots": {}, "owner": "mine"}
        issuer_arn = "arn:aws:iam::123456789012:oidc-provider/identity.example.org/realms/dev"
        env.aws = Mock(side_effect=[
            {"Certificate": {"Status": "ISSUED", "SubjectAlternativeNames": ["*.example.org"]}},
            {"OpenIDConnectProviderList": [{"Arn": issuer_arn}]},
            {"ClientIDList": ["boundary-dev"]},
        ])
        with self.assertRaisesRegex(eph.Error, "explicit reference") as caught:
            env.preflight()
        message = str(caught.exception)
        self.assertIn(f'"existing_provider_arn": "{issuer_arn}"', message)
        self.assertIn("manual deployment", message)
        self.assertIn("account-wide", message)
        self.assertIn("changing region will not resolve", message)
        self.assertIn("without adopting or deleting", message)
        self.assertIn("local record may already exist", message)

    def test_github_oidc_collision_explains_reference_and_account_scope(self):
        env = self.env()
        env.meta = {"roots": {}, "owner": "mine"}
        env.aws = Mock(side_effect=[
            {"Certificate": {"Status": "ISSUED", "SubjectAlternativeNames": ["ta.example.org"]}},
            {"OpenIDConnectProviderList": [{
                "Arn": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com",
            }]},
            {},
        ])
        with self.assertRaisesRegex(eph.Error, "explicit reference") as caught:
            env.preflight()
        message = str(caught.exception)
        self.assertIn('"reference_existing_github_oidc": true', message)
        self.assertIn("boundary_account", message)
        self.assertIn("account-wide", message)
        self.assertIn("Changing region will not resolve", message)
        self.assertIn("original ownership", message)

    def test_preflight_explicit_reference_does_not_manage_provider(self):
        env = self.env()
        env.meta = {"roots": {}, "owner": "mine"}
        issuer_arn = "arn:aws:iam::123456789012:oidc-provider/identity.example.org/realms/dev"
        self.config["keycloak"]["existing_provider_arn"] = issuer_arn
        env.aws = Mock(side_effect=[
            {"Certificate": {"Status": "ISSUED", "SubjectAlternativeNames": ["ta.example.org"]}},
            {"OpenIDConnectProviderList": [{"Arn": issuer_arn}]},
            {"ClientIDList": ["boundary-dev"]},
            {},
        ])
        env.preflight()
        self.assertNotIn("create_keycloak_oidc_provider", env.variables("boundary-account"))

    def test_generated_inputs_are_declared_in_current_terraform_roots(self):
        env = self.env()
        self.config["network"] = {}
        env.meta = {"owner": "mine", "images": {"boundary": {"uri": "b@sha256:abc"},
                                                "ta": {"uri": "t@sha256:def"}}}
        env.outputs = Mock(return_value={"internal_fqdn": "https://ta.example.org",
                                        "secretsmanager_secret_arn": "arn:owned"})
        repository = Path(__file__).resolve().parents[1]
        network = {"vpc_id": "vpc", "public_subnet_ids": ["a", "b"],
                   "private_subnet_ids": ["c", "d"]}
        for key, relative in eph.ROOTS.items():
            variables = env.variables(key, network)
            declared = set(re.findall(r'\bvariable\s+"([^"]+)"\s*\{',
                "\n".join(path.read_text() for path in (repository / relative).glob("*.tf"))))
            with self.subTest(root=key):
                self.assertFalse(set(variables) - declared,
                                 f"Undeclared inputs: {set(variables) - declared}")
        account = env.variables("boundary-account", network)
        global_variables = env.variables("ta-global", network)
        self.assertNotIn("manage_secret_version", global_variables)
        self.assertEqual(global_variables["backplane_client_secret"], "script-populates-secrets-manager")
        self.assertFalse(account["enable_lambda_ecr_source"])
        self.assertFalse(account["enable_s3_replication_role"])
        self.assertEqual(account["lambda_ecr_replication_regions"], [])
        self.assertEqual(account["bedrock_model_agreements"], {})
        self.assertEqual(account["bedrock_logging_source_regions"], ["us-west-2"])
        self.assertIsNone(account["replication"])


class OwnedStateResourceTests(Fixture):
    def resource(self):
        return {"mode": "managed", "type": "aws_ecs_service", "name": "app",
                "instances": [{"attributes": {"name": "owned-service"}}]}

    def write_state(self, env, resources):
        eph.write_json(env.home / "ta-regional.tfstate", {"resources": resources})
        env.record_state("ta-regional")

    def test_resource_requires_owned_unchanged_state(self):
        env = self.env()
        env.meta = {"roots": {}}
        path = env.home / "ta-regional.tfstate"
        eph.write_json(path, {"resources": [self.resource()]})
        with self.assertRaisesRegex(eph.Error, "ownership"):
            env.state_resource("ta-regional", "aws_ecs_service", "app")
        env.record_state("ta-regional")
        self.assertEqual(env.state_resource("ta-regional", "aws_ecs_service", "app"),
                         {"name": "owned-service"})
        path.write_text("{}")
        with self.assertRaisesRegex(eph.Error, "ownership"):
            env.state_resource("ta-regional", "aws_ecs_service", "app")

    def test_resource_ignores_data_modules_other_names_and_deposed_instances(self):
        env = self.env()
        env.meta = {"roots": {}}
        current = self.resource()
        current["instances"].append({"deposed": "old-instance",
                                     "attributes": {"name": "stale-service"}})
        ignored = []
        for field, value in (("mode", "data"), ("module", "module.other"),
                             ("name", "other"), ("type", "aws_ecs_cluster")):
            resource = self.resource()
            resource[field] = value
            ignored.append(resource)
        self.write_state(env, ignored + [current])
        self.assertEqual(env.state_resource("ta-regional", "aws_ecs_service", "app"),
                         {"name": "owned-service"})

    def test_resource_rejects_missing_and_ambiguous_current_instances(self):
        env = self.env()
        env.meta = {"roots": {}}
        with self.assertRaises(eph.Error):
            env.state_resource("ta-regional", "aws_ecs_service", "app")
        empty = self.resource()
        empty["instances"] = []
        duplicate = self.resource()
        duplicate["instances"] *= 2
        for resources in ([], [empty], [duplicate], [self.resource(), self.resource()]):
            with self.subTest(resources=resources):
                self.write_state(env, resources)
                with self.assertRaisesRegex(eph.Error, "expected one"):
                    env.state_resource("ta-regional", "aws_ecs_service", "app")


class BedrockLoggingTests(Fixture):
    def logging(self, owned=True):
        env = self.env()
        env.meta = {"roots": {}}
        cloudwatch = {
            "logGroupName": "/aws/bedrock/owned",
            "roleArn": "arn:aws:iam::123456789012:role/owned-logging",
            "largeDataDeliveryS3Config": {"bucketName": "owned-large-data",
                                        "keyPrefix": "bedrock/"},
        }
        live = {"cloudWatchConfig": cloudwatch, "textDataDeliveryEnabled": True}
        if owned:
            eph.write_json(env.home / "boundary-regional.tfstate", {"resources": [{
                "mode": "managed",
                "type": "aws_bedrock_model_invocation_logging_configuration",
                "name": "boundary", "instances": [{"attributes": {"logging_config": [{
                    "cloudwatch_config": [{
                        "log_group_name": cloudwatch["logGroupName"],
                        "role_arn": cloudwatch["roleArn"],
                        "large_data_delivery_s3_config": [{
                            "bucket_name": "owned-large-data", "key_prefix": "bedrock/"}],
                    }],
                }]}}],
            }]})
            env.record_state("boundary-regional")
        env.aws = Mock(return_value={"loggingConfig": live})
        return env, live

    def test_absent_configuration_is_safe_for_fresh_environment(self):
        env, _ = self.logging(owned=False)
        env.aws.return_value = {}
        env.check_bedrock_logging()
        env.aws.assert_called_once_with("bedrock", "get-model-invocation-logging-configuration")

    def test_existing_unowned_configuration_is_never_adopted(self):
        env, _ = self.logging(owned=False)
        with self.assertRaisesRegex(eph.Error, "not owned"):
            env.check_bedrock_logging()
        self.assertFalse((env.home / "boundary-regional.tfstate").exists())

    def test_matching_owned_configuration_accepts_absent_or_false_nontext_flags(self):
        env, live = self.logging()
        env.check_bedrock_logging()
        for flag in ("imageDataDeliveryEnabled", "embeddingDataDeliveryEnabled",
                     "videoDataDeliveryEnabled"):
            live[flag] = False
        env.check_bedrock_logging()

    def test_changed_cloudwatch_or_large_data_destination_is_rejected(self):
        env, original = self.logging()
        for field in ("logGroupName", "roleArn", "bucketName", "keyPrefix"):
            with self.subTest(field=field):
                live = copy.deepcopy(original)
                target = live["cloudWatchConfig"]
                if field in {"bucketName", "keyPrefix"}:
                    target = target["largeDataDeliveryS3Config"]
                target[field] = "foreign"
                env.aws.return_value = {"loggingConfig": live}
                with self.assertRaisesRegex(eph.Error, "differs"):
                    env.check_bedrock_logging()

    def test_nontext_delivery_extra_s3_and_disabled_text_are_rejected(self):
        env, original = self.logging()
        changes = [
            {"s3Config": {"bucketName": "foreign"}},
            {"textDataDeliveryEnabled": False},
            {"textDataDeliveryEnabled": None},
            {"imageDataDeliveryEnabled": True},
            {"embeddingDataDeliveryEnabled": True},
            {"videoDataDeliveryEnabled": True},
        ]
        missing_text = copy.deepcopy(original)
        del missing_text["textDataDeliveryEnabled"]
        for live in [dict(original, **change) for change in changes] + [missing_text]:
            with self.subTest(live=live):
                env.aws.return_value = {"loggingConfig": live}
                with self.assertRaisesRegex(eph.Error, "differs"):
                    env.check_bedrock_logging()

    def test_tampered_owned_state_cannot_authorize_existing_configuration(self):
        env, _ = self.logging()
        (env.home / "boundary-regional.tfstate").write_text("{}")
        with self.assertRaisesRegex(eph.Error, "not owned"):
            env.check_bedrock_logging()

    def test_incomplete_owned_configuration_fails_closed(self):
        env, _ = self.logging()
        env.state_resource = Mock(return_value={"logging_config": []})
        with self.assertRaisesRegex(eph.Error, "supported configuration"):
            env.check_bedrock_logging()

    def test_foreign_logging_blocks_apply_and_destroy_before_terraform(self):
        env, _ = self.logging(owned=False)
        env.check_work = Mock()
        env.guard_account = Mock()
        env.tf = Mock()
        env.approve = Mock()
        for destroy in (False, True):
            with self.subTest(destroy=destroy), self.assertRaisesRegex(eph.Error, "not owned"):
                env.plan_apply("boundary-regional", destroy=destroy, yes=True,
                               account=self.config["account_id"])
        env.tf.assert_not_called()
        env.approve.assert_not_called()


class TransportTests(Fixture):
    def test_http_rejects_old_snapshot_before_any_aws_call(self):
        del self.config["ta_tls"]
        self.fake_source()
        (self.repo / "components/trusted-actions/internal/rta/command.go").unlink()
        env = self.env()
        env.create()
        env.aws = Mock()
        with self.assertRaisesRegex(eph.Error, "private HTTP requires"):
            env.deploy()
        env.aws.assert_not_called()

    def test_http_uses_private_alb_dns_without_hostname_or_certificate_inputs(self):
        del self.config["ta_tls"]
        self.config = eph.load_config(self.config_file(), self.repo)
        env = self.env()
        env.meta = {"owner": "mine", "images": {"ta": {"uri": "ta@sha256:test"},
                                               "boundary": {"uri": "boundary@sha256:test"}}}
        outputs = {"internal_fqdn": None,
                   "alb_dns_name": "internal-ta-123.us-west-2.elb.amazonaws.com"}
        env.outputs = Mock(return_value=outputs)
        network = {"vpc_id": "vpc", "private_subnet_ids": ["a", "b"],
                   "public_subnet_ids": ["c", "d"]}
        self.assertEqual(env.variables("ta-global")["internal_fqdn"], "")
        ta = env.variables("ta-regional", network)
        self.assertEqual(ta["internal_fqdn"], "")
        self.assertEqual(ta["alb_certificate_arn"], "")
        self.assertEqual(env.variables("boundary-regional", network)["rta_server_url"],
                         "http://" + outputs["alb_dns_name"])
        env.meta["outputs"] = {"trusted_actions": outputs, "boundary": {
            "lambda_function_name": "lambda", "lambda_invoker_role_arn": "arn:invoker",
            "sre_shared_role_arn": "arn:sre", "ecs_cluster_name": "boundary",
            "efs_filesystem_id": "fs-test",
        }}
        env.write_cli_profile = Mock(return_value=self.base / "config/rosa-boundary/config.yaml")
        env.write_connection()
        connection = env.meta["connection"]["client_environment"]
        self.assertEqual(connection["RTA_SERVER"], "http://" + outputs["alb_dns_name"])
        self.assertEqual(connection["RTA_ALLOW_INSECURE_HTTP"], "true")

    def test_http_preflight_skips_acm_but_keeps_identity_and_logging_checks(self):
        del self.config["ta_tls"]
        env = self.env()
        env.meta = {"roots": {}, "owner": "mine"}
        env.aws = Mock(side_effect=[{"OpenIDConnectProviderList": []}, {}])
        env.preflight()
        self.assertEqual([call.args[:2] for call in env.aws.call_args_list], [
            ("iam", "list-open-id-connect-providers"),
            ("bedrock", "get-model-invocation-logging-configuration"),
        ])

    def test_http_endpoint_rejects_public_foreign_or_malformed_dns(self):
        del self.config["ta_tls"]
        env = self.env()
        for hostname in (None, "", "ta.example.org", "ta-123.us-west-2.elb.amazonaws.com",
                         "internal-ta-123.us-east-1.elb.amazonaws.com",
                         "internal-ta-123.us-west-2.elb.amazonaws.com.evil.invalid",
                         "http://internal-ta-123.us-west-2.elb.amazonaws.com",
                         "user@internal-ta-123.us-west-2.elb.amazonaws.com"):
            with self.subTest(hostname=hostname), self.assertRaisesRegex(eph.Error, "internal ALB"):
                env.ta_endpoint({"alb_dns_name": hostname})

    def test_explicit_tls_configuration_is_all_or_nothing(self):
        for tls in (None, {}, "", False, {"internal_fqdn": "ta.example.org"},
                    {"certificate_arn": self.config["ta_tls"]["certificate_arn"]}):
            with self.subTest(tls=tls):
                config = copy.deepcopy(self.config)
                config["ta_tls"] = tls
                with self.assertRaises(eph.Error):
                    eph.load_config(self.config_file(config), self.repo)

    def test_tls_never_falls_back_to_http_after_an_endpoint_mismatch(self):
        env = self.env()
        for endpoint in (None, "http://ta.example.org", "https://wrong.example.org"):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(eph.Error, "HTTPS endpoint"):
                env.ta_endpoint({"internal_fqdn": endpoint,
                                 "alb_dns_name": "internal-ta-123.us-west-2.elb.amazonaws.com"})
        self.assertEqual(env.ta_endpoint({"internal_fqdn": "https://ta.example.org"}),
                         "https://ta.example.org")


class LifecycleTests(Fixture):
    def test_deploy_dependency_order_and_secret_before_ta_regional(self):
        self.fake_source()
        env = self.env()
        env.create()
        self.config["network"] = {}
        # Config is normally normalized before create.
        env.meta["config_hash"] = eph.digest(eph.canonical(self.config))
        env.save()
        env.guard_account = Mock()
        env.preflight = Mock()
        env.require_tools = Mock()
        env.verify_deployment = Mock()
        env.approve = Mock()
        env.vault_credentials = Mock(return_value=self.credentials)
        env.publish_images = Mock(side_effect=lambda: env.meta.update(
            images={"boundary": {"uri": "b@sha256:d"}, "ta": {"uri": "t@sha256:d"}}))
        events = []
        env.plan_apply = Mock(side_effect=lambda key, *a, **kw: events.append(key))
        network = {"vpc_id": "vpc", "public_subnet_ids": ["a", "b"],
                   "private_subnet_ids": ["c", "d"]}
        boundary = {"lambda_function_name": "lambda-dev", "lambda_invoker_role_arn": "arn:invoker",
                    "sre_shared_role_arn": "arn:sre", "ecs_cluster_name": "boundary",
                    "efs_filesystem_id": "fs-dev"}
        env.outputs = Mock(side_effect=lambda key: network if key == "boundary-network" else (
            {"secretsmanager_secret_arn": "arn:owned"} if key == "ta-global" else (
                boundary if key == "boundary-regional" else
                 {"internal_fqdn": "https://ta.example.org", "alb_dns_name": "private.example.org"})))
        env.put_credentials = Mock(side_effect=lambda *a: events.append("secret-value"))
        env.runner = Mock(env={})
        env.write_cli_profile = Mock(return_value=self.base / "config/rosa-boundary/config.yaml")
        env.deploy(yes=True, account=self.config["account_id"])
        self.assertEqual(events, ["boundary-account", "boundary-network",
                                  "ta-global", "secret-value", "ta-regional", "boundary-regional"])
        env.put_credentials.assert_called_once_with("arn:owned", self.credentials)
        self.assertEqual(env.meta["status"], "deployed")
        env.runner.run.assert_called_once()
        self.assertIn("build-lambda", env.runner.run.call_args.args[0])
        self.assertEqual(env.meta["connection"]["trusted_actions_url"], "https://ta.example.org")
        profile = env.write_cli_profile.call_args.args[0]
        self.assertEqual(profile["keycloak_realm"], "dev")
        self.assertEqual(profile["environment"], "dev")
        self.assertEqual(env.meta["connection"]["cli_profile"], "example")
        self.assertNotIn("XDG_CONFIG_HOME", env.meta["connection"]["client_environment"])
        self.assertNotIn("XDG_CACHE_HOME", env.meta["connection"]["client_environment"])
        self.assertEqual(env.meta["connection"]["client_environment"]["RTA_SERVER"], "https://ta.example.org")

    def test_destroy_reverse_order_only_owned_roots(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.prepare()
        env.meta["roots"] = {key: {"state_hash": "owned"} for key in eph.ROOTS}
        env.save()
        env.guard_account = Mock()
        env.state_check = Mock()
        env.ensure_quiescent = Mock()
        env.plan_apply = Mock()
        env.destroy(yes=True, account=self.config["account_id"])
        self.assertEqual([call.args[0] for call in env.plan_apply.call_args_list],
                         list(reversed(eph.ROOTS)))
        self.assertTrue(all(call.kwargs["destroy"] for call in env.plan_apply.call_args_list))
        self.assertEqual(env.meta["status"], "destroyed")
        self.assertTrue((env.home / "source").exists())

    def test_failure_does_not_trigger_automatic_destroy(self):
        env = self.env()
        env.create()
        env.guard_account = Mock(side_effect=eph.Error("wrong account"))
        env.plan_apply = Mock()
        with self.assertRaises(eph.Error):
            env.deploy(yes=True, account=self.config["account_id"])
        env.plan_apply.assert_not_called()
        self.assertEqual(env.meta["status"], "created")

    def test_fresh_plan_cannot_update_or_import_existing_resource(self):
        env = self.env()
        env.create()
        env.check_work = Mock()
        env.state_check = Mock()
        env.guard_account = Mock()
        for change in ({"actions": ["update"]},
                       {"actions": ["create"], "importing": {"id": "existing"}}):
            def tf(key, *args):
                if args[:2] == ("show", "-json"):
                    return json.dumps({"resource_changes": [
                        {"address": "aws_resource.existing", "change": change}]}).encode()
                return b""
            env.tf = Mock(side_effect=tf)
            with self.subTest(change=change), self.assertRaises(eph.Error):
                env.plan_apply("ta-global", yes=True, account=self.config["account_id"])
            self.assertFalse(any(call.args[1] == "apply" for call in env.tf.call_args_list))

    def test_unsupported_snapshot_fails_before_any_aws_call(self):
        env = self.env()
        env.create()
        env.aws = Mock()
        with self.assertRaisesRegex(eph.Error, "Unsupported snapshot"):
            env.deploy(yes=True, account=self.config["account_id"])
        env.aws.assert_not_called()
        self.assertFalse(env.work.exists())

    def setup_plan(self, env, apply_fails=False):
        env.check_work = Mock()
        env.guard_account = Mock()
        env.approve = Mock()
        env.create()
        plan = env.home / "ta-global.plan"
        state = env.home / "ta-global.tfstate"
        def tf(key, *args):
            if args[0] == "plan":
                plan.write_bytes(b"saved plan contents")
            if args[:2] == ("show", "-json"):
                return json.dumps({"resource_changes": [
                    {"address": "aws_secretsmanager_secret.app", "type": "aws_secretsmanager_secret",
                     "change": {"actions": ["create"]}}]}).encode()
            if args[:2] == ("show", "-no-color"):
                return b"Reviewed Terraform plan"
            if args[0] == "apply":
                checkpoint = json.loads(state.read_text())
                checkpoint["serial"] += 1
                eph.write_json(state, checkpoint)
                if apply_fails:
                    raise eph.Error("partial apply")
            return b""
        env.tf = Mock(side_effect=tf)
        return plan, state

    def test_apply_uses_exact_saved_reviewed_plan(self):
        env = self.env()
        plan, state = self.setup_plan(env)
        env.plan_apply("ta-global", yes=True, account=self.config["account_id"])
        applies = [call for call in env.tf.call_args_list if call.args[1] == "apply"]
        self.assertEqual(applies[0].args, ("ta-global", "apply", "-input=false", str(plan)))
        self.assertEqual(env.meta["roots"]["ta-global"]["state_hash"], eph.digest(state.read_bytes()))
        self.assertNotIn("pending_plan", env.meta["roots"]["ta-global"])
        self.assertEqual((env.home / "ta-global.review.txt").stat().st_mode & 0o777, 0o600)

    def test_real_generated_tfvars_are_journaled_and_used_without_blessing_changes(self):
        self.fake_source()
        env = self.env()
        self.setup_plan(env)
        env.prepare()
        env.check_work = eph.Environment.check_work.__get__(env)
        inputs = {"backplane_client_secret": "script-populates-secrets-manager"}
        env.plan_apply("ta-global", inputs, yes=True, account=self.config["account_id"])
        path = env.root("ta-global") / "ephemeral.auto.tfvars.json"
        self.assertEqual(json.loads(path.read_text()), inputs)
        env.load()
        env.check_work()
        recorded = env.generation_record()["generated_inputs"][path.relative_to(env.work).as_posix()]
        self.assertEqual(recorded, eph.digest(path.read_bytes()))
        env.approve.side_effect = lambda *a: path.write_text("{}")
        env.tf.reset_mock()
        with self.assertRaisesRegex(eph.Error, "Generated deployment input changed"):
            env.plan_apply("ta-global", inputs, yes=True, account=self.config["account_id"])
        self.assertFalse(any(call.args[1] == "apply" for call in env.tf.call_args_list))

    def test_added_executable_input_after_review_never_applies(self):
        self.fake_source()
        env = self.env()
        self.setup_plan(env)
        env.prepare()
        env.check_work = eph.Environment.check_work.__get__(env)
        env.approve.side_effect = lambda *a: (env.work / "GNUmakefile").write_text("# unreviewed")
        with self.assertRaisesRegex(eph.Error, "changed"):
            env.plan_apply("ta-global", yes=True, account=self.config["account_id"])
        self.assertFalse(any(call.args[1] == "apply" for call in env.tf.call_args_list))

    def test_plan_changed_after_review_never_applies(self):
        env = self.env()
        plan, _ = self.setup_plan(env)
        env.approve.side_effect = lambda *a: plan.write_bytes(b"tampered plan")
        with self.assertRaisesRegex(eph.Error, "changed after approval"):
            env.plan_apply("ta-global", yes=True, account=self.config["account_id"])
        self.assertFalse(any(call.args[1] == "apply" for call in env.tf.call_args_list))

    def test_partial_apply_records_owned_state_for_explicit_cleanup(self):
        env = self.env()
        _, state = self.setup_plan(env, apply_fails=True)
        with self.assertRaisesRegex(eph.Error, "partial apply"):
            env.plan_apply("ta-global", yes=True, account=self.config["account_id"])
        self.assertEqual(env.meta["roots"]["ta-global"]["state_hash"], eph.digest(state.read_bytes()))
        self.assertIn("pending_plan", env.meta["roots"]["ta-global"])

    def test_redeploy_refuses_live_tasks_without_stopping_them(self):
        env = self.env()
        env.meta = {"roots": {"boundary-regional": {}}}
        state = {"resources": [{"mode": "managed", "type": "aws_ecs_cluster",
                                "instances": [{"attributes": {"name": "owned-cluster"}}]}]}
        eph.write_json(env.home / "boundary-regional.tfstate", state)
        env.aws = Mock(return_value={"taskArns": ["task-running"]})
        with self.assertRaisesRegex(eph.Error, "stop tasks"):
            env.ensure_quiescent()
        env.aws.assert_called_once_with("ecs", "list-tasks", "--cluster", "owned-cluster",
                                        "--desired-status", "RUNNING")

    def test_redeploy_refuses_unclosed_investigation_access_points(self):
        env = self.env()
        env.meta = {"roots": {"boundary-regional": {}}}
        state = {"resources": [{"mode": "managed", "type": "aws_efs_file_system",
                                "instances": [{"attributes": {"id": "fs-owned"}}]}]}
        eph.write_json(env.home / "boundary-regional.tfstate", state)
        env.aws = Mock(return_value={"AccessPoints": [{"AccessPointId": "fsap-investigation"}]})
        with self.assertRaisesRegex(eph.Error, "Unclosed"):
            env.ensure_quiescent()

    def test_redeploy_rotates_secrets_then_restarts_and_verifies_ta(self):
        self.fake_source()
        self.config["network"] = {}
        env = self.env()
        env.create()
        env.meta["roots"]["ta-regional"] = {"state_hash": "owned-existing-state", "touched": True}
        env.save()
        env.guard_account = Mock()
        env.state_check = Mock()
        env.preflight = Mock()
        env.require_tools = Mock()
        env.approve = Mock()
        env.vault_credentials = Mock(return_value=self.credentials)
        env.publish_images = Mock(side_effect=lambda: env.meta.update(
            images={"boundary": {"uri": "b@sha256:d"}, "ta": {"uri": "t@sha256:d"}}))
        events = []
        env.plan_apply = Mock(side_effect=lambda key, *a, **kw: events.append(key))
        network = {"vpc_id": "vpc", "public_subnet_ids": ["a", "b"],
                   "private_subnet_ids": ["c", "d"]}
        env.outputs = Mock(side_effect=lambda key: network if key == "boundary-network" else {
            "internal_fqdn": "https://ta.example.org", "secretsmanager_secret_arn": "arn:owned",
            "ecs_cluster_name": "owned-ta-cluster"})
        env.state_resource = Mock(return_value={"name": "owned-ta-service"})
        env.put_credentials = Mock(side_effect=lambda *a: events.append("secret-rotated"))
        env.aws = Mock(side_effect=lambda *a, **kw: (
            events.append("ecs-forced-restart") or
            {"service": {"deployments": [{"status": "PRIMARY", "id": "new-deployment"}]}}))
        env.write_connection = Mock()
        env.verify_deployment = Mock(side_effect=lambda: events.append("verified"))
        env.runner = Mock(env={})
        env.deploy(yes=True, account=self.config["account_id"])
        self.assertLess(events.index("secret-rotated"), events.index("ecs-forced-restart"))
        self.assertLess(events.index("ecs-forced-restart"), events.index("verified"))
        env.aws.assert_called_once_with("ecs", "update-service", "--cluster", "owned-ta-cluster",
                                       "--service", "owned-ta-service", "--force-new-deployment")
        self.assertEqual(env.meta["expected_ta_deployment"], "new-deployment")
        env.state_resource.assert_called_with("ta-regional", "aws_ecs_service", "app")


class ProvenanceTests(Fixture):
    def test_build_publication_uses_supported_make_targets_and_registry_digest(self):
        self.assert_publication("working-tree")

    def test_commit_publication_records_exact_source_commit_in_rta_binary(self):
        self.assert_publication("HEAD")

    def assert_publication(self, selection):
        runner = Mock()
        env = self.env(runner)
        env.check_work = Mock()
        env.meta = {"owner": "mine", "source": {"fingerprint": "a" * 64,
                                              "commit": "c" * 40,
                                              "selection": selection},
                    "images": {}, "roots": {}}
        sha = "sha256:" + "b" * 64
        def aws(*args, **kwargs):
            if args[:2] == ("ecr", "create-repository"):
                name = args[args.index("--repository-name") + 1]
                return {"repository": {"repositoryUri":
                    f"123456789012.dkr.ecr.us-west-2.amazonaws.com/{name}"}}
            if args[:2] == ("ecr", "get-login-password"):
                return b"registry-password"
            if args[:2] == ("ecr", "describe-images"):
                return {"imageDetails": [{"imageDigest": sha}]}
            raise AssertionError("Unexpected AWS call")
        env.aws = Mock(side_effect=aws)
        def run(args, **kwargs):
            if args[:2] == ["podman", "push"]:
                Path(args[args.index("--digestfile") + 1]).write_text(sha)
            return b""
        runner.run.side_effect = run
        runner.json.return_value = [{"Architecture": "amd64", "Id": "sha256:local"}]
        with patch.dict(os.environ, {"GITHUB_TOKEN": "github-build-token",
                                     "GITHUB_APP_ID": "test-app",
                                     "GITHUB_APP_PEM": "test-build-pem",
                                     "GITHUB_APP_INSTALL_ID": "test-install",
                                     "VAULT_TOKEN": "never-forward-vault",
                                     "AWS_SECRET_ACCESS_KEY": "never-forward-aws"}):
            env.publish_images()
        make_calls = [call.args[0] for call in runner.run.call_args_list if call.args[0][0] == "make"]
        self.assertEqual([args[1] for args in make_calls],
                         ["build-amd64", "build-amd64"])
        for args in make_calls:
            self.assertTrue(any(value.startswith("IMAGE_NAME=") for value in args))
            self.assertTrue(any(value.startswith("TAG=") for value in args))
            self.assertFalse(any(value.startswith(("IMAGE=", "IMAGE_PLATFORM=")) for value in args))
        self.assertTrue(all(call.args[0][-1].endswith("-amd64")
                            for call in runner.json.call_args_list))
        base_build = next(call for call in runner.run.call_args_list
                          if call.args[0][:2] == ["make", "build-amd64"])
        self.assertEqual(base_build.kwargs["extra_env"]["GITHUB_TOKEN"], "github-build-token")
        self.assertNotIn("VAULT_TOKEN", base_build.kwargs["extra_env"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", base_build.kwargs["extra_env"])
        inspected = runner.json.call_args_list[0].args[0][-1]
        self.assertTrue(inspected.endswith("-amd64"))
        pushes = [call.args[0] for call in runner.run.call_args_list
                  if call.args[0][:2] == ["podman", "push"]]
        self.assertEqual(pushes[0][-2], inspected)
        self.assertEqual(pushes[0][-1], "docker://" + inspected.removesuffix("-amd64"))
        self.assertEqual(base_build.kwargs["cwd"], env.work)
        revision = "c" * 40
        if selection == "working-tree":
            revision += "-dirty"
        self.assertRegex(revision, r"^[a-f0-9]{40}(?:-dirty)?$")
        self.assertIn("RTA_COMMIT=" + revision, base_build.args[0])
        for call in runner.run.call_args_list:
            if call is not base_build:
                self.assertNotIn("GITHUB_TOKEN", call.kwargs.get("extra_env", {}))
        self.assertTrue(all(call.kwargs.get("build") for call in runner.run.call_args_list))
        self.assertTrue(env.meta["images"]["boundary"]["uri"].endswith("@" + sha))
        self.assertEqual(env.meta["images"]["boundary"]["source"], "a" * 64)
        self.assertFalse((env.home / "registry-auth.json").exists())
        self.assertNotIn("registry-password", (env.home / "environment.json").read_text())

    def deployment(self, env):
        env.meta = {
            "images": {"boundary": {"uri": "boundary@sha256:abc"}, "ta": {"uri": "ta@sha256:def"}},
            "outputs": {
                "trusted_actions": {"ecs_cluster_name": "owned-ta"},
                "boundary": {"task_definition_arn": "boundary-definition"},
            },
            "expected_ta_deployment": "current-deployment",
            "roots": {},
        }
        eph.write_json(env.home / "ta-regional.tfstate", {"resources": [{
            "mode": "managed", "type": "aws_ecs_service", "name": "app",
            "instances": [{"attributes": {"name": "owned-service"}}],
        }]})
        env.record_state("ta-regional")
        root = env.root("boundary-regional")
        root.mkdir(parents=True)
        resources = []
        hashes = {}
        for index in range(3):
            filename = root / f"lambda-{index}.zip"
            filename.write_bytes(f"zip-content-{index}".encode())
            sha = base64.b64encode(hashlib.sha256(filename.read_bytes()).digest()).decode()
            name = f"lambda-{index}"
            hashes[name] = sha
            resources.append({
                "mode": "managed", "type": "aws_lambda_function",
                "instances": [{"attributes": {"filename": str(filename),
                               "source_code_hash": sha, "function_name": name}}],
            })
        eph.write_json(env.home / "boundary-regional.tfstate", {"resources": resources})
        def aws(*args, **kwargs):
            if args[:3] == ("ecs", "wait", "services-stable"):
                return b""
            if args[:2] == ("ecs", "describe-services"):
                return {"services": [{
                    "status": "ACTIVE", "desiredCount": 1, "runningCount": 1, "pendingCount": 0,
                    "taskDefinition": "ta-definition", "deployments": [{
                        "id": "current-deployment", "status": "PRIMARY",
                        "taskDefinition": "ta-definition", "rolloutState": "COMPLETED",
                    }],
                }]}
            if args[:2] == ("ecs", "describe-task-definition"):
                image = "boundary@sha256:abc" if args[-1] == "boundary-definition" else "ta@sha256:def"
                return {"taskDefinition": {"containerDefinitions": [{"image": image}]}}
            if args[:2] == ("lambda", "get-function-configuration"):
                return {"State": "Active", "LastUpdateStatus": "Successful",
                        "PackageType": "Zip", "CodeSha256": hashes[args[-1]]}
            raise AssertionError("Unexpected AWS call")
        env.aws = Mock(side_effect=aws)
        return aws

    def test_readiness_verifies_service_primary_images_and_all_lambda_hashes(self):
        env = self.env()
        self.deployment(env)
        env.verify_deployment()
        self.assertEqual(env.meta["verification"]["ta_ecs_service"], "stable")
        self.assertEqual(len(env.meta["verification"]["lambdas"]), 3)
        self.assertEqual(env.meta["verification"]["private_api_connectivity"], "unverified")
        self.assertEqual(env.meta["verification"]["cluster_actions"], "not-run")

    def test_service_stable_is_not_enough_when_primary_is_stale(self):
        env = self.env()
        self.deployment(env)
        env.meta["expected_ta_deployment"] = "newer-deployment"
        with self.assertRaisesRegex(eph.Error, "PRIMARY"):
            env.verify_deployment()

    def test_deployed_lambda_code_must_match_local_zip(self):
        env = self.env()
        aws = self.deployment(env)
        def wrong_hash(*args, **kwargs):
            result = aws(*args, **kwargs)
            if args[:2] == ("lambda", "get-function-configuration"):
                result["CodeSha256"] = "wrong-code-hash"
            return result
        env.aws = Mock(side_effect=wrong_hash)
        with self.assertRaisesRegex(eph.Error, "expected Zip code hash"):
            env.verify_deployment()


class GenerationRecoveryTests(Fixture):
    def prepared(self):
        self.fake_source()
        env = self.env()
        env.create()
        env.prepare()
        return env

    def test_changed_worktree_gets_new_generation_preserving_owner_and_states(self):
        env = self.prepared()
        original = env.generation
        owner = env.meta["owner"]
        state_hashes = {key: value["state_hash"] for key, value in env.meta["roots"].items()}
        (self.repo / "tracked").write_text("new local source")
        env.refresh_source()
        self.assertNotEqual(env.generation, original)
        self.assertEqual(env.meta["owner"], owner)
        self.assertEqual({key: value["state_hash"] for key, value in env.meta["roots"].items()}, state_hashes)
        self.assertEqual((env.source / "tracked").read_text(), "new local source")
        self.assertEqual((env.home / "generations" / original / "source/tracked").read_text(), "original")
        self.assertEqual(env.meta["images"], {})
        env.prepare()
        env.check_work()

    def test_unchanged_worktree_reuses_exact_generation_and_provenance(self):
        env = self.prepared()
        original = env.generation
        env.meta["images"] = {"boundary": {"uri": "owned@sha256:digest"}}
        env.save()
        env.refresh_source()
        self.assertEqual(env.generation, original)
        self.assertEqual(env.meta["images"]["boundary"]["uri"], "owned@sha256:digest")
        self.assertEqual(len(env.meta["source_requests"]), 2)
        env.check_work()

    def test_commit_on_existing_environment_selects_requested_content(self):
        env = self.prepared()
        self.git("add", "deploy", "components/trusted-actions", "Makefile", "Containerfile")
        self.git("commit", "-qm", "supported environment source")
        (self.repo / "tracked").write_text("dirty content")
        env.refresh_source()
        dirty_generation = env.generation
        env.refresh_source("HEAD")
        self.assertNotEqual(env.generation, dirty_generation)
        self.assertEqual((env.source / "tracked").read_text(), "original")
        self.assertEqual(env.meta["source_requests"][-1]["selection"], "HEAD")
        self.assertEqual(env.meta["source_requests"][-1]["commit"],
                         self.git("rev-parse", "HEAD").decode().strip())

    def test_destroy_uses_last_applied_generation_not_unapplied_new_source(self):
        env = self.prepared()
        applied = env.generation
        env.meta["roots"]["boundary-account"].update(touched=True, generation=applied)
        env.save()
        (self.repo / "tracked").write_text("new generation never applied")
        env.refresh_source()
        self.assertNotEqual(env.generation, applied)
        env.guard_account = Mock()
        observed = []
        env.plan_apply = Mock(side_effect=lambda key, **kwargs: observed.append((key, env.generation)))
        env.destroy(yes=True, account=self.config["account_id"])
        self.assertEqual(observed, [("boundary-account", applied)])

    def test_changed_source_refuses_running_boundary_tasks_without_replacing_generation(self):
        env = self.prepared()
        original = env.generation
        state_path = env.home / "boundary-regional.tfstate"
        state = json.loads(state_path.read_text())
        state.update(serial=1, resources=[{
            "mode": "managed", "type": "aws_ecs_cluster",
            "instances": [{"attributes": {"name": "owned-boundary-cluster"}}],
        }])
        eph.write_json(state_path, state)
        env.meta["roots"]["boundary-regional"]["touched"] = True
        env.record_state("boundary-regional")
        (self.repo / "tracked").write_text("new source while old session runs")
        env.guard_account = Mock()
        env.aws = Mock(return_value={"taskArns": ["running-task"]})
        with self.assertRaisesRegex(eph.Error, "stop tasks"):
            env.refresh_source()
        self.assertEqual(env.generation, original)
        self.assertEqual((env.source / "tracked").read_text(), "original")
        env.aws.assert_called_once_with("ecs", "list-tasks", "--cluster", "owned-boundary-cluster",
                                        "--desired-status", "RUNNING")

    def test_same_generation_lambda_dependencies_are_not_rebuilt(self):
        env = self.prepared()
        env.runner = Mock(env={})
        env.build_lambda()
        recorded = env.meta["generations"][env.generation]["lambda_package_fingerprint"]
        env.build_lambda()
        env.runner.run.assert_called_once()
        self.assertTrue(recorded)
        self.assertTrue(env.runner.run.call_args.kwargs["build"])
        self.assertNotIn("VAULT_TOKEN", env.runner.run.call_args.kwargs["extra_env"])

    def pending_apply(self):
        env = self.prepared()
        key = "ta-global"
        record = env.meta["roots"][key]
        plan = env.home / f"{key}.plan"
        plan.write_bytes(b"journaled Terraform plan")
        record.update(pending_plan=eph.digest(plan.read_bytes()), pending_generation=env.generation,
                      pending_addresses=["aws_iam_role.app"], touched=True)
        state_path = env.home / f"{key}.tfstate"
        state = json.loads(state_path.read_text())
        state.update(serial=1, resources=[{
            "mode": "managed", "type": "aws_iam_role", "name": "app",
            "instances": [{"attributes": {
                "arn": "arn:aws:iam::123456789012:role/owned-role",
                "tags_all": {"EphemeralOwner": env.meta["owner"]},
            }}],
        }])
        eph.write_json(state_path, state)
        env.save()  # Intent is durable; the state hash still precedes the interrupted apply.
        env.guard_account = Mock()
        env.approve = Mock()
        env.tf = Mock(return_value=json.dumps({"resource_changes": [
            {"address": "aws_iam_role.app", "mode": "managed",
             "change": {"actions": ["create"]}}]}).encode())
        return env, state_path

    def test_explicit_recovery_accepts_only_journaled_owned_checkpoint(self):
        env, state_path = self.pending_apply()
        with self.assertRaisesRegex(eph.Error, "ownership"):
            env.state_check("ta-global")
        env.recover(yes=True, account=self.config["account_id"])
        record = env.meta["roots"]["ta-global"]
        self.assertEqual(record["state_hash"], eph.digest(state_path.read_bytes()))
        self.assertEqual(record["serial"], 1)
        self.assertNotIn("pending_plan", record)
        self.assertIn("recovered_from_plan", record)
        env.approve.assert_called_once()

    def test_recovery_rejects_foreign_lineage_owner_account_or_addresses(self):
        for case in ("lineage", "owner", "account", "address"):
            with self.subTest(case=case):
                # Each environment needs its own fresh fixture source directory.
                env, state_path = self.pending_apply() if case == "lineage" else (None, None)
                if env is None:
                    # Reuse a valid journal after resetting state from the first
                    # case, without reusing a foreign checkpoint.
                    env = original_env
                    eph.write_json(state_path := original_path, original_state)
                    env.meta = copy.deepcopy(original_meta)
                    env.save()
                else:
                    original_env, original_path = env, state_path
                    original_state = json.loads(state_path.read_text())
                    original_meta = copy.deepcopy(env.meta)
                state = json.loads(state_path.read_text())
                attrs = state["resources"][0]["instances"][0]["attributes"]
                if case == "lineage":
                    state["lineage"] = "foreign-lineage"
                elif case == "owner":
                    attrs["tags_all"]["EphemeralOwner"] = "somebody-else"
                elif case == "account":
                    attrs["arn"] = "arn:aws:iam::999999999999:role/foreign"
                else:
                    state["resources"][0]["name"] = "not-in-plan"
                eph.write_json(state_path, state)
                with self.assertRaises(eph.Error):
                    env.recover(yes=True, account=self.config["account_id"])
                env.approve.assert_not_called()

    def test_recovery_does_not_adopt_arbitrary_unjournaled_state_edits(self):
        env = self.prepared()
        path = env.home / "ta-global.tfstate"
        state = json.loads(path.read_text())
        state["serial"] = 1
        eph.write_json(path, state)
        env.guard_account = Mock()
        env.approve = Mock()
        with self.assertRaisesRegex(eph.Error, "ownership"):
            env.recover(yes=True, account=self.config["account_id"])
        env.approve.assert_not_called()

    def test_recovery_never_force_unlocks_a_running_or_stale_apply(self):
        env, _ = self.pending_apply()
        (env.home / ".ta-global.tfstate.lock.info").write_text("{}")
        with self.assertRaisesRegex(eph.Error, "lock remains"):
            env.recover(yes=True, account=self.config["account_id"])
        env.tf.assert_not_called()

    def test_recovery_records_only_live_owner_tagged_pending_ecr_create(self):
        env = self.prepared()
        env.meta["pending_ecr"] = {"boundary": {
            "repository": "eph-example/boundary", "generation": env.generation}}
        env.save()
        env.guard_account = Mock()
        env.approve = Mock()
        env.aws = Mock(side_effect=[
            {"repositories": [{"repositoryArn":
                "arn:aws:ecr:us-west-2:123456789012:repository/eph-example/boundary"}]},
            {"tags": [{"Key": "EphemeralOwner", "Value": env.meta["owner"]}]},
        ])
        env.recover(yes=True, account=self.config["account_id"])
        self.assertEqual(env.meta["repositories"]["boundary"], "eph-example/boundary")
        self.assertFalse(env.meta["pending_ecr"])
        self.assertTrue(all(call.args[1] not in {"create-repository", "delete-repository"}
                            for call in env.aws.call_args_list))

    def test_recovery_verifies_or_abandons_interrupted_publication_without_overwrite(self):
        env = self.prepared()
        sha = "sha256:" + "a" * 64
        digest_file = env.source.parent / "push.digest"
        digest_file.write_text(sha)
        pending = {
            "repository": "eph-example/boundary", "generation": env.generation,
            "source": env.meta["source"]["fingerprint"], "tag": "unique-attempt",
            "uri": "123456789012.dkr.ecr.us-west-2.amazonaws.com/eph-example/boundary",
            "local_id": "local-image-id", "digest_file": str(digest_file),
        }
        env.meta["repositories"] = {"boundary": pending["repository"]}
        env.meta["pending_publications"] = {"boundary": pending}
        env.save()
        env.guard_account = Mock()
        env.approve = Mock()
        env.aws = Mock(side_effect=[
            {"tags": [{"Key": "EphemeralOwner", "Value": env.meta["owner"]}]},
            {"imageDetails": [{"imageDigest": sha}]},
        ])
        env.recover(yes=True, account=self.config["account_id"])
        self.assertTrue(env.meta["images"]["boundary"]["uri"].endswith("@" + sha))
        env.meta["pending_publications"] = {"boundary": pending}
        env.meta["images"] = {}
        digest_file.unlink()
        env.save()
        env.aws = Mock(return_value={"tags": [{"Key": "EphemeralOwner", "Value": env.meta["owner"]}]})
        env.recover(yes=True, account=self.config["account_id"])
        self.assertNotIn("boundary", env.meta["images"])
        self.assertEqual(len(env.meta["abandoned_publications"]), 1)
        self.assertFalse(env.meta["pending_publications"])

    def test_build_failures_preserve_private_redacted_diagnostics_not_secret_commands(self):
        runner = eph.Runner(self.config)
        runner.log_dir = self.base / "logs"
        result = subprocess.CompletedProcess([], 2, b"full compiler stdout token-value", b"full compiler stderr")
        with patch.object(subprocess, "run", return_value=result):
            with self.assertRaisesRegex(eph.Error, "private diagnostics"):
                runner.run(["make", "build-amd64"], log=True, build=True,
                           extra_env={"GITHUB_TOKEN": "token-value"})
        logs = list(runner.log_dir.glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
        self.assertIn("full compiler stdout", logs[0].read_text())
        self.assertIn("full compiler stderr", logs[0].read_text())
        self.assertNotIn("token-value", logs[0].read_text())
        with patch.object(subprocess, "run", return_value=result):
            with self.assertRaises(eph.Error):
                runner.run(["vault", "read", "reference"])
        self.assertEqual(len(list(runner.log_dir.glob("*.log"))), 1)

    def interrupted_delete(self):
        env = self.prepared()
        repository = "eph-example/boundary"
        arn = "arn:aws:ecr:us-west-2:123456789012:repository/" + repository
        env.meta["repositories"] = {"boundary": repository}
        env.save()
        env.guard_account = Mock()
        env.approve = Mock()
        deleted = False
        def aws(*args, **kwargs):
            nonlocal deleted
            if args[:2] == ("ecr", "describe-repositories"):
                if deleted:
                    raise eph.CommandError(
                        "repository absent",
                        b"An error occurred (RepositoryNotFoundException) when calling "
                        b"the DescribeRepositories operation: repository does not exist",
                    )
                return {"repositories": [{"repositoryArn": arn}]}
            if args[:2] == ("ecr", "list-tags-for-resource"):
                return {"tags": [{"Key": "EphemeralOwner", "Value": env.meta["owner"]}]}
            if args[:2] == ("ecr", "delete-repository"):
                persisted = json.loads((env.home / "environment.json").read_text())
                journal = persisted["pending_ecr_deletions"]["boundary"]
                self.assertEqual(journal["arn"], arn)
                self.assertEqual(journal["account_id"], self.config["account_id"])
                self.assertEqual(journal["region"], self.config["region"])
                self.assertEqual(journal["owner"], env.meta["owner"])
                self.assertIs(journal["owner_verified"], True)
                deleted = True
                # The AWS deletion succeeded, but the next ledger write never ran.
                raise KeyboardInterrupt("simulated process death after remote deletion")
            raise AssertionError("Unexpected AWS call")
        env.aws = Mock(side_effect=aws)
        with self.assertRaises(KeyboardInterrupt):
            env.destroy(yes=True, account=self.config["account_id"])
        env.approve.reset_mock()
        return env, repository, arn

    def test_interrupted_repository_delete_requires_explicit_recovery_then_destroy_completes(self):
        env, repository, _ = self.interrupted_delete()
        calls = env.aws.call_count
        with self.assertRaisesRegex(eph.Error, "recover"):
            env.destroy(yes=True, account=self.config["account_id"])
        self.assertEqual(env.aws.call_count, calls)
        self.assertNotIn(repository, env.meta.get("deleted_repositories", []))
        env.recover(yes=True, account=self.config["account_id"])
        env.approve.assert_called_once()
        self.assertIn(repository, env.meta["deleted_repositories"])
        self.assertFalse(env.meta["pending_ecr_deletions"])
        env.destroy(yes=True, account=self.config["account_id"])
        self.assertEqual(env.meta["status"], "destroyed")
        self.assertEqual(sum(call.args[:2] == ("ecr", "delete-repository")
                             for call in env.aws.call_args_list), 1)

    def test_delete_recovery_rejects_wrong_account_region_name_or_owner_journals(self):
        env, _, _ = self.interrupted_delete()
        original = copy.deepcopy(env.meta)
        for field, value in (
            ("account_id", "999999999999"), ("region", "eu-west-1"),
            ("repository", "foreign/repository"), ("arn", "arn:aws:ecr:foreign"),
            ("owner", "foreign-owner"), ("owner_verified", False),
        ):
            with self.subTest(field=field):
                env.meta = copy.deepcopy(original)
                env.meta["pending_ecr_deletions"]["boundary"][field] = value
                env.save()
                env.aws.reset_mock()
                with self.assertRaisesRegex(eph.Error, "journal does not match"):
                    env.recover(yes=True, account=self.config["account_id"])
                env.aws.assert_not_called()
                env.approve.assert_not_called()

    def test_delete_recovery_still_requires_matching_live_aws_account(self):
        env, _, _ = self.interrupted_delete()
        env.guard_account = eph.Environment.guard_account.__get__(env)
        env.aws = Mock(return_value={
            "Account": "999999999999", "Arn": "arn:aws:iam::999999999999:user/wrong",
        })
        with self.assertRaisesRegex(eph.Error, "does not match"):
            env.recover(yes=True, account=self.config["account_id"])
        env.aws.assert_called_once_with("sts", "get-caller-identity")
        env.approve.assert_not_called()

    def test_delete_recovery_refuses_a_recreated_unowned_repository(self):
        env, repository, arn = self.interrupted_delete()
        env.aws = Mock(side_effect=[
            {"repositories": [{"repositoryArn": arn}]},
            {"tags": [{"Key": "EphemeralOwner", "Value": "somebody-else"}]},
        ])
        with self.assertRaisesRegex(eph.Error, "now-unowned"):
            env.recover(yes=True, account=self.config["account_id"])
        env.approve.assert_not_called()
        self.assertIn("boundary", env.meta["pending_ecr_deletions"])
        self.assertNotIn(repository, env.meta.get("deleted_repositories", []))
        self.assertFalse(any(call.args[:2] == ("ecr", "delete-repository")
                             for call in env.aws.call_args_list))

    def test_delete_recovery_does_not_swallow_other_aws_errors(self):
        env, _, arn = self.interrupted_delete()
        for effects in (
            [eph.CommandError("denied", b"An error occurred (AccessDeniedException) when calling "
                              b"the DescribeRepositories operation: denied")],
            [{"repositories": [{"repositoryArn": arn}]},
             eph.CommandError("tag lookup failed", b"An error occurred (RepositoryNotFoundException) "
                              b"when calling the ListTagsForResource operation: missing")],
        ):
            with self.subTest(effects=len(effects)):
                env.aws = Mock(side_effect=effects)
                with self.assertRaises(eph.CommandError):
                    env.recover(yes=True, account=self.config["account_id"])
                env.approve.assert_not_called()
                self.assertIn("boundary", env.meta["pending_ecr_deletions"])

    def test_delete_recovery_of_existing_owned_repository_requires_fresh_destroy_approval(self):
        env, repository, arn = self.interrupted_delete()
        description = {"repositories": [{"repositoryArn": arn}]}
        tags = {"tags": [{"Key": "EphemeralOwner", "Value": env.meta["owner"]}]}
        env.aws = Mock(side_effect=[description, tags, description, tags, {}])
        env.recover(yes=True, account=self.config["account_id"])
        self.assertFalse(env.meta["pending_ecr_deletions"])
        self.assertNotIn(repository, env.meta.get("deleted_repositories", []))
        self.assertFalse(any(call.args[:2] == ("ecr", "delete-repository")
                             for call in env.aws.call_args_list))
        env.approve.reset_mock()
        env.destroy(yes=True, account=self.config["account_id"])
        env.approve.assert_called_once()
        self.assertEqual(env.meta["status"], "destroyed")

    def test_destroy_never_journals_or_deletes_an_unowned_repository(self):
        env = self.prepared()
        env.meta["repositories"] = {"boundary": "eph-example/boundary"}
        env.save()
        env.guard_account = Mock()
        env.approve = Mock()
        env.aws = Mock(side_effect=[
            {"repositories": [{"repositoryArn":
                "arn:aws:ecr:us-west-2:123456789012:repository/eph-example/boundary"}]},
            {"tags": [{"Key": "EphemeralOwner", "Value": "foreign-owner"}]},
        ])
        with self.assertRaisesRegex(eph.Error, "unowned"):
            env.destroy(yes=True, account=self.config["account_id"])
        env.approve.assert_not_called()
        self.assertFalse(env.meta.get("pending_ecr_deletions"))
        self.assertFalse(any(call.args[:2] == ("ecr", "delete-repository")
                             for call in env.aws.call_args_list))


if __name__ == "__main__":
    unittest.main()
