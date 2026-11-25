"""Local-only reset of records that provably never reached cloud mutation."""

import json
import os
import re
import uuid

from .common import Error, ROOTS, SCHEMA, canonical, digest, private_directory, write_json


class Reset:
    def reset(self, yes=False):
        """Archive an unused record under the existing environment lock.

        Do not use load(): the current config may intentionally differ from the
        original. Never call AWS, Vault, Terraform, or selected source code.
        Keep home/lock in place so concurrent commands use the same lock inode.
        """
        if self.runner.lock_fd is None:
            raise Error("Reset requires the environment lock")
        metadata = self.home / "environment.json"
        if not metadata.exists() and not metadata.is_symlink():
            if set(self.home.iterdir()) == {self.home / "lock"}:
                return {"name": self.name, "status": "absent", "aws_operations": "none"}
            raise Error("Reset refused: incomplete local record. Preserve it for investigation.")

        refusal = (
            "Reset refused: this record is not provably unused. "
            "Preserve its state and journals; use recover for interrupted operations or "
            "make ephemeral-env-destroy with the original configuration for AWS cleanup."
        )

        def read_local(path):
            if path.is_symlink() or not path.is_file():
                raise Error(refusal)
            try:
                return path.read_bytes()
            except OSError:
                raise Error(refusal) from None

        def verify():
            try:
                meta = json.loads(read_local(metadata))
                original = json.loads(read_local(self.home / "config.json"))
                allowed = {
                    "schema", "name", "home", "config_hash", "owner", "source",
                    "status", "roots", "images", "active_generation", "generations",
                    "source_requests", "updated_at", "work_fingerprint",
                }
                if (not isinstance(meta, dict) or set(meta) - allowed
                        or meta.get("schema") != SCHEMA or meta.get("name") != self.name
                        or meta.get("home") != str(self.home)
                        or meta.get("status") not in {"created", "source-prepared"}
                        or not isinstance(original, dict)
                        or meta.get("config_hash") != digest(canonical(original))
                        or original.get("state_dir") != self.config["state_dir"]
                        or not re.fullmatch(r"[a-f0-9-]{36}", meta.get("owner", ""))
                        or meta.get("images") != {}
                        or set(meta.get("roots", {})) != set(ROOTS)):
                    raise Error(refusal)
                generations = meta["generations"]
                if not generations or meta["active_generation"] not in generations:
                    raise Error(refusal)
                for generation in generations.values():
                    if (generation.get("images") != {} or set(generation) - {
                            "source", "images", "work_fingerprint", "identical_to"}):
                        raise Error(refusal)
                for key in ROOTS:
                    record = meta["roots"][key]
                    if (set(record) != {"state_hash", "lineage", "serial", "touched"}
                            or record["touched"] is not False or record["serial"] != 0
                            or not re.fullmatch(r"[a-f0-9-]{36}", record["lineage"])):
                        raise Error(refusal)
                    initial = {
                        "version": 4, "serial": 0, "lineage": record["lineage"],
                        "outputs": {}, "resources": [],
                    }
                    data = read_local(self.home / f"{key}.tfstate")
                    if (data != canonical(initial) + b"\n"
                            or digest(data) != record["state_hash"]):
                        raise Error(refusal)
                expected_states = {self.home / f"{key}.tfstate" for key in ROOTS}
                if (set(self.home.rglob("*.tfstate*")) != expected_states
                        or any(self.home.rglob("*.lock.info"))):
                    raise Error(refusal)
                # Recheck all local evidence after interactive approval.
                return digest(canonical(meta)), digest(canonical(original))
            except (KeyError, TypeError, ValueError, AttributeError):
                raise Error(refusal) from None

        checkpoint = verify()
        self.approve(
            f"Archive unused local record\n  {self.home}\n\n"
            "  No AWS operations. Your current config file is unchanged.\n"
            "  The environment name can be deployed again after reset.",
            yes,
        )
        if verify() != checkpoint:
            raise Error("Reset refused: local evidence changed during approval; retry after investigation")
        archives = self.home.parent / ".reset-archives"
        private_directory(archives)
        archive = archives / f"{self.name}-{uuid.uuid4().hex}"
        archive.mkdir(mode=0o700)
        write_json(archive / "reset.json", {
            "name": self.name, "original_home": str(self.home),
            "operation": "archive-unused-local-record", "aws_operations": "none",
        })
        # Metadata moves last. An interrupted archive leaves the active record
        # present and fail-closed on missing inputs, rather than permitting deploy.
        # Use a dedicated subdirectory so an unrelated local filename cannot
        # overwrite the reset receipt at archive/reset.json.
        record_directory = archive / "record"
        record_directory.mkdir(mode=0o700)
        entries = sorted(
            (path for path in self.home.iterdir() if path.name != "lock"),
            key=lambda path: (path.name == "environment.json", path.name),
        )
        for path in entries:
            os.rename(path, record_directory / path.name)
        return {
            "name": self.name, "status": "reset", "archive": str(archive),
            "aws_operations": "none", "next_step": "make ephemeral-env",
        }
