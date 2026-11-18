import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipper import config as config_mod, notify, submit_tracker as st
from clipper.db import DB

LACY = {"id": "lacy-gta", "name": "$12,500 LACY GTA VI Marathon Pool (Reach)", "url": "https://contentrewards.com/c/campaigns/x"}
CLIX = {"id": "clix-gta", "name": "$5,000 Clix GTA VI Marathon Pool (Reach)", "url": "https://contentrewards.com/c/campaigns/y"}


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {"posting": {"timezone": "Asia/Kolkata"}})
        self.db = DB(Path(tempfile.mkdtemp()) / "t.db")
        self.calls = []
        env = mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"})
        env.start()
        self.addCleanup(env.stop)
        fake = mock.patch.object(notify, "_call", side_effect=self.call)
        fake.start()
        self.addCleanup(fake.stop)
        self.now = time.time()

    def call(self, method, params, timeout=15, token=None):
        self.calls.append((method, params))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 900 + len(self.calls), "chat": {"id": 42}}}
        return {"ok": True}

    def post(self):
        posted = [{"campaign": LACY, "platform": "youtube", "url": "https://youtube.com/shorts/a", "clip_id": 1, "hook": ""},
                  {"campaign": LACY, "platform": "instagram", "url": "https://instagram.com/reel/a", "clip_id": 1, "hook": ""},
                  {"campaign": CLIX, "platform": "instagram", "url": "https://instagram.com/reel/b", "clip_id": 2, "hook": ""}]
        self.assertTrue(st.send_batch(self.cfg, self.db, posted, [], now=self.now))
        method, params = self.calls[-1]
        return json.loads(params["reply_markup"])["inline_keyboard"], params

    def tap(self, sid, chat=42, at=None):
        msg_id = self.db.one("SELECT message_id FROM submissions WHERE id = ?", (sid,))["message_id"]
        return st.handle_update(self.cfg, self.db, {"callback_query": {
            "id": "cq", "data": f"sub:{sid}", "message": {"message_id": msg_id, "chat": {"id": chat}}}}, now=at)

    def test_one_button_per_campaign_with_the_links(self):
        kb, params = self.post()
        self.assertEqual([row[0]["text"] for row in kb],
                         ["✅ Submitted: LACY GTA VI Marathon Pool", "✅ Submitted: Clix GTA VI Marathon Pool"])
        self.assertIn("https://youtube.com/shorts/a", params["text"])
        self.assertIn("Submit here: https://contentrewards.com/c/campaigns/x", params["text"])
        rows = [dict(r) for r in self.db.query("SELECT * FROM submissions ORDER BY id")]
        self.assertEqual(len(json.loads(rows[0]["urls"])), 2)
        self.assertEqual(rows[0]["deadline"] - rows[0]["sent_at"], 30 * 60)

    def test_tap_marks_submitted_and_redraws(self):
        self.post()
        self.assertIn("marked submitted", self.tap(1))
        self.assertIsNotNone(self.db.one("SELECT submitted_at FROM submissions WHERE id = 1")["submitted_at"])
        edit = [p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]
        labels = [r[0]["text"] for r in json.loads(edit["reply_markup"])["inline_keyboard"]]
        self.assertTrue(labels[0].startswith("✔ LACY GTA VI Marathon Pool: submitted"))
        self.assertTrue(labels[1].startswith("✅ Submitted: Clix"))

    def test_taps_from_other_chats_are_ignored(self):
        self.post()
        self.assertIn("ignored", self.tap(1, chat=999))
        self.assertIsNone(self.db.one("SELECT submitted_at FROM submissions WHERE id = 1")["submitted_at"])

    def test_reminds_once_at_20_minutes_only_if_not_submitted(self):
        self.post()
        self.tap(1)  # LACY done
        self.assertEqual(st.remind_due(self.cfg, self.db, now=self.now + 10 * 60), 0)  # too early
        self.assertEqual(st.remind_due(self.cfg, self.db, now=self.now + 20 * 60 + 1), 1)  # only Clix
        reminder = [p for m, p in self.calls if m == "sendMessage"][-1]
        self.assertIn("Clix GTA VI Marathon Pool: 10 min left", reminder["text"])
        self.assertIn("reply_to_message_id", reminder)
        self.assertEqual(st.remind_due(self.cfg, self.db, now=self.now + 25 * 60), 0)  # once
        self.assertEqual(st.remind_due(self.cfg, self.db, now=self.now + 31 * 60), 0)  # window closed

    def test_no_campaign_means_no_button(self):
        posted = [{"campaign": None, "platform": "youtube", "url": "u", "clip_id": 3, "hook": ""}]
        st.send_batch(self.cfg, self.db, posted, [], now=self.now)
        self.assertNotIn("reply_markup", self.calls[-1][1])


if __name__ == "__main__":
    unittest.main()
