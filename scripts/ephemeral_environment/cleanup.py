"""Confirmed cleanup of consumers inside an owned disposable Boundary stack."""

import json
import re
import time

from .common import Error, canonical
from .runtime import CommandError


class BoundaryCleanup:
    def cleanup_bedrock_payloads(self, yes=False, account=None):
        """Explicit dev teardown of the state-owned payloads, never bypass retention."""
        self.state_check("boundary-regional")
        path = self.home / "boundary-regional.tfstate"
        if not path.exists() or self.meta["roots"].get("boundary-regional", {}).get("destroyed"):
            return
        matches = [
            instance["attributes"]
            for resource in json.loads(path.read_text()).get("resources", [])
            if (resource.get("mode") == "managed" and not resource.get("module")
                and resource.get("type") == "aws_s3_bucket"
                and resource.get("name") == "bedrock_large_payloads")
            for instance in resource.get("instances", []) if not instance.get("deposed")
        ]
        if not matches:
            return
        expected = (f"{self.config['account_id']}-eph-{self.name}-dev-"
                    f"{self.config['region']}-bedrock-invocations")
        if len(matches) != 1 or matches[0].get("id") != expected:
            raise Error("Bedrock payload bucket does not match this disposable environment")
        bucket_args = ["--bucket", expected, "--expected-bucket-owner", self.config["account_id"]]

        def verify_bucket():
            self.state_check("boundary-regional")
            try:
                response = self.aws("s3api", "get-bucket-tagging", *bucket_args)
            except CommandError as error:
                if re.search(rb"\(NoSuchBucket\) when calling the GetBucketTagging operation",
                             error.stderr):
                    return False
                raise
            tags = {t["Key"]: t["Value"] for t in response.get("TagSet", [])}
            if tags.get("EphemeralOwner") != self.meta["owner"]:
                raise Error("Refusing to empty a Bedrock bucket without matching live ownership")
            region = self.aws("s3api", "get-bucket-location", *bucket_args).get("LocationConstraint")
            region = {"EU": "eu-west-1", None: "us-east-1"}.get(region, region)
            if region != self.config["region"]:
                raise Error("Bedrock payload bucket is outside the approved region")
            return True

        if not verify_bucket():
            return  # A prior partial destroy may already have removed the bucket.
        self.check_bedrock_logging()  # Do not interfere with a foreign account/region singleton.
        warning = (
            "\n  WARNING: Permanently deletes model inputs/outputs, possibly from outside Boundary."
            "\n  Object retention and legal holds will NOT be bypassed."
        ) if not getattr(self, "_operation_approval", None) else ""
        self.approve(
            f"Bedrock payload cleanup\n  Bucket: {expected}\n"
            "  Remove all versions, delete markers and unfinished uploads.\n"
            "  Disable owned Bedrock logging." + warning, yes)
        self.guard_account(account, yes)
        if not verify_bucket():
            return
        # Stop new deliveries before draining the bucket. Terraform will refresh
        # this already-deleted singleton; its state and snapshot are not edited.
        self.check_bedrock_logging()
        if self.aws("bedrock", "get-model-invocation-logging-configuration").get("loggingConfig"):
            self.check_bedrock_logging()
            self.aws("bedrock", "delete-model-invocation-logging-configuration", json_output=False)

        def entries_from(response, fields, identifier):
            if not isinstance(response, dict) or type(response.get("IsTruncated")) is not bool:
                raise Error("Malformed Bedrock bucket listing; cleanup stopped without assuming emptiness")
            entries = []
            for field in fields:
                items = response.get(field, [])
                if not isinstance(items, list) or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("Key"), str) or not item["Key"]
                    or not isinstance(item.get(identifier), str) or not item[identifier]
                    for item in items
                ):
                    raise Error("Malformed Bedrock bucket entries; cleanup stopped")
                entries.extend(items)
            if not entries and response["IsTruncated"]:
                raise Error("Bedrock bucket listing was truncated without entries; cleanup stopped")
            return entries

        # Abort uploads first: an upload completing after a version drain
        # would otherwise repopulate the bucket before Terraform deletes it.
        for _ in range(1000):
            response = self.aws("s3api", "list-multipart-uploads", *bucket_args,
                                "--no-paginate", "--max-uploads", "1000")
            uploads = entries_from(response, ("Uploads",), "UploadId")
            if not uploads:
                break
            for upload in uploads:
                self.aws("s3api", "abort-multipart-upload", *bucket_args,
                         "--cli-input-json", "file:///dev/stdin",
                         input=canonical({"Key": upload["Key"], "UploadId": upload["UploadId"]}),
                         json_output=False)
            print(f"Bedrock payload cleanup: aborted {len(uploads)} unfinished uploads", flush=True)
        else:
            raise Error("Bedrock upload cleanup exceeded its bounded batch limit; retry destroy")
        removed = 0
        for _ in range(1000):
            # Repeatedly drain the first service page: no ambient AWS CLI
            # pagination setting can cause versions or delete markers to be skipped.
            response = self.aws("s3api", "list-object-versions", *bucket_args,
                                "--no-paginate", "--max-keys", "1000")
            entries = entries_from(response, ("Versions", "DeleteMarkers"), "VersionId")
            if not entries:
                break
            objects = [{"Key": item["Key"], "VersionId": item["VersionId"]} for item in entries]
            # Keys can contain sensitive names. Send the payload on stdin, never
            # command arguments, terminal output, Terraform variables or logs.
            result = self.aws("s3api", "delete-objects", *bucket_args,
                              "--delete", "file:///dev/stdin",
                              input=canonical({"Objects": objects}))
            if result.get("Errors") or len(result.get("Deleted", [])) != len(objects):
                raise Error("Bedrock payload deletion was refused or incomplete; retained/held objects "
                            "and access errors are not bypassed. Inspect bucket retention/permissions "
                            "under the approved account, then retry destroy.")
            removed += len(objects)
            print(f"Bedrock payload cleanup: removed {removed} object versions/delete markers", flush=True)
        else:
            raise Error("Bedrock payload cleanup exceeded its bounded batch limit; retry destroy")

    def boundary_consumers(self):
        """Inventory only hash-verified state parents with matching live ownership."""
        inventory = {"tasks": [], "access_points": [], "task_definitions": []}
        self.state_check("boundary-regional")
        path = self.home / "boundary-regional.tfstate"
        if not path.exists() or self.meta["roots"].get("boundary-regional", {}).get("destroyed"):
            return inventory
        resources = {}
        for resource in json.loads(path.read_text()).get("resources", []):
            if resource.get("mode") == "managed":
                for instance in resource.get("instances", []):
                    if not instance.get("deposed"):
                        resources.setdefault(resource["type"], []).append(instance["attributes"])
        prefix = f"arn:aws:ecs:{self.config['region']}:{self.config['account_id']}:"
        owner = self.meta["owner"]
        for cluster in resources.get("aws_ecs_cluster", []):
            name = cluster["name"]
            response = self.aws("ecs", "describe-clusters", "--clusters", name, "--include", "TAGS")
            live = response.get("clusters", [])
            failures = response.get("failures", [])
            if (not live and len(failures) == 1 and failures[0].get("reason") == "MISSING"
                    and failures[0].get("arn") in (name, prefix + "cluster/" + name)):
                continue  # An interrupted prior destroy may already have removed this parent.
            if (response.get("failures") or len(live) != 1
                    or live[0].get("clusterArn") != prefix + "cluster/" + name
                    or {t["key"]: t["value"] for t in live[0].get("tags", [])}.get("EphemeralOwner") != owner):
                raise Error("Boundary cleanup refuses a cluster without matching live ownership")
            arns = set()
            # Desired STOPPED also finds tasks that are still STOPPING/unmounting.
            for status in ("RUNNING", "STOPPED"):
                arns.update(self.aws("ecs", "list-tasks", "--cluster", name,
                                     "--desired-status", status).get("taskArns", []))
            arns = sorted(arns)
            for offset in range(0, len(arns), 100):
                batch = arns[offset:offset + 100]
                response = self.aws("ecs", "describe-tasks", "--cluster", name,
                                    "--tasks", *batch, "--include", "TAGS")
                if response.get("failures") or {t["taskArn"] for t in response.get("tasks", [])} != set(batch):
                    raise Error("Cannot completely describe Boundary tasks; cleanup stopped")
                for task in response["tasks"]:
                    if (task.get("clusterArn") != live[0]["clusterArn"]
                            or not task["taskArn"].startswith(prefix + "task/")):
                        raise Error("Boundary cleanup found a task outside the owned cluster/account")
                    if task.get("lastStatus") == "STOPPED":
                        continue
                    tags = {t["key"]: t["value"] for t in task.get("tags", [])}
                    inventory["tasks"].append({
                        "cluster": name, "arn": task["taskArn"],
                        "stopping": task.get("desiredStatus") == "STOPPED",
                        "investigation": tags.get("investigation_id", "(untagged)"),
                    })
        filesystems = set()
        managed_points = {a["id"] for a in resources.get("aws_efs_access_point", [])}
        for filesystem in resources.get("aws_efs_file_system", []):
            identifier = filesystem["id"]
            try:
                response = self.aws("efs", "describe-file-systems", "--file-system-id", identifier)
            except CommandError as error:
                if not re.search(
                    rb"\(FileSystemNotFound\) when calling the DescribeFileSystems operation",
                    error.stderr,
                ):
                    raise
                # No access points can remain, but definitions referencing this
                # state-owned, now absent filesystem still need deregistration.
                filesystems.add(identifier)
                continue
            live = response.get("FileSystems", [])
            if (len(live) != 1 or live[0].get("FileSystemId") != identifier
                    or live[0].get("OwnerId") != self.config["account_id"]
                    or {t["Key"]: t["Value"] for t in live[0].get("Tags", [])}.get("EphemeralOwner") != owner):
                raise Error("Boundary cleanup refuses a filesystem without matching live ownership")
            filesystems.add(identifier)
            response = self.aws("efs", "describe-access-points", "--file-system-id", identifier)
            for point in response.get("AccessPoints", []):
                if point.get("FileSystemId") != identifier:
                    raise Error("Boundary cleanup found an access point outside the owned filesystem")
                if point["AccessPointId"] in managed_points:
                    continue  # Terraform deletes its own access points.
                tags = {t["Key"]: t["Value"] for t in point.get("Tags", [])}
                inventory["access_points"].append({
                    "filesystem": identifier, "id": point["AccessPointId"],
                    "deleting": point.get("LifeCycleState") == "deleting",
                    "cluster_id": tags.get("ClusterID", "(untagged)"),
                    "investigation": tags.get("InvestigationID", "(untagged)"),
                })
        # Investigation task definitions are created by Lambda, not Terraform.
        # Match both the exact generated family shape and an owned EFS mount;
        # never deregister a definition based only on an account-wide name prefix.
        definitions = None
        for base in resources.get("aws_ecs_task_definition", []):
            family = base["family"]
            if definitions is None:
                # ECS's family-prefix option actually selects one exact family;
                # it will not find the generated per-investigation families.
                definitions = self.aws("ecs", "list-task-definitions",
                                       "--status", "ACTIVE").get("taskDefinitionArns", [])
            for arn in definitions:
                if not re.fullmatch(re.escape(prefix + "task-definition/" + family)
                                    + r"-.+-\d{8}T\d{6}:\d+", arn):
                    continue
                definition = self.aws("ecs", "describe-task-definition",
                                      "--task-definition", arn)["taskDefinition"]
                if (definition.get("taskDefinitionArn") != arn
                        or definition.get("status") != "ACTIVE"):
                    continue
                mounts = [v["efsVolumeConfiguration"] for v in definition.get("volumes", [])
                          if "efsVolumeConfiguration" in v]
                if mounts and all(v.get("fileSystemId") in filesystems for v in mounts):
                    inventory["task_definitions"].append({"arn": arn})
        return inventory

    @staticmethod
    def consumer_ids(inventory):
        return {
            (kind, item.get("arn") or item["id"])
            for kind, items in inventory.items() for item in items
        }

    def cleanup_boundary_consumers(self, yes=False, account=None):
        """Obtain approval before stopping sessions or removing runtime resources."""
        inventory = self.boundary_consumers()
        if not self.consumer_ids(inventory):
            return
        lines = [
            "Boundary sessions and investigations",
            *[f"  task {t['arn']} (investigation {t['investigation']!r})"
              for t in inventory["tasks"]],
            *[f"  investigation {p['cluster_id']!r}/{p['investigation']!r}: {p['id']}"
              for p in inventory["access_points"]],
            *[f"  investigation task definition {t['arn']}"
              for t in inventory["task_definitions"]],
            "",
            "  Stop listed sessions, wait for shutdown, then remove investigation resources.",
            "  Audit sync is best-effort; export required evidence before cleanup.",
        ]
        self.approve("\n".join(lines), yes)
        self.guard_account(account, yes)
        current = self.boundary_consumers()  # Recheck ownership and detect new consumers after approval.
        if not self.consumer_ids(current) <= self.consumer_ids(inventory):
            raise Error("New Boundary consumers appeared during approval; rerun destroy to review them")
        tasks = current["tasks"]
        for task in tasks:
            if not task["stopping"]:
                self.aws("ecs", "stop-task", "--cluster", task["cluster"], "--task", task["arn"],
                         "--reason", "Approved disposable environment destruction")
        for cluster in sorted({t["cluster"] for t in tasks}):
            arns = [t["arn"] for t in tasks if t["cluster"] == cluster]
            for offset in range(0, len(arns), 100):
                self.aws("ecs", "wait", "tasks-stopped", "--cluster", cluster,
                         "--tasks", *arns[offset:offset + 100], json_output=False)
        for definition in current["task_definitions"]:
            self.aws("ecs", "deregister-task-definition", "--task-definition", definition["arn"])
        for point in current["access_points"]:
            if not point["deleting"]:
                self.aws("efs", "delete-access-point", "--access-point-id", point["id"],
                         json_output=False)
        pending = {p["id"] for p in current["access_points"]}
        filesystems = {p["filesystem"] for p in current["access_points"]}
        for _ in range(60):
            remaining = set()
            for filesystem in filesystems:
                response = self.aws("efs", "describe-access-points", "--file-system-id", filesystem)
                remaining.update(p["AccessPointId"] for p in response.get("AccessPoints", []))
            if not pending.intersection(remaining):
                break
            time.sleep(2)
        else:
            raise Error("Investigation access point deletion is still pending; retry destroy")
        if self.consumer_ids(self.boundary_consumers()):
            raise Error("Boundary consumers remain or appeared during cleanup; retry destroy to review them")
