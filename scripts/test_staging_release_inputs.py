"""Source-inheritance tests using real Git tree comparisons."""

import subprocess

import pytest

from staging_release_inputs import verify_component_inputs


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
