import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipper import config as config_mod, pipeline, telegram_review
from clipper.db import DB


class WindowTests(unittest.TestCase):
    def test_longest_clean_stretch(self):
        self.assertEqual(pipeline.clean_window(20, [[5, 8, "x"]], 1), (9.0, 20))
        self.assertEqual(pipeline.clean_window(20, [[12, 14, "x"]], 1), (0.0, 11.0))
        self.assertEqual(pipeline.clean_window(30, [[4, 5, "x"], [5.5, 7, "y"], [20, 21, "z"]], 1), (8.0, 19.0))
        self.assertEqual(pipeline.clean_window(10, [[0, 10, "x"]], 1), (0.0, 0.0))


class CutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": str(self.tmp)}, "channels": [{"login": "clix", "permission": "own"}]})
        self.db = DB(self.tmp / "t.db")
        (self.tmp / "src.mp4").write_bytes(b"src")
        self.video = self.tmp / "final.mp4"
        self.video.write_bytes(b"uncut")
        words = [{"start": 1.0, "end": 1.4, "text": "robbing"}, {"start": 12.0, "end": 12.5, "text": "home"}]
        gem = {"quality": 6, "violence": [[3.0, 8.0, "NPCs beat him up"]]}
        now = time.time()
        self.cid = self.db.insert("clips", channel="clix", status="rendering", source=str(self.tmp / "src.mp4"),
                                  start_s=100.0, duration_s=19.0, final_path=str(self.video), quality=6, hook="h",
                                  words_json=json.dumps(words), transcript="robbing home",
                                  ai_notes=json.dumps({"gemini": gem}), created_at=now, updated_at=now)
        self.renders, self.reviews = [], []

    def render(self, db, cfg, clip_id, ch, src, start, dur, words, hook):
        self.renders.append((start, dur, [(w.start, w.text) for w in words]))
        self.video.write_bytes(b"cut")
        return self.video

    def review(self, db, cfg, clip_id):
        self.reviews.append(clip_id)
        row = db.one("SELECT ai_notes FROM clips WHERE id = ?", (clip_id,))
        notes = json.loads(row["ai_notes"])
        notes["gemini"] = {"quality": 6, "violence": []}
        db.update("clips", clip_id, ai_notes=json.dumps(notes))
        return True

    def test_cuts_to_the_clean_stretch_once(self):
        with mock.patch.object(pipeline, "render_final", side_effect=self.render), \
                mock.patch.object(pipeline, "gemini_review", side_effect=self.review):
            self.assertEqual(pipeline.cut_violence(self.db, self.cfg, self.cid), "cut")
            self.assertEqual(pipeline.cut_violence(self.db, self.cfg, self.cid), "")  # clean now / once
        row = dict(self.db.one("SELECT * FROM clips WHERE id = ?", (self.cid,)))
        self.assertEqual((row["start_s"], row["duration_s"]), (109.5, 9.5))  # kept 9.5-19s of the clip
        self.assertEqual(self.renders, [(109.5, 9.5, [(2.5, "home")])])  # words shifted, cut ones gone
        self.assertEqual(row["transcript"], "home")
        notes = json.loads(row["ai_notes"])
        self.assertEqual(notes["violence_cut"]["kept"], [9.5, 19.0])
        self.assertEqual(self.reviews, [self.cid])  # checked again after cutting
        self.assertFalse((self.tmp / "final.uncut.mp4").exists())

    def test_mostly_violence_and_your_clips(self):
        notes = {"gemini": {"quality": 6, "violence": [[1.0, 17.0, "long fight"]]}}
        self.db.update("clips", self.cid, ai_notes=json.dumps(notes), decided_by="human")
        self.assertEqual(pipeline.cut_violence(self.db, self.cfg, self.cid), "")  # you decided: untouched
        self.db.update("clips", self.cid, decided_by="")
        self.assertEqual(pipeline.cut_violence(self.db, self.cfg, self.cid), "mostly")
        self.assertTrue(json.loads(self.db.one("SELECT ai_notes FROM clips WHERE id = ?", (self.cid,))["ai_notes"])["violence_mostly"])

    def test_failed_render_keeps_the_original(self):
        with mock.patch.object(pipeline, "render_final", side_effect=RuntimeError("ffmpeg")):
            self.assertEqual(pipeline.cut_violence(self.db, self.cfg, self.cid), "")
        row = dict(self.db.one("SELECT * FROM clips WHERE id = ?", (self.cid,)))
        self.assertEqual((row["start_s"], row["duration_s"], row["transcript"]), (100.0, 19.0, "robbing home"))
        self.assertEqual(self.video.read_bytes(), b"uncut")
        self.assertNotIn("violence_cut", json.loads(row["ai_notes"]))


