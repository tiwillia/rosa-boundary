"""Source-inheritance tests using real Git tree comparisons."""

import subprocess
import shlex
from pathlib import Path, PurePosixPath

import pytest

from staging_release_inputs import RUNTIME_INPUTS, verify_component_inputs


def test_runtime_inputs_cover_local_copies():
    # A new local COPY must not silently make digest inheritance unsound.
    for line in (Path(__file__).resolve().parents[1] / "Containerfile").read_text().splitlines():
        if not line.startswith(("COPY ", "ADD ")):
            continue
        args = shlex.split(line)[1:]
        if any(arg.startswith("--from=") for arg in args):
            continue
        for source in (arg for arg in args[:-1] if not arg.startswith("--")):
            path = PurePosixPath(source)
            assert any(path.is_relative_to(base) for base in RUNTIME_INPUTS), source


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=test",
                                   "-c", "user.email=test@example.invalid",
                                   "-c", "commit.gpgsign=false", *args], text=True).strip()


def change(repo, path):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("change\n")
    git(repo, "add", "--", path)
    git(repo, "commit", "-qm", "fixture")
    return git(repo, "rev-parse", "HEAD")


def test_per_component_inheritance(tmp_path):
    git(tmp_path, "init", "-q")
    base = change(tmp_path, "Containerfile")
    infra = change(tmp_path, "deploy/regional/ecs.tf")
    verify_component_inputs(tmp_path, infra, base, base)
    helper = change(tmp_path, "utils/rosa-boundary-skill-sync.sh")
    with pytest.raises(ValueError, match="runtime: component inputs differ"):
        verify_component_inputs(tmp_path, helper, base, base)
    verify_component_inputs(tmp_path, helper, helper, base)
    handler = change(tmp_path, "lambda/create-investigation/handler.py")
    with pytest.raises(ValueError, match="lambda: component inputs differ"):
        verify_component_inputs(tmp_path, handler, helper, base)
    verify_component_inputs(tmp_path, handler, helper, handler)
    with pytest.raises(ValueError, match="not an ancestor"):
        verify_component_inputs(tmp_path, helper, handler, handler)
    with pytest.raises(ValueError, match="full lowercase"):
        verify_component_inputs(tmp_path, "main", helper, handler)
