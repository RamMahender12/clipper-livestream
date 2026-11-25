import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from clipper import autoapprove, config as config_mod, guardrails, review_server, scheduler
from clipper.autoapprove import Estimate, PastClip
from clipper.db import DB

AUTO = dict(config_mod.DEFAULTS["auto_approve"], enabled=True)


def make_cfg(tmp: str) -> dict:
    cfg = config_mod._merge(config_mod.DEFAULTS, {
        "paths": {"data_dir": tmp},
        "auto_approve": {"enabled": True, "veto_minutes": 10},
        "gemini": {"enabled": False},  # Claude-only behaviour; the Gemini gates are tested below
        "posting": {"platforms": ["export"]},
        "campaigns": [{"id": "c1", "platforms": ["youtube"], "rate_per_1k": 1.0}],
        "channels": [
            {"login": "streamer", "permission": "campaign:c1"},
            {"login": "tester", "permission": "test"},
        ],
    })
    return cfg


def add_clip(db: DB, tmp: str, **kw) -> int:
    video = Path(tmp) / f"v{time.time_ns()}.mp4"
    video.write_bytes(b"fake")
    now = time.time()
    fields = dict(channel="streamer", source="", start_s=0, duration_s=30, score=5.0,
                  status="ready", final_path=str(video), quality=8, spoiler_risk="none",
                  hook="no way", title="t", caption="c",
                  ai_notes=json.dumps({"source": "claude-opus-5-5", "why": "funny", "unsafe": False}),
                  created_at=now, updated_at=now)
    fields.update(kw)
    return db.insert("clips", **fields)


class MathTests(unittest.TestCase):
    def test_percentile_ranks(self):
        self.assertEqual(autoapprove.percentile_ranks([10]), [0.5])
        r = autoapprove.percentile_ranks([1, 2, 3, 4])
        self.assertEqual(r, [0.125, 0.375, 0.625, 0.875])
        self.assertEqual(autoapprove.percentile_ranks([5, 5]), [0.5, 0.5])

    def test_estimate_starts_average_and_learns(self):
        clip = {"channel": "a", "quality": 9, "score": 3.0, "spike_id": 1}
        empty = autoapprove.estimate([], clip, {})
        self.assertAlmostEqual(empty.pct, 0.5)
        hist = [PastClip(i, "a", 9, 3.0, True, 0.9, 10_000, "h", "w", 30) for i in range(10)]
        hist += [PastClip(100 + i, "b", 3, 1.0, True, 0.1, 100, "h", "w", 30) for i in range(10)]
        est = autoapprove.estimate(hist, clip, {"youtube": sorted([100] * 10 + [10_000] * 10)}, ["youtube"])
        self.assertGreater(est.pct, 0.8)
        self.assertEqual(est.expected_views, 10_000)
        weak = autoapprove.estimate(hist, {"channel": "b", "quality": 3, "score": 1.0, "spike_id": 1}, {})
        self.assertLess(weak.pct, 0.25)

    def test_one_lucky_clip_barely_moves_estimate(self):
        hist = [PastClip(1, "a", 7, 2.0, True, 1.0, 1_000_000, "h", "w", 30)]
        est = autoapprove.estimate(hist, {"channel": "a", "quality": 7, "score": 2.0, "spike_id": 1}, {})
        self.assertLess(est.pct, 0.7)  # shrinkage toward average


