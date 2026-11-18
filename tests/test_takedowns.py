import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipper import config as config_mod, takedowns
from clipper.db import DB
from clipper.publishers import instagram, youtube


class FakePub:
    states = {}

    def __init__(self, platform):
        self.platform = platform

    def check_status(self, remote_id):
        return self.states.get(remote_id, ("ok", ""))


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": self.tmp},
            "channels": [{"login": "lacy", "display_name": "LACY", "permission": "own"}]})
        self.db = DB(Path(self.tmp) / "t.db")
        now = time.time()
        c = self.db.insert("clips", channel="lacy", status="posted", created_at=now, updated_at=now)
        self.yt = self.db.insert("posts", clip_id=c, platform="youtube", account="secrets/youtube_lacy.json",
                                 remote_id="YT1", url="https://youtube.com/shorts/YT1", status="published", posted_at=now)
        self.ig = self.db.insert("posts", clip_id=c, platform="instagram", account="1111",
                                 remote_id="IG1", url="https://instagram.com/reel/IG1", status="published", posted_at=now)
        self.sent, self.buttons = [], []
        p = mock.patch("clipper.publishers.get_publisher", side_effect=lambda platform, cfg, ch=None: FakePub(platform))
        p.start()
        self.addCleanup(p.stop)
        FakePub.states = {}

    def run_check(self):
        return takedowns.run_once(self.db, self.cfg, send=self.sent.append,
                                  send_pause=lambda text, ch, pl, name: self.buttons.append((ch, pl, name))
                                  or self.sent.append(text))

    def test_removal_pauses_the_account_and_alerts_once(self):
        FakePub.states = {"YT1": ("rejected", "YouTube rejected it: copyright")}
        lines = self.run_check()
        self.assertEqual(len(lines), 1)
        self.assertIn("copyright", takedowns.paused(self.db, "secrets/youtube_lacy.json"))
        self.assertEqual(takedowns.paused(self.db, "1111"), "")  # Instagram untouched
        self.assertIn("PAUSED", self.sent[0])
        self.assertIn("clipper resume lacy youtube", self.sent[0])
        self.assertEqual(self.buttons, [("lacy", "youtube", "LACY")])  # sent with the Resume button
        self.assertEqual(self.db.one("SELECT status FROM posts WHERE id = ?", (self.yt,))["status"], "rejected")
        self.assertEqual(self.run_check(), [])  # reported once
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(takedowns.resume(self.db, "secrets/youtube_lacy.json"))
        self.assertEqual(takedowns.paused(self.db, "secrets/youtube_lacy.json"), "")

    def test_age_restriction_and_blocks_only_alert(self):
        FakePub.states = {"YT1": ("age", "age-restricted by YouTube"), "IG1": ("ok", "")}
        self.run_check()
        self.assertEqual(takedowns.paused(self.db, "secrets/youtube_lacy.json"), "")
        self.assertIn("Posting continues", self.sent[0])

    def test_unknown_or_failed_checks_change_nothing(self):
        FakePub.states = {"YT1": ("unknown", "HTTP 500"), "IG1": ("unknown", "connection")}
        self.assertEqual(self.run_check(), [])
        self.assertEqual(self.sent, [])
        FakePub.states = {"IG1": ("removed", "gone")}  # still checked later, since unknown wasn't recorded
        self.run_check()
        self.assertIn("removed", takedowns.paused(self.db, "1111"))


class ResumeButtonTests(unittest.TestCase):
    def test_tap_resumes_posting_from_your_chat_only(self):
        import os
        from clipper import notify, submit_tracker
        tmp = tempfile.mkdtemp()
        cfg = config_mod._merge(config_mod.DEFAULTS, {"paths": {"data_dir": tmp},
                                                      "channels": [{"login": "lacy", "display_name": "LACY", "permission": "own"}]})
        db = DB(Path(tmp) / "t.db")
        from clipper.publishers import channel_account
        account = channel_account(cfg, cfg["channels"][0], "youtube")
        takedowns._schema(db)
        db.execute("INSERT INTO paused_accounts (account, platform, channel, reason, paused_at) VALUES (?, ?, ?, ?, ?)",
                   (account, "youtube", "lacy", "removed: gone", time.time()))
        calls = []
        tap = lambda chat: {"callback_query": {"id": "q", "data": "resume:youtube:lacy",
                                               "message": {"message_id": 5, "chat": {"id": chat}}}}
        with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "42", "TELEGRAM_BOT_TOKEN": "t"}), \
                mock.patch.object(notify, "_call", side_effect=lambda m, p, **k: calls.append((m, p)) or {"ok": True}):
            self.assertIn("ignored", submit_tracker.handle_update(cfg, db, tap(7)))
            self.assertTrue(takedowns.paused(db, account))
            self.assertEqual(submit_tracker.handle_update(cfg, db, tap(42)), "posting to LACY youtube resumed")
        self.assertEqual(takedowns.paused(db, account), "")
        edit = [p for m, p in calls if m == "editMessageReplyMarkup"][-1]
        self.assertIn("Posting to LACY youtube resumed", edit["reply_markup"])


class StatusParsingTests(unittest.TestCase):
    cfg = config_mod._merge(config_mod.DEFAULTS, {})

    def yt(self, status, body):
        with mock.patch.object(youtube, "_access_token", return_value="tok"), \
                mock.patch.object(youtube, "_request", return_value=(status, {}, json.dumps(body).encode())):
            return youtube.YouTubePublisher(self.cfg).check_status("v1")

    def test_youtube_states(self):
        self.assertEqual(self.yt(200, {"items": []})[0], "removed")
        rej = {"items": [{"status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}, "contentDetails": {}}]}
        self.assertEqual(self.yt(200, rej), ("rejected", "YouTube rejected it: copyright"))
        blk = {"items": [{"status": {"uploadStatus": "processed"},
                          "contentDetails": {"regionRestriction": {"blocked": ["DE", "FR"]}}}]}
        self.assertEqual(self.yt(200, blk)[0], "blocked")
        age = {"items": [{"status": {"uploadStatus": "processed"},
                          "contentDetails": {"contentRating": {"ytRating": "ytAgeRestricted"}}}]}
        self.assertEqual(self.yt(200, age)[0], "age")
        ok = {"items": [{"status": {"uploadStatus": "processed"}, "contentDetails": {}}]}
        self.assertEqual(self.yt(200, ok), ("ok", ""))
        self.assertEqual(self.yt(403, {"error": {"message": "quota"}})[0], "unknown")  # never pauses on an API hiccup

    def test_instagram_states(self):
        pub = instagram.InstagramPublisher(self.cfg)
        with mock.patch.object(pub, "_token", return_value="t"), \
                mock.patch.object(instagram, "_call", return_value={"id": "IG1"}):
            self.assertEqual(pub.check_status("IG1"), ("ok", ""))
        gone = instagram._ApiError("Instagram HTTP 400 (code 100/33): Unsupported get request. Object with ID 'IG1' does not exist")
        with mock.patch.object(pub, "_token", return_value="t"), mock.patch.object(instagram, "_call", side_effect=gone):
            self.assertEqual(pub.check_status("IG1")[0], "removed")
        hiccup = instagram._ApiError("Instagram connection error: timed out", transient=True)
        with mock.patch.object(pub, "_token", return_value="t"), mock.patch.object(instagram, "_call", side_effect=hiccup):
            self.assertEqual(pub.check_status("IG1")[0], "unknown")


if __name__ == "__main__":
    unittest.main()
