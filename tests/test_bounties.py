"""tests/test_bounties.py - Unit tests for bounty directory, account linking, and campaign import."""
import json
import os
import shutil
import tempfile
import unittest

from clipper import bounties, campaigns


class TestBounties(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.orig_bounties_file = bounties.BOUNTIES_FILE
        self.orig_camp_dir = "campaigns"

        bounties.BOUNTIES_FILE = os.path.join(self.tmp_dir, "bounties.json")
        self.test_camp_dir = os.path.join(self.tmp_dir, "campaigns")
        os.makedirs(self.test_camp_dir, exist_ok=True)

    def tearDown(self):
        bounties.BOUNTIES_FILE = self.orig_bounties_file
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_load_and_upsert_bounties(self):
        # Default seeding
        blist = bounties.load_bounties()
        self.assertGreater(len(blist), 0)
        self.assertTrue(any(b["source"] == "vyro" for b in blist))

        # Add custom bounty
        custom_bounty = {
            "id": "discord_custom_pool",
            "title": "Custom Kick Streamer Pool",
            "streamer": "New Streamer",
            "source": "discord",
            "payout_rate": "$3.00 CPM",
            "required_hashtags": ["#custom", "#kick"],
        }
        bid = bounties.upsert_bounty(custom_bounty)
        self.assertEqual(bid, "discord_custom_pool")

        loaded = bounties.get_bounty("discord_custom_pool")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["title"], "Custom Kick Streamer Pool")

    def test_link_account_and_import_campaign(self):
        bounties.load_bounties()
        # Link account
        ok = bounties.link_bounty_account("discord_adin_ross", "acc_adin_hub")
        self.assertTrue(ok)

        b = bounties.get_bounty("discord_adin_ross")
        self.assertEqual(b["assigned_account_id"], "acc_adin_hub")

        # Import as campaign
        camp = bounties.import_bounty_as_campaign("discord_adin_ross", "acc_adin_hub")
        self.assertIsNotNone(camp)
        self.assertEqual(camp["id"], "discord_adin_ross")
        self.assertEqual(camp["assigned_account_id"], "acc_adin_hub")
        self.assertIn("#adinross", camp["required_hashtags"])

        # Check bounty reflects imported status
        refreshed = bounties.get_bounty("discord_adin_ross")
        self.assertTrue(refreshed["is_imported"])
        self.assertEqual(refreshed["assigned_account_id"], "acc_adin_hub")

    def test_delete_bounty(self):
        bounties.load_bounties()
        bounties.upsert_bounty({"id": "to_delete", "title": "Temporary Bounty"})
        self.assertIsNotNone(bounties.get_bounty("to_delete"))
        deleted = bounties.delete_bounty("to_delete")
        self.assertTrue(deleted)
        self.assertIsNone(bounties.get_bounty("to_delete"))


if __name__ == "__main__":
    unittest.main()
