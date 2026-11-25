"""Prepared work integrity and narrow, private ephemeral Terraform adaptations.

Shared Terraform remains unchanged. Generated overrides select owned local state,
leave TA secret values to the script, and optionally reference an existing OIDC
provider. Generated inputs and executable caches are journaled, not trusted merely
because their filenames look like build outputs.
"""

import json
import os
from pathlib import Path
import re
import shutil

from .common import Error, ROOTS, canonical, digest, write_json
from .snapshot import tree_manifest, writable_copy


class Workspace:
    """Workspace operations used by Environment; no independent lifecycle."""

    @property
    def work(self):
        if self.generation:
            return self.source.parent / "work"
        return self.home / "work"

    def root(self, key):
        return self.work / ROOTS[key]

    def generation_record(self):
        return (self.meta["generations"][self.generation] if self.generation else self.meta)

    def prepare(self):
        """Prepare a private copy; never initialize Terraform in the source tree."""
        if self.work.exists():
            return
        writable_copy(self.source, self.work)
        (self.work / ".env").write_text("")
        (self.home / "terraform.rc").write_text("")
        self.runner.env["TF_CLI_CONFIG_FILE"] = str(self.home / "terraform.rc")
        for key in ROOTS:
            root = self.root(key)
            if not root.is_dir():
                raise Error(f"Snapshot lacks {key} Terraform root")
            for path in root.rglob("*"):
                if path.is_symlink():
                    raise Error("Terraform deployment roots may not contain symlinks")
            for path in root.rglob("*"):
                if path.name.endswith((".auto.tfvars", ".auto.tfvars.json", "_override.tf",
                                       "_override.tf.json")) or path.name in {
                                           "override.tf", "override.tf.json",
                                           "terraform.tfvars", "terraform.tfvars.json"}:
                    raise Error("Snapshot contains implicit Terraform configuration")
            override = {"terraform": {"backend": {"local": {
                "path": str(self.home / f"{key}.tfstate"),
            }}}}
            if key == "ta-regional":
                override["data"] = {"terraform_remote_state": {"global": {
                    "backend": "local",
                    "config": {"path": str(self.home / "ta-global.tfstate")},
                }}}
                # Private deployment wiring only: the shared service account
                # reach ECS via Secrets Manager selectors, never app_config/S3
                # or Terraform values. Keep the original secret/token bindings.
                override["locals"] = {"container_secrets": [
                    {"name": name,
                     "valueFrom": "${local.global.secretsmanager_secret_arn}:" + field + "::"}
                    for name, field in (
                        ("ROSA_TA_OCM_CLIENT_SECRET", "ocm_client_secret"),
                        ("ROSA_TA_OCM_TOKEN", "ocm_token"),
                        ("ROSA_TA_BACKPLANE_CLIENT_SECRET", "backplane_client_secret"),
                        ("ROSA_TA_OCM_CLIENT_ID", "ocm_client_id"),
                        ("ROSA_TA_BACKPLANE_CLIENT_ID", "backplane_client_id"),
                        ("ROSA_TA_BACKPLANE_URL", "backplane_url"),
                    )
                ]}
            elif key == "ta-global":
                # The original root always declares this resource. A private
                # count override excludes all secret versions from local state.
                override["resource"] = {"aws_secretsmanager_secret_version": {
                    "app": {"count": 0},
                }}
            elif key == "boundary-account" and self.config["keycloak"].get("existing_provider_arn"):
                arn = self.config["keycloak"]["existing_provider_arn"]
                # Both original singleton references must be replaced when
                # count=0; never import or destroy the externally owned provider.
                override["resource"] = {"aws_iam_openid_connect_provider": {
                    "keycloak": {"count": 0},
                }}
                override["module"] = {"shared_iam": {"keycloak_provider_arn": arn}}
                override["output"] = {"oidc_provider_arn": {"value": arn}}
            elif key == "boundary-account" and "thumbprint" not in self.config["keycloak"]:
                # AWS IAM retrieves the top intermediate CA thumbprint on create.
                # Per the public hashicorp/aws provider docs, null omits this
                # optional/computed argument; do not pin a leaf certificate or
                # weaken TLS verification with a local scraping fallback.
                override["resource"] = {"aws_iam_openid_connect_provider": {
                    "keycloak": {"thumbprint_list": None},
                }}
            write_json(root / "ephemeral_override.tf.json", override)
        self.meta["work_fingerprint"] = self.work_fingerprint()
        self.save()

    def generated_input(self, path, value):
        """Write and journal one script-owned input (never credential material)."""
        write_json(path, value)
        name = path.relative_to(self.work).as_posix()
        self.generation_record().setdefault("generated_inputs", {})[name] = digest(path.read_bytes())
        self.save()

    def record_terraform_outputs(self, key):
        """Checkpoint init/plan products, including executable providers/modules."""
        record = self.generation_record()
        cache = self.root(key) / ".terraform"
        if cache.exists():
            if cache.is_symlink():
                raise Error("Terraform cache may not be a symlink")
            record.setdefault("terraform_caches", {})[key] = digest(canonical(tree_manifest(cache)))
        lock = self.root(key) / ".terraform.lock.hcl"
        if lock.exists() and not (self.source / ROOTS[key] / lock.name).exists():
            if lock.is_symlink() or not lock.is_file():
                raise Error("Terraform dependency lock must be a regular file")
            record.setdefault("generated_inputs", {})[lock.relative_to(self.work).as_posix()] = digest(lock.read_bytes())
        self.save()

    def work_fingerprint(self):
        """Hash every prepared input; only verified generated products are omitted."""
        record = self.generation_record()
        generated = record.get("generated_inputs", {})
        source_names = {row[0] for row in tree_manifest(self.source)}
        package = self.work / "lambda/create-investigation"
        package_hash = (record.get("lambda_build_fingerprint")
                        or record.get("lambda_package_fingerprint"))
        if package_hash and digest(canonical(tree_manifest(package))) != package_hash:
            raise Error("Generated Lambda deployment package changed")
        caches = {str(Path(relative) / ".terraform"): key for key, relative in ROOTS.items()}
        entries = []
        # os.walk lets us prune only exact, verified Terraform cache directories.
        for directory, directories, files in os.walk(self.work, followlinks=False):
            for item in sorted([*directories, *files]):
                path = Path(directory) / item
                name = path.relative_to(self.work).as_posix()
                if name in caches:
                    key = caches[name]
                    expected = record.get("terraform_caches", {}).get(key)
                    if (path.is_symlink() or not path.is_dir() or expected is None
                            or digest(canonical(tree_manifest(path))) != expected):
                        raise Error("Generated Terraform cache changed or is unrecorded")
                    directories.remove(item)
                    continue
                if name in generated:
                    if (path.is_symlink() or not path.is_file()
                            or digest(path.read_bytes()) != generated[name]):
                        raise Error("Generated deployment input changed")
                    continue
                if (package_hash and name not in source_names
                        and path.is_relative_to(package)):
                    continue  # The complete package manifest was checked above.
                if path.is_symlink():
                    entries.append([name, "symlink", os.readlink(path)])
                elif path.is_file():
                    entries.append([name, "executable" if path.stat().st_mode & 0o111 else "file",
                                    digest(path.read_bytes())])
                elif not path.is_dir():
                    raise Error("Special files are forbidden in deployment work")
        for name in generated:
            if not (self.work / name).is_file():
                raise Error("Generated deployment input is missing")
        for key in record.get("terraform_caches", {}):
            if not (self.root(key) / ".terraform").is_dir():
                raise Error("Generated Terraform cache is missing")
        return digest(canonical(sorted(entries)))

    def check_work(self):
        self.runner.env["TF_CLI_CONFIG_FILE"] = str(self.home / "terraform.rc")
        cli_config = self.home / "terraform.rc"
        if cli_config.is_symlink() or not cli_config.is_file() or cli_config.read_bytes() != b"":
            raise Error("Private Terraform CLI configuration changed")
        if self.generation:
            source = self.meta["generations"][self.generation]["source"]
            if digest(canonical(tree_manifest(self.source))) != source["fingerprint"]:
                raise Error("Immutable source generation was modified")
        expected = self.generation_record()["work_fingerprint"]
        if self.work_fingerprint() != expected:
            raise Error("Deployment source/wiring changed after environment preparation")
        for key in ROOTS:
            for path in self.root(key).glob("*.tf*"):
                if path.suffix not in {".tf", ".json"} or path.name.endswith("tfvars.json"):
                    continue
                text = path.read_text()
                if re.search(r'\bcloud\s*\{|\bbackend\s+"(?:remote|s3|azurerm|gcs)"', text):
                    raise Error("Remote/cloud Terraform backends are forbidden")
                if re.search(r'backend\s*=\s*"(?!local")[^"]+"', text):
                    if key != "ta-regional" or path.name != "remote_state.tf":
                        raise Error("Nonlocal Terraform remote-state access is forbidden")
                    override = json.loads((self.root(key) / "ephemeral_override.tf.json").read_text())
                    data = override.get("data", {}).get("terraform_remote_state", {}).get("global", {})
                    if (data.get("backend") != "local"
                            or data.get("config") != {"path": str(self.home / "ta-global.tfstate")}):
                        raise Error("TA global state reader is not wired to the owned local state")

    def declared_variables(self, key, source=False):
        root = (self.source / ROOTS[key]) if source else self.root(key)
        return set(re.findall(r'\bvariable\s+"([^"]+)"\s*\{',
                              "\n".join(path.read_text() for path in root.glob("*.tf"))))

    def check_capabilities(self):
        """Unknown tfvars only warn: require safety features before any AWS call."""
        required = {
            "boundary-account": {"aws_account_id", "create_github_actions_oidc_provider",
                                 "adopt_existing_resources", "enable_lambda_ecr_source",
                                 "lambda_ecr_replication_regions", "bedrock_model_agreements",
                                 "enable_s3_replication_role", "bedrock_logging_source_regions",
                                 "replication"},
            "boundary-network": {"aws_account_id"},
            "boundary-regional": {"aws_account_id", "ephemeral_environment",
                                  "rta_server_url", "bedrock_model_agreements", "replication"},
            "ta-global": {"backplane_client_secret"},
            "ta-regional": {"ephemeral_environment"},
        }
        for key, names in required.items():
            missing = names - self.declared_variables(key, source=True)
            if missing:
                raise Error(f"Unsupported snapshot: {key} lacks required safety capabilities: "
                            + ", ".join(sorted(missing)))
        for relative, component in (("components/trusted-actions/Makefile", "Trusted Actions"),
                                    ("Makefile", "Boundary")):
            makefile = self.source / relative
            if not makefile.is_file() or not re.search(r"^build-amd64\s*:", makefile.read_text(), re.M):
                raise Error(f"Unsupported snapshot: {component} lacks the build-amd64 Make target")
            if relative == "Makefile" and not re.search(r"^build-cli\s*:", makefile.read_text(), re.M):
                raise Error("Unsupported snapshot: Boundary lacks the build-cli profile registration target")
        containerfile = self.source / "Containerfile"
        if not containerfile.is_file() or " AS rta-builder" not in containerfile.read_text():
            raise Error("Unsupported snapshot: Boundary image lacks the source-built rta CLI")
        if "ta_tls" not in self.config:
            for relative, marker in (
                ("deploy/regional/ecs.tf", "RTA_ALLOW_INSECURE_HTTP"),
                ("components/trusted-actions/internal/rta/command.go", "allow-insecure-http"),
            ):
                path = self.source / relative
                if not path.is_file() or marker not in path.read_text():
                    raise Error("Unsupported snapshot: private HTTP requires the dev RTA "
                                "credential opt-in; select a newer source or configure ta_tls")

    def require_tools(self):
        missing = [tool for tool in ("aws", "terraform", "make", "podman", "vault", "uv", "go")
                   if shutil.which(tool, path=self.runner.env.get("PATH")) is None]
        if missing:
            raise Error("Required executables are missing: " + ", ".join(missing))
