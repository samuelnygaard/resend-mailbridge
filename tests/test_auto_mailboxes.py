"""Configuration contracts for recipient-derived IMAP accounts."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"


class AutomaticMailboxTests(unittest.TestCase):
    def run_config(self, recipients, assertions, **extra):
        env = os.environ.copy()
        for key in (
            "MAILBRIDGE_RECIPIENTS", "MAILBRIDGE_ROUTES", "ALLOWED_RECIPIENTS",
            "MAILBRIDGE_DEFAULT_MAILBOX", "MAILBRIDGE_IMAP_USER",
        ):
            env.pop(key, None)
        if recipients is not None:
            env["MAILBRIDGE_RECIPIENTS"] = recipients
        env.update(extra, DISABLE_RECONCILER="1")
        with tempfile.TemporaryDirectory(prefix="mailbridge-config-") as root:
            env["MAILBRIDGE_MAILDIR_ROOT"] = root
            return subprocess.run(
                [sys.executable, "-c", "import main\n" + assertions],
                cwd=APP_DIR, env=env, capture_output=True, text=True,
            )

    def assert_config(self, recipients, assertions, **extra):
        result = self.run_config(recipients, assertions, **extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_accepted_addresses_create_distinct_maildirs_and_users(self):
        self.assert_config(
            "support@nelgixa.resend.app,sales@nelgixa.resend.app",
            """
assert main.MAILBOXES == ['sales', 'support'], main.MAILBOXES
main._startup()
for box in main.MAILBOXES:
    for sub in ['tmp', 'new', 'cur']:
        assert (main.maildir_for(box) / sub).is_dir()
assert main.mailbox_for({'to': ['support@nelgixa.resend.app']}) == 'support'
assert main.mailbox_for({'to': ['sales@nelgixa.resend.app']}) == 'sales'
""",
        )

    def test_acceptance_order_wins_over_recipient_order(self):
        self.assert_config(
            "sales@nelgixa.resend.app,support@nelgixa.resend.app",
            "assert main.mailbox_for({'to': ['support@nelgixa.resend.app', 'sales@nelgixa.resend.app']}) == 'sales'",
        )

    def test_unlisted_addresses_and_domains_are_dropped(self):
        self.assert_config(
            "support@nelgixa.resend.app",
            """
assert main.DEFAULT_MAILBOX == ''
assert main.mailbox_for({'to': ['admin@nelgixa.resend.app']}) is None
assert main.mailbox_for({'to': ['support@other.example']}) is None
""",
        )

    def test_case_and_whitespace_are_normalized(self):
        self.assert_config(
            " Support@NELGIXA.RESEND.APP , Sales@nelgixa.resend.app ",
            "assert main.mailbox_for({'to': ['Sales Team <SALES@NELGIXA.RESEND.APP>']}) == 'sales'",
        )

    def test_invalid_addresses_or_unsafe_local_parts_fail_startup(self):
        for recipient in (
            "../escape@nelgixa.resend.app", ".@nelgixa.resend.app",
            "..@nelgixa.resend.app", "-sales@nelgixa.resend.app",
            "support+tag@nelgixa.resend.app", "a" * 65 + "@nelgixa.resend.app",
            "support", "support@", "support@@example.com", "support@bad domain",
            "support @example.com", "support@-example.com", "support@bad_domain",
            "Support <support@nelgixa.resend.app>", ", ,",
        ):
            with self.subTest(recipient=recipient):
                result = self.run_config(recipient, "")
                self.assertNotEqual(result.returncode, 0, "accepted: " + recipient)
                self.assertIn("ValueError", result.stderr)

    def test_ambiguous_configuration_is_rejected(self):
        result = self.run_config(
            "sales@nelgixa.resend.app", "",
            MAILBRIDGE_ROUTES="support@nelgixa.resend.app=support",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ValueError", result.stderr)

    def test_different_addresses_cannot_silently_share_a_derived_username(self):
        result = self.run_config("support@one.example,support@two.example", "")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ValueError", result.stderr)

    def test_duplicate_address_is_harmless(self):
        self.assert_config(
            "sales@nelgixa.resend.app,SALES@nelgixa.resend.app",
            "assert main.ROUTES == [('sales@nelgixa.resend.app', 'sales')]",
        )

    def test_explicit_routes_and_legacy_single_mailbox_remain_supported(self):
        self.assert_config(
            None, "assert main.mailbox_for({'to': ['sales@example.com']}) == 'commercial'",
            MAILBRIDGE_ROUTES="sales@example.com=commercial",
        )
        self.assert_config(
            None, "assert main.mailbox_for({'to': ['sales@example.com']}) == 'support'",
            ALLOWED_RECIPIENTS="sales@example.com",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