class DecideTests(unittest.TestCase):
    judge = {"decision": "approve", "confidence": 8, "predicted_percentile": 70,
             "brief_ok": True, "issues": "", "reason": "great moment"}

    def d(self, judge=None, est=None, quality=9, today=0, gate=(True, "ok"), err="", bar=None):
        return autoapprove.decide(AUTO, gate[0], gate[1], judge, err,
                                  est or Estimate(pct=0.5, n=0), quality, today, cold_start_min_quality=bar)

    def test_gate_failure_holds(self):
        self.assertEqual(self.d(self.judge, gate=(False, "spoiler risk is high")).action, "hold")

    def test_no_ai_holds(self):
        self.assertEqual(self.d(None, err="no-credentials").action, "hold")

    def test_cold_start(self):
        self.assertEqual(self.d(self.judge, quality=9).action, "approve")
        self.assertEqual(self.d(self.judge, quality=6).action, "hold")
        self.assertEqual(self.d(self.judge, quality=9, today=AUTO["cold_start_max_per_day"]).action, "hold")

    def test_cold_start_confidence_is_its_own_setting(self):
        loose = dict(AUTO, cold_start_min_quality=6, cold_start_min_confidence=6)
        sure6 = dict(self.judge, confidence=6)
        cold = Estimate(pct=0.5, n=0)
        warm = Estimate(pct=0.7, n=50, expected_views=5000)
        self.assertEqual(autoapprove.decide(loose, True, "ok", sure6, "", cold, 6, 0).action, "approve")
        self.assertEqual(autoapprove.decide(loose, True, "ok", sure6, "", cold, 5, 0).action, "hold")
        self.assertEqual(autoapprove.decide(loose, True, "ok", dict(self.judge, confidence=5), "", cold, 6, 0).action,
                         "hold")
        held = autoapprove.decide(loose, True, "ok", sure6, "", warm, 6, 0)  # history phase keeps min_confidence 7
        self.assertEqual(held.action, "hold")
        self.assertIn("confidence below 7", held.note)

    def test_viewer_backed_cold_start_bar(self):
        self.assertEqual(self.d(self.judge, quality=6, bar=6).action, "approve")
        self.assertEqual(self.d(self.judge, quality=5, bar=6).action, "hold")
        self.assertEqual(self.d(self.judge, quality=9, bar=10).action, "approve")  # never stricter
        self.assertEqual(self.d(dict(self.judge, confidence=5), quality=6, bar=6).action, "hold")

    def test_warm_uses_history(self):
        good = Estimate(pct=0.7, n=50, expected_views=5000)
        bad = Estimate(pct=0.2, n=50)
        self.assertEqual(self.d(self.judge, est=good, quality=6).action, "approve")
        self.assertEqual(self.d(self.judge, est=bad).action, "hold")
        self.assertEqual(self.d(self.judge, est=good, today=AUTO["max_per_day"]).action, "hold")

    def test_low_confidence_or_brief_issue_holds(self):
        self.assertEqual(self.d({**self.judge, "confidence": 5}).action, "hold")
        self.assertEqual(self.d({**self.judge, "brief_ok": False, "issues": "no facecam"}).action, "hold")

    def test_reject_rules(self):
        rej = {**self.judge, "decision": "reject", "reason": "boring"}
        self.assertEqual(self.d(rej).action, "reject")
        self.assertEqual(self.d(rej, est=Estimate(pct=0.8, n=50)).action, "hold")
        self.assertEqual(self.d({**self.judge, "decision": "hold"}).action, "hold")


