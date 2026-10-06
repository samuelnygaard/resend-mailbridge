"""Regression tests for webhook security, delivery and recovery failures."""

import base64
import copy
import datetime
import json
import os
import pathlib
import sys
import tempfile
import socketserver
import threading
import concurrent.futures
import time
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
        stats = copy.deepcopy(main.STATS)
        self.addCleanup(lambda: (main.STATS.clear(), main.STATS.update(stats)))
        for key in main.STATS:
            main.STATS[key] = {} if key == "per_mailbox" else (0 if isinstance(main.STATS[key], int) else None)
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
    def test_bootstrap_without_signing_secret_rejects_ingestion(self):
        with patch.object(main, "WEBHOOK_SECRET", ""), \
             patch.object(main, "fetch_message", return_value=b"Subject: unsigned\r\n\r\nbody\r\n"):
            response = self.client.post("/webhook", json={"type": "email.received", "data": {
                "email_id": "unsigned", "to": ["support@example.com"],
            }})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(list((self.root / "support/new").iterdir()), [])
            with patch.object(main, "imap_reachable", return_value=True), \
                 patch.object(main, "imap_authenticated", return_value=True):
                health = self.client.get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertFalse(health.json()["webhook_enabled"])

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


class RejectingIMAP(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"* OK IMAP ready\r\n")
        while True:
            try:
                line = self.rfile.readline()
            except ConnectionResetError:
                return
            if not line:
                return
            tag, command, *_ = line.split()
            if command.upper() == b"CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK capabilities\r\n")
            elif command.upper() == b"LOGOUT":
                self.wfile.write(b"* BYE closing\r\n" + tag + b" OK logout\r\n")
                return
            else:
                self.wfile.write(tag + b" NO authentication failed\r\n")


class HealthTests(BridgeTest):
    def test_imap_greeting_without_working_authentication_is_unhealthy(self):
        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), RejectingIMAP)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with patch.object(main, "IMAP_PORT", server.server_address[1]):
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["detail"]["imap_ok"])

    def test_ingest_error_cannot_publish_signed_download_url(self):
        url = "https://storage.example/raw?X-Amz-Signature=PRIVATE-TOKEN"
        with patch.object(main, "fetch_message", side_effect=RuntimeError("GET failed: " + url)):
            response = self.signed_post({"type": "email.received", "data": {
                "email_id": "failed-download", "to": ["support@example.com"],
            }})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("PRIVATE-TOKEN", self.client.get("/healthz").text)

    def test_failed_reconciliation_is_recorded_without_leaking_urls(self):
        with patch.object(main, "_get", side_effect=RuntimeError("https://example.com?secret=PRIVATE-TOKEN")):
            with self.assertRaises(RuntimeError):
                main.reconcile_once()
        self.assertIsNotNone(main.STATS.get("last_reconcile_error"))
        self.assertIsNotNone(main.STATS.get("last_reconcile_attempt_at"))
        self.assertNotIn("PRIVATE-TOKEN", self.client.get("/healthz").text)

    def test_reconciler_outage_and_stall_are_separate_from_imap(self):
        with patch.object(main, "RECONCILER_ENABLED", True), \
             patch.object(main, "imap_reachable", return_value=True), \
             patch.object(main, "imap_authenticated", return_value=True):
            main.STATS["last_reconcile_error"] = "UpstreamError"
            response = self.client.get("/healthz")
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.json()["detail"]["imap_ok"])
            self.assertFalse(response.json()["detail"]["reconcile_ok"])
            main.STATS["last_reconcile_error"] = None
            main.STATS["last_reconcile_attempt_at"] = 1
            self.assertEqual(self.client.get("/healthz").status_code, 503)

    def test_partial_sweep_does_not_report_success(self):
        class Page:
            def json(self):
                return {"data": [{"id": "unavailable", "to": ["support@example.com"]}]}
        with patch.object(main, "_get", return_value=Page()), \
             patch.object(main, "fetch_message", side_effect=OSError("failed")):
            main.reconcile_once()
        self.assertIsNone(main.STATS["last_reconcile_at"])
        self.assertEqual(main.STATS["reconcile_failures"], 1)

    def test_http_failure_diagnostics_omit_request_url(self):
        import requests
        response = requests.Response()
        response.status_code = 403
        response.url = "https://storage.example?signature=PRIVATE-TOKEN"
        with patch.object(main.requests, "get", return_value=response), patch.object(main.time, "sleep"):
            with self.assertRaises(RuntimeError) as error:
                main._get(response.url)
        self.assertNotIn("PRIVATE-TOKEN", str(error.exception))


class DeliveryTests(BridgeTest):
    def test_concurrent_delivery_has_one_copy_across_mailboxes(self):
        entered = threading.Event()
        second_fsync = threading.Event()
        release = threading.Event()
        real_fsync = os.fsync
        count = 0
        count_lock = threading.Lock()

        def slow_fsync(fd):
            nonlocal count
            with count_lock:
                count += 1
                first = count == 1
            if first:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release write")
            else:
                second_fsync.set()
            return real_fsync(fd)

        with patch.object(main.os, "fsync", side_effect=slow_fsync), \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(main.deliver, b"original bytes", "concurrent-id", "support")
            self.assertTrue(entered.wait(1))
            second = pool.submit(main.deliver, b"original bytes", "concurrent-id", "sales")
            # Without serialization the second write reaches fsync before the first publishes.
            second_fsync.wait(0.2)
            release.set()
            results = [first.result(timeout=2), second.result(timeout=2)]
        files = list(self.root.glob("*/new/*"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b"original bytes")
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(list(self.root.glob("*/tmp/*")), [])

    def test_failed_delivery_cleans_temporary_file_and_can_retry(self):
        with patch.object(main.os, "fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                main.deliver(b"original bytes", "retry-id", "support")
        self.assertEqual(list(self.root.glob("*/tmp/*")), [])
        self.assertEqual(list(self.root.glob("*/new/*")), [])
        self.assertTrue(main.deliver(b"original bytes", "retry-id", "support"))

    def test_email_id_cannot_escape_or_match_other_messages(self):
        main.deliver(b"existing", "existing-id", "support")
        for email_id in ("../../escape", "*", "id/path", "", None):
            with self.subTest(email_id=email_id), self.assertRaises(ValueError):
                main.deliver(b"unsafe", email_id, "support")


if __name__ == "__main__":
    unittest.main()
