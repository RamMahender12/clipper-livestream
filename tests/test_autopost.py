import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from clipper import config as config_mod, notify, publishers, scheduler
from clipper.db import DB
from clipper.publishers.base import PostResult, Publisher


class FakeYouTube(Publisher):
    name = "youtube"
    calls: list = []

    def ready(self):
        return True, "ok"

    def publish(self, video, meta):
        token = self.cfg["youtube"]["token_file"]
        FakeYouTube.calls.append((token, meta.clip_id, meta.caption))
        return PostResult(True, "youtube", remote_id=f"v{meta.clip_id}",
                          url=f"https://youtube.com/shorts/v{meta.clip_id}", status="published")


class AutoPostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        sec = Path(self.tmp) / "secrets"
        sec.mkdir()
        (sec / "youtube_lacy.json").write_text("{}")
        (sec / "youtube_clix.json").write_text("{}")
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": str(Path(self.tmp) / "data")},
            "posting": {"platforms": ["youtube", "instagram"], "min_gap_minutes": 60,
                        "max_posts_per_day": {"youtube": 5, "instagram": 5}, "timezone": "Asia/Kolkata",
                        "earliest_footage": "2026-11-18T11:00:00+00:00"},
            "auto_approve": {"veto_minutes": 0},
            "campaigns": [
                {"id": "lacy-gta", "name": "LACY pool", "url": "https://cr/lacy", "platforms": ["youtube", "instagram"]},
                {"id": "clix-gta", "name": "Clix pool", "url": "https://cr/clix", "platforms": ["youtube", "instagram"]},
            ],
            "channels": [
                {"login": "lacy", "permission": "campaign:lacy-gta",
                 "youtube_token_file": str(sec / "youtube_lacy.json")},
                {"login": "clix", "permission": "campaign:clix-gta",
                 "youtube_token_file": str(sec / "youtube_clix.json")},
            ],
        })
        self.db = DB(Path(self.tmp) / "t.db")
        FakeYouTube.calls = []

    def clip(self, channel, cid_hint=0):
        video = Path(self.tmp) / f"{channel}{cid_hint}.mp4"
        video.write_bytes(b"x")
        now = time.time()
        return self.db.insert("clips", channel=channel, campaign_id=f"{channel}-gta", status="approved",
                              final_path=str(video), spoiler_risk="none", caption=f"{channel} moment",
                              hashtags=channel.upper(), quality=8, wall_start=now - 600,
                              decided_by="human", created_at=now, updated_at=now)

    def test_account_resolution(self):
        lacy = self.cfg["channels"][0]
        self.assertTrue(publishers.channel_account(self.cfg, lacy, "youtube").endswith("youtube_lacy.json"))
        self.assertEqual(publishers.channel_account(self.cfg, lacy, "instagram"), "")  # no IG id set
        self.assertEqual(publishers.channel_account(self.cfg, {"login": "nobody"}, "youtube"), "")
        acfg, acct = publishers.account_cfg(self.cfg, {**lacy, "instagram_user_id": "178"}, "instagram")
        self.assertEqual((acct, acfg["instagram"]["ig_user_id"]), ("178", "178"))
        self.assertNotEqual(self.cfg["instagram"].get("ig_user_id"), "178")  # original untouched

    def test_paused_account_is_skipped_until_resumed(self):
        from clipper import takedowns
        self.clip("lacy", 1)
        c1 = self.clip("clix", 1)
        lacy_account = publishers.channel_account(self.cfg, self.cfg["channels"][0], "youtube")
        takedowns._schema(self.db)
        self.db.execute("INSERT INTO paused_accounts (account, platform, channel, reason, paused_at) VALUES (?, ?, ?, ?, ?)",
                        (lacy_account, "youtube", "lacy", "removed: gone", time.time()))
        with mock.patch.dict(publishers._REGISTRY, {"youtube": FakeYouTube}), \
                mock.patch("clipper.submit_tracker.send_batch", return_value=True):
            lines = scheduler.post_once(self.db, self.cfg)
        self.assertEqual([cid for _, cid, _ in FakeYouTube.calls], [c1])  # only Clix posted
        self.assertTrue(any("paused after a takedown" in l for l in lines))
        takedowns.resume(self.db, lacy_account)
        with mock.patch.dict(publishers._REGISTRY, {"youtube": FakeYouTube}), \
                mock.patch("clipper.submit_tracker.send_batch", return_value=True):
            scheduler.post_once(self.db, self.cfg)
        self.assertEqual(len(FakeYouTube.calls), 2)  # LACY posts again

    def test_each_streamer_posts_to_its_own_account_and_alerts(self):
        l1, l2, c1 = self.clip("lacy", 1), self.clip("lacy", 2), self.clip("clix", 1)
        sent = []
        with mock.patch.dict(publishers._REGISTRY, {"youtube": FakeYouTube}), \
                mock.patch("clipper.submit_tracker.send_batch",
                           side_effect=lambda cfg, db, posted, failed: sent.append(notify.batch_message(cfg, posted, failed)) or True):
            scheduler.post_once(self.db, self.cfg)
        tokens = {Path(t).name: cid for t, cid, _ in FakeYouTube.calls}
        self.assertEqual(set(tokens), {"youtube_lacy.json", "youtube_clix.json"})  # one per account
        self.assertEqual(tokens["youtube_clix.json"], c1)
        self.assertEqual(len(FakeYouTube.calls), 2)
        self.assertEqual(len(sent), 1)
        self.assertIn("https://youtube.com/shorts/v", sent[0])
        self.assertIn("Submit here: https://cr/lacy", sent[0])
        self.assertIn("Submit here: https://cr/clix", sent[0])
        # no instagram account configured -> nothing attempted there, clip counts as done
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM posts WHERE platform='instagram'")["n"], 0)
        statuses = {r["id"]: r["status"] for r in self.db.query("SELECT id, status FROM clips")}
        self.assertEqual(statuses[c1], "posted")
        # second pass: lacy's account is inside its 60-min gap, so the 2nd lacy clip waits
        with mock.patch.dict(publishers._REGISTRY, {"youtube": FakeYouTube}), \
                mock.patch("clipper.submit_tracker.send_batch", return_value=True):
            lines = scheduler.post_once(self.db, self.cfg)
        self.assertEqual(len(FakeYouTube.calls), 2)
        self.assertTrue(any("next slot" in l for l in lines))
        self.assertEqual(statuses[l1] if tokens["youtube_lacy.json"] == l1 else statuses[l2], "posted")

    def test_waiting_reasons_give_each_clip_a_start_time(self):
        a, b = self.clip("lacy", 1), self.clip("lacy", 2)
        w = scheduler.waiting_reasons(self.db, self.cfg)
        self.assertEqual(w[a], ["YouTube: uploads within a minute"])
        self.assertTrue(w[b][0].startswith("YouTube: uploads at "), w)
        self.assertTrue(w[b][0].endswith("(in 1h 01m) · 60-min gap between posts · 1 clip ahead in line"), w)
        acct = publishers.channel_account(self.cfg, self.cfg["channels"][0], "youtube")
        for i in range(5):  # cap is 5: full
            self.db.insert("posts", clip_id=900 + i, platform="youtube", account=acct, status="published",
                           posted_at=time.time() - 7200 - i)
        self.assertIn("daily limit reached (5 uploads per 24h)", scheduler.waiting_reasons(self.db, self.cfg)[a][0])

    def test_slots_respect_gap_and_daily_cap(self):
        now = 1_000_000.0
        acct = "acct"
        for i in range(5):  # cap is 5: posted hourly, the last one 1h ago
            self.db.insert("posts", clip_id=900 + i, platform="youtube", account=acct, status="published",
                           posted_at=now - 3600 * (5 - i))
        s = scheduler._slots(self.db, self.cfg, "youtube", acct, now)
        # full until the oldest (5h ago) ages out at now + 19h, then the next one an hour later
        self.assertEqual([next(s)[0] - now for _ in range(2)], [19 * 3600, 20 * 3600])

    def test_batch_message(self):
        now = datetime(2026, 11, 19, 7, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
        msg = notify.batch_message(self.cfg, [
            {"campaign": self.cfg["campaigns"][0], "platform": "youtube", "url": "https://y/1", "clip_id": 1, "hook": ""},
            {"campaign": self.cfg["campaigns"][0], "platform": "instagram", "url": "https://i/1", "clip_id": 1, "hook": ""},
        ], ["instagram @ clix, clip 3: token expired"], now=now)
        self.assertIn("Submit by 07:30", msg)
        self.assertEqual(msg.count("Submit here: https://cr/lacy"), 1)
        self.assertIn("token expired", msg)

    def test_send_without_config_is_noop(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            self.assertFalse(notify.send("hi"))


if __name__ == "__main__":
    unittest.main()