class PostedCaptionTests(unittest.TestCase):
    def test_reviewer_sees_hashtags_without_duplicates(self):
        clip = {"caption": "LACY steals a cop car\n#LACY", "hashtags": "LACY,gta6,gaming", "title": "t"}
        text = autoapprove._posted_caption(clip)
        self.assertEqual(text.count("#LACY"), 1)
        self.assertIn("#gta6", text)
        self.assertIn("#gaming", text)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(tempfile.mkdtemp())
        self.ch = {"login": "streamer", "permission": "campaign:c1"}
        self.clip = {"final_path": "/x.mp4", "spoiler_risk": "none"}
        self.notes = {"source": "claude-opus-5-5", "unsafe": False}

    def test_passes(self):
        self.assertTrue(guardrails.auto_gate(self.cfg, self.ch, self.clip, self.notes)[0])

    def test_blocks(self):
        g = guardrails.auto_gate
        self.assertFalse(g(self.cfg, {"login": "t", "permission": "test"}, self.clip, self.notes)[0])
        self.assertFalse(g(self.cfg, self.ch, self.clip, {"source": "template"})[0])
        self.assertFalse(g(self.cfg, self.ch, self.clip, {**self.notes, "unsafe": True})[0])
        self.assertFalse(g(self.cfg, self.ch, {**self.clip, "spoiler_risk": "high"}, self.notes)[0])
        self.assertFalse(g(self.cfg, self.ch, {**self.clip, "spoiler_risk": "low"}, self.notes)[0])
        self.cfg["auto_approve"]["allow_spoiler_low"] = True
        self.assertTrue(g(self.cfg, self.ch, {**self.clip, "spoiler_risk": "low"}, self.notes)[0])

    def test_gemini_gates(self):
        g = guardrails.auto_gate
        self.cfg["gemini"]["enabled"] = True
        ok = {"quality": 7, "hook_matches": True, "age_risk": "low", "age_reason": "",
              "is_gta6": True, "streamer_featured": True, "violence": []}
        why = lambda gem: g(self.cfg, self.ch, self.clip, {**self.notes, **({"gemini": gem} if gem else {})})
        self.assertTrue(why(ok)[0])
        self.assertIn("no video check", why(None)[1])
        self.assertIn("fresh video check", why({"quality": 7, "hook_matches": True})[1])  # reviewed before the checks
        self.assertIn("age-restrict", why({**ok, "age_risk": "high", "age_reason": "a beating up close"})[1])
        self.assertIn("a beating up close", why({**ok, "age_risk": "high", "age_reason": "a beating up close"})[1])
        self.assertIn("GTA 6", why({**ok, "is_gta6": False})[1])
        self.assertIn("streamer", why({**ok, "streamer_featured": False})[1])
        self.assertIn("rates it 5/10", why({**ok, "quality": 5})[1])
        self.assertIn("still shows violence: a beating", why({**ok, "violence": [[3, 6, "a beating"]]})[1])
        no_key = {k: v for k, v in ok.items() if k != "violence"}
        self.assertIn("fresh video check", why(no_key)[1])  # reviewed before violence was checked


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.db = DB(Path(self.tmp) / "t.db")

    def judge(self, decision="approve"):
        return mock.patch.object(autoapprove, "judge_clip", return_value=(
            {"decision": decision, "confidence": 9, "predicted_percentile": 80, "brief_ok": True,
             "issues": "", "reason": "r"}, "claude-opus-5-5"))

    def test_approves_holds_and_skips(self):
        good = add_clip(self.db, self.tmp, quality=9)
        weak = add_clip(self.db, self.tmp, quality=5)
        test = add_clip(self.db, self.tmp, channel="tester", quality=10)
        with self.judge() as j:
            autoapprove.run_once(self.db, self.cfg)
        row = lambda i: self.db.one("SELECT * FROM clips WHERE id = ?", (i,))
        self.assertEqual(row(good)["status"], "approved")
        self.assertEqual(row(good)["decided_by"], "auto")
        self.assertEqual(row(weak)["status"], "rejected")  # cold start needs quality 8: no judge call
        self.assertIn("Below the bar, not judged", row(weak)["decision_note"])
        self.assertEqual(row(test)["status"], "ready")
        self.assertIn("test footage", row(test)["decision_note"])
        self.assertEqual(j.call_count, 1)  # only the clip that could pass reaches the judge
        with self.judge() as j:
            autoapprove.run_once(self.db, self.cfg)
            self.assertEqual(j.call_count, 0)  # unchanged clips aren't re-reviewed

    def test_below_bar_without_auto_reject_is_held_unjudged(self):
        self.cfg["auto_approve"]["auto_reject"] = False
        weak = add_clip(self.db, self.tmp, quality=5)
        with self.judge() as j:
            autoapprove.run_once(self.db, self.cfg)
        row = self.db.one("SELECT status, decision_note FROM clips WHERE id = ?", (weak,))
        self.assertEqual((row["status"], j.call_count), ("ready", 0))
        self.assertIn("cold-start bar of 8", row["decision_note"])

    def test_below_bar_reasons(self):
        acfg = dict(AUTO)
        self.assertIn("cold-start bar of 8", autoapprove.below_bar(acfg, Estimate(pct=0.5, n=0), 6))
        self.assertEqual(autoapprove.below_bar(acfg, Estimate(pct=0.5, n=0), 6, cold_start_min_quality=6), "")
        self.assertEqual(autoapprove.below_bar(acfg, Estimate(pct=0.5, n=0), 8), "")
        self.assertIn("history predicts p20", autoapprove.below_bar(acfg, Estimate(pct=0.2, n=50), 9))
        self.assertEqual(autoapprove.below_bar(acfg, Estimate(pct=0.7, n=50), 3), "")  # history decides once warm

    def test_held_clips_are_listed_as_waiting(self):
        self.cfg["auto_approve"]["auto_reject"] = False
        good = add_clip(self.db, self.tmp, quality=9)
        weak = add_clip(self.db, self.tmp, quality=5)
        fresh = add_clip(self.db, self.tmp, quality=9)
        self.db.update("clips", fresh, auto_checked_at=0)  # checked before its last change
        with self.judge():
            autoapprove.run_once(self.db, self.cfg)
        self.db.update("clips", fresh, hook="edited")  # changed since review -> not waiting
        waiting = [r["id"] for r in autoapprove.waiting_for_human(self.db)]
        self.assertIn(weak, waiting)
        self.assertNotIn(good, waiting)  # approved
        self.assertNotIn(fresh, waiting)

    def test_human_decisions_are_respected(self):
        cid = add_clip(self.db, self.tmp, decided_by="human")
        with self.judge():
            autoapprove.run_once(self.db, self.cfg)
        self.assertEqual(self.db.one("SELECT status FROM clips WHERE id = ?", (cid,))["status"], "ready")

    def test_stale_clips_expire(self):
        old = add_clip(self.db, self.tmp, created_at=time.time() - 13 * 3600)
        with self.judge():
            autoapprove.run_once(self.db, self.cfg)
        r = self.db.one("SELECT status, decision_note FROM clips WHERE id = ?", (old,))
        self.assertEqual(r["status"], "rejected")
        self.assertIn("Expired", r["decision_note"])

    def test_history_feeds_the_model(self):
        now = time.time()
        for i in range(20):
            cid = add_clip(self.db, self.tmp, status="posted", channel="streamer" if i < 10 else "other",
                           quality=9 if i < 10 else 4)
            self.db.insert("posts", clip_id=cid, platform="youtube", status="published",
                           posted_at=now - 2 * 86400, views=50_000 if i < 10 else 300, stats_at=now)
        hist, pv = autoapprove.load_history(self.db, self.cfg)
        self.assertEqual(len(hist), 20)
        est = autoapprove.estimate(hist, {"channel": "streamer", "quality": 9, "score": 5.0,
                                          "spike_id": None}, pv, ["youtube"])
        self.assertGreater(est.pct, 0.7)
        self.assertEqual(est.expected_views, 50_000)


