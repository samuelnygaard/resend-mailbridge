"""Exercise bounded cursor recovery and retries against a local Resend API."""

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from test_reliability import BridgeTest, main


class RecoveryTests(BridgeTest):
    def setUp(self):
        super().setUp()
        self.ids = ["id4", "id3", "id2", "id1", "id0"]
        self.failed_ids = set()
        self.failed_cursor = None
        self.queries = []
        fixture = self

        class API(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                status = 200
                if url.path == "/emails/receiving":
                    assert self.headers.get("Authorization") == "Bearer test-key"
                    params = parse_qs(url.query)
                    cursor = params.get("after", [None])[0]
                    fixture.queries.append(cursor)
                    if cursor and cursor == fixture.failed_cursor:
                        status, body = 500, b'{}'
                    else:
                        start = fixture.ids.index(cursor) + 1 if cursor else 0
                        size = int(params["limit"][0])
                        ids = fixture.ids[start:start + size]
                        body = json.dumps({"object": "list", "has_more": start + size < len(fixture.ids),
                                           "data": [{"id": item, "to": ["support@example.com"]} for item in ids]}).encode()
                elif url.path.startswith("/emails/receiving/"):
                    assert self.headers.get("Authorization") == "Bearer test-key"
                    email_id = url.path.rsplit("/", 1)[1]
                    body = json.dumps({"id": email_id, "to": ["support@example.com"], "raw": {
                        "download_url": f"http://127.0.0.1:{fixture.server.server_port}/raw/{email_id}",
                    }}).encode()
                elif url.path.startswith("/raw/"):
                    assert not self.headers.get("Authorization")
                    email_id = url.path.rsplit("/", 1)[1]
                    status = 500 if email_id in fixture.failed_ids else 200
                    body = b"Subject: recovered\r\n\r\n" + email_id.encode()
                else:
                    status, body = 404, b'{}'
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), API)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        for attr, value in (("API_BASE", f"http://127.0.0.1:{self.server.server_port}"),
                            ("RECONCILE_LIMIT", 1), ("RECONCILE_MAX_PAGES", 2)):
            patcher = patch.object(main, attr, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        delay = patch.object(main.time, "sleep")
        delay.start()
        self.addCleanup(delay.stop)

    def delivered_ids(self):
        return {path.name.split(".")[1] for path in self.root.glob("*/new/*")}

    def test_bounded_sweeps_resume_old_mail_and_check_newest_page(self):
        main.reconcile_once()
        self.assertEqual(self.queries, [None, "id4"])
        self.assertEqual(self.delivered_ids(), {"id4", "id3"})
        self.ids.insert(0, "id5")
        for _ in range(3):
            start = len(self.queries)
            main.reconcile_once()
            self.assertLessEqual(len(self.queries) - start, 2)
        self.assertEqual(self.delivered_ids(), {"id5", "id4", "id3", "id2", "id1", "id0"})

    def test_continuation_survives_service_restart(self):
        main.reconcile_once()
        env = os.environ.copy()
        env.update(RESEND_API_BASE=main.API_BASE, MAILBRIDGE_MAILDIR_ROOT=str(self.root),
                   RECONCILE_LIMIT="1", RECONCILE_MAX_PAGES="2")
        result = subprocess.run([sys.executable, "-c", "import main; main.reconcile_once()"],
                                env=env, cwd=str(main.pathlib.Path(main.__file__).parent), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("id2", self.delivered_ids())

    def test_failed_item_is_retried_after_cursor_has_advanced(self):
        self.failed_ids.add("id3")
        main.reconcile_once()
        self.assertNotIn("id3", self.delivered_ids())
        self.assertIsNone(main.STATS["last_reconcile_at"])
        self.assertEqual(main.STATS.get("reconcile_pending"), 1)
        self.failed_ids.clear()
        main.reconcile_once()
        self.assertIn("id3", self.delivered_ids())
        self.assertIn("id2", self.delivered_ids())
        self.assertEqual(main.STATS.get("reconcile_pending"), 0)
        self.assertIsNotNone(main.STATS["last_reconcile_at"])

    def test_failed_page_keeps_its_cursor_for_the_next_sweep(self):
        main.reconcile_once()
        self.failed_cursor = "id3"
        with self.assertRaises(RuntimeError):
            main.reconcile_once()
        self.failed_cursor = None
        main.reconcile_once()
        self.assertIn("id2", self.delivered_ids())


if __name__ == "__main__":
    unittest.main()
