"""Ephemeral, locally owned Boundary + Trusted Actions development environments.

Only Python's standard library is required. External tools run without a shell.
Create is offline; mutating commands request one operation-wide approval. The private
environment directory is intentionally retained after destruction.
"""

import argparse
import os
from pathlib import Path
import sys

from .common import Error
from .config import load_config
from .environment import Environment
from .output import environment_payload, print_result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "deploy", "destroy", "status", "recover", "reset"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--commit", help="Commit to snapshot instead of the current working tree")
    parser.add_argument("--yes", action="store_true",
                        help="Approve the entire operation noninteractively, including all "
                             "plans and destroy-time session/investigation cleanup")
    parser.add_argument("--account-id", help="Required with --yes for AWS operations, must match config and STS")
    parser.add_argument("--json", action="store_true",
                        help="Print the detailed JSON result instead of a concise summary")
    args = parser.parse_args(argv)
    repository = Path(__file__).resolve().parents[2]
    try:
        os.umask(0o077)
        config = load_config(args.config, repository)
        env = Environment(repository, config, args.name)
        with env.locked(), env.approval_scope(args.action):
            if args.commit and args.action not in {"create", "deploy"}:
                raise Error("--commit is only valid when creating a snapshot")
            if args.action == "reset":
                if args.config.expanduser().resolve().is_relative_to(env.home.resolve()):
                    raise Error("Reset config must live outside the environment directory; "
                                "use your private ephemeral-dev.json, not the saved local copy")
                print_result(args.action, env.reset(yes=args.yes), json_output=args.json)
                return 0
            if args.action == "create":
                env.create(args.commit)
            elif args.action == "deploy":
                if not (env.home / "environment.json").exists():
                    env.create(args.commit)
                else:
                    env.refresh_source(args.commit, args.yes, args.account_id)
                env.deploy(args.yes, args.account_id)
            elif args.action == "destroy":
                env.destroy(args.yes, args.account_id)
            elif args.action == "recover":
                env.recover(args.yes, args.account_id)
            else:
                env.load()
            print_result(args.action, environment_payload(args.name, config, env.home, env.meta),
                         json_output=args.json)
        return 0
    except (Error, OSError, ValueError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, Error) else type(error).__name__
        print(f"ephemeral-env: {message}", file=sys.stderr)
        return 1
