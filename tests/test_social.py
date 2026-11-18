"""tests/test_social.py - accounts, the publish plan, honest statuses, quota and platform payloads."""
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from clipper import social

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (social.ACCOUNTS_FILE, social.SCHEDULE_QUEUE_FILE, social.TOKENS_DIR, social.CLIENT_SECRET_FILE)
        social.ACCOUNTS_FILE = os.path.join(self.tmp, "accounts.yaml")
        social.SCHEDULE_QUEUE_FILE = os.path.join(self.tmp, "posts.json")
        social.TOKENS_DIR = os.path.join(self.tmp, "tokens")
        social.CLIENT_SECRET_FILE = os.path.join(self.tmp, "client.json")

    def tearDown(self):
        (social.ACCOUNTS_FILE, social.SCHEDULE_QUEUE_FILE, social.TOKENS_DIR, social.CLIENT_SECRET_FILE) = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clip(self, name="a.mp4"):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    def acct(self, aid="acc1", **kw):
        d = {"name": "Main", "max_clips_per_day": 2, "schedule_slots": ["10:00", "14:00", "18:00"],
             "platforms": {"youtube": {"enabled": True, "handle": "@me"}, "tiktok": {"enabled": True, "handle": "@me"}}}
        d.update(kw)
        return social.upsert_account(aid, d)


class TestAccounts(Base):
    def test_no_fake_accounts_by_default(self):
        self.assertEqual(social.load_accounts(), {})

    def test_crud_and_handles(self):
        aid = self.acct()
        a = social.get_account(aid)
        self.assertEqual(a["platforms"]["youtube"]["handle"], "@me")
        self.assertFalse(a["platforms"]["instagram"]["enabled"])
        social.upsert_account(aid, {"platforms": {"instagram": {"enabled": True, "handle": "@ig"}}})
        a = social.get_account(aid)
        self.assertTrue(a["platforms"]["instagram"]["enabled"])
        self.assertTrue(a["platforms"]["youtube"]["enabled"])  # untouched
        self.assertTrue(social.delete_account(aid))
        self.assertIsNone(social.get_account(aid))

    def test_secrets_not_in_account_file(self):
        aid = self.acct()
        social.save_credentials(aid, "facebook", {"access_token": "SECRET123", "page_id": "9"})
        self.assertNotIn("SECRET123", open(social.ACCOUNTS_FILE, encoding="utf-8").read())
        self.assertTrue(social.connection_state(aid)["facebook"])
        social.delete_account(aid)
        self.assertFalse(os.path.exists(os.path.join(social.TOKENS_DIR, f"facebook_{aid}.json")))

    def test_client_secret_validation(self):
        with self.assertRaises(ValueError):
            social.save_client_secret(b'{"nope": 1}')
        social.save_client_secret(json.dumps({"installed": {"client_id": "x"}}).encode())
        self.assertTrue(social.has_client_secret())

    def test_gitignore_protects_secrets(self):
        gi = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
        for needle in ("config/tokens/", "config/youtube_client_secret.json", "config/social_accounts.yaml", "config/scheduled_posts.json"):
            self.assertIn(needle, gi)


