"""Immutable source selection and manifests, independent of deployment tools."""

import os
from pathlib import PurePosixPath
import shutil
import stat

from .common import Error, canonical, digest
from .runtime import Runner


def excluded_source(name):
    path = PurePosixPath(name)
    return (
        any(p in {".git", ".terraform", "__pycache__", ".aws", ".kube",
                  ".vault-token", ".netrc"} for p in path.parts)
        or path.name == ".env"
        or (path.name.startswith(".env.") and path.name not in {".env.example", ".env.sample"})
        or path.name.endswith((".tfvars", ".tfvars.json", ".tfstate", ".tfstate.backup",
                               ".pem", ".key", ".p12", ".pfx"))
    )


def safe_relative(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise Error("Unsafe source path")
    return path


def tree_manifest(root):
    entries = []
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            resolved = (path.parent / target).resolve()
            if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
                raise Error("Snapshot contains an escaping or dangling symlink")
            entries.append([name, "symlink", target])
        elif path.is_file():
            entries.append([name, "executable" if path.stat().st_mode & 0o111 else "file",
                            digest(path.read_bytes())])
        elif not path.is_dir():
            raise Error("Special files are forbidden in a source snapshot")
    return entries


def snapshot(repository, destination, revision=None, runner=None):
    """Capture working files, not the index: staged + unstaged + untracked."""
    runner = runner or Runner()
    destination.mkdir(mode=0o700)
    if revision:
        commit = runner.run(["git", "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
                            cwd=repository).decode().strip()
        records = runner.run(["git", "ls-tree", "-r", "-z", commit],
                             cwd=repository).split(b"\0")
        sources = []
        for record in filter(None, records):
            metadata, raw_name = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            if kind != "blob":
                raise Error("Submodules are not supported in snapshots")
            name = os.fsdecode(raw_name)
            if not excluded_source(name):
                data = runner.run(["git", "cat-file", "blob", oid], cwd=repository)
                sources.append((name, mode, data))
    else:
        commit = runner.run(["git", "rev-parse", "HEAD"], cwd=repository).decode().strip()
        names = runner.run(["git", "ls-files", "--cached", "--others",
                            "--exclude-standard", "-z"], cwd=repository)
        candidates = sorted(set(filter(None, names.split(b"\0"))))
        sources = []
        for raw_name in candidates:
            name = os.fsdecode(raw_name)
            # Ignore rules apply to untracked discovery, not tracked build
            # inputs (for example the public keys under build/keys/).
            if excluded_source(name):
                continue
            original = repository / str(safe_relative(name))
            if not original.exists() and not original.is_symlink():
                continue  # Deleted working-tree file.
            if not original.resolve().is_relative_to(repository.resolve()):
                raise Error("Snapshot contains an escaping or dangling symlink")
            if original.is_symlink():
                sources.append((name, "120000", os.fsencode(os.readlink(original))))
            elif original.is_file():
                sources.append((name, "100755" if original.stat().st_mode & 0o111 else "100644",
                                original.read_bytes()))
            else:
                raise Error("Submodules and special source files are unsupported")
    for name, mode, data in sources:
        target = destination / str(safe_relative(name))
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(parent.is_symlink() for parent in target.parents if parent != destination.parent):
            raise Error("Symlink parent in snapshot")
        if mode == "120000":
            target.symlink_to(os.fsdecode(data))
        else:
            target.write_bytes(data)
            # Read-only snapshots still need conventional read/execute bits:
            # container COPY and archive providers preserve these permissions.
            # The enclosing generation/environment directory remains private.
            target.chmod(0o555 if mode == "100755" else 0o444)
    manifest = tree_manifest(destination)
    for path in sorted(destination.rglob("*"), reverse=True):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o555)
    destination.chmod(0o555)
    return {"commit": commit, "selection": revision or "working-tree",
            "fingerprint": digest(canonical(manifest)), "files": len(manifest)}


def writable_copy(source, target):
    shutil.copytree(source, target, symlinks=True)
    for path in [target, *target.rglob("*")]:
        if path.is_symlink():
            continue
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & stat.S_IXUSR else 0o644)
