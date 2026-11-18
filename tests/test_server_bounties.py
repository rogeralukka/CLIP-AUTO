"""tests/test_server_bounties.py - Unit tests for server bounty endpoints."""
import os
import shutil
import tempfile
import unittest
from starlette.testclient import TestClient

from clipper import bounties
import server


class TestServerBounties(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.orig_bounties_file = bounties.BOUNTIES_FILE
        bounties.BOUNTIES_FILE = os.path.join(self.tmp_dir, "bounties.json")
        self.client = TestClient(server.app)

    def tearDown(self):
        bounties.BOUNTIES_FILE = self.orig_bounties_file
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_bounties_endpoints(self):
        # 1. GET /api/bounties
        res = self.client.get("/api/bounties")
        self.assertEqual(res.status_code, 200)
        items = res.json()
        self.assertGreater(len(items), 0)

        # 2. POST /api/bounties
        new_bounty = {
            "id": "test_bounty_1",
            "title": "Test Kick Streamer Bounty",
            "streamer": "Streamer Pro",
            "source": "discord",
            "payout_rate": "$3.00 CPM",
            "pool_size": "$5,000",
            "required_hashtags": ["#test", "#stream"],
        }
        res = self.client.post("/api/bounties", json=new_bounty)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["id"], "test_bounty_1")

        # 3. POST /api/bounties/{bid}/link-account
        res = self.client.post("/api/bounties/test_bounty_1/link-account", json={"account_id": "acc_main"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["bounty"]["assigned_account_id"], "acc_main")

        # 4. POST /api/bounties/{bid}/import
        res = self.client.post("/api/bounties/test_bounty_1/import", json={"account_id": "acc_main"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["campaign"]["id"], "test_bounty_1")
        self.assertEqual(res.json()["campaign"]["assigned_account_id"], "acc_main")

        # 5. DELETE /api/bounties/{bid}
        res = self.client.delete("/api/bounties/test_bounty_1")
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
