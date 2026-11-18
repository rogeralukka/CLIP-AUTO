"""tests/test_server_social.py - Unit tests for server social and scheduling endpoints."""
import io
import os
import shutil
import tempfile
import unittest
from starlette.testclient import TestClient

from clipper import social
import server


class TestServerSocialEndpoints(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.orig_accounts_file = social.ACCOUNTS_FILE
        self.orig_queue_file = social.SCHEDULE_QUEUE_FILE

        social.ACCOUNTS_FILE = os.path.join(self.tmp_dir, "social_accounts.yaml")
        social.SCHEDULE_QUEUE_FILE = os.path.join(self.tmp_dir, "scheduled_posts.json")
        self.client = TestClient(server.app)

    def tearDown(self):
        social.ACCOUNTS_FILE = self.orig_accounts_file
        social.SCHEDULE_QUEUE_FILE = self.orig_queue_file
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_social_accounts_endpoints(self):
        # 1. Create account
        payload = {
            "id": "acc_stream1",
            "name": "Streamer 1 Main",
            "max_clips_per_day": 3,
            "schedule_slots": ["11:00", "16:00", "20:00"],
            "platforms": {
                "youtube": {"enabled": False, "channel_name": ""},
                "tiktok": {"enabled": False, "account_name": ""},
            }
        }
        res = self.client.post("/api/social/accounts", json=payload)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["id"], "acc_stream1")

        # 2. Get accounts list
        res = self.client.get("/api/social/accounts")
        self.assertEqual(res.status_code, 200)
        accounts = res.json()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["name"], "Streamer 1 Main")
        self.assertEqual(accounts[0]["today_count"], 0)
        self.assertIn("next_slot", accounts[0])

        # 3. Delete account
        res = self.client.delete("/api/social/accounts/acc_stream1")
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/api/social/accounts")
        self.assertEqual(len(res.json()), 0)

    def test_social_schedule_and_queue(self):
        # Setup account
        social.upsert_account("acc_prod", {
            "name": "Production Channel",
            "max_clips_per_day": 4,
            "schedule_slots": ["12:00", "18:00"],
        })

        # Create dummy video clip
        dummy_clip = os.path.join(self.tmp_dir, "clip_sample.mp4")
        with open(dummy_clip, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42")

        # Schedule post
        res = self.client.post("/api/social/schedule", json={
            "account_id": "acc_prod",
            "clip_path": dummy_clip,
            "title": "Insane 1v4 Clutch",
            "description": "Watch this clutch! #gaming",
        })
        self.assertEqual(res.status_code, 200)
        post_data = res.json()
        self.assertEqual(post_data["account_id"], "acc_prod")
        self.assertEqual(post_data["title"], "Insane 1v4 Clutch")

        # Check queue
        res = self.client.get("/api/social/queue")
        self.assertEqual(res.status_code, 200)
        queue = res.json()
        self.assertEqual(len(queue), 1)
        post_id = queue[0]["id"]

        # Cancel/delete from queue
        res = self.client.delete(f"/api/social/queue/{post_id}")
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/api/social/queue")
        self.assertEqual(len(res.json()), 0)

    def test_custom_clip_upload(self):
        file_bytes = b"\x00\x00\x00\x18ftypmp42dummycontent"
        files = {"file": ("test_upload.mp4", io.BytesIO(file_bytes), "video/mp4")}
        res = self.client.post("/api/social/upload-file", files=files)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIn("custom_uploads", data["path"])
        # Clean up uploaded file
        if os.path.exists(data["full_path"]):
            os.remove(data["full_path"])


if __name__ == "__main__":
    unittest.main()
