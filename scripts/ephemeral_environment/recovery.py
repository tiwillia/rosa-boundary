"""Journal-only interrupted-operation recovery and ownership-checked cleanup."""

import json
from pathlib import Path
import re

from .common import Error, ROOTS, digest
from .runtime import CommandError


class Recovery:
    def recover(self, yes=False, account=None):
        """Reconcile journaled operations; neither rollback nor finish deployment."""
        self.load()
        self.guard_account(account, yes)
        for key, record in self.meta["roots"].items():
            state_path = self.home / f"{key}.tfstate"
            if not record.get("pending_plan"):
                self.state_check(key)
                continue
            if any((self.home / name).exists() for name in (
                f".{key}.tfstate.lock.info", f"{key}.tfstate.lock.info"
            )):
                raise Error("Terraform lock remains; verify the old process has exited and "
                            "resolve its stale lock explicitly before recover")
            generation = record.get("pending_generation")
            if generation not in self.meta["generations"]:
                raise Error("Pending apply lacks an owned source generation")
            self._generation = generation
            try:
                self.check_work()
                plan = self.home / f"{key}.plan"
                if digest(plan.read_bytes()) != record["pending_plan"]:
                    raise Error("Pending saved plan does not match its journal")
                rendered = json.loads(self.tf(key, "show", "-json", str(plan)))
                permitted = {address for change in rendered.get("resource_changes", [])
                             if change.get("mode") != "data"
                             for address in (change["address"], change.get("previous_address"))
                             if address}
                if permitted != set(record.get("pending_addresses", [])):
                    raise Error("Pending saved-plan addresses do not match their journal")
                if state_path.is_symlink():
                    raise Error("Recovery refuses symlink state")
                state_bytes = state_path.read_bytes()
                state = json.loads(state_bytes)
                if (not record.get("lineage") or state.get("lineage") != record["lineage"]
                        or state.get("serial", -1) < record.get("serial", 0)):
                    raise Error("Recovery refuses a different state lineage or older serial")
                for resource in state.get("resources", []):
                    if resource.get("mode") != "managed":
                        continue
                    address = (resource.get("module", "") + "." if resource.get("module") else "")
                    address += resource["type"] + "." + resource["name"]
                    for instance in resource.get("instances", []):
                        indexed = address
                        if "index_key" in instance:
                            indexed += "[" + json.dumps(instance["index_key"]) + "]"
                        if indexed not in permitted:
                            raise Error("Recovery state contains a resource outside the saved plan")
                        attrs = instance["attributes"]
                        for field in ("arn", "id"):
                            value = attrs.get(field, "")
                            if isinstance(value, str) and value.startswith("arn:"):
                                parts = value.split(":", 5)
                                if (len(parts) != 6 or parts[1] != "aws"
                                        or parts[4] not in ("", self.config["account_id"])):
                                    raise Error("Recovery found a resource outside the approved account")
                        if "tags_all" in attrs or "tags" in attrs:
                            tags = attrs.get("tags_all") or attrs.get("tags") or {}
                            if tags.get("EphemeralOwner") != self.meta["owner"]:
                                raise Error("Recovery found a resource without the environment owner tag")
                self.approve(
                    f"RECOVER interrupted {key} checkpoint in {self.config['account_id']}/"
                    f"{self.config['region']}; verified saved plan, lineage, resource addresses "
                    "and state owner tags. This does not claim the apply completed.", yes)
                if digest(state_path.read_bytes()) != digest(state_bytes):
                    raise Error("State changed during recovery approval; retry after the old process exits")
                record["generation"] = generation
                record["recovered_from_plan"] = record.pop("pending_plan")
                record.pop("pending_generation", None)
                record.pop("pending_addresses", None)
                self.record_state(key)
            finally:
                self._generation = None
        for component, pending in list(self.meta.get("pending_ecr", {}).items()):
            repository = pending["repository"]
            if (component not in {"boundary", "ta"} or repository != f"eph-{self.name}/{component}"
                    or pending.get("generation") not in self.meta["generations"]):
                raise Error("ECR create journal does not match this environment")
            owned = False
            try:
                description = self.aws("ecr", "describe-repositories",
                                       "--repository-names", repository)["repositories"][0]
                expected = (f"arn:aws:ecr:{self.config['region']}:{self.config['account_id']}:"
                            f"repository/{repository}")
                if description["repositoryArn"] != expected:
                    raise Error("Recovery ECR destination does not match the approved account")
                tags = self.aws("ecr", "list-tags-for-resource", "--resource-arn", expected)
                owned = any(tag["Key"] == "EphemeralOwner"
                            and tag["Value"] == self.meta["owner"] for tag in tags.get("tags", []))
            except CommandError as error:
                if b"RepositoryNotFoundException" not in error.stderr:
                    raise
            self.approve(
                f"RECOVER ECR create journal {repository}: "
                + ("record the repository carrying this environment's exact owner tag"
                   if owned else "discard unfulfilled intent; do NOT adopt/delete the absent or foreign repository"),
                yes)
            if owned:
                self.meta.setdefault("repositories", {})[component] = repository
            self.meta["pending_ecr"].pop(component)
            self.save()
        for component, pending in list(self.meta.get("pending_publications", {}).items()):
            generation = pending["generation"]
            if generation not in self.meta["generations"]:
                raise Error("Publication journal lacks an owned source generation")
            self._generation = generation
            try:
                self.check_work()
                record = self.meta["generations"][generation]
                if pending["source"] != record["source"]["fingerprint"]:
                    raise Error("Publication journal source does not match its generation")
                expected_uri = (f"{self.config['account_id']}.dkr.ecr.{self.config['region']}"
                                f".amazonaws.com/{pending['repository']}")
                if (pending["uri"] != expected_uri
                        or self.meta.get("repositories", {}).get(component) != pending["repository"]):
                    raise Error("Publication journal points outside an owned repository")
                arn = (f"arn:aws:ecr:{self.config['region']}:{self.config['account_id']}:"
                       f"repository/{pending['repository']}")
                tags = self.aws("ecr", "list-tags-for-resource", "--resource-arn", arn)
                if not any(tag["Key"] == "EphemeralOwner" and tag["Value"] == self.meta["owner"]
                           for tag in tags.get("tags", [])):
                    raise Error("Recovery refuses an unowned publication repository")
                digest_file = Path(pending["digest_file"])
                if not digest_file.resolve().is_relative_to(self.source.parent.resolve()):
                    raise Error("Publication digest file is outside its generation")
                sha = digest_file.read_text().strip() if digest_file.is_file() else ""
                verified = False
                if re.fullmatch(r"sha256:[a-f0-9]{64}", sha):
                    try:
                        details = self.aws("ecr", "describe-images", "--repository-name",
                                           pending["repository"], "--image-ids", "imageTag=" + pending["tag"])
                        verified = details["imageDetails"][0]["imageDigest"] == sha
                    except CommandError as error:
                        if b"ImageNotFoundException" not in error.stderr:
                            raise
                self.approve(
                    f"RECOVER publication {pending['repository']}:{pending['tag']}: "
                    + ("record registry digest verified against the journaled podman push"
                       if verified else "abandon unverified image provenance; keep the owned tag "
                       "for repository cleanup and rebuild with a NEW immutable tag on deploy"),
                    yes)
                if verified:
                    image = {"uri": expected_uri + "@" + sha, "local_id": pending["local_id"],
                             "source": pending["source"], "architecture": "amd64"}
                    record["images"][component] = image
                    if generation == self.meta["active_generation"]:
                        self.meta["images"][component] = image
                else:
                    self.meta.setdefault("abandoned_publications", []).append(pending)
                self.meta["pending_publications"].pop(component)
                self.save()
            finally:
                self._generation = None
        for component, pending in list(self.meta.get("pending_ecr_deletions", {}).items()):
            repository = f"eph-{self.name}/{component}"
            arn = (f"arn:aws:ecr:{self.config['region']}:{self.config['account_id']}:"
                   f"repository/{repository}")
            if (component not in {"boundary", "ta"}
                    or self.meta.get("repositories", {}).get(component) != repository
                    or pending.get("owner_verified") is not True
                    or pending != {
                        "repository": repository, "arn": arn,
                        "account_id": self.config["account_id"], "region": self.config["region"],
                        "owner": self.meta["owner"], "owner_verified": True,
                    }):
                raise Error("ECR deletion journal does not match this environment/account/owner")
            missing = False
            try:
                described = self.aws("ecr", "describe-repositories", "--repository-names", repository)
            except CommandError as error:
                if not re.search(
                    rb"\(RepositoryNotFoundException\) when calling the DescribeRepositories operation",
                    error.stderr,
                ):
                    raise
                missing = True
            if not missing:
                if described["repositories"][0]["repositoryArn"] != arn:
                    raise Error("Recovery ECR deletion destination does not match the approved account")
                tags = self.aws("ecr", "list-tags-for-resource", "--resource-arn", arn)
                if not any(tag["Key"] == "EphemeralOwner" and tag["Value"] == self.meta["owner"]
                           for tag in tags.get("tags", [])):
                    raise Error("Recovery refuses an ECR deletion for a now-unowned repository")
            self.approve(
                f"RECOVER ECR deletion {repository} in {self.config['account_id']}/"
                f"{self.config['region']}: "
                + ("record completion of the journaled, pre-delete owner-verified deletion"
                   if missing else "clear the uncompleted delete intent; destroy must reverify "
                   "live ownership and obtain deletion approval again"),
                yes,
            )
            if missing and repository not in self.meta.get("deleted_repositories", []):
                self.meta.setdefault("deleted_repositories", []).append(repository)
            self.meta["pending_ecr_deletions"].pop(component)
            self.save()
        self.meta["status"] = "recovered"
        self.save()

    def destroy(self, yes=False, account=None):
        self.load()
        self.guard_account(account, yes)
        if self.meta["status"] == "destroyed":
            return
        if (self.meta.get("pending_ecr") or self.meta.get("pending_publications")
                or self.meta.get("pending_ecr_deletions")):
            raise Error("Resolve interrupted ECR journals with recover before destroy")
        if self.meta["roots"]:
            for key in ROOTS:
                self.state_check(key)
            self.cleanup_boundary_consumers(yes=yes, account=account)
            self.cleanup_bedrock_payloads(yes=yes, account=account)
        for key in reversed(ROOTS):
            record = self.meta["roots"].get(key)
            if (record and record.get("state_hash") and record.get("touched", True)
                    and not record.get("destroyed")):
                self._generation = record.get("generation", self.meta["active_generation"])
                try:
                    self.check_work()
                    self.plan_apply(key, destroy=True, yes=yes, account=account)
                finally:
                    self._generation = None
        for component, repository in self.meta.get("repositories", {}).items():
            if repository in self.meta.get("deleted_repositories", []):
                continue
            if component not in {"boundary", "ta"} or repository != f"eph-{self.name}/{component}":
                raise Error("ECR deletion destination does not match this environment")
            described = self.aws("ecr", "describe-repositories", "--repository-names", repository)
            arn = described["repositories"][0]["repositoryArn"]
            if arn != (f"arn:aws:ecr:{self.config['region']}:{self.config['account_id']}:"
                       f"repository/{repository}"):
                raise Error("ECR repository ARN is outside the approved account/region")
            tags = self.aws("ecr", "list-tags-for-resource", "--resource-arn", arn)
            if not any(t["Key"] == "EphemeralOwner" and t["Value"] == self.meta["owner"]
                       for t in tags.get("tags", [])):
                raise Error("Refusing to delete an unowned ECR repository")
            self.approve(f"Delete owned ECR repository {repository}, including its images", yes)
            self.guard_account(account, yes)
            self.meta.setdefault("pending_ecr_deletions", {})[component] = {
                "repository": repository, "arn": arn,
                "account_id": self.config["account_id"], "region": self.config["region"],
                "owner": self.meta["owner"], "owner_verified": True,
            }
            self.save()
            self.aws("ecr", "delete-repository", "--repository-name", repository, "--force")
            self.meta.setdefault("deleted_repositories", []).append(repository)
            self.meta["pending_ecr_deletions"].pop(component)
            self.save()
        self.meta["status"] = "destroyed"
        self.save()

    def ensure_quiescent(self):
        """Keep redeploy/source refresh from disrupting open investigations."""
        state_path = self.home / "boundary-regional.tfstate"
        if not state_path.exists() or self.meta["roots"].get("boundary-regional", {}).get("destroyed"):
            return
        state = json.loads(state_path.read_text())
        clusters, filesystems, owned_access_points = [], [], set()
        for resource in state.get("resources", []):
            if resource.get("mode") != "managed":
                continue
            for instance in resource.get("instances", []):
                attributes = instance["attributes"]
                if resource["type"] == "aws_ecs_cluster":
                    clusters.append(attributes["name"])
                elif resource["type"] == "aws_efs_file_system":
                    filesystems.append(attributes["id"])
                elif resource["type"] == "aws_efs_access_point":
                    owned_access_points.add(attributes["id"])
        for cluster in clusters:
            tasks = self.aws("ecs", "list-tasks", "--cluster", cluster, "--desired-status", "RUNNING")
            if tasks.get("taskArns"):
                raise Error("Boundary tasks remain; stop tasks and close investigations before redeploy")
        for filesystem in filesystems:
            points = self.aws("efs", "describe-access-points", "--file-system-id", filesystem)
            if any(point["AccessPointId"] not in owned_access_points
                   for point in points.get("AccessPoints", [])):
                raise Error("Unclosed Boundary investigations remain; close them before redeploy")
