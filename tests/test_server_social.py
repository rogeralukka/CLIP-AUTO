"""tests/test_server_social.py - publish endpoints."""
import io
import os
import shutil
import tempfile
import unittest

from starlette.testclient import TestClient

import server
from clipper import social


class TestServerSocial(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (social.ACCOUNTS_FILE, social.SCHEDULE_QUEUE_FILE, social.TOKENS_DIR, social.CLIENT_SECRET_FILE)
        social.ACCOUNTS_FILE = os.path.join(self.tmp, "a.yaml")
        social.SCHEDULE_QUEUE_FILE = os.path.join(self.tmp, "q.json")
        social.TOKENS_DIR = os.path.join(self.tmp, "tokens")
        social.CLIENT_SECRET_FILE = os.path.join(self.tmp, "c.json")
        self.client = TestClient(server.app)
        self.clipdir = os.path.join(server.ROOT, "clips", "_test_publish")
        os.makedirs(self.clipdir, exist_ok=True)
        self.clip = os.path.join(self.clipdir, "t.mp4")
        with open(self.clip, "wb") as f:
            f.write(b"x")

    def tearDown(self):
        (social.ACCOUNTS_FILE, social.SCHEDULE_QUEUE_FILE, social.TOKENS_DIR, social.CLIENT_SECRET_FILE) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.clipdir, ignore_errors=True)

    def add_account(self):
        r = self.client.post("/api/social/accounts", json={"name": "Main", "platforms": {"youtube": {"enabled": True, "handle": "@m"}}})
        self.assertEqual(r.status_code, 200)
        return r.json()["id"]

    def test_starts_empty(self):
        self.assertEqual(self.client.get("/api/social/accounts").json(), [])
        self.assertEqual(self.client.get("/api/social/queue").json(), [])

    def test_account_and_plan_flow(self):
        aid = self.add_account()
        accs = self.client.get("/api/social/accounts").json()
        self.assertEqual(accs[0]["connected"]["youtube"], False)
        self.assertEqual(accs[0]["today_count"], 0)
        r = self.client.post("/api/social/schedule", json={"account_id": aid, "clip_path": self.clip, "title": "Moment",
                                                           "scheduled_time": "2099-01-01T10:00:00"})
        self.assertEqual(r.status_code, 200, r.text)
        pid = r.json()["id"]
        self.assertEqual(r.json()["status"], "TO POST")
        r = self.client.post(f"/api/social/queue/{pid}/youtube/posted")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/social/queue").json()[0]["status"], "DONE")
        self.assertIsInstance(self.client.get("/api/social/library").json(), list)
        self.assertEqual(self.client.delete(f"/api/social/queue/{pid}").json()["ok"], True)

    def test_clip_outside_project_is_refused(self):
        aid = self.add_account()
        outside = os.path.join(self.tmp, "x.mp4")
        open(outside, "wb").write(b"x")
        r = self.client.post("/api/social/schedule", json={"account_id": aid, "clip_path": outside})
        self.assertEqual(r.status_code, 400)

    def test_bad_client_secret_rejected(self):
        r = self.client.post("/api/social/client-secret", files={"file": ("c.json", io.BytesIO(b'{"a":1}'), "application/json")})
        self.assertEqual(r.status_code, 400)
        good = b'{"installed":{"client_id":"x","client_secret":"y"}}'
        r = self.client.post("/api/social/client-secret", files={"file": ("c.json", io.BytesIO(good), "application/json")})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.client.get("/api/social/info").json()["has_client_secret"])

    def test_credentials_validation_and_disconnect(self):
        aid = self.add_account()
        r = self.client.post(f"/api/social/accounts/{aid}/credentials", json={"platform": "facebook", "access_token": "t"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(f"/api/social/accounts/{aid}/credentials", json={"platform": "facebook", "access_token": "t", "page_id": "5"})
        self.assertTrue(r.json()["connected"]["facebook"])
        r = self.client.delete(f"/api/social/accounts/{aid}/connect/facebook")
        self.assertFalse(r.json()["connected"]["facebook"])

    def test_open_sites_endpoint(self):
        aid = self.add_account()
        r = self.client.post("/api/social/open", json={"account_id": aid, "login": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["opened"], 0)       # no profile set -> the page opens the links itself
        self.assertEqual(self.client.post("/api/social/open", json={"account_id": "nope", "urls": []}).status_code, 404)
        self.assertEqual(self.client.post("/api/social/open", json={"account_id": aid, "urls": ["https://evil.example"]}).status_code, 400)

    def test_custom_clip_upload(self):
        files = {"file": ("test_upload.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42dummy"), "video/mp4")}
        r = self.client.post("/api/social/upload-file", files=files)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("custom_uploads", data["path"])
        os.remove(data["full_path"])


if __name__ == "__main__":
    unittest.main()
