"""tests/test_social.py - Unit tests for social accounts management and scheduling."""
import datetime
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from clipper import social


class TestSocialManager(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.orig_accounts_file = social.ACCOUNTS_FILE
        self.orig_queue_file = social.SCHEDULE_QUEUE_FILE

        social.ACCOUNTS_FILE = os.path.join(self.tmp_dir, "social_accounts.yaml")
        social.SCHEDULE_QUEUE_FILE = os.path.join(self.tmp_dir, "scheduled_posts.json")

    def tearDown(self):
        social.ACCOUNTS_FILE = self.orig_accounts_file
        social.SCHEDULE_QUEUE_FILE = self.orig_queue_file
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_account_crud(self):
        # 1. Create
        aid = social.upsert_account("acc_test", {
            "name": "Gaming Clips Daily",
            "max_clips_per_day": 4,
            "schedule_slots": ["10:00", "14:00", "18:00", "21:00"],
            "platforms": {
                "youtube": {"enabled": True, "channel_name": "Test YT"},
                "instagram": {"enabled": False},
            }
        })
        self.assertEqual(aid, "acc_test")

        # 2. Read
        acc = social.get_account("acc_test")
        self.assertIsNotNone(acc)
        self.assertEqual(acc["name"], "Gaming Clips Daily")
        self.assertEqual(acc["max_clips_per_day"], 4)

        # 3. Update
        social.upsert_account("acc_test", {"name": "Gaming Highlights Pro"})
        updated = social.get_account("acc_test")
        self.assertEqual(updated["name"], "Gaming Highlights Pro")

        # 4. Delete
        ok = social.delete_account("acc_test")
        self.assertTrue(ok)
        self.assertIsNone(social.get_account("acc_test"))

    def test_daily_limit_and_slots(self):
        social.upsert_account("acc_limits", {
            "name": "Limit Tester",
            "max_clips_per_day": 3,
            "schedule_slots": ["10:00", "14:00", "18:00"],
        })

        today_str = datetime.date.today().isoformat()
        self.assertEqual(social.get_daily_scheduled_count("acc_limits", today_str), 0)

        # Mock queue with 3 posts today
        fake_queue = [
            {"account_id": "acc_limits", "scheduled_time": f"{today_str}T10:00:00", "clip_path": "clip1.mp4"},
            {"account_id": "acc_limits", "scheduled_time": f"{today_str}T14:00:00", "clip_path": "clip2.mp4"},
            {"account_id": "acc_limits", "scheduled_time": f"{today_str}T18:00:00", "clip_path": "clip3.mp4"},
        ]
        social.save_schedule_queue(fake_queue)

        self.assertEqual(social.get_daily_scheduled_count("acc_limits", today_str), 3)

        # Unique clip check
        self.assertEqual(social.get_assigned_clip_account("clip1.mp4"), "acc_limits")
        self.assertIsNone(social.get_assigned_clip_account("clip_unused.mp4"))

        # Verify 4th post throws ValueError for exceeding daily limit of 3
        test_video = os.path.join(self.tmp_dir, "test_clip.mp4")
        with open(test_video, "w") as f:
            f.write("dummy video data")

        with self.assertRaises(ValueError) as ctx:
            social.schedule_clip_to_account(
                account_id="acc_limits",
                clip_path=test_video,
                target_time_iso=f"{today_str}T20:00:00"
            )
        self.assertIn("already reached its daily maximum", str(ctx.exception))

    def test_unique_clip_enforcement(self):
        social.upsert_account("acc_1", {"name": "Account 1"})
        social.upsert_account("acc_2", {"name": "Account 2"})

        test_video = os.path.join(self.tmp_dir, "unique_test.mp4")
        with open(test_video, "w") as f:
            f.write("dummy video data")

        today_str = datetime.date.today().isoformat()
        fake_queue = [
            {"account_id": "acc_1", "scheduled_time": f"{today_str}T12:00:00", "clip_path": test_video},
        ]
        social.save_schedule_queue(fake_queue)

        # Attempting to schedule same clip to Account 2 should fail
        with self.assertRaises(ValueError) as ctx:
            social.schedule_clip_to_account(
                account_id="acc_2",
                clip_path=test_video,
                target_time_iso=f"{today_str}T15:00:00"
            )
        self.assertIn("already scheduled to 'acc_1'", str(ctx.exception))

    def test_delete_schedule_entry(self):
        fake_queue = [
            {"id": "post_1", "account_id": "acc_1", "title": "Post 1"},
            {"id": "post_2", "account_id": "acc_1", "title": "Post 2"},
        ]
        social.save_schedule_queue(fake_queue)
        self.assertTrue(social.delete_schedule_entry("post_1"))
        q = social.load_schedule_queue()
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["id"], "post_2")
        self.assertFalse(social.delete_schedule_entry("non_existent"))


if __name__ == "__main__":
    unittest.main()
