"""Connection metadata and infrastructure readiness, not end-to-end access tests."""

import base64
import fcntl
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .common import Error, write_json
from .runtime import CommandError


class Readiness:
    def write_cli_profile(self, profile):
        """Use the selected source's CLI to merge YAML, never hand-parse it.

        Config init preserves other named profiles and the active selection.
        Stage its output privately and check it with the installed CLI before
        atomically publishing: the source helper may write a newer schema.
        """
        self.check_work()
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if not config_home.is_absolute():
            raise Error("XDG_CONFIG_HOME must be absolute for CLI profile registration")
        directory = config_home / "rosa-boundary"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        helper = self.source.parent / "profile-cli"
        self.runner.run(["make", "build-cli", f"CLI_BIN={helper}"],
                        cwd=self.work, build=True, log=True)
        self.check_work()
        args = [
            str(helper), "config", "init", "--name", self.name,
            "--auto-discover=false", "--overwrite", "--yes", "--use-device-code=false",
        ]
        for flag, key in (
            ("account-id", "account_id"), ("region", "aws_region"), ("project", "project"),
            ("environment", "environment"), ("keycloak-url", "keycloak_url"),
            ("realm", "keycloak_realm"), ("client-id", "oidc_client_id"),
            ("ocm-url", "ocm_url"), ("lambda-function-name", "lambda_function_name"),
            ("invoker-role-arn", "invoker_role_arn"), ("sre-role-arn", "sre_role_arn"),
            ("ecs-cluster", "ecs_cluster_name"), ("efs-filesystem-id", "efs_filesystem_id"),
        ):
            args.extend(["--" + flag, profile[key]])
        # Serialize concurrent ephemeral deployments updating the same document.
        # Leave the lock inode in place between operations.
        lock = directory / ".ephemeral-profiles.lock"
        if lock.is_symlink():
            raise Error("CLI profile lock must not be a symlink")
        with lock.open("a") as stream:
            os.chmod(lock, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            target = directory / "config.yaml"
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise Error("CLI configuration must be a regular file, not a symlink")
            original = target.read_bytes() if target.exists() else None
            with tempfile.TemporaryDirectory(prefix=".ephemeral-profile-", dir=directory) as staging:
                staged_home = Path(staging)
                staged = staged_home / "rosa-boundary" / "config.yaml"
                staged.parent.mkdir(mode=0o700)
                if original is not None:
                    staged.touch(mode=0o600)
                    staged.write_bytes(original)
                extra_env = {"XDG_CONFIG_HOME": str(staged_home)}
                try:
                    self.runner.run(args, build=True, extra_env=extra_env)
                except CommandError:
                    raise Error(
                        f"Could not register CLI profile '{self.name}' in {target}. "
                        "The CLI refuses invalid configs rather than discarding existing profiles. "
                        f"Inspect it with {helper} config list-profiles using the same "
                        "XDG_CONFIG_HOME. No AWS redeployment is needed to repair local profile setup."
                    ) from None
                installed = shutil.which("rosa-boundary", path=self.runner.env.get("PATH"))
                if installed:
                    try:
                        self.runner.run([installed, "config", "list-profiles"],
                                        build=True, extra_env=extra_env)
                    except CommandError:
                        raise Error(
                            "The installed rosa-boundary cannot read the proposed CLI configuration. "
                            "Your existing config was not changed. Update the workstation CLI from "
                            f"the selected source ({self.work}) with 'make install-cli', then retry. "
                            "No AWS redeployment is needed."
                        ) from None
                if target.is_symlink() or (
                    (target.read_bytes() if target.exists() else None) != original
                ):
                    raise Error("CLI configuration changed during registration; retry without concurrent config edits")
                os.chmod(staged, 0o600)
                os.replace(staged, target)
        return directory / "config.yaml"

    def ta_endpoint(self, outputs):
        """Select the approved TLS name or AWS's private ALB DNS name."""
        if "ta_tls" in self.config:
            expected = "https://" + self.config["ta_tls"]["internal_fqdn"]
            if outputs.get("internal_fqdn") != expected:
                raise Error("TA output does not match the approved HTTPS endpoint")
            return expected
        hostname = outputs.get("alb_dns_name")
        region = re.escape(self.config["region"])
        if not isinstance(hostname, str) or not re.fullmatch(
            rf"internal-[a-zA-Z0-9-]+\.{region}\.elb\.amazonaws\.com", hostname
        ):
            raise Error("HTTP TA endpoint must be an internal ALB DNS name in the configured region")
        return "http://" + hostname

    def refresh_ta_service(self):
        """ECS resolves Secrets Manager versions at task startup, not on rotation."""
        ta = self.outputs("ta-regional")
        service_name = self.state_resource("ta-regional", "aws_ecs_service", "app")["name"]
        response = self.aws("ecs", "update-service", "--cluster", ta["ecs_cluster_name"],
                            "--service", service_name, "--force-new-deployment")
        primary = [item for item in response.get("service", {}).get("deployments", [])
                   if item.get("status") == "PRIMARY"]
        if len(primary) != 1:
            raise Error("TA ECS restart did not return a unique PRIMARY deployment")
        self.meta["expected_ta_deployment"] = primary[0]["id"]
        self.save()

    def write_connection(self):
        ta = self.meta["outputs"]["trusted_actions"]
        boundary = self.meta["outputs"]["boundary"]
        endpoint = self.ta_endpoint(ta)
        issuer, realm = self.config["keycloak"]["issuer_url"].rstrip("/").rsplit("/realms/", 1)
        profile = {
            "account_id": self.config["account_id"], "environment": "dev",
            "project": f"eph-{self.name}", "aws_region": self.config["region"],
            "keycloak_url": issuer, "keycloak_realm": realm,
            "ocm_url": self.config["ocm"]["base_url"],
            "oidc_client_id": self.config["keycloak"]["client_id"],
            "oidc_use_device_code": False,
            "lambda_function_name": boundary["lambda_function_name"],
            "invoker_role_arn": boundary["lambda_invoker_role_arn"],
            "sre_role_arn": boundary["sre_shared_role_arn"],
            "ecs_cluster_name": boundary["ecs_cluster_name"],
            "efs_filesystem_id": boundary["efs_filesystem_id"],
        }
        config_path = self.write_cli_profile(profile)
        self.meta["connection"] = {
            "trusted_actions_url": endpoint,
            "usage": "Private endpoint; reach from Boundary sessions in this environment's VPC",
            "keycloak_issuer_url": self.config["keycloak"]["issuer_url"],
            "keycloak_client_id": self.config["keycloak"]["client_id"],
            "ocm_url": self.config["ocm"]["base_url"],
            "cli_profile": self.name,
            "cli_config_path": str(config_path),
            "cli_command": f"rosa-boundary --profile {self.name}",
            "client_environment": {"RTA_SERVER": endpoint,
                                   "RTA_ALLOW_INSECURE_HTTP": str("ta_tls" not in self.config).lower()},
        }
        write_json(self.home / "connection.json", self.meta["connection"])
        self.save()

    def verify_deployment(self):
        """Check infrastructure readiness only; never start cluster actions/tasks."""
        ta = self.meta["outputs"]["trusted_actions"]
        service_name = self.state_resource("ta-regional", "aws_ecs_service", "app")["name"]
        self.aws("ecs", "wait", "services-stable", "--cluster", ta["ecs_cluster_name"],
                 "--services", service_name, json_output=False)
        services = self.aws("ecs", "describe-services", "--cluster", ta["ecs_cluster_name"],
                            "--services", service_name)
        if services.get("failures") or len(services.get("services", [])) != 1:
            raise Error("TA ECS service readiness could not be established")
        service = services["services"][0]
        if (service.get("status") != "ACTIVE" or service.get("desiredCount", 0) < 1
                or service.get("runningCount") != service["desiredCount"]
                or service.get("pendingCount", 0)):
            raise Error("TA ECS service is not ready")
        primary = [item for item in service.get("deployments", [])
                   if item.get("status") == "PRIMARY"]
        if (len(primary) != 1 or len(service["deployments"]) != 1
                or primary[0].get("taskDefinition") != service["taskDefinition"]
                or primary[0].get("rolloutState") != "COMPLETED"
                or (self.meta.get("expected_ta_deployment")
                    and primary[0].get("id") != self.meta["expected_ta_deployment"])):
            raise Error("TA ECS PRIMARY deployment is not the expected completed deployment")
        definition = self.aws("ecs", "describe-task-definition",
                              "--task-definition", service["taskDefinition"])["taskDefinition"]
        if not any(container.get("image") == self.meta["images"]["ta"]["uri"]
                   for container in definition.get("containerDefinitions", [])):
            raise Error("TA service does not reference this snapshot's immutable image")
        boundary = self.meta["outputs"]["boundary"]
        definition = self.aws("ecs", "describe-task-definition",
                              "--task-definition", boundary["task_definition_arn"])["taskDefinition"]
        if not any(container.get("image") == self.meta["images"]["boundary"]["uri"]
                   for container in definition.get("containerDefinitions", [])):
            raise Error("Boundary task definition does not reference this snapshot's image")
        lambdas = {}
        state = json.loads((self.home / "boundary-regional.tfstate").read_text())
        for resource in state.get("resources", []):
            if resource.get("mode") != "managed" or resource["type"] != "aws_lambda_function":
                continue
            for instance in resource.get("instances", []):
                attributes = instance["attributes"]
                filename = (self.root("boundary-regional") / attributes["filename"]).resolve()
                if not filename.is_relative_to(self.root("boundary-regional").resolve()):
                    raise Error("Lambda package path escapes the isolated deployment root")
                local_hash = base64.b64encode(hashlib.sha256(filename.read_bytes()).digest()).decode()
                if local_hash != attributes["source_code_hash"]:
                    raise Error("Lambda ZIP no longer matches the applied Terraform state")
                function = self.aws("lambda", "get-function-configuration",
                                    "--function-name", attributes["function_name"])
                if (function.get("State") != "Active"
                        or function.get("LastUpdateStatus") != "Successful"
                        or function.get("PackageType") != "Zip"
                        or function.get("CodeSha256") != local_hash):
                    raise Error("Boundary Lambda is not active with the expected Zip code hash")
                lambdas[attributes["function_name"]] = {"code_sha256": local_hash,
                                                       "package_type": "Zip"}
        if len(lambdas) != 3:
            raise Error("Expected all three Boundary Lambda ZIP deployments")
        self.meta["verification"] = {
            "ta_ecs_service": "stable", "task_definition_images": "matched",
            "lambdas": lambdas, "private_api_connectivity": "unverified",
            "cluster_actions": "not-run",
            "generation": self.generation,
            "source_fingerprint": self.meta.get("source", {}).get("fingerprint"),
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save()
