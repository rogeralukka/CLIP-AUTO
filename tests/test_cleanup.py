"""tests/test_cleanup.py - Verification of fake data removal and clean campaign/metadata/quality logic."""
import unittest
from clipper.downloader import get_available_qualities
from clipper.campaigns import save_campaign, list_campaigns, delete_campaign
from clipper.metadata import generate_clip_metadata


class TestCleanApp(unittest.TestCase):
    def test_qualities_empty_input(self):
        opts = get_available_qualities("")
        self.assertEqual(opts, [])
        opts_ws = get_available_qualities("   ")
        self.assertEqual(opts_ws, [])

    def test_qualities_no_audio_only(self):
        opts = get_available_qualities("https://www.youtube.com/watch?v=invalid_or_offline")
        ids = [o["id"] for o in opts]
        self.assertNotIn("audio_only", ids)
        for o in opts:
            self.assertIn("size unknown", o["label"])

    def test_campaign_crud(self):
        cid = save_campaign({
            "name": "Valorant Creator Program",
            "streamer": "AcePlayer",
            "platform": "Twitch",
            "min_clip_length": 20,
            "max_clip_length": 50,
            "required_hashtags": ["#val", "#clutch"],
            "required_disclosure": "#sponsored",
            "rules_note": "Focus on high intensity rounds",
        })
        camps = list_campaigns("campaigns")
        self.assertTrue(any(c["id"] == cid for c in camps))

        # Check metadata with campaign
        meta = generate_clip_metadata({"transcript": "unbelievable ace in round five"}, camps[0])
        self.assertIn("AcePlayer", meta["title"])
        self.assertIn("Unbelievable ace", meta["title"])
        self.assertIn("#val", meta["hashtags"])
        self.assertIn("#sponsored", meta["description"])

        # Check metadata with no campaign and empty transcript
        meta_empty = generate_clip_metadata({"transcript": ""}, None)
        self.assertEqual(meta_empty["title"], "")
        self.assertEqual(meta_empty["hashtags"], "")
        self.assertEqual(meta_empty["description"], "")

        # Delete campaign
        res = delete_campaign(cid)
        self.assertTrue(res)
        self.assertEqual(len(list_campaigns("campaigns")), 0)


if __name__ == "__main__":
    unittest.main()
