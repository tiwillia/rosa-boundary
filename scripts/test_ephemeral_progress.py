"""Safe Terraform progress is timely without exposing captured subprocess data."""

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ephemeral_environment.common import Error
from ephemeral_environment.runtime import CommandError, Runner, _terraform_progress
from ephemeral_environment.terraform import Terraform


class ProgressFilterTests(unittest.TestCase):
    def render(self, *lines):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for line in lines:
                _terraform_progress(line)
        return output.getvalue()

    def test_all_lifecycle_states_and_summaries_are_allowlisted(self):
        statuses = (
            "Destroying...", "Still destroying...", "Destruction complete after 2s",
            "Creating...", "Still creating...", "Creation complete after 1m2s",
            "Modifying...", "Still modifying...", "Modifications complete after 0s",
            "Reading...", "Read complete after 0s",
        )
        lines = [f"module.network[0].aws_vpc.main: {status}".encode() for status in statuses]
        summaries = (
            b"Plan: 2 to add, 1 to change, 3 to destroy.",
            b"Apply complete! Resources: 2 added, 1 changed, 3 destroyed.",
            b"Destroy complete! Resources: 3 destroyed.",
        )
        self.assertEqual(self.render(*lines, *summaries),
                         b"\n".join([*lines, *summaries]).decode() + "\n")

    def test_colors_removed_and_ids_and_string_instance_keys_withheld(self):
        self.assertEqual(
            self.render(
                b'\x1b[0m\x1b[1mmodule.net["private-key"].aws_vpc.main["escaped\\"key"]: '
                b'Creation complete after 1s [id=private-resource-id]\x1b[0m\r',
                b"aws_vpc.main: Still creating... [10s elapsed]",
            ),
            'module.net["redacted"].aws_vpc.main["redacted"]: Creation complete after 1s\n'
            "aws_vpc.main: Still creating...\n",
        )

    def test_plan_values_errors_controls_and_nonexact_matches_are_private(self):
        self.assertEqual(self.render(
            b'  password = "synthetic-private-value"',
            b'Error: synthetic-private-value',
            b'echo aws_vpc.main: Creating...',
            b'aws_vpc.main: Creating... synthetic-private-value',
            b'Plan: synthetic-private-value',
            b'Plan: 1 to add, 0 to change, 0 to destroy. synthetic-private-value',
            b'aws_vpc.main: Creating...\rError: synthetic-private-value',
            b'\x1b]0;synthetic-private-value\x07aws_vpc.main: Creating...',
            b'aws_vpc.main: Creating...\x1b[2J',
            b'aws_vpc.main: Creating... [id=private\x00value]',
            b'aws_vpc.main: Creating... [id=private\xc2\x85value]',
        ), "")