class WeakMomentTests(unittest.TestCase):
    def test_weak_chat_moments_are_not_rendered_but_viewer_clips_are(self):
        from clipper import metadata
        tmp = Path(tempfile.mkdtemp())
        (tmp / "seg.ts").write_bytes(b"x")
        cfg = config_mod._merge(config_mod.DEFAULTS, {"paths": {"data_dir": str(tmp)},
                                                      "channels": [{"login": "lacy", "permission": "own"}]})
        db = DB(tmp / "t.db")
        now = time.time()
        common = dict(channel="lacy", status="rendering", source=str(tmp / "seg.ts"), start_s=0, duration_s=20,
                      words_json="[]", score=3.0, created_at=now, updated_at=now)
        chat = db.insert("clips", spike_id=7, **common)
        viewer = db.insert("clips", **common)
        weak = metadata.ClipMeta(hook="h", title="t", caption="c", quality=4, source="claude-opus-5-5")
        with mock.patch.object(metadata, "generate", return_value=weak), \
                mock.patch.object(pipeline, "render_final") as render, \
                mock.patch.object(pipeline, "gemini_review"), mock.patch.object(pipeline, "cut_violence", return_value=""), \
                mock.patch.object(pipeline, "swap_hook"):
            pipeline.process_clip(db, cfg, dict(db.one("SELECT * FROM clips WHERE id = ?", (chat,))))
            render.assert_not_called()
            pipeline.process_clip(db, cfg, dict(db.one("SELECT * FROM clips WHERE id = ?", (viewer,))))
            self.assertEqual(render.call_count, 1)  # viewer clips always get the full check
        row = dict(db.one("SELECT * FROM clips WHERE id = ?", (chat,)))
        self.assertEqual((row["status"], row["decided_by"]), ("rejected", "auto"))
        self.assertIn("rated it 4/10", row["decision_note"])
        self.assertEqual(db.one("SELECT status FROM clips WHERE id = ?", (viewer,))["status"], "ready")


class PhoneFilterTests(unittest.TestCase):
    def test_only_good_clips_reach_the_phone(self):
        tmp = Path(tempfile.mkdtemp())
        video = tmp / "f.mp4"
        video.write_bytes(b"x")
        cfg = config_mod._merge(config_mod.DEFAULTS, {"channels": [{"login": "lacy", "permission": "own"}],
                                                      "posting": {"earliest_footage": ""}})
        base = {"channel": "lacy", "final_path": str(video), "spoiler_risk": "none", "quality": 6,
                "wall_start": time.time(), "ai_notes": json.dumps({"gemini": {"age_risk": "low", "is_gta6": True}})}
        ok = lambda **kw: telegram_review.reviewable(cfg, {**base, **kw})
        self.assertTrue(ok())
        self.assertFalse(ok(quality=5))  # below the bar: dashboard only
        self.assertFalse(ok(ai_notes=json.dumps({"gemini": {"age_risk": "high"}})))
        self.assertFalse(ok(ai_notes=json.dumps({"gemini": {"is_gta6": False}})))
        self.assertFalse(ok(ai_notes=json.dumps({"violence_mostly": True})))
        cfg["review_bot"]["min_quality"] = 5
        self.assertTrue(ok(quality=5))


if __name__ == "__main__":
    unittest.main()