class TestPlan(Base):
    def test_requires_a_platform(self):
        social.upsert_account("empty", {"name": "Empty"})
        with self.assertRaises(ValueError):
            social.schedule_clip_to_account("empty", self.clip())

    def test_plan_mode_is_honest(self):
        aid = self.acct()
        e = social.schedule_clip_to_account(aid, self.clip(), title="Big moment", target_time_iso="2099-01-01T10:00:00")
        self.assertEqual(e["status"], "TO POST")
        self.assertEqual(set(e["platforms"]), {"youtube", "tiktok"})
        self.assertTrue(all(p["status"] == social.PLANNED for p in e["platforms"].values()))
        self.assertIn("#Shorts", e["platforms"]["youtube"]["title"])
        self.assertTrue(e["platforms"]["tiktok"]["link"].startswith("https://www.tiktok.com"))

    def test_marking_posted_finishes_the_post(self):
        aid = self.acct()
        e = social.schedule_clip_to_account(aid, self.clip(), target_time_iso="2099-01-01T10:00:00")
        social.mark_platform(e["id"], "youtube")
        self.assertEqual(social.queue_view()[0]["status"], "TO POST")
        social.mark_platform(e["id"], "tiktok")
        self.assertEqual(social.queue_view()[0]["status"], "DONE")
        social.mark_platform(e["id"], "tiktok", posted=False)
        self.assertEqual(social.queue_view()[0]["status"], "TO POST")

    def test_unique_clip_and_daily_limit(self):
        a1, a2 = self.acct("a1"), self.acct("a2")
        c = self.clip("one.mp4")
        social.schedule_clip_to_account(a1, c, target_time_iso="2099-01-01T10:00:00")
        with self.assertRaises(ValueError):
            social.schedule_clip_to_account(a2, c, target_time_iso="2099-01-01T11:00:00")
        social.schedule_clip_to_account(a1, self.clip("two.mp4"), target_time_iso="2099-01-01T14:00:00")
        with self.assertRaises(ValueError):  # limit is 2
            social.schedule_clip_to_account(a1, self.clip("three.mp4"), target_time_iso="2099-01-01T18:00:00")
        self.assertEqual(social.get_daily_scheduled_count(a1, "2099-01-01"), 2)

    def test_failed_post_frees_quota_and_clip(self):
        aid = self.acct()
        c = self.clip()
        e = social.schedule_clip_to_account(aid, c, target_time_iso="2099-01-01T10:00:00")
        for p in ("youtube", "tiktok"):
            social._set_platform(e["id"], p, status=social.FAILED, note="boom")
        self.assertEqual(social.queue_view()[0]["status"], "FAILED")
        self.assertEqual(social.get_daily_scheduled_count(aid, "2099-01-01"), 0)
        self.assertIsNone(social.get_assigned_clip_account(c))
        # one platform ok -> still counts, still locked
        e2 = social.schedule_clip_to_account(aid, self.clip("b.mp4"), target_time_iso="2099-01-01T14:00:00")
        social._set_platform(e2["id"], "youtube", status=social.FAILED)
        self.assertEqual(social.entry_status(social._find(e2["id"])), "PARTIAL")
        self.assertEqual(social.get_daily_scheduled_count(aid, "2099-01-01"), 1)

    def test_next_slot_skips_full_days(self):
        aid = self.acct(max_clips_per_day=1)
        d1, t1 = social.calculate_next_slot(aid)
        social.schedule_clip_to_account(aid, self.clip(), target_time_iso=f"{d1}T{t1}:00")
        d2, _ = social.calculate_next_slot(aid)
        self.assertGreater(d2, d1)

    def test_times_are_timezone_aware(self):
        aid = self.acct()
        e = social.schedule_clip_to_account(aid, self.clip(), target_time_iso="2099-06-01T10:00:00")
        self.assertIsNotNone(datetime.datetime.fromisoformat(e["scheduled_time"]).tzinfo)

    def test_remove_entry_unlocks_clip(self):
        aid = self.acct()
        c = self.clip()
        e = social.schedule_clip_to_account(aid, c, target_time_iso="2099-01-01T10:00:00")
        self.assertTrue(social.delete_schedule_entry(e["id"]))
        self.assertIsNone(social.get_assigned_clip_account(c))


