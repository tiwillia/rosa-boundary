"""Shared metadata format, private filesystem operations and safe errors."""

import hashlib
import json
import os
from pathlib import Path
import tempfile


ROOTS = {
    "boundary-account": "deploy/account",
    "boundary-network": "deploy/network",
    "ta-global": "components/trusted-actions/terraform/global",
    "ta-regional": "components/trusted-actions/terraform/regional",
    "boundary-regional": "deploy/regional",
}
SCHEMA = 2
INTEGRATION_OCM = "https://api.integration.openshift.com"


class Error(RuntimeError):
    """A safe-to-display failure, never containing subprocess output."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    """Atomically write private metadata, including on interrupted operations."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def private_directory(path):
    if path.is_symlink():
        raise Error("State directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Error("State directory must be owned by you and mode 0700")
