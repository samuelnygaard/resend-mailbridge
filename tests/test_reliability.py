"""Regression tests for webhook security, delivery and recovery failures."""

import base64
import datetime
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "app"))
os.environ.update(
    MAILBRIDGE_RECIPIENTS="support@example.com,sales@example.com",
    RESEND_API_KEY="test-key",
    RESEND_WEBHOOK_SECRET="whsec_" + base64.b64encode(b"test-signing-secret").decode(),
    MAILBRIDGE_IMAP_PASSWORD="test-password",
    DISABLE_RECONCILER="1",
)
for key in ("MAILBRIDGE_ROUTES", "MAILBRIDGE_DEFAULT_MAILBOX", "ALLOWED_RECIPIENTS"):
    os.environ.pop(key, None)

import main
from fastapi.testclient import TestClient
from svix.webhooks import Webhook


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)
        self.maildir_patch = patch.object(main, "MAILDIR_ROOT", self.root)
        self.maildir_patch.start()
        self.addCleanup(self.maildir_patch.stop)
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        main.ensure_maildir()

    def signed_post(self, body):
        if not isinstance(body, str):
            body = json.dumps(body)
        now = datetime.datetime.now(datetime.timezone.utc)
        signature = Webhook(main.WEBHOOK_SECRET).sign("msg_test", now, body)
        return self.client.post("/webhook", content=body, headers={
            "svix-id": "msg_test", "svix-timestamp": str(int(now.timestamp())),
            "svix-signature": signature, "content-type": "application/json",
        })


class SignedWebhookTests(BridgeTest):
    def test_verified_event_is_parsed_independently_of_verifier_return(self):
        # Svix 2.x returns None. Keep signature verification real.
        response = self.signed_post({"type": "domain.created", "data": {}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "ignored": "domain.created"})

    def test_bad_signature_cannot_reach_delivery(self):
        response = self.client.post("/webhook", json={
            "type": "email.received", "data": {"email_id": "forged", "to": ["support@example.com"]},
        })
        self.assertEqual(response.status_code, 401)
        self.assertEqual(list((self.root / "support/new").iterdir()), [])

    def test_signed_malformed_payload_is_a_client_error(self):
        for body in ("not JSON", "[]", '{"type":"email.received","data":[]} ',
                     '{"type":"email.received","data":"invalid"}'):
            with self.subTest(body=body):
                self.assertEqual(self.signed_post(body).status_code, 400)


if __name__ == "__main__":
    unittest.main()
