"""Entrypoint regressions; run as root in an isolated Linux test container."""

import os
import pathlib
import subprocess
import tempfile
import unittest

ENTRYPOINT = pathlib.Path(__file__).resolve().parent.parent / "entrypoint.sh"


class EntrypointTests(unittest.TestCase):
    def test_failed_password_file_preparation_preserves_previous_accounts(self):
        users = pathlib.Path("/etc/dovecot/users")
        previous = b"previous-account-file\n"
        users.write_bytes(previous)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            command = root / "chown"
            command.write_text('#!/bin/sh\nexit 23\n')
            command.chmod(0o755)
            env = os.environ.copy()
            env.update(
                PATH=f"{root}:" + env["PATH"],
                PYTHONPATH=str(ENTRYPOINT.parent / "app"),
                RESEND_API_KEY="test-key", MAILBRIDGE_IMAP_PASSWORD="test-password",
                MAILBRIDGE_RECIPIENTS="support@example.com,sales@example.com",
                MAILBRIDGE_MAILDIR_ROOT=str(root / "mail"),
                DOVECOT_CONF=str(root / "dovecot.conf"),
            )
            env.pop("MAILBRIDGE_ROUTES", None)
            result = subprocess.run(["sh", str(ENTRYPOINT)], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(users.read_bytes() == previous, "existing accounts were replaced before permissions succeeded")
            self.assertEqual(list(users.parent.glob("users.*")), [])


if __name__ == "__main__":
    unittest.main()