class TestAutoMode(Base):
    def test_unconnected_platform_falls_back_to_manual(self):
        aid = self.acct()
        e = social.schedule_clip_to_account(aid, self.clip(), mode="auto", target_time_iso="2099-01-01T10:00:00")
        self.assertTrue(all(p["status"] == social.PLANNED for p in e["platforms"].values()))
        self.assertIn("Not connected", e["platforms"]["youtube"]["note"])

    def test_instagram_is_held_not_claimed_scheduled(self):
        aid = self.acct(platforms={"instagram": {"enabled": True, "handle": "@i"}})
        social.save_credentials(aid, "instagram", {"access_token": "t", "ig_user_id": "1"})
        e = social.schedule_clip_to_account(aid, self.clip(), mode="auto", target_time_iso="2099-01-01T10:00:00")
        self.assertEqual(e["platforms"]["instagram"]["status"], social.HELD)
        self.assertIn("Studio", e["platforms"]["instagram"]["note"])

    def test_failed_upload_is_recorded_as_failed(self):
        aid = self.acct(platforms={"facebook": {"enabled": True, "handle": "p"}})
        social.save_credentials(aid, "facebook", {"access_token": "t", "page_id": "1"})
        with patch.object(social, "upload_to_facebook_reel", side_effect=RuntimeError("token expired")):
            e = social.schedule_clip_to_account(aid, self.clip(), mode="auto", target_time_iso="2099-01-01T10:00:00")
            social._run_uploads(e["id"])
        got = social.queue_view()[0]
        self.assertEqual(got["platforms"]["facebook"]["status"], social.FAILED)
        self.assertIn("token expired", got["platforms"]["facebook"]["note"])
        self.assertEqual(got["status"], "FAILED")

    def test_successful_facebook_upload_is_scheduled(self):
        aid = self.acct(platforms={"facebook": {"enabled": True, "handle": "p"}})
        social.save_credentials(aid, "facebook", {"access_token": "t", "page_id": "1"})
        ok = {"status": social.UPLOADED_SCHEDULED, "external_id": "v1", "note": "held"}
        with patch.object(social, "upload_to_facebook_reel", return_value=ok):
            e = social.schedule_clip_to_account(aid, self.clip(), mode="auto", target_time_iso="2099-01-01T10:00:00")
            social._run_uploads(e["id"])
        self.assertEqual(social.queue_view()[0]["status"], "SCHEDULED")

    def test_due_instagram_post_publishes(self):
        aid = self.acct(platforms={"instagram": {"enabled": True, "handle": "@i"}})
        social.save_credentials(aid, "instagram", {"access_token": "t", "ig_user_id": "1"})
        e = social.schedule_clip_to_account(aid, self.clip(), mode="auto", target_time_iso="2099-01-01T10:00:00")
        self.assertEqual(social.publish_due_posts(), 0)  # not due yet
        q = social.load_schedule_queue()
        q[0]["scheduled_time"] = (social._now() - datetime.timedelta(minutes=1)).isoformat()
        social.save_schedule_queue(q)
        with patch.object(social, "upload_to_instagram_reel", return_value={"status": social.PUBLISHED, "external_id": "m1", "note": "Published."}):
            self.assertEqual(social.publish_due_posts(), 1)
        self.assertEqual(social.queue_view()[0]["platforms"]["instagram"]["status"], social.PUBLISHED)


class TestPlatformPayloads(Base):
    def test_tiktok_is_private_and_has_no_schedule_field(self):
        aid = self.acct()
        social.save_credentials(aid, "tiktok", {"access_token": "t"})
        clip = self.clip()
        init = MagicMock()
        init.json.return_value = {"data": {"publish_id": "p1", "upload_url": "https://up"}, "error": {"code": "ok"}}
        with patch("requests.post", return_value=init) as post, patch("requests.put", return_value=MagicMock(status_code=200)):
            r = social.upload_to_tiktok(aid, clip, "hello")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["post_info"]["privacy_level"], "SELF_ONLY")
        self.assertNotIn("schedule_time", body["post_info"])
        self.assertEqual(r["status"], social.UPLOADED_PRIVATE)

    def test_tiktok_error_raises(self):
        aid = self.acct()
        social.save_credentials(aid, "tiktok", {"access_token": "t"})
        bad = MagicMock()
        bad.json.return_value = {"error": {"code": "access_token_invalid", "message": "bad token"}}
        with patch("requests.post", return_value=bad), self.assertRaises(RuntimeError):
            social.upload_to_tiktok(aid, self.clip(), "x")

    def test_facebook_needs_ten_minutes(self):
        aid = self.acct()
        social.save_credentials(aid, "facebook", {"access_token": "t", "page_id": "1"})
        start = MagicMock()
        start.json.return_value = {"video_id": "v", "upload_url": "https://u"}
        up = MagicMock()
        up.json.return_value = {"success": True}
        soon = (social._now() + datetime.timedelta(minutes=2)).isoformat()
        with patch("requests.post", side_effect=[start, up]), self.assertRaises(ValueError):
            social.upload_to_facebook_reel(aid, self.clip(), "cap", soon)

    def test_graph_error_detected(self):
        self.assertEqual(social._graph_error({"error": {"message": "Invalid token"}}), "Invalid token")
        self.assertIsNone(social._graph_error({"id": "1"}))


if __name__ == "__main__":
    unittest.main()
