import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from clipper import config as config_mod, twitch_clips as tc
from clipper.db import DB


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeHelix:
    def __init__(self, clips):
        self._clips = clips
        self.asked = []

    def broadcaster_id(self, login):
        return {"lacy": "100", "tester": "200"}.get(login)

    def clips(self, bid, since, first=50):
        self.asked.append(bid)
        return sorted(self._clips.get(bid, []), key=lambda c: c["view_count"], reverse=True)


class HarvestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": self.tmp},
            "posting": {"earliest_footage": "2026-11-18T11:00:00+00:00"},
            "twitch_clips": {"per_poll": 2, "min_views": 10},
            "campaigns": [{"id": "lacy-gta"}],
            "channels": [{"login": "lacy", "permission": "campaign:lacy-gta"},
                         {"login": "tester", "permission": "test"}],
        })
        self.db = DB(Path(self.tmp) / "t.db")
        self.fetched = []
        self.now = time.time()

    def fetch(self, clip, out):
        self.fetched.append(clip["id"])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return out

    def clip(self, cid, views, age_s=600, title="LACY gets bitten"):
        return {"id": cid, "url": f"https://clips.twitch.tv/{cid}", "view_count": views, "title": title,
                "duration": 30.0, "created_at": iso(self.now - age_s)}

    def test_picks_top_viewed_new_clips(self):
        helix = FakeHelix({"100": [self.clip("a", 500), self.clip("b", 50, age_s=1200),
                                   self.clip("c", 30, age_s=2400), self.clip("d", 5, age_s=3600)],
                           "200": [self.clip("t", 999)]})
        lines = tc.harvest_once(self.db, self.cfg, helix, fetch=self.fetch)
        self.assertEqual(self.fetched, ["a", "b"])  # per_poll = 2, sorted by views
        self.assertEqual(helix.asked, ["100"])  # test channel never queried
        rows = [dict(r) for r in self.db.query("SELECT * FROM clips ORDER BY id")]
        self.assertEqual(len(rows), 2)
        self.assertIn('"LACY gets bitten" (500 views', rows[0]["source_note"])
        self.assertIn("#1 of the 4 clips", rows[0]["source_note"])
        self.assertEqual((rows[0]["source_views"], rows[0]["source_pct"]), (500, None))  # 4 < min_pool
        self.assertGreater(rows[0]["score"], rows[1]["score"])
        self.assertEqual(rows[0]["campaign_id"], "lacy-gta")
        self.assertTrue(any("queued clip" in l for l in lines))
        # next poll: a and b are seen, c qualifies, d is under min_views
        tc.harvest_once(self.db, self.cfg, helix, fetch=self.fetch)
        self.assertEqual(self.fetched, ["a", "b", "c"])

    def test_skips_overlap_prerelease_and_retries_failed_downloads(self):
        start = self.now - 600 - 30
        self.db.insert("clips", channel="lacy", status="ready", wall_start=start + 5,
                       created_at=self.now, updated_at=self.now)
        old = {"id": "old", "url": "u", "view_count": 900, "title": "early", "duration": 30.0,
               "created_at": "2026-11-18T10:00:00Z"}
        helix = FakeHelix({"100": [self.clip("dup", 400), old, self.clip("flaky", 100, age_s=3000)]})

        def failing(clip, out):
            raise RuntimeError("network")

        lines = tc.harvest_once(self.db, self.cfg, helix, fetch=failing)
        self.assertTrue(any("overlaps our clip" in l for l in lines))
        seen = {r["id"]: r["note"] for r in self.db.query("SELECT id, note FROM twitch_clips_seen")}
        self.assertEqual(seen.get("old"), "pre-release")
        self.assertNotIn("flaky", seen)  # failed download is retried next time
        tc.harvest_once(self.db, self.cfg, helix, fetch=self.fetch)
        self.assertEqual(self.fetched, ["flaky"])

    def test_ai_rejected_overlap_gives_viewer_clip_a_turn(self):
        start = self.now - 600 - 30
        mine = self.db.insert("clips", channel="lacy", status="ready", wall_start=start,
                              created_at=self.now, updated_at=self.now)
        human = self.db.insert("clips", channel="lacy", status="rejected", decided_by="human",
                               wall_start=start - 3000, created_at=self.now, updated_at=self.now)
        helix = FakeHelix({"100": [self.clip("v", 1500), self.clip("h", 900, age_s=3600)]})
        tc.harvest_once(self.db, self.cfg, helix, fetch=self.fetch)
        self.assertEqual(self.fetched, [])  # ours is still alive; the one you rejected stays rejected
        # the AI later rejects our cut of that moment: the viewers' cut gets its turn next poll
        self.db.update("clips", mine, status="rejected", decided_by="auto")
        lines = tc.harvest_once(self.db, self.cfg, helix, fetch=self.fetch)
        self.assertEqual(self.fetched, ["v"])
        self.assertTrue(any("queued clip" in l for l in lines))
        tc.harvest_once(self.db, self.cfg, helix, fetch=self.fetch)
        self.assertEqual(self.fetched, ["v"])  # not twice
        self.assertIsNotNone(human)


class EvidenceTests(unittest.TestCase):
    cfg = config_mod._merge(config_mod.DEFAULTS, {})

    def test_rank_and_strength(self):
        pool = [1616, 785, 620, 606, 466, 300, 120, 80, 40, 12]
        self.assertEqual(tc.rank_in(1616, pool), (1, 1.0))
        self.assertEqual(tc.rank_in(466, pool), (5, 0.556))
        self.assertEqual(tc.rank_in(50, [50, 10]), (1, None))  # too few to rank
        ev = lambda views, pct: tc.evidence(self.cfg, {"source_views": views, "source_pct": pct})
        self.assertEqual(ev(1616, 1.0), "strong")
        self.assertEqual(ev(40, 1.0), "neutral")  # top of a quiet window, but few views
        self.assertEqual(ev(466, 0.556), "neutral")
        self.assertEqual(ev(80, 0.2), "weak")
        self.assertEqual(ev(500, None), "neutral")
        self.assertIsNone(tc.evidence(self.cfg, {"source_views": 0}))  # our own chat-spike clip
        self.assertEqual(tc.evidence_text({"source_views": 1616, "source_pct": 0.97}),
                         "1,616 views on Twitch, top 3% of recent viewer clips")


if __name__ == "__main__":
    unittest.main()
