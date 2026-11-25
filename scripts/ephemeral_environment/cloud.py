"""AWS account/singleton ownership guards and memory-only Vault secret transport."""

import contextlib
import os
import re
import shlex
import sys
import textwrap

from .common import Error, canonical
from .config import https_url
from .runtime import CommandError


class Cloud:
    def aws(self, *args, json_output=True, **kwargs):
        if len(args) > 1 and args[1] in {"create-repository", "delete-repository",
                                       "put-secret-value", "update-service", "stop-task",
                                       "delete-access-point", "deregister-task-definition",
                                       "delete-objects", "abort-multipart-upload",
                                       "delete-model-invocation-logging-configuration"}:
            self.guard_account()
        command = ["aws", "--profile", self.config["profile"], "--region",
                   self.config["region"], *args]
        if json_output:
            # AWS CLI can emit no stdout for this successful GET when the
            # account/region has no invocation-logging configuration. Limit
            # that exception to this read; errors and malformed JSON still fail.
            if args[:2] == ("bedrock", "get-model-invocation-logging-configuration"):
                kwargs["allow_empty_object"] = True
            return self.runner.json([*command, "--output", "json"],
                                    label=f"aws {args[0]} {args[1]}", **kwargs)
        return self.runner.run(command, **kwargs)

    def guard_account(self, explicit_account=None, yes=False):
        if yes and explicit_account != self.config["account_id"]:
            raise Error("--yes requires --account-id matching config account_id")
        if explicit_account and explicit_account != self.config["account_id"]:
            raise Error("Explicit account does not match configuration")
        identity = self.aws("sts", "get-caller-identity")
        if identity.get("Account") != self.config["account_id"]:
            raise Error("AWS identity does not match approved account")
        if not re.match(rf"^arn:aws:(?:iam|sts)::{self.config['account_id']}:", identity.get("Arn", "")):
            raise Error("Only the AWS commercial partition is supported")

    @contextlib.contextmanager
    def approval_scope(self, action):
        """One approval for this invocation, never for a later command or target."""
        if getattr(self, "_operation_approval", None) is not None:
            raise Error("An operation approval scope is already active")
        self._operation_approval = {
            "action": action, "target": (self.name, self.config["account_id"], self.config["region"]),
            "approved": False,
        }
        try:
            yield
        finally:
            self._operation_approval = None

    def approve(self, description, yes=False):
        scope = getattr(self, "_operation_approval", None)
        if scope:
            if scope["target"] != (self.name, self.config["account_id"], self.config["region"]):
                raise Error("Operation target changed after approval scope was established")
            if not scope["approved"]:
                if scope["action"] == "reset":
                    print(f"\nReset local record: {self.name}\n")
                else:
                    print(f"\n{scope['action'].capitalize()} environment: {self.name}")
                    print(f"AWS: {self.config['account_id']}/{self.config['region']}\n")
                if scope["action"] == "destroy":
                    print("Delete\n"
                          "  - Boundary/TA stacks, sessions and investigation resources\n"
                          "  - ECR images, workspaces, dev audit data and Bedrock payloads\n"
                          "  - Owned account/region Bedrock logging\n\n"
                          "Warnings\n"
                          "  - Workspace and dev audit data will be lost. Export needed evidence first.\n"
                          "  - Bedrock model inputs/outputs may include invocations outside Boundary.\n"
                          "  - Retention/holds will not be bypassed.\n")
                elif scope["action"] == "deploy":
                    print("Deploy\n"
                          "  - Build/publish images and apply Boundary/TA infrastructure\n"
                          "  - Populate service credentials and restart TA on redeploy\n"
                          "  - No Keycloak or OCM resources are created\n")
                elif scope["action"] == "recover":
                    print("Recover\n"
                          "  - Reconcile verified interrupted-operation journals\n"
                          "  - Do not finish deployment or delete foreign resources\n")
        # Keep paths and identifiers intact; wrap prose without flattening lists.
        for line in description.splitlines():
            indent = line[:len(line) - len(line.lstrip())]
            print(textwrap.fill(line, width=88, subsequent_indent=indent,
                                break_long_words=False, break_on_hyphens=False) if line else "")
        if not yes and not (scope and scope["approved"]):
            if scope:
                print("\nOne approval for this command."
                      + (" No AWS operations." if scope["action"] == "reset"
                         else " Account and ownership checks still apply."))
            print()
            if not sys.stdin.isatty() or input(f"Type {self.name} to approve: ") != self.name:
                raise Error("Approval declined")
        print(flush=True)
        if scope:
            scope["approved"] = True

    def preflight(self):
        """Singleton resources must never silently overwrite another stack."""
        if "ta_tls" in self.config:
            tls = self.config["ta_tls"]
            certificate = self.aws("acm", "describe-certificate",
                                   "--certificate-arn", tls["certificate_arn"])["Certificate"]
            hostname = tls["internal_fqdn"]
            def matches(pattern):
                if pattern == hostname:
                    return True
                return (pattern.startswith("*.") and hostname.endswith(pattern[1:])
                        and len(hostname.split(".")) == len(pattern.split(".")))
            if certificate.get("Status") != "ISSUED" or not any(
                matches(pattern) for pattern in certificate.get("SubjectAlternativeNames", [])
            ):
                raise Error("TA TLS certificate must be ISSUED and cover internal_fqdn")
        providers = self.aws("iam", "list-open-id-connect-providers")
        issuer = self.config["keycloak"]["issuer_url"].removeprefix("https://").rstrip("/")
        found = set()
        for entry in providers.get("OpenIDConnectProviderList", []):
            suffix = entry["Arn"].split("oidc-provider/", 1)[-1]
            found.add(suffix)
            if suffix == issuer:
                details = self.aws("iam", "get-open-id-connect-provider",
                                   "--open-id-connect-provider-arn", entry["Arn"])
                owned = {tag["Key"]: tag["Value"] for tag in details.get("Tags", [])}
                was_created = bool(self.meta["roots"].get("boundary-account", {}).get("touched"))
                if (self.config["keycloak"].get("existing_provider_arn") != entry["Arn"]
                        and not (was_created and owned.get("EphemeralOwner") == self.meta["owner"])):
                    raise Error(
                        "Existing Keycloak OIDC provider collides; explicit reference required.\n"
                        "This provider already exists in the AWS account; it may belong to a "
                        "manual deployment or another stack, not this tooling.\n"
                        f'Add "existing_provider_arn": "{entry["Arn"]}" to the keycloak '
                        "object in your private JSON config, after verifying the provider is "
                        "approved for reuse. This references it without adopting or deleting it.\n"
                        "IAM OIDC providers are account-wide: changing region will not resolve "
                        "this collision. Do not delete a shared provider to bypass this guard.\n"
                        "Preflight stopped before this deployment's AWS changes, but its local "
                        "record may already exist. Changing config can then cause a local "
                        "configuration mismatch; follow that error's guidance."
                    )
                if self.config["keycloak"]["client_id"] not in details.get("ClientIDList", []):
                    raise Error("Existing Keycloak provider lacks the configured audience")
            elif suffix == "token.actions.githubusercontent.com":
                details = self.aws("iam", "get-open-id-connect-provider",
                                   "--open-id-connect-provider-arn", entry["Arn"])
                owned = {tag["Key"]: tag["Value"] for tag in details.get("Tags", [])}
                was_created = bool(self.meta["roots"].get("boundary-account", {}).get("touched"))
                if (not self.config["boundary_account"].get("reference_existing_github_oidc")
                        and not (was_created and owned.get("EphemeralOwner") == self.meta["owner"])):
                    raise Error(
                        "Existing GitHub OIDC provider collides; explicit reference required.\n"
                        "After verifying this account-wide provider is approved for reuse, set "
                        '"reference_existing_github_oidc": true in boundary_account in your '
                        "private JSON config. The provider remains under its original ownership.\n"
                        "Changing region will not resolve this collision. Do not delete a shared "
                        "provider to bypass this guard. A local record may already exist from "
                        "this attempt; changing config can require a new EPHEMERAL_NAME."
                    )
        if self.config["keycloak"].get("existing_provider_arn") and issuer not in found:
            raise Error("Referenced Keycloak OIDC provider does not exist")
        if (self.config["boundary_account"].get("reference_existing_github_oidc")
                and "token.actions.githubusercontent.com" not in found):
            raise Error("Referenced GitHub OIDC provider does not exist")
        self.check_bedrock_logging()

    def check_bedrock_logging(self):
        """Never overwrite another stack's account/Region logging singleton."""
        live = self.aws("bedrock", "get-model-invocation-logging-configuration").get("loggingConfig")
        if not live:
            return
        try:
            owned = self.state_resource(
                "boundary-regional", "aws_bedrock_model_invocation_logging_configuration", "boundary")
        except Error:
            raise Error("Existing account/Region Bedrock invocation logging is not owned by "
                        "this environment; use an account/Region without a competing owner") from None
        try:
            config = owned["logging_config"][0]
            cloudwatch = config["cloudwatch_config"][0]
            large_data = cloudwatch["large_data_delivery_s3_config"][0]
            expected = {
                "logGroupName": cloudwatch["log_group_name"],
                "roleArn": cloudwatch["role_arn"],
                "largeDataDeliveryS3Config": {
                    "bucketName": large_data["bucket_name"], "keyPrefix": large_data["key_prefix"],
                },
            }
        except (KeyError, IndexError, TypeError):
            raise Error("Owned Bedrock logging state does not match the supported configuration") from None
        if (live.get("cloudWatchConfig") != expected or live.get("s3Config")
                or live.get("textDataDeliveryEnabled") is not True
                or any(live.get(field, False) for field in (
                    "imageDataDeliveryEnabled", "embeddingDataDeliveryEnabled", "videoDataDeliveryEnabled"))):
            raise Error("Live Bedrock invocation logging differs from this environment's owned "
                        "configuration; refusing to overwrite it")

    def vault_credentials(self):
        c = self.config["vault"]
        env = {"VAULT_ADDR": c["address"]}
        # Only Vault receives the caller's session token.
        if os.environ.get("VAULT_TOKEN"):
            env["VAULT_TOKEN"] = os.environ["VAULT_TOKEN"]
        try:
            data = self.runner.json(["vault", "read", "-format=json", c["path"]],
                                    label="vault read", extra_env=env)
        except CommandError:
            raise Error(
                "Vault could not read the configured secret. You may not be logged in, "
                "your token may have expired, or your token may lack read permission. "
                "Response contents withheld because they may contain credentials.\n"
                "Log in to the same Vault server configured for this environment:\n"
                f"  export VAULT_ADDR={shlex.quote(c['address'])}\n"
                "If VAULT_TOKEN contains an expired token, unset VAULT_TOKEN first; "
                "it overrides the CLI's saved login.\n"
                "For a server with approved OIDC/browser login:\n"
                "  vault login -no-print -method=oidc\n"
                "Use your administrator's auth mount/role if different (for example "
                "-path=<approved-auth-mount> role=<approved-role>); OIDC is not assumed "
                "to be enabled on every Vault server.\n"
                "For an existing approved token instead:\n"
                "  vault login -no-print\n"
                "Enter the token at the hidden prompt, never as a command argument. "
                "Login saves it in the Vault CLI token helper, not the deployment config.\n"
                "Verify the login without displaying its token:\n"
                "  vault token lookup -format=json >/dev/null\n"
                "Then retry make ephemeral-env; no reset is needed for this failure. "
                "If login succeeds but the read still fails, check policy access to vault.path "
                "(including /data/ for KV v2), server connectivity and CA trust. "
                "Do not paste token/secret responses into chat or logs.\n"
                "See docs/runbooks/ephemeral-development-environment.md#log-in-to-vault."
            ) from None
        if not isinstance(data, dict):
            raise Error("Vault read returned an unexpected response shape; expected a secret object. "
                        "Check vault.address and vault.path. Response contents withheld.")
        warnings = data.get("warnings")
        versioned_path_warning = isinstance(warnings, list) and any(
            isinstance(warning, str)
            and "invalid path for a versioned k/v" in warning.lower()
            for warning in warnings
        )
        data = data.get("data")
        if not isinstance(data, dict):
            detail = (
                "Vault reports an invalid path for a versioned KV secrets engine. "
                if versioned_path_warning else ""
            )
            raise Error(
                "Vault read returned no secret data. " + detail
                + "Check vault.path in your private JSON config: this tool uses vault read, "
                "so KV v2 needs the raw API path <mount>/data/<secret-path>, not the UI/CLI "
                "path <mount>/<secret-path> or a /metadata/ path. KV v1 does not use /data/. "
                "Confirm the mount and secret path with the secret owner; do not change the "
                "secret values or disclose the response. Retry after correcting the path; "
                "no AWS resources have been created by this deployment attempt. "
                "A config change to an unused record requires make ephemeral-env-reset first."
            )
        if isinstance(data.get("data"), dict):
            data = data["data"]
        try:
            values = {
                "client_id": data["trusted_actions_sa_client_id"],
                "client_secret": data["trusted_actions_sa_client_secret"],
                "backplane_url": data["trusted_actions_backplane_url"],
            }
        except (KeyError, TypeError):
            raise Error("Vault response lacks required service-account fields") from None
        if not all(isinstance(x, str) and x.strip() for x in values.values()):
            raise Error("Vault service-account fields must be nonempty strings")
        https_url(values["backplane_url"], "Vault Backplane URL")
        return values

    def put_credentials(self, secret_arn, credentials):
        expected = f"arn:aws:secretsmanager:{self.config['region']}:{self.config['account_id']}:secret:"
        if not secret_arn.startswith(expected):
            raise Error("Secret ARN is outside the approved account/region")
        tags = self.aws("secretsmanager", "describe-secret", "--secret-id", secret_arn)
        owned = {tag["Key"]: tag["Value"] for tag in tags.get("Tags", [])}
        if owned.get("EphemeralOwner") != self.meta["owner"]:
            raise Error("Refusing to populate an unowned secret")
        # TA has separately named settings for its two consumers, but this
        # development workflow deliberately supplies one service account.
        payload = canonical({
            "ocm_client_id": credentials["client_id"],
            "ocm_client_secret": credentials["client_secret"],
            "backplane_client_id": credentials["client_id"],
            "backplane_client_secret": credentials["client_secret"],
            "backplane_url": credentials["backplane_url"],
            "ocm_token": "",
        })
        response = self.aws("secretsmanager", "put-secret-value", "--secret-id", secret_arn,
                            "--secret-string", "file:///dev/stdin", input=payload)
        self.meta["secret_version_id"] = response.get("VersionId")
        self.save()
