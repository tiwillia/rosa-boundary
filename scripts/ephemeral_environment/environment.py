"""Environment identity, immutable generations and dependency-ordered deployment.

The operation mixins have one owner and one injected Runner. They divide the
workflow by responsibility without introducing separate service lifecycles.
"""

import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import uuid

from .artifacts import Artifacts
from .cloud import Cloud
from .cleanup import BoundaryCleanup
from .common import Error, ROOTS, SCHEMA, canonical, digest, private_directory, write_json
from .readiness import Readiness
from .recovery import Recovery
from .reset import Reset
from .runtime import Runner
from .snapshot import snapshot, tree_manifest
from .terraform import Terraform
from .workspace import Workspace


class Environment(Workspace, Terraform, Cloud, Artifacts, Readiness, Recovery, Reset, BoundaryCleanup):
    def __init__(self, repository, config, name, runner=None):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,17}", name):
            raise Error("name must be 1-18 lowercase letters/digits/hyphens, starting with a letter")
        longest_bucket = (f"{config['account_id']}-eph-{name}-dev-{config['region']}"
                          "-bedrock-invocations")
        if len(longest_bucket) > 63:
            raise Error("name is too long for this region's generated S3 bucket names; choose a shorter name")
        self.repository = Path(repository).resolve()
        self.config = config
        self.name = name
        self.home = Path(config["state_dir"]) / name
        self.runner = runner or Runner(config)
        self.runner.log_dir = self.home / "logs"
        self.meta = None
        self._generation = None

    @contextlib.contextmanager
    def locked(self):
        private_directory(Path(self.config["state_dir"]))
        private_directory(self.home)
        lock = self.home / "lock"
        if lock.is_symlink():
            raise Error("Invalid environment lock")
        with lock.open("a") as stream:
            os.chmod(lock, 0o600)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Error("Another operation owns this environment lock") from None
            self.runner.lock_fd = stream.fileno()
            try:
                yield
            finally:
                self.runner.lock_fd = None

    def save(self):
        if self.meta.get("active_generation"):
            record = self.meta["generations"][self.meta["active_generation"]]
            record.update(source=self.meta["source"], images=self.meta["images"])
            if "work_fingerprint" in self.meta:
                record["work_fingerprint"] = self.meta["work_fingerprint"]
            if self.meta.get("verification", {}).get("generation") == self.meta["active_generation"]:
                record["verification"] = self.meta["verification"]
        self.meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_json(self.home / "environment.json", self.meta)

    def load(self):
        try:
            self.meta = json.loads((self.home / "environment.json").read_text())
        except (ValueError, OSError):
            raise Error("No valid owned environment metadata; refusing adoption") from None
        if (self.meta.get("schema") != SCHEMA or self.meta.get("name") != self.name
                or self.meta.get("home") != str(self.home)):
            raise Error(
                f"Environment ownership/configuration mismatch in {self.home}: "
                "the recorded schema, name or state location does not match this invocation. "
                "Preserve this directory and use the original name, state location and "
                "compatible tooling; do not edit ownership metadata or adopt another environment."
            )
        if self.meta.get("config_hash") != digest(canonical(self.config)):
            changed = ""
            try:
                original = json.loads((self.home / "config.json").read_text())
                if digest(canonical(original)) == self.meta["config_hash"]:
                    # Report only known section names, never configuration values
                    # (which can include private endpoints and Vault references).
                    sections = [
                        key for key in ("account_id", "region", "profile", "state_dir",
                                        "keycloak", "vault", "ocm", "network", "ta_tls",
                                        "boundary_account", "ta_app_config")
                        if original.get(key) != self.config.get(key)
                    ]
                    if sections:
                        changed = "\nChanged configuration fields/sections: " + ", ".join(sections) + "."
            except (OSError, ValueError, AttributeError):
                pass  # Missing/unverified original config is not a reason to bypass ownership.
            raise Error(
                f"Environment ownership/configuration mismatch for '{self.name}'."
                f"{changed}\nLocal record: {self.home / 'environment.json'}.\n"
                "A local record is created before AWS preflight, so it can exist even if "
                "deployment never created AWS resources. This invocation stopped before AWS changes.\n"
                f"Restore the original configuration saved in {self.home / 'config.json'} "
                "to continue with this record. For an intentional configuration change, use "
                "make ephemeral-env EPHEMERAL_NAME=<new-name> with a new, valid short name.\n"
                "If the attempt never touched AWS, make ephemeral-env-reset can verify and "
                "archive the unused local record so you can reuse this name with changed config.\n"
                "Deleting AWS resources will not clear this local mismatch. Preserve the old "
                "record and states for any required cleanup; do not delete them or edit their hashes."
            )
        if digest(canonical(tree_manifest(self.source))) != self.meta["source"]["fingerprint"]:
            raise Error("Immutable source snapshot was modified")
        return self.meta

    def create(self, revision=None):
        if (self.home / "environment.json").exists() or (self.home / "source").exists():
            raise Error("Environment already exists; never automatically resnapshot/adopt")
        generation = uuid.uuid4().hex
        directory = self.home / "generations" / generation
        directory.mkdir(parents=True, mode=0o700)
        source = snapshot(self.repository, directory / "source", revision, self.runner)
        self.meta = {
            "schema": SCHEMA, "name": self.name, "home": str(self.home),
            "config_hash": digest(canonical(self.config)), "owner": str(uuid.uuid4()),
            "source": source, "status": "created", "roots": {}, "images": {},
            "active_generation": generation,
            "generations": {generation: {"source": source, "images": {}}},
            "source_requests": [{"generation": generation, "selection": revision or "working-tree"}],
        }
        for key in ROOTS:
            state_path = self.home / f"{key}.tfstate"
            if state_path.exists() or state_path.is_symlink():
                raise Error("Existing state cannot be adopted")
            lineage = str(uuid.uuid4())
            write_json(state_path, {"version": 4, "serial": 0, "lineage": lineage,
                                    "outputs": {}, "resources": []})
            self.meta["roots"][key] = {
                "state_hash": digest(state_path.read_bytes()), "lineage": lineage,
                "serial": 0, "touched": False,
            }
        self.update_aliases()
        write_json(self.home / "config.json", self.config)
        self.save()

    @property
    def generation(self):
        return self._generation or (self.meta or {}).get("active_generation")

    @property
    def source(self):
        if not self.generation:
            return self.home / "source"
        if not re.fullmatch(r"[a-f0-9]{32}", self.generation):
            raise Error("Invalid source generation identifier")
        return self.home / "generations" / self.generation / "source"

    def update_aliases(self):
        """Convenience pointers only; state/provenance use generation directories."""
        for name in ("source", "work"):
            path = self.home / name
            if path.exists() and not path.is_symlink():
                raise Error("Legacy/foreign environment layout cannot be adopted")
            temporary = self.home / (".pointer-" + name)
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(Path("generations") / self.meta["active_generation"] / name)
            os.replace(temporary, path)

    def refresh_source(self, revision=None, yes=False, account=None):
        """Select today's bytes or an explicit commit, preserving all owned state."""
        self.load()
        if self.meta["status"] == "destroyed":
            raise Error("Destroyed environment names cannot be reused")
        generation = uuid.uuid4().hex
        directory = self.home / "generations" / generation
        directory.mkdir(mode=0o700)
        selected = snapshot(self.repository, directory / "source", revision, self.runner)
        self._generation = generation
        try:
            self.check_capabilities()
        finally:
            self._generation = None
        boundary = self.meta["roots"].get("boundary-regional", {})
        if (selected["fingerprint"] != self.meta["source"]["fingerprint"]
                and boundary.get("touched") and not boundary.get("destroyed")):
            self.state_check("boundary-regional")
            self.guard_account(account, yes)
            self.ensure_quiescent()
        matching = next((key for key, entry in self.meta["generations"].items()
                         if entry["source"]["fingerprint"] == selected["fingerprint"]), None)
        if matching:
            self.meta["generations"][generation] = {
                "source": selected, "images": {}, "identical_to": matching,
            }
            generation = matching
        else:
            self.meta["generations"][generation] = {"source": selected, "images": {}}
        record = self.meta["generations"][generation]
        self.meta.update(active_generation=generation, source=record["source"],
                         images=record["images"], status="source-prepared")
        self.meta.pop("verification", None)
        self.meta.pop("work_fingerprint", None)
        if "work_fingerprint" in record:
            self.meta["work_fingerprint"] = record["work_fingerprint"]
        self.meta["source_requests"].append({
            "generation": generation, "selection": revision or "working-tree",
            "fingerprint": selected["fingerprint"],
            "commit": selected["commit"],
            "requested_at": datetime.now(timezone.utc).isoformat(),
        })
        self.update_aliases()
        self.save()

    def deploy(self, yes=False, account=None):
        self.load()
        if self.meta["status"] == "destroyed":
            raise Error("Destroyed environment names cannot be reused")
        self.check_capabilities()
        self.require_tools()
        self.prepare()
        self.check_work()
        for key in ROOTS:
            self.state_check(key)
        self.guard_account(account, yes)
        self.preflight()
        if "ta_tls" not in self.config:
            print("\nWARNING: TA uses private HTTP; tokens and data cross the VPC unencrypted.\n"
                  "  Disposable dev only. No customer data. Configure ta_tls for HTTPS.\n")
        self.approve(
            f"Source snapshot\n  {self.meta['source']['fingerprint']}\n"
            "  Plans and deployment progress follow after approval.", yes)
        credentials = self.vault_credentials()
        redeploy_ta = bool(self.meta["roots"].get("ta-regional", {}).get("touched"))
        self.meta["status"] = "deploying"
        self.save()
        self.publish_images()
        network = None
        for key in ROOTS:
            if key == "boundary-regional":
                self.build_lambda()
            variables = self.variables(key, network)
            self.plan_apply(key, variables, yes=yes, account=account)
            if key == "boundary-network":
                network = self.outputs(key)
            if key == "ta-global":
                self.put_credentials(self.outputs(key)["secretsmanager_secret_arn"], credentials)
            if key == "ta-regional" and redeploy_ta:
                self.refresh_ta_service()
        self.meta["outputs"] = {
            "boundary": self.outputs("boundary-regional"),
            "trusted_actions": self.outputs("ta-regional"),
        }
        self.meta["outputs_generation"] = self.generation
        self.save()
        self.write_connection()
        self.verify_deployment()
        self.meta["status"] = "deployed"
        self.save()