class VetoWindowTests(unittest.TestCase):
    def test_auto_approved_clip_waits_for_undo_window(self):
        tmp = tempfile.mkdtemp()
        cfg = make_cfg(tmp)
        cfg["channels"].append({"login": "me", "permission": "own"})
        db = DB(Path(tmp) / "t.db")
        cid = add_clip(db, tmp, channel="me", status="approved", decided_by="auto", decided_at=time.time())
        scheduler.post_once(db, cfg)
        self.assertIsNone(db.one("SELECT id FROM posts WHERE clip_id = ?", (cid,)))
        db.update("clips", cid, decided_at=time.time() - 11 * 60)
        scheduler.post_once(db, cfg)
        self.assertIsNotNone(db.one("SELECT id FROM posts WHERE clip_id = ?", (cid,)))


class MigrationTests(unittest.TestCase):
    def test_old_database_gains_columns(self):
        path = Path(tempfile.mkdtemp()) / "old.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE clips (id INTEGER PRIMARY KEY, channel TEXT NOT NULL, "
                    "created_at REAL NOT NULL, updated_at REAL NOT NULL, status TEXT)")
        con.execute("INSERT INTO clips (channel, created_at, updated_at, status) VALUES ('x', 1, 1, 'ready')")
        con.commit()
        con.close()
        db = DB(path)
        row = db.one("SELECT decided_by, expected_views FROM clips")
        self.assertEqual((row["decided_by"], row["expected_views"]), ("", 0))


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.db = DB(Path(self.tmp) / "t.db")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), review_server.make_handler(self.db, self.cfg, []))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_manual_views_and_undo(self):
        cid = add_clip(self.db, self.tmp, status="approved", decided_by="auto", decided_at=time.time())
        pid = self.db.insert("posts", clip_id=cid, platform="export", status="exported", posted_at=time.time())
        self.assertEqual(self.post(f"/api/posts/{pid}", {"url": "https://youtube.com/shorts/x", "views": "1234"}), 200)
        p = self.db.one("SELECT * FROM posts WHERE id = ?", (pid,))
        self.assertEqual((p["views"], p["url"]), (1234, "https://youtube.com/shorts/x"))
        self.assertIsNotNone(p["stats_at"])
        self.assertEqual(self.post(f"/api/posts/{pid}", {"url": "javascript:alert(1)"}), 400)
        self.assertEqual(self.post(f"/api/clips/{cid}", {"action": "unapprove", "hook": "no way"}), 200)
        c = self.db.one("SELECT status, decided_by FROM clips WHERE id = ?", (cid,))
        self.assertEqual((c["status"], c["decided_by"]), ("ready", "human"))
        with urllib.request.urlopen(self.base + "/api/summary") as r:
            summary = json.load(r)
        self.assertTrue(summary["auto"]["enabled"])

    def test_campaign_tabs_filter_clips(self):
        self.cfg["campaigns"] = [{"id": "speedcamp", "name": "Speed", "active": True},
                                 {"id": "oldcamp", "name": "Old", "active": False}]
        for camp in ("speedcamp", "speedcamp", "oldcamp", ""):
            cid = add_clip(self.db, self.tmp, status="ready")
            self.db.execute("UPDATE clips SET campaign_id = ? WHERE id = ?", (camp, cid))

        def get(path):
            with urllib.request.urlopen(self.base + path) as r:
                return json.load(r)

        self.assertEqual(len(get("/api/clips?status=ready&campaign=speedcamp")), 2)
        self.assertEqual(len(get("/api/clips?status=ready&campaign=none")), 2)  # inactive + no campaign
        self.assertEqual(len(get("/api/clips?status=ready")), 4)
        tabs = {c["id"]: c["counts"] for c in get("/api/summary")["campaigns"]}
        self.assertEqual(tabs, {"speedcamp": {"ready": 2}, "none": {"ready": 2}})


if __name__ == "__main__":
    unittest.main()
