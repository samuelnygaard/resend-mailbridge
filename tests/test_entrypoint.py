"""Entrypoint regressions; run as root in an isolated Linux test container."""

import os
import pathlib
import subprocess
import tempfile
import unittest

ENTRYPOINT = pathlib.Path(__file__).resolve().parent.parent / "entrypoint.sh"


class EntrypointTests(unittest.TestCase):
    def test_shell_rejects_every_invalid_name_before_creating_any_maildir(self):
        # Bypass the Python parser to prove the independent root shell guard.
        for invalid in (".", "..", "../escape", "bad name", "bad\tname", "*", "UPPER", "a" * 65):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                command = root / "python"
                command.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_MAILBOXES"\n')
                command.chmod(0o755)
                env = os.environ.copy()
                env.update(PATH=f"{root}:" + env["PATH"],
                           RESEND_API_KEY="test-key", MAILBRIDGE_IMAP_PASSWORD="test-password",
                           MAILBRIDGE_MAILDIR_ROOT=str(root / "mail"),
                           DOVECOT_CONF=str(root / "dovecot.conf"), SUPERVISORD_BIN="/bin/true",
                           FAKE_MAILBOXES="support\n" + invalid)
                result = subprocess.run(["sh", str(ENTRYPOINT)], env=env, capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / "mail").exists(), "filesystem changed before all names were validated")

    def test_shell_accepts_valid_names_at_the_length_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            command = root / "python"
            command.write_text('#!/bin/sh\nprintf "%s\\n" "$FAKE_MAILBOXES"\n')
            command.chmod(0o755)
            supervisor = root / "supervisor"
            supervisor.write_text('#!/bin/sh\nexit 0\n')
            supervisor.chmod(0o755)
            env = os.environ.copy()
            env.update(PATH=f"{root}:" + env["PATH"],
                       RESEND_API_KEY="test-key", MAILBRIDGE_IMAP_PASSWORD="test-password",
                       MAILBRIDGE_MAILDIR_ROOT=str(root / "mail"),
                       DOVECOT_CONF=str(root / "dovecot.conf"), SUPERVISORD_BIN=str(supervisor),
                       FAKE_MAILBOXES="support\n0.valid-name_1\n" + "a" * 64)
            result = subprocess.run(["sh", str(ENTRYPOINT)], env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertTrue((root / "mail" / ("a" * 64) / "new").is_dir())
            self.assertTrue((root / "mail/0.valid-name_1/cur").is_dir())

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
