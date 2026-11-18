import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from clipper import ai, config as config_mod, digest, submit_tracker, takedowns
from clipper.db import DB

IST = ZoneInfo("Asia/Kolkata")


def at(hour, day=20):
    return datetime(2026, 11, day, hour, 0, tzinfo=IST).timestamp()


class DigestTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": tmp},
            "posting": {"timezone": "Asia/Kolkata"},
            "campaigns": [{"id": "lacy-gta", "name": "$12,500 LACY GTA VI Marathon Pool (Reach)", "active": True,
                           "pool_min_views": 750000}],
            "channels": [{"login": "lacy", "display_name": "LACY", "permission": "campaign:lacy-gta"}]})
        self.db = DB(Path(tmp) / "clipper.db")
        now = at(9)
        c1 = self.db.insert("clips", channel="lacy", status="posted", decided_by="human", decided_at=now - 3600,
                            quality=7, hook="Drive-thru getaway", created_at=now - 7200, updated_at=now)
        self.db.insert("clips", channel="lacy", status="rejected", decided_by="auto", decided_at=now - 600,
                       decision_note="Weak moment: Claude rated it 3/10", created_at=now - 900, updated_at=now)
        w = self.db.insert("clips", channel="lacy", status="ready", quality=6, score=5, hook="Tiger ambush",
                           created_at=now - 1800, updated_at=now)
        self.db.conn.executescript("CREATE TABLE IF NOT EXISTS review_messages (clip_id INTEGER, kind TEXT, chat_id TEXT, "
                                   "message_id INTEGER, sent_at REAL, PRIMARY KEY (clip_id, kind));")
        self.db.execute("INSERT INTO review_messages VALUES (?, 'review', '42', 1, ?)", (w, now))
        self.p1 = self.db.insert("posts", clip_id=c1, platform="instagram", account="1", campaign_id="lacy-gta",
                                 url="https://instagram.com/reel/a", status="published", views=2061, posted_at=now - 3000)
        self.db.insert("posts", clip_id=c1, platform="youtube", account="y", campaign_id="lacy-gta",
                       url="https://youtube.com/shorts/a", status="published", views=1, posted_at=now - 3000)
        ai.record(self.cfg, "packaging", "claude-haiku-5-5", type("U", (), {"input_tokens": 1700, "output_tokens": 250})())
        self.db.conn.executescript(submit_tracker.SCHEMA)
        self.db.insert("submissions", campaign_id="lacy-gta", campaign_name="LACY", sent_at=now - 3000,
                       deadline=now - 1200, submitted_at=now - 2900)
        takedowns._schema(self.db)

    def test_message_covers_everything(self):
        text, snap = digest.build(self.db, self.cfg, at(9))
        self.assertIn("Posts: 2 (Instagram 1, YouTube 1)", text)
        self.assertIn("Views gained: +2,062 (Instagram +2,061, YouTube +1), new posts only", text)  # first digest
        self.assertIn("Top: LACY #1 Instagram 2,061 views", text)
        self.assertIn("LACY GTA VI Marathon Pool: 2,062 / 750,000 (0.3%)", text)
        self.assertIn("you approved 1, rejected 0", text)
        self.assertIn("weak moment 1", text)
        self.assertIn("not reviewed yet 1", text)
        self.assertIn("Submissions: 1 to do · 1 on time", text)
        self.assertIn("Claude: $0.00", text)
        self.assertIn("Takedowns: none · Paused: none", text)
        self.assertIn("#3 LACY q6: Tiger ambush", text)
        self.assertEqual(snap[self.p1], 2061)

    def test_gains_against_yesterdays_snapshot_and_once_a_day(self):
        sent = []
        self.assertFalse(digest.due(self.db, self.cfg, at(8)))  # before 9:00
        self.assertTrue(digest.due(self.db, self.cfg, at(9)))
        self.assertTrue(digest.send(self.db, self.cfg, at(9), sender=lambda t: sent.append(t) or True))
        self.assertEqual(len(sent), 1)
        self.assertFalse(digest.due(self.db, self.cfg, at(15)))  # already sent today
        self.db.execute("UPDATE posts SET views = 3000 WHERE id = ?", (self.p1,))
        self.assertTrue(digest.due(self.db, self.cfg, at(9, day=21)))
        text, _ = digest.build(self.db, self.cfg, at(9, day=21))
        self.assertIn("Views gained: +939 (Instagram +939, YouTube +0)", text)

    def test_reasons_are_short(self):
        self.assertEqual(digest._reason("AI hold 5/10: meh | held: AI confidence below 6"), "AI confidence below 6")
        self.assertEqual(digest._reason("Needs a human: flagged unsafe"), "unsafe")
        self.assertEqual(digest._reason("Stale: waited over 60 min to render"), "stale in queue")


if __name__ == "__main__":
    unittest.main()
