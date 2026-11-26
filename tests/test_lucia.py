import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipper import lucia
from clipper.db import DB

HTML = ('<a href="https://drive.google.com/file/d/AAA/view?usp=drive_web"><div class="flip-entry-title">one.mp4</div></a>'
        '<a href="https://drive.google.com/file/d/BBB/view?usp=drive_web"><div class="flip-entry-title">notes.png</div></a>'
        '<a href="https://drive.google.com/file/d/C-c_1/view?usp=drive_web"><div class="flip-entry-title">two.mp4</div></a>')


class LuciaTests(unittest.TestCase):
    def test_lists_only_the_folders_videos(self):
        self.assertEqual(lucia.list_folder(HTML), [("AAA", "one.mp4"), ("C-c_1", "two.mp4")])

    def test_picks_the_least_used_clips(self):
        pair = lucia.pick_pair(["a", "b", "c"], {"a": 3, "b": 1}, random.Random(1))
        self.assertEqual(set(pair), {"b", "c"})

    def test_every_hook_carries_the_ai_line(self):
        for opener, reveal, cta in lucia.HOOKS:
            self.assertIn("AI", reveal)  # says she's AI before the end card
            self.assertIn("in seconds", cta)

    def test_top_up_builds_only_the_missing_videos_under_the_campaign(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d) / "t.db")
            db.insert("clips", channel="ai_lucia", campaign_id="lucia", status="ready", created_at=0, updated_at=0)
            cfg = {"campaigns": [{"id": "lucia", "active": True, "keep_ready": 3}]}
            with mock.patch.object(lucia, "make", return_value=[7, 8]) as make:
                self.assertEqual(lucia.top_up(db, cfg), [7, 8])
            make.assert_called_once_with(db, cfg, 2)
            cfg["campaigns"][0]["active"] = False
            self.assertEqual(lucia.top_up(db, cfg), [])
            db.conn.close()


if __name__ == "__main__":
    unittest.main()
