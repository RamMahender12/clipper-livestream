import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipper import ai, claude_vision as cv, config as config_mod, gemini, pipeline
from clipper.db import DB

ANSWER = {"what_happens": "plane hits a bird", "visual_quality": 7, "spoiler_risk": "none", "is_cutscene": False,
          "unsafe": False, "hook_matches": True, "better_hook": "", "age_restriction_risk": "low",
          "age_restriction_reason": "", "is_gta6_gameplay": True, "streamer_featured": True, "reason": "funny"}


class ClaudeVisionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {"gemini": {"backend": "claude", "frames": 4}})

    def test_schema_and_frames(self):
        s = cv.schema()
        self.assertNotIn("minimum", s["properties"]["visual_quality"])
        self.assertFalse(s["additionalProperties"])
        self.assertIn("minimum", gemini.SCHEMA["properties"]["visual_quality"])  # Gemini's left alone
        times = cv.frame_times(20.0, 4)
        self.assertEqual(len(times), 4)
        self.assertLess(times[0], 3.0)  # a frame inside the hook
        self.assertLess(times[-1], 20.0)
        content = cv.build_content([(0.8, b"jpg1"), (6.0, b"jpg2")], "questions")
        self.assertEqual([c["type"] for c in content], ["text", "image", "text", "image", "text"])
        self.assertEqual(content[1]["source"]["media_type"], "image/jpeg")
        self.assertEqual(content[-1]["text"], "questions")

    def test_review_shape_and_failure(self):
        seen = {}

        def call(cfg, system, user, schema, effort=None, model=None, max_tokens=16000):
            seen.update(model=model, effort=effort, n_images=sum(1 for c in user if c["type"] == "image"),
                        prompt=user[-1]["text"])
            return dict(ANSWER), "claude-sonnet-5-5"

        with mock.patch.object(cv, "sample_frames", return_value=[(0.8, b"a"), (5.0, b"b")]), \
                mock.patch.object(ai, "call_json", side_effect=call), \
                mock.patch.object(ai, "have_credentials", return_value=True):
            self.assertTrue(gemini.configured(self.cfg))
            out = gemini.review(self.cfg, Path("/x.mp4"), streamer="LACY", hook="His first death? A bird.",
                                transcript="did I just die to a bird", chat_terms=["LOL"])
        self.assertEqual((seen["model"], seen["n_images"]), ("claude-sonnet-5-5", 2))
        self.assertIn("age_restriction_risk", seen["prompt"])
        self.assertIn("A bird.", seen["prompt"])
        self.assertEqual((out["quality"], out["age_risk"], out["is_gta6"], out["model"]), (7, "low", True, "claude-sonnet-5-5"))
        self.assertEqual(gemini.label(out), "Video check")
        self.assertEqual(gemini.label({"model": "gemini-3.8-flash"}), "Gemini")
        with mock.patch.object(cv, "sample_frames", return_value=[(0.8, b"a")]), \
                mock.patch.object(ai, "call_json", return_value=(None, "refusal")):
            with self.assertRaises(gemini.GeminiError):
                gemini.review(self.cfg, Path("/x.mp4"), streamer="s", hook="h", transcript="", chat_terms=[])


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {"gemini": {"max_per_hour": 3, "fail_limit": 2}})
        self.b = gemini.Breaker()

    def test_policy_rejection_pauses_and_alerts_once(self):
        with mock.patch("clipper.notify.send") as send:
            self.assertTrue(self.b.allow(self.cfg)[0])
            self.b.fail(self.cfg, gemini.GeminiError("Vertex AI HTTP 403: 'Request rejected in accordance with our terms of use'"))
            ok, why = self.b.allow(self.cfg)
        self.assertFalse(ok)
        self.assertIn("paused until", why)
        self.assertEqual(send.call_count, 1)
        self.assertGreater(self.b.until, time.time() + 5 * 3600)

    def test_repeated_failures_then_recovery_and_hourly_cap(self):
        with mock.patch("clipper.notify.send"):
            self.b.fail(self.cfg, gemini.GeminiError("timed out"))
            self.assertTrue(self.b.allow(self.cfg)[0])  # one blip doesn't pause
            self.b.ok()
            self.b.fail(self.cfg, gemini.GeminiError("timed out"))
            self.assertTrue(self.b.allow(self.cfg)[0])  # the success in between reset the count
            self.b.fail(self.cfg, gemini.GeminiError("timed out"))
            self.assertFalse(self.b.allow(self.cfg)[0])  # two in a row
        self.b.reset()
        self.assertEqual([self.b.allow(self.cfg)[0] for _ in range(4)], [True, True, True, False])


class PipelineGuardTests(unittest.TestCase):
    def test_flagged_clips_are_never_uploaded_and_open_breaker_skips(self):
        tmp = tempfile.mkdtemp()
        cfg = config_mod._merge(config_mod.DEFAULTS, {"paths": {"data_dir": tmp},
                                                      "channels": [{"login": "lacy", "permission": "own"}]})
        db = DB(Path(tmp) / "t.db")
        video = Path(tmp) / "final.mp4"
        video.write_bytes(b"x")
        now = time.time()
        cid = db.insert("clips", channel="lacy", status="rendering", final_path=str(video), quality=5,
                        ai_notes=json.dumps({"claude_quality": 5, "claude_unsafe": True}),
                        created_at=now, updated_at=now)
        gemini.BREAKER.reset()
        with mock.patch.object(gemini, "configured", return_value=True), \
                mock.patch.object(gemini, "review") as review:
            self.assertFalse(pipeline.gemini_review(db, cfg, cid))
            review.assert_not_called()
            notes = json.loads(db.one("SELECT ai_notes FROM clips WHERE id = ?", (cid,))["ai_notes"])
            self.assertIn("unsafe", notes["gemini_skipped"])
            db.update("clips", cid, ai_notes=json.dumps({"claude_quality": 5, "claude_unsafe": False}))
            gemini.BREAKER.until, gemini.BREAKER.why = time.time() + 60, "HTTP 403"
            self.assertFalse(pipeline.gemini_review(db, cfg, cid))
            review.assert_not_called()
        gemini.BREAKER.reset()


if __name__ == "__main__":
    unittest.main()
