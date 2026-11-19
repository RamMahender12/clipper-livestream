import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from clipper import autoapprove, config as config_mod
from clipper.db import DB

IST = ZoneInfo("Asia/Kolkata")
SAFE = {"source": "claude-haiku-5-5", "gemini": {"quality": 6, "hook_matches": True, "age_risk": "low", "is_gta6": True,
                                                 "streamer_featured": True, "violence": [], "music": "none"}}


def at(hour, minute=0):
    return datetime(2026, 11, 19, hour, minute, tzinfo=IST).timestamp()


class DailyPickTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": tmp},
            "posting": {"timezone": "Asia/Kolkata", "quiet_hours": [0, 1, 2, 3, 4, 5]},
            "campaigns": [{"id": "c1"}],
            "channels": [{"login": "lacy", "permission": "campaign:c1"}, {"login": "tester", "permission": "test"}]})
        self.db = DB(Path(tmp) / "t.db")
        self.video = Path(tmp) / "v.mp4"
        self.video.write_bytes(b"x")

    def clip(self, quality=6, note="AI hold 5/10: meh", notes=None, age_min=30, now=None, channel="lacy", score=5.0):
        now = now or at(12)
        return self.db.insert("clips", channel=channel, status="ready", quality=quality, score=score,
                              spoiler_risk="none", final_path=str(self.video), decision_note=note,
                              ai_notes=json.dumps(notes or SAFE), auto_checked_at=now - 60,
                              created_at=now - age_min * 60, updated_at=now - 60)

    def status(self, cid):
        return self.db.one("SELECT status, decided_by, decision_note FROM clips WHERE id = ?", (cid,))

    def test_picks_the_best_safe_clip_and_spaces_them(self):
        good = self.clip(quality=7)
        ok = self.clip(quality=6)
        lines = autoapprove.daily_picks(self.db, self.cfg, at(12))
        self.assertEqual(len(lines), 1)
        self.assertEqual(self.status(good)["status"], "approved")
        self.assertIn("Daily pick 1/6", self.status(good)["decision_note"])
        self.assertEqual(self.status(ok)["status"], "ready")
        self.assertEqual(autoapprove.daily_picks(self.db, self.cfg, at(12, 30)), [])  # 90 min gap
        self.assertEqual(len(autoapprove.daily_picks(self.db, self.cfg, at(13, 31))), 1)
        self.assertEqual(self.status(ok)["status"], "approved")

    def test_never_unsafe_rejected_stale_weak_or_test(self):
        self.cfg["auto_approve"]["pick_fallback_quality"] = 6  # fallback off: covered below
        violent = self.clip(notes={**SAFE, "gemini": {**SAFE["gemini"], "violence": [[1, 3, "beating"]]}})
        rejected = self.clip(note="AI reject 7/10: boring")
        stale = self.clip(age_min=240)
        weak = self.clip(quality=5)
        test = self.clip(channel="tester")
        self.assertEqual(autoapprove.daily_picks(self.db, self.cfg, at(12)), [])
        for cid in (violent, rejected, stale, weak, test):
            self.assertEqual(self.status(cid)["status"], "ready")

    def test_quota_counts_your_approvals_and_quiet_hours(self):
        self.cfg["auto_approve"]["daily_picks"] = 3
        for i in range(3):
            self.db.insert("clips", channel="lacy", status="posted", decided_by="human", decided_at=at(9 + i),
                           quality=7, score=1, created_at=at(9), updated_at=at(9))
        cid = self.clip(now=at(15))
        self.assertEqual(autoapprove.daily_picks(self.db, self.cfg, at(15)), [])  # 3 already today
        self.db.execute("UPDATE clips SET decided_at = ? WHERE status = 'posted'", (at(9) - 86400,))  # yesterday
        self.assertEqual(autoapprove.daily_picks(self.db, self.cfg, at(3)), [])  # quiet hours
        self.assertEqual(len(autoapprove.daily_picks(self.db, self.cfg, at(15))), 1)
        self.assertEqual(self.status(cid)["status"], "approved")

    def test_fallback_to_a_safe_5_after_two_quiet_hours(self):
        self.db.insert("clips", channel="lacy", status="posted", decided_by="auto", decided_at=at(11), quality=7,
                       score=1, created_at=at(11), updated_at=at(11))
        five = self.clip(quality=5, note="Below the bar, not judged: quality 5 is under the cold-start bar of 6",
                         now=at(12, 30))
        self.db.execute("UPDATE clips SET status = 'rejected', decided_by = 'auto' WHERE id = ?", (five,))
        no_video = self.clip(quality=5, note="AI hold 5/10: ok", notes={"source": "claude-haiku-5-5"}, now=at(12, 30))
        self.assertEqual(autoapprove.daily_picks(self.db, self.cfg, at(12, 30)), [])  # posted 1.5 h ago: wait
        lines = autoapprove.daily_picks(self.db, self.cfg, at(13, 5))  # 2 h without a post, no 6+ waiting
        self.assertEqual(len(lines), 1)
        self.assertEqual(self.status(five)["status"], "approved")
        self.assertIn("nothing at 6+", self.status(five)["decision_note"])
        self.assertEqual(self.status(no_video)["status"], "ready")  # never without a 6+ video check


if __name__ == "__main__":
    unittest.main()
