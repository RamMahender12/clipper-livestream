import json
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from clipper import ai, config as config_mod, stats
from clipper.db import DB

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=IST).timestamp()


class StatsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": tmp},
            "posting": {"timezone": "Asia/Kolkata"},
            "costs": {"server_usd_per_day": 2.0, "ai_unmetered_usd": 4.0, "started": "2026-11-18"},
            "campaigns": [
                {"id": "lacy-gta", "name": "LACY pool", "active": True, "pool_min_views": 750000, "budget_usd": 12500},
                {"id": "jeff", "name": "AsianJeff", "active": True, "rate_per_1k": 1.0, "min_views": 10000,
                 "cap_per_clip": 300, "budget_usd": 2500}],
            "channels": [{"login": "lacy", "display_name": "LACY", "permission": "campaign:lacy-gta"},
                         {"login": "asianjeff", "display_name": "AsianJeff", "permission": "campaign:jeff"}]})
        self.db = DB(Path(tmp) / "clipper.db")
        mk = lambda ch, **kw: self.db.insert("clips", channel=ch, created_at=NOW - 3600, updated_at=NOW, **kw)  # noqa: E731
        c1 = mk("lacy", status="posted", decided_by="human", quality=7, title="Drive-thru getaway")
        c2 = mk("asianjeff", status="posted", decided_by="auto", decision_note="Daily pick 1/6 ...", quality=6)
        c3 = mk("asianjeff", status="posted", decided_by="auto", quality=6)
        for _ in range(3):
            mk("asianjeff", status="rejected", decided_by="auto", decision_note="Weak moment: Claude rated it 3/10")
        mk("lacy", status="ready", decision_note="AI hold 5/10: meh", quality=6, final_path="/x.mp4",
           ai_notes=json.dumps({"gemini": {"quality": 6}}))
        post = lambda c, pl, v, camp: self.db.insert("posts", clip_id=c, platform=pl, account="a", campaign_id=camp,  # noqa: E731
                                                      url=f"https://x/{c}{pl}", status="published", views=v, posted_at=NOW - 2 * 86400)
        self.p_ig = post(c1, "instagram", 2000, "lacy-gta")
        post(c1, "youtube", 10, "lacy-gta")
        post(c2, "instagram", 25000, "jeff")  # pays $25
        post(c3, "instagram", 9000, "jeff")   # under 10K: pays nothing
        stats.record_views(self.db, self.p_ig, 500, now=NOW - 86400 - 60)  # a day ago it had 500
        use = lambda i, o: types.SimpleNamespace(input_tokens=i, output_tokens=o)  # noqa: E731
        ai.set_channel("asianjeff")
        ai.record(self.cfg, "video_check", "claude-sonnet-5-5", use(1_000_000, 0))  # $2.00
        ai.set_channel("")
        ai.record(self.cfg, "packaging", "claude-sonnet-5-5", use(500_000, 0))   # $1.00, nobody's
        ai.set_channel("")

    def test_numbers(self):
        s = stats.build(self.db, self.cfg, NOW)
        json.dumps(s)  # the page gets it as JSON
        o = s["overview"]
        self.assertEqual((o["posts"], o["views"]), (4, 36010))
        self.assertEqual(o["earned"], 25.0)
        # server 2.5 days (18 Nov 00:00 -> 20 Nov 12:00) x $2 + $4 unmetered + $3 metered
        self.assertAlmostEqual(o["spend"], 5.0 + 4.0 + 3.0, places=1)
        camps = {c["id"]: c for c in s["campaigns"]}
        lacy, jeff = camps["lacy-gta"], camps["jeff"]
        self.assertEqual((lacy["kind"], lacy["views"], lacy["views_to_threshold"]), ("pool", 2010, 747990))
        self.assertEqual(lacy["gained_24h"], 1500)  # 2000 now vs 500 a day ago (+ the YouTube post's 10 had no history)
        self.assertEqual((jeff["kind"], jeff["earned"], jeff["paying_posts"]), ("per view", 25.0, 1))
        # spend: AsianJeff made 5 of 7 clips -> 5/7 of the shared $10 (server 5 + unmetered 4 + unattributed 1) + its $2
        spend = s["spend_by_streamer"]
        self.assertAlmostEqual(spend["AsianJeff"]["ai_metered"], 2.0, places=2)
        self.assertAlmostEqual(spend["AsianJeff"]["total"], 2.0 + 10.0 * 5 / 7, places=1)
        self.assertAlmostEqual(spend["LACY"]["total"], 10.0 * 2 / 7, places=1)
        need = jeff["spend"] - 25.0
        if need > 0:
            self.assertEqual(jeff["breakeven"]["views_needed"], -(-int(need * 1000) // 1))
        else:
            self.assertEqual(jeff["breakeven"]["usd_needed"], 0)
        self.assertAlmostEqual(lacy["breakeven"]["pool_share_needed"], lacy["spend"] / 12500, places=4)
        f = {x["streamer"]: x for x in s["funnel"]}
        self.assertEqual((f["AsianJeff"]["made"], f["AsianJeff"]["posted"], f["AsianJeff"]["picks"]), (5, 2, 1))
        self.assertEqual(f["LACY"]["waiting_for_you"], 1)
        self.assertEqual(s["top_posts"][0]["views"], 25000)
        self.assertEqual(s["top_posts"][0]["source"], "daily pick")
        self.assertEqual(len(s["views_series"]), 7)
        self.assertEqual(s["views_series"][-1]["instagram"], 36000)  # today: current views
        self.assertEqual(s["views_series"][-2]["instagram"], 500)  # yesterday's end: from the history
        self.assertIsNone(s["views_series"][0]["instagram"])  # before history started: unknown

    def test_spend_is_tagged_with_the_streamer(self):
        conn = ai._db(self.cfg)
        try:
            rows = conn.execute("SELECT channel, cost_usd FROM ai_usage ORDER BY ts").fetchall()
        finally:
            conn.close()
        self.assertEqual([r[0] for r in rows], ["asianjeff", ""])

    def test_page_and_route_names(self):
        from clipper.stats_page import STATS_PAGE
        self.assertIn('fetch("/api/stats")', STATS_PAGE)
        self.assertIn("Breakeven", STATS_PAGE)


if __name__ == "__main__":
    unittest.main()