class ProgressProcessTests(unittest.TestCase):
    def command(self, code, *args):
        return [sys.executable, "-c", code, *map(str, args)]

    def test_progress_is_flushed_before_child_can_exit_and_stderr_is_drained(self):
        read_fd, write_fd = os.pipe()
        runner = Runner()
        runner.lock_fd = read_fd

        class HandshakeOutput(io.StringIO):
            acknowledged = False

            def flush(self):
                if not self.acknowledged and "aws_vpc.main: Creating...\n" in self.getvalue():
                    os.write(write_fd, b"go")
                    self.acknowledged = True
                super().flush()

        output = HandshakeOutput()
        code = """
import os, select, sys
fd = int(sys.argv[1])
os.write(2, b"synthetic-private-stderr\\n" * 20000)
os.write(1, b'password = "synthetic-private-value"\\n')
os.write(1, b"\\x1b[1maws_vpc.main: Creating...\\x1b[0m\\n")
if not select.select([fd], [], [], 5)[0]:
    sys.exit(7)
assert os.read(fd, 2) == b"go"
os.write(1, b"aws_vpc.main: Creation complete after 1s [id=private-id]\\n")
"""
        try:
            with contextlib.redirect_stdout(output):
                captured = runner.run(self.command(code, read_fd), terraform_progress=True)
        finally:
            os.close(read_fd)
            os.close(write_fd)
        self.assertTrue(output.acknowledged)
        self.assertIn(b"synthetic-private-value", captured)
        self.assertIn(b"[id=private-id]", captured)
        self.assertEqual(output.getvalue(),
                         "aws_vpc.main: Creating...\n"
                         "aws_vpc.main: Creation complete after 1s\n")

    def test_large_stdout_stderr_and_stdin_do_not_deadlock(self):
        code = """
import os, sys
os.write(1, b"x" * 200000 + b"\\naws_vpc.main: Reading...\\n")
os.write(2, b"private-stderr" * 20000)
assert sys.stdin.buffer.read() == b"input" * 40000
os.write(1, b"aws_vpc.main: Read complete after 0s")
"""
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            captured = Runner().run(self.command(code), input=b"input" * 40000,
                                    terraform_progress=True)
        self.assertTrue(captured.startswith(b"x" * 200000))
        self.assertEqual(output.getvalue(),
                         "aws_vpc.main: Reading...\naws_vpc.main: Read complete after 0s\n")

    def test_chunk_boundaries_colors_and_oversized_lines(self):
        # Put newlines, ANSI sequences and a lifecycle message across read boundaries.
        code = """
import os
os.write(1, b"x" * 65530 + b"\\n\\x1b[1maws_vpc.main: Creating...\\x1b[0m\\n")
os.write(1, b"x" * 200000 + b"aws_vpc.fake: Creating...\\n")
os.write(1, b"aws_vpc.main: Still creating... [20s elapsed]\\n")
"""
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            Runner().run(self.command(code), terraform_progress=True)
        self.assertEqual(output.getvalue(),
                         "aws_vpc.main: Creating...\naws_vpc.main: Still creating...\n")

    def test_small_reads_preserve_partial_lines_and_never_display_stderr(self):
        original_read = os.read
        output = io.StringIO()
        code = """
import os
os.write(1, b'password = "synthetic-private"\\n\\x1b[1maws_vpc.main: Creating...\\x1b[0m\\r\\n')
os.write(2, b"aws_vpc.stderr: Creating...\\nError: synthetic-private-stderr\\n")
os.write(1, b"aws_vpc.main: Creation complete after 0s [id=private-id]")
"""
        with patch("ephemeral_environment.runtime.os.read",
                   side_effect=lambda fd, size: original_read(fd, min(size, 7))):
            with contextlib.redirect_stdout(output):
                captured = Runner().run(self.command(code), terraform_progress=True)
        self.assertIn(b"synthetic-private", captured)
        self.assertIn(b"\x1b[1m", captured)
        self.assertEqual(output.getvalue(),
                         "aws_vpc.main: Creating...\naws_vpc.main: Creation complete after 0s\n")

    def test_failure_keeps_full_private_diagnostics_and_redacts_split_build_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = Runner()
            runner.log_dir = Path(directory) / "logs"
            output = io.StringIO()
            code = """
import os, sys
os.write(1, b"x" * 65530 + b"synthetic-build-token\\n")
os.write(1, b"aws_vpc.main: Destroying... [id=private-id]\\n")
os.write(2, b"Error: synthetic-private-stderr\\n")
sys.exit(2)
"""
            with contextlib.redirect_stdout(output), self.assertRaisesRegex(
                Error, "private diagnostics"
            ) as caught:
                runner.run(self.command(code), terraform_progress=True, log=True,
                           extra_env={"GITHUB_TOKEN": "synthetic-build-token"})
            self.assertEqual(output.getvalue(), "aws_vpc.main: Destroying...\n")
            self.assertNotIn("synthetic-private", str(caught.exception))
            logs = list(runner.log_dir.glob("*.log"))
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
            contents = logs[0].read_text()
            self.assertIn("Error: synthetic-private-stderr", contents)
            self.assertIn("[id=private-id]", contents)
            self.assertIn("[REDACTED BUILD SECRET]", contents)
            self.assertNotIn("synthetic-build-token", contents)

    def test_failure_without_logging_keeps_stderr_only_in_private_exception(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(CommandError) as caught:
            Runner().run(self.command(
                'import sys; print("synthetic-private"); '
                'sys.stderr.write("synthetic-private-error"); sys.exit(2)'
            ), terraform_progress=True)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(caught.exception.stderr, b"synthetic-private-error")
        self.assertNotIn("synthetic-private", str(caught.exception))

    def test_default_and_build_calls_never_display_even_valid_progress(self):
        for options in ({}, {"build": True}, {"build": True, "terraform_progress": True}):
            with self.subTest(options=options):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    captured = Runner().run(self.command(
                        'print("aws_vpc.main: Creating..."); print("synthetic-private")'
                    ), **options)
                self.assertEqual(output.getvalue(), "")
                self.assertIn(b"synthetic-private", captured)

    def test_allowed_nonzero_codes_and_missing_executable(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(Runner().run(self.command(
                'import sys; print("Plan: 1 to add, 0 to change, 0 to destroy."); sys.exit(2)'
            ), ok_codes=(0, 2), terraform_progress=True),
                b"Plan: 1 to add, 0 to change, 0 to destroy.\n")
        self.assertIn("Plan:", output.getvalue())
        with self.assertRaisesRegex(Error, "Cannot execute"):
            Runner().run(["/nonexistent/terraform-test-executable"], terraform_progress=True)


class TerraformProgressSelectionTests(unittest.TestCase):
    def test_only_terraform_lifecycle_operations_opt_in_including_make_wrappers(self):
        subject = SimpleNamespace(
            runner=Mock(), root=lambda key: Path("/unused") / key,
            config={"keycloak": {"issuer_url": "https://issuer.example"}},
        )
        for key in ("boundary-account", "boundary-network", "boundary-regional",
                    "ta-global", "ta-regional"):
            for operation in ("init", "validate", "plan", "apply", "destroy", "show", "output"):
                with self.subTest(key=key, operation=operation):
                    subject.runner.reset_mock()
                    Terraform.tf(subject, key, operation, "-json" if operation in {
                        "show", "output"
                    } else "-input=false")
                    args, kwargs = subject.runner.run.call_args
                    self.assertEqual(kwargs["terraform_progress"],
                                     operation in {"init", "validate", "plan", "apply", "destroy"})
                    self.assertTrue(kwargs["log"])
                    self.assertEqual(args[0][0], "make" if key.startswith("boundary-")
                                     and operation in {"init", "validate", "plan", "apply"}
                                     else "terraform")


if __name__ == "__main__":
    unittest.main()
