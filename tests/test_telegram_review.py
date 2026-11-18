import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipper import config as config_mod, telegram_review as tr
from clipper.db import DB


class ReviewBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": self.tmp},
            "posting": {"quiet_hours": [], "timezone": "UTC",
                        "earliest_footage": "2026-11-18T11:00:00+00:00"},
            "auto_approve": {"enabled": True, "veto_minutes": 10},
            "campaigns": [{"id": "lacy-gta", "name": "LACY pool"}],
            "channels": [{"login": "lacy", "display_name": "LACY", "permission": "campaign:lacy-gta"},
                         {"login": "tester", "permission": "test"}],
        })
        self.db = DB(Path(self.tmp) / "t.db")
        self.db.conn.executescript(tr.SCHEMA)
        self.env = mock.patch.dict(os.environ, {tr.TOKEN_ENV: "t", tr.CHAT_ENV: "42"})
        self.env.start()
        self.calls = []
        self.api = mock.patch.object(tr, "api", side_effect=self.fake_api)
        self.api.start()

    def tearDown(self):
        self.api.stop()
        self.env.stop()

    def fake_api(self, method, params=None, files=None, timeout=60):
        self.calls.append((method, params or {}, bool(files)))
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def clip(self, **kw):
        video = Path(self.tmp) / f"v{time.time_ns()}.mp4"
        video.write_bytes(b"mp4")
        now = time.time()
        fields = dict(channel="lacy", status="ready", final_path=str(video), spoiler_risk="none",
                      quality=6, score=3.0, hook="h", caption="c #LACY", wall_start=now - 60,
                      ai_notes=json.dumps({"why": "funny", "unsafe": False}),
                      auto_checked_at=now + 1, created_at=now, updated_at=now, duration_s=30)
        fields.update(kw)
        return self.db.insert("clips", **fields)

    def test_multipart(self):
        body, ctype = tr.multipart({"chat_id": "42"}, {"video": ("a.mp4", b"BYTES", "video/mp4")})
        boundary = ctype.split("boundary=")[1]
        self.assertIn(f"--{boundary}".encode(), body)
        self.assertIn(b'name="chat_id"\r\n\r\n42', body)
        self.assertIn(b'filename="a.mp4"\r\nContent-Type: video/mp4\r\n\r\nBYTES', body)

    def test_only_reviewable_clips_are_sent(self):
        good = self.clip(quality=7)
        self.clip(channel="tester")                                  # test footage
        self.clip(spoiler_risk="high")                               # spoiler
        self.clip(ai_notes=json.dumps({"unsafe": True}))             # unsafe
        self.clip(wall_start=1794999600.0 - 60)                      # before release
        self.clip(auto_checked_at=None)                              # auto-approver hasn't looked yet
        ids = [c["id"] for c in tr.pending_review(self.db, self.cfg, 10)]
        self.assertEqual(ids, [good])
        tr.push(self.db, self.cfg)
        self.assertEqual([c[0] for c in self.calls], ["sendVideo"])
        self.assertIn("✅ Approve", json.dumps(self.calls[0][1]["reply_markup"], ensure_ascii=False))
        self.assertEqual(tr.pending_review(self.db, self.cfg, 10), [])  # not sent twice

    def test_buttons(self):
        a, b, spoiler = self.clip(), self.clip(), self.clip(spoiler_risk="high")
        self.assertTrue(tr.decide(self.db, self.cfg, "a", a).startswith("✅"))
        row = self.db.one("SELECT status, decided_by FROM clips WHERE id = ?", (a,))
        self.assertEqual((row["status"], row["decided_by"]), ("approved", "human"))
        self.assertTrue(tr.decide(self.db, self.cfg, "r", b).startswith("❌"))
        self.assertIn("spoiler", tr.decide(self.db, self.cfg, "a", spoiler))
        # undo works until something is posted
        self.assertTrue(tr.decide(self.db, self.cfg, "u", a).startswith("↩️"))
        tr.decide(self.db, self.cfg, "a", a)
        self.db.insert("posts", clip_id=a, platform="youtube", status="published", posted_at=time.time())
        self.assertIn("Too late", tr.decide(self.db, self.cfg, "u", a))

    def test_callbacks_only_from_your_chat(self):
        cid = self.clip()
        stranger = {"callback_query": {"id": "1", "data": f"a:{cid}", "message": {"chat": {"id": 999}}}}
        tr.handle_update(self.db, self.cfg, stranger)
        self.assertEqual(self.db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"], "ready")
        mine = {"callback_query": {"id": "2", "data": f"a:{cid}",
                                   "message": {"chat": {"id": 42}, "message_id": 7, "caption": "#1 LACY"}}}
        tr.handle_update(self.db, self.cfg, mine)
        self.assertEqual(self.db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"], "approved")
        methods = [c[0] for c in self.calls]
        self.assertIn("editMessageCaption", methods)
        edit = [c for c in self.calls if c[0] == "editMessageCaption"][0][1]
        self.assertIn("Approved", edit["caption"])
        self.assertEqual(edit["reply_markup"], {"inline_keyboard": []})

    def test_auto_approved_get_undo_and_caps_apply(self):
        self.clip(status="approved", decided_by="auto", decided_at=time.time())
        for _ in range(20):
            self.clip()
        self.cfg["review_bot"]["max_per_hour"] = 4
        tr.push(self.db, self.cfg)
        kinds = [json.dumps(c[1]["reply_markup"], ensure_ascii=False) for c in self.calls]
        self.assertTrue(any("Undo" in k for k in kinds))
        self.assertEqual(sum("Approve" in k for k in kinds), 3)  # per_push
        tr.push(self.db, self.cfg)
        self.assertEqual(sum("Approve" in json.dumps(c[1]["reply_markup"], ensure_ascii=False)
                             for c in self.calls), 4)  # hourly cap
        self.cfg["posting"]["quiet_hours"] = list(range(24))
        before = len(self.calls)
        tr.push(self.db, self.cfg)
        self.assertEqual(len(self.calls), before)  # quiet hours: nothing new
        tr.push(self.db, self.cfg, force=True, limit=2)  # /next overrides
        self.assertEqual(len(self.calls), before + 2)


if __name__ == "__main__":
    unittest.main()
