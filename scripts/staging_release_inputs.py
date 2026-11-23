"""Check inherited component source against a release's Git tree, not registry time.

Run from the trusted release job, with complete history. This checks source
equivalence; build provenance, platform and release approval are separate.
"""

import re
import subprocess
from pathlib import Path


# Include the push build recipe as well as its local inputs. Keep this list
# aligned with Containerfile COPY/bind inputs and the Konflux push triggers.
RUNTIME_INPUTS = (
    "Containerfile", ".containerignore", "entrypoint.sh", "build", "skel",
    "utils/rosa-boundary-credential-helper", "utils/rosa-boundary-skill-sync.sh",
    "utils/rosa-boundary-agent-defaults.py",
    "components/trusted-actions/cmd/rta", "components/trusted-actions/internal",
    "components/trusted-actions/go.mod", "components/trusted-actions/go.sum",
    "components/trusted-actions/VERSION", ".tekton/rosa-boundary-push.yaml",
)
LAMBDA_INPUTS = ("lambda/create-investigation", ".tekton/create-investigation-lambda-push.yaml")


def verify_component_inputs(repo: Path, infrastructure_revision: str,
                            runtime_revision: str, lambda_revision: str) -> None:
    for revision in (infrastructure_revision, runtime_revision, lambda_revision):
        if not re.fullmatch("[0-9a-f]{40}", revision):
            raise ValueError("expected full lowercase Git commit IDs")
    for name, revision, paths in (
        ("runtime", runtime_revision, RUNTIME_INPUTS),
        ("lambda", lambda_revision, LAMBDA_INPUTS),
    ):
        for args, error in (
            (["merge-base", "--is-ancestor", revision, infrastructure_revision],
             "component commit is missing or not an ancestor of the release"),
            (["diff", "--quiet", "--no-ext-diff", "--ignore-submodules=none",
              revision, infrastructure_revision, "--", *paths],
             "component inputs differ or cannot be inspected; a matching build is required"),
        ):
            result = subprocess.run(["git", "--no-replace-objects", "-C", str(repo), *args],
                                    check=False, capture_output=True, timeout=60)
            if result.returncode:
                raise ValueError(f"{name}: {error}")
