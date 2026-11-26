"""Instagram reach fixes (26 Nov): posting window, quality bar, no filler packaging, caption question, trial reels."""
import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from clipper import guardrails, metadata, scheduler
from clipper.publishers.base import PostMeta
from clipper.publishers.instagram import instagram_container_params

CFG = {"campaigns": [{"id": "c", "platforms": ["instagram", "youtube"]}],
       "posting": {"instagram_min_quality": 6, "quiet_hours": [h for h in range(24) if not 5 <= h <= 10],
                   "timezone": "Asia/Kolkata"}}
CH = {"login": "x", "permission": "campaign:c"}


def clip(**kw):
    return {"status": "approved", "quality": 7, "hook": "good hook", "ai_notes": "{}", **kw}


class ReachTests(unittest.TestCase):
    def test_instagram_takes_only_clips_over_the_quality_bar(self):
        self.assertTrue(guardrails.can_post(CFG, CH, clip(), "instagram")[0])
        ok, why = guardrails.can_post(CFG, CH, clip(quality=5), "instagram")
        self.assertFalse(ok)
        self.assertIn("quality", why)
        self.assertTrue(guardrails.can_post(CFG, CH, clip(quality=5), "youtube")[0])

    def test_filler_packaging_waits_unless_you_wrote_the_hook(self):
        notes = json.dumps({"packaging_error": "budget", "template_hook": "x in GTA 6"})
        self.assertFalse(guardrails.can_post(CFG, CH, clip(hook="x in GTA 6", ai_notes=notes), "youtube")[0])
        self.assertTrue(guardrails.can_post(CFG, CH, clip(hook="my own hook", ai_notes=notes), "youtube")[0])

    def test_caption_gets_a_question_above_the_credit(self):
        out = metadata.with_question("#ad\nWild moment\nCredit: twitch.tv/x", 0)
        self.assertEqual(out.split("\n")[2], metadata.QUESTIONS[0])
        self.assertTrue(out.endswith("Credit: twitch.tv/x"))
        self.assertEqual(metadata.with_question("did he really?\nCredit: y", 1), "did he really?\nCredit: y")

    def test_trial_reels_only_when_switched_on(self):
        meta = PostMeta(title="t", caption="c")
        self.assertNotIn("trial_params", instagram_container_params(meta, {}))
        p = instagram_container_params(meta, {"trial_reels": True})
        self.assertEqual(json.loads(p["trial_params"]), {"graduation_strategy": "SS_PERFORMANCE"})

    def test_slot_estimates_skip_quiet_hours(self):
        tz = ZoneInfo("Asia/Kolkata")
        night = datetime(2026, 11, 26, 23, 15, tzinfo=tz).timestamp()
        self.assertEqual(datetime.fromtimestamp(scheduler._after_quiet(CFG, night), tz),
                         datetime(2026, 11, 27, 5, 0, tzinfo=tz))
        morning = datetime(2026, 11, 27, 7, 30, tzinfo=tz).timestamp()
        self.assertEqual(scheduler._after_quiet(CFG, morning), morning)


if __name__ == "__main__":
    unittest.main()
