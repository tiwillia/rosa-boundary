"""Credential-isolated subprocess execution and private failure diagnostics."""

import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile

from .common import Error, canonical, private_directory


_ANSI_COLOR = re.compile(r"\x1b\[[0-9;]*m")
_TF_INDEX = r'\[(?:[0-9]+|"(?:[^"\\\x00-\x1f]|\\.)*")\]'
_TF_NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
_TF_SEGMENT = _TF_NAME + rf"(?:{_TF_INDEX})?"
_TF_LIFECYCLE = re.compile(
    rf"(?P<address>(?:{_TF_SEGMENT}\.)+{_TF_SEGMENT}): "
    r"(?P<status>(?:Destroying|Still destroying|Creating|Still creating|"
    r"Modifying|Still modifying|Reading)\.\.\.|"
    r"(?:Destruction complete|Creation complete|Modifications complete|Read complete)"
    r" after [0-9]+[hms](?:[0-9]+[ms])*(?:\.[0-9]+s)?)"
    r"(?: \[[^\r\n]*\])?"
)
_TF_SUMMARY = re.compile(
    r"(?:Plan: [0-9]+ to add, [0-9]+ to change, [0-9]+ to destroy\.|"
    r"Apply complete! Resources: [0-9]+ added, [0-9]+ changed, [0-9]+ destroyed\.|"
    r"Destroy complete! Resources: [0-9]+ destroyed\.)"
)


def _terraform_progress(line):
    """Print only lifecycle metadata, never plan values, IDs or diagnostics."""
    text = _ANSI_COLOR.sub("", line.decode("utf-8", errors="replace").rstrip("\r"))
    # Reject cursor/OSC escapes, carriage-return rewrites and other controls.
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in text):
        return
    match = _TF_LIFECYCLE.fullmatch(text)
    if match:
        # for_each keys can contain private values; retain only numeric indexes.
        address = re.sub(r'\["(?:[^"\\]|\\.)*"\]', '["redacted"]', match["address"])
        print(f"{address}: {match['status']}", flush=True)
    elif _TF_SUMMARY.fullmatch(text):
        print(text, flush=True)


def _run_with_terraform_progress(args, **kwargs):
    """Drain both pipes concurrently while retaining the original private bytes."""
    input_data = kwargs.pop("input")
    with subprocess.Popen(args, stdin=subprocess.PIPE if input_data is not None else None,
                          **kwargs) as process:
        try:
            captured = {process.stdout: [], process.stderr: []}
            pending = b""
            oversized = False
            written = 0
            with selectors.DefaultSelector() as selector:
                for pipe in captured:
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ)
                if input_data:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                elif process.stdin:
                    process.stdin.close()
                while selector.get_map():
                    for event, _ in selector.select():
                        pipe = event.fileobj
                        if pipe is process.stdin:
                            try:
                                written += os.write(pipe.fileno(), input_data[written:written + 4096])
                            except BrokenPipeError:
                                written = len(input_data)
                            if written == len(input_data):
                                selector.unregister(pipe)
                                pipe.close()
                            continue
                        chunk = os.read(pipe.fileno(), 65536)
                        if not chunk:
                            selector.unregister(pipe)
                            if pipe is process.stdout and pending and not oversized:
                                _terraform_progress(pending)
                            continue
                        captured[pipe].append(chunk)
                        if pipe is not process.stdout:
                            continue  # stderr always remains private.
                        lines = chunk.split(b"\n")
                        for index, part in enumerate(lines):
                            # Limit the display parser, not the retained diagnostics.
                            if len(pending) + len(part) > 16384:
                                oversized = True
                            if not oversized:
                                pending += part
                            if index < len(lines) - 1:
                                if not oversized:
                                    _terraform_progress(pending)
                                pending, oversized = b"", False
            return subprocess.CompletedProcess(
                args, process.wait(), b"".join(captured[process.stdout]),
                b"".join(captured[process.stderr]),
            )
        except BaseException:
            # Popen.__exit__ waits: terminate first if output handling is interrupted.
            process.kill()
            process.wait()
            raise


class CommandError(Error):
    """Private in-memory diagnostics for classifying documented AWS failures."""

    def __init__(self, message, stderr=b""):
        super().__init__(message)
        self.stderr = stderr


def clean_environment(config=None):
    """No ambient TF_VAR, Make flags, .env, AWS endpoint or static key overrides."""
    allowed = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "SSL_CERT_FILE",
               "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(TF_IN_AUTOMATION="1", TF_INPUT="0", AWS_PAGER="",
               AWS_CLI_AUTO_PROMPT="off", AWS_EC2_METADATA_DISABLED="true",
               AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true")
    if config:
        env.update(AWS_PROFILE=config["profile"], AWS_REGION=config["region"],
                   AWS_DEFAULT_REGION=config["region"])
    return env


class Runner:
    def __init__(self, config=None):
        self.env = clean_environment(config)
        self.log_dir = None
        self.lock_fd = None

    def run(self, args, cwd=None, input=None, extra_env=None, ok_codes=(0,), build=False, log=False,
            terraform_progress=False):
        env = clean_environment() if build else dict(self.env)
        env.update(extra_env or {})
        try:
            # This is deliberately not a generic raw-log streaming facility.
            run = _run_with_terraform_progress if terraform_progress and not build else subprocess.run
            options = {} if terraform_progress and not build else {"check": False}
            result = run(
                [str(x) for x in args], cwd=cwd, env=env, input=input,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options,
                pass_fds=(self.lock_fd,) if self.lock_fd is not None else (),
            )
        except OSError:
            raise Error(f"Cannot execute {Path(str(args[0])).name}") from None
        if result.returncode not in ok_codes:
            if log and self.log_dir:
                private_directory(self.log_dir)
                fd, filename = tempfile.mkstemp(prefix="command-", suffix=".log", dir=self.log_dir)
                output = (b"Command: " + canonical([str(x) for x in args]) + b"\n"
                          b"--- stdout ---\n" + result.stdout
                          + b"\n--- stderr ---\n" + result.stderr)
                for key in ("GITHUB_TOKEN", "GITHUB_APP_PEM"):
                    if env.get(key):
                        output = output.replace(env[key].encode(), b"[REDACTED BUILD SECRET]")
                with os.fdopen(fd, "wb") as stream:
                    stream.write(output)
                raise Error(f"{Path(str(args[0])).name} failed (exit {result.returncode}); "
                            f"private diagnostics: {filename}")
            # Vault, STS/ECR credentials and Secrets Manager are never logged.
            raise CommandError(f"{Path(str(args[0])).name} failed (exit {result.returncode}); "
                               "output withheld because it may contain credentials", result.stderr)
        return result.stdout

    def json(self, args, label=None, allow_empty_object=False, **kwargs):
        # Labels are supplied by call sites, not built from full argument lists:
        # Vault paths and credential-bearing arguments must never be disclosed.
        label = label or Path(str(args[0])).name
        output = self.run(args, **kwargs)
        if allow_empty_object and not output.strip():
            return {}
        try:
            return json.loads(output)
        except ValueError:
            problem = "empty output" if not output.strip() else "invalid JSON"
            raise Error(
                f"{label} returned {problem} despite a successful exit; expected JSON. "
                "Response contents withheld because they may contain credentials. "
                "Check that the expected CLI executable is on PATH and returns JSON."
            ) from None
