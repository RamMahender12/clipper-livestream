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

    def test_each_streamer_posts_to_its_own_account_and_alerts(self):
        l1, l2, c1 = self.clip("lacy", 1), self.clip("lacy", 2), self.clip("clix", 1)
        sent = []
        with mock.patch.dict(publishers._REGISTRY, {"youtube": FakeYouTube}), \
                mock.patch.object(notify, "send", side_effect=lambda t: sent.append(t) or True):
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
                mock.patch.object(notify, "send", return_value=True):
            lines = scheduler.post_once(self.db, self.cfg)
        self.assertEqual(len(FakeYouTube.calls), 2)
        self.assertTrue(any("next slot" in l for l in lines))
        self.assertEqual(statuses[l1] if tokens["youtube_lacy.json"] == l1 else statuses[l2], "posted")

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
