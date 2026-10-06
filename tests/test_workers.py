"""Async responsiveness and bounded admission while ingestion blocks or fails."""

import asyncio
import datetime
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from svix.webhooks import Webhook
from test_reliability import BridgeTest, main


class WorkerTests(BridgeTest):
    def test_cancelled_request_holds_capacity_until_delivery_finishes(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_download(email_id):
            entered.set()
            release.wait(2)
            return b"cancelled request bytes"

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                task = asyncio.create_task(self.post(client, "cancelled-id"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    blocked = await self.post(client, "overflow-id")
                    self.assertEqual(blocked.status_code, 503)
                finally:
                    release.set()
                deadline = time.monotonic() + 1
                while not list(self.root.glob("*/new/*")) and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.02)
                retry = await self.post(client, "cancelled-id")
                self.assertEqual(retry.status_code, 200)

        with patch.object(main, "INGEST_SLOTS", threading.BoundedSemaphore(1), create=True), \
             patch.object(main, "fetch_message", side_effect=slow_download):
            asyncio.run(scenario())
        self.assertEqual(len(list(self.root.glob("*/new/*"))), 1)

    async def post(self, client, email_id):
        body = json.dumps({"type": "email.received", "data": {
            "email_id": email_id, "to": ["support@example.com"],
        }})
        now = datetime.datetime.now(datetime.timezone.utc)
        return await client.post("/webhook", content=body, headers={
            "svix-id": "msg_worker", "svix-timestamp": str(int(now.timestamp())),
            "svix-signature": Webhook(main.WEBHOOK_SECRET).sign("msg_worker", now, body),
        })

    def test_slow_ingestion_keeps_event_loop_and_health_responsive(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_download(email_id):
            entered.set()
            release.wait(1)
            return b"Subject: slow\r\n\r\noriginal bytes"

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                task = asyncio.create_task(self.post(client, "slow-id"))
                try:
                    started = time.monotonic()
                    await asyncio.sleep(0.05)
                    self.assertLess(time.monotonic() - started, 0.5, "ingest blocked the event loop")
                    self.assertTrue(entered.is_set())
                    health = await asyncio.wait_for(client.get("/healthz"), 0.5)
                    self.assertEqual(health.status_code, 503)  # IMAP is intentionally absent.
                    self.assertFalse(task.done(), "success was acknowledged before delivery finished")
                finally:
                    release.set()
                    response = await task
                self.assertEqual(response.status_code, 200)

        with patch.object(main, "fetch_message", side_effect=slow_download):
            asyncio.run(scenario())
        self.assertEqual(len(list(self.root.glob("*/new/*"))), 1)

    def test_full_worker_capacity_rejects_more_work_and_recovers_after_failure(self):
        entered = threading.Event()
        release = threading.Event()

        def failed_download(email_id):
            entered.set()
            release.wait(1)
            raise OSError("download unavailable")

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                first = asyncio.create_task(self.post(client, "failure-id"))
                try:
                    await asyncio.sleep(0.05)
                    overloaded = await self.post(client, "overflow-id")
                    self.assertEqual(overloaded.status_code, 503)
                finally:
                    release.set()
                    failed = await first
                self.assertEqual(failed.status_code, 500)
                with patch.object(main, "fetch_message", return_value=b"retry bytes"):
                    retry = await self.post(client, "failure-id")
                self.assertEqual(retry.status_code, 200)

        with patch.object(main, "INGEST_SLOTS", threading.BoundedSemaphore(1), create=True), \
             patch.object(main, "fetch_message", side_effect=failed_download):
            asyncio.run(scenario())
        self.assertEqual(len(list(self.root.glob("*/new/*"))), 1)


if __name__ == "__main__":
    unittest.main()
