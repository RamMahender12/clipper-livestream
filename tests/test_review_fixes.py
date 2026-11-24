"""Regression tests for the project-review fixes: AI and upload retries, human decisions winning
over background workers, media retention, and batched YouTube reads."""
import fcntl
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipper import ai, autoapprove, config as config_mod, pipeline, recorder, retention, scheduler
from clipper.db import DB
from clipper.publishers import instagram, youtube
from clipper.publishers.base import PostResult, Publisher, status_many

from test_autoapprove import add_clip, make_cfg

JUDGE_OK = ({"decision": "approve", "confidence": 9, "predicted_percentile": 80, "brief_ok": True,
             "issues": "", "reason": "r"}, "claude-opus-5-5")


def row(db, cid):
    return dict(db.one("SELECT * FROM clips WHERE id = ?", (cid,)))


class JudgeRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.db = DB(Path(self.tmp) / "t.db")

    def test_temporary_failure_is_judged_again_next_pass(self):
        cid = add_clip(self.db, self.tmp, quality=9)
        with mock.patch.object(autoapprove, "judge_clip", return_value=(None, "budget")):
            autoapprove.run_once(self.db, self.cfg)
        r = row(self.db, cid)
        self.assertEqual(r["status"], "ready")
        self.assertIsNone(r["auto_checked_at"])
        self.assertIn("trying again automatically", r["decision_note"])
        with mock.patch.object(autoapprove, "judge_clip", return_value=JUDGE_OK) as j:
            autoapprove.run_once(self.db, self.cfg)
        self.assertEqual(j.call_count, 1)  # the cap reset: it gets its review
        self.assertEqual(row(self.db, cid)["status"], "approved")

    def test_failure_about_the_clip_is_held_once(self):
        cid = add_clip(self.db, self.tmp, quality=9)
        with mock.patch.object(autoapprove, "judge_clip", return_value=(None, "refusal")):
            autoapprove.run_once(self.db, self.cfg)
        self.assertIsNotNone(row(self.db, cid)["auto_checked_at"])
        with mock.patch.object(autoapprove, "judge_clip", return_value=JUDGE_OK) as j:
            autoapprove.run_once(self.db, self.cfg)
        self.assertEqual(j.call_count, 0)  # a refusal waits for you; no paid re-tries

    def test_your_decision_during_the_judge_call_wins(self):
        cid = add_clip(self.db, self.tmp, quality=9)

        def judge_while_you_reject(*a, **kw):
            self.db.update("clips", cid, status="rejected", decided_by="human", decision_note="Rejected by you")
            return JUDGE_OK

        with mock.patch.object(autoapprove, "judge_clip", side_effect=judge_while_you_reject):
            lines = autoapprove.run_once(self.db, self.cfg)
        r = row(self.db, cid)
        self.assertEqual((r["status"], r["decided_by"]), ("rejected", "human"))
        self.assertIn("not applied", lines[-1])
        self.assertEqual(autoapprove.approved_in_last_day(self.db), 0)

    def test_temporary_reasons(self):
        for r in ("budget", "connection", "rate-limit", "api-529", "api-503", "no-credit"):
            self.assertTrue(ai.temporary(r), r)
        for r in ("refusal", "bad-json", "api-400", "no-credentials", ""):
            self.assertFalse(ai.temporary(r), r)


class RedoAfterOutageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.db = DB(Path(self.tmp) / "t.db")
        src = Path(self.tmp) / "raw.ts"
        src.write_bytes(b"x")
        self.src = str(src)

    def template_clip(self, **kw):
        notes = {"source": "template", "packaging_error": "budget", "template_hook": "Nobody expected this"}
        return add_clip(self.db, self.tmp, hook="Nobody expected this", source=self.src,
                        words_json="[]", ai_notes=json.dumps(notes), **kw)

    def redo(self, up=True):
        with mock.patch.object(ai, "available", return_value=(up, "" if up else "budget")), \
                mock.patch.object(pipeline, "process_clip") as proc, \
                mock.patch.object(pipeline, "check_and_finish") as check:
            done = pipeline.redo_after_outage(self.db, self.cfg)
        return done, proc, check

    def test_template_packaging_is_redone_once_claude_is_back(self):
        cid = self.template_clip()
        done, proc, _ = self.redo(up=False)
        self.assertIsNone(done)  # still over the cap: leave it
        done, proc, _ = self.redo(up=True)
        self.assertEqual(done, cid)
        clip = proc.call_args[0][2]
        self.assertEqual((clip["id"], clip["hook"]), (cid, ""))  # hook cleared so packaging runs again
        self.assertEqual(row(self.db, cid)["status"], "rendering")
        self.assertEqual(json.loads(row(self.db, cid)["ai_notes"])["ai_redos"], 1)

    def test_a_hook_you_typed_is_never_replaced(self):
        self.template_clip()
        self.db.execute("UPDATE clips SET hook = 'my own hook'")
        done, proc, _ = self.redo()
        self.assertIsNone(done)
        proc.assert_not_called()

    def test_tries_are_capped_and_spaced(self):
        cid = self.template_clip()
        notes = json.loads(row(self.db, cid)["ai_notes"])
        self.db.update("clips", cid, ai_notes=json.dumps({**notes, "ai_redos": 1, "ai_redo_at": time.time()}))
        self.assertIsNone(self.redo()[0])  # tried a minute ago
        self.db.update("clips", cid, ai_notes=json.dumps({**notes, "ai_redos": pipeline.AI_REDOS}))
        self.assertIsNone(self.redo()[0])  # out of tries

    def test_missing_video_check_is_rerun(self):
        from clipper import gemini
        gemini.BREAKER.reset()
        self.cfg["gemini"].update(enabled=True, backend="claude")
        self.cfg["ai"]["enabled"] = True
        cid = add_clip(self.db, self.tmp, ai_notes=json.dumps({"source": "claude-haiku-5-5",
                                                               "gemini_error": "Claude video check failed: budget"}))
        with mock.patch.object(ai, "have_credentials", return_value=True):
            done, proc, check = self.redo()
        self.assertEqual(done, cid)
        proc.assert_not_called()
        check.assert_called_once_with(self.db, self.cfg, cid, own_hook=True)

    def test_render_does_not_overwrite_your_rejection(self):
        cid = add_clip(self.db, self.tmp, status="rendering")
        self.db.update("clips", cid, status="rejected", decided_by="human")  # you, mid-render
        with mock.patch.object(pipeline, "gemini_review"), mock.patch.object(pipeline, "cut_violence", return_value=""), \
                mock.patch.object(pipeline, "swap_hook"):
            pipeline.check_and_finish(self.db, self.cfg, cid, own_hook=True)
        self.assertEqual(row(self.db, cid)["status"], "rejected")


class OffGameTests(unittest.TestCase):
    IRL = {"source": "claude-haiku-5-5", "gemini": {"is_gta6": False, "quality": 3, "model": "claude-sonnet-5-5",
                                                    "what_happens": "Real-life IRL footage of Clix in an SUV"}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.db = DB(Path(self.tmp) / "t.db")

    def finish(self, cid):
        with mock.patch.object(pipeline, "gemini_review"), \
                mock.patch.object(pipeline, "cut_violence", return_value="") as cut, \
                mock.patch.object(pipeline, "swap_hook") as swap:
            pipeline.check_and_finish(self.db, self.cfg, cid, own_hook=True)
        return cut, swap

    def test_dropped_after_the_first_check_before_any_rerender(self):
        cid = add_clip(self.db, self.tmp, status="rendering", ai_notes=json.dumps(self.IRL))
        cut, swap = self.finish(cid)
        r = row(self.db, cid)
        self.assertEqual((r["status"], r["decided_by"]), ("rejected", "auto"))
        self.assertIn("not GTA 6 gameplay", r["decision_note"])
        self.assertIn("IRL footage of Clix", r["decision_note"])
        cut.assert_not_called()
        swap.assert_not_called()  # no re-render, no second check

    def test_gameplay_and_your_clips_go_on(self):
        gta = add_clip(self.db, self.tmp, status="rendering",
                       ai_notes=json.dumps({"source": "x", "gemini": {"is_gta6": True}}))
        yours = add_clip(self.db, self.tmp, status="rendering", decided_by="human", ai_notes=json.dumps(self.IRL))
        self.finish(gta)
        self.finish(yours)
        self.assertEqual(row(self.db, gta)["status"], "ready")
        self.assertEqual(row(self.db, yours)["status"], "ready")

    def test_held_ones_are_cleared_by_the_auto_approver(self):
        held = add_clip(self.db, self.tmp, ai_notes=json.dumps(self.IRL), auto_checked_at=time.time() + 5,
                        decision_note="Needs a human: Video check doesn't think this is GTA 6 gameplay")
        yours = add_clip(self.db, self.tmp, decided_by="human", ai_notes=json.dumps(self.IRL))
        with mock.patch.object(autoapprove, "judge_clip", return_value=JUDGE_OK):
            lines = autoapprove.run_once(self.db, self.cfg)
        self.assertEqual(row(self.db, held)["status"], "rejected")
        self.assertEqual(row(self.db, yours)["status"], "ready")
        self.assertIn(f"clip {held}: rejected, not GTA 6 gameplay", lines)

    def test_can_be_switched_back_to_holding(self):
        self.cfg["gemini"]["reject_off_game"] = False
        cid = add_clip(self.db, self.tmp, status="rendering", ai_notes=json.dumps(self.IRL))
        self.finish(cid)
        self.assertEqual(row(self.db, cid)["status"], "ready")


class QuickGameCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.cfg["gemini"].update(enabled=True, backend="claude")
        self.db = DB(Path(self.tmp) / "t.db")
        src = Path(self.tmp) / "raw.ts"
        src.write_bytes(b"x")
        self.src = src

    def clip(self, **kw):
        fields = dict(status="rendering", hook="", source=str(self.src), start_s=10, duration_s=20,
                      words_json="", ai_notes="")
        fields.update(kw)
        return add_clip(self.db, self.tmp, **fields)

    def run_clip(self, cid, shows="not_a_game", what="Clix runs a giveaway draw on a dashboard"):
        from clipper import claude_vision, metadata
        meta = metadata.ClipMeta(hook="h", title="t", caption="c", quality=8, source="claude-haiku-5-5")
        with mock.patch.object(claude_vision, "precheck", return_value=(shows, what, "claude-haiku-5-5")) as pre, \
                mock.patch.object(pipeline.captions, "transcribe_full", return_value=([], [])) as whisper, \
                mock.patch.object(metadata, "generate", return_value=meta), \
                mock.patch.object(pipeline, "render_final") as render, \
                mock.patch.object(pipeline, "check_and_finish") as check:
            pipeline.process_clip(self.db, self.cfg, row(self.db, cid))
        return pre, whisper, render, check

    def test_no_game_on_screen_stops_before_any_work(self):
        cid = self.clip()
        pre, whisper, render, check = self.run_clip(cid)
        self.assertAlmostEqual(pre.call_args[0][2], 20.0)  # the middle of the moment
        r = row(self.db, cid)
        self.assertEqual((r["status"], r["decided_by"]), ("rejected", "auto"))
        self.assertIn("quick check, no game on screen", r["decision_note"])
        self.assertIn("giveaway draw", r["decision_note"])
        render.assert_not_called()
        check.assert_not_called()
        self.assertEqual(json.loads(r["ai_notes"])["precheck"]["shows"], "not_a_game")

    def test_gta6_another_game_and_unsure_go_on_and_the_verdict_is_kept(self):
        # "another game" too: Haiku took real GTA 6 footage for other games (it's newer than Haiku)
        for shows in ("gta6", "other_game", "unsure"):
            cid = self.clip()
            _, _, render, check = self.run_clip(cid, shows=shows, what="driving")
            render.assert_called_once()
            check.assert_called_once()
            self.assertEqual(json.loads(row(self.db, cid)["ai_notes"])["precheck"]["shows"], shows)

    def test_a_failed_check_never_drops_the_clip(self):
        from clipper import claude_vision
        cid = self.clip()
        with mock.patch.object(claude_vision, "precheck", side_effect=claude_vision.GeminiError("budget")):
            self.assertEqual(pipeline.quick_game_check(self.db, self.cfg, row(self.db, cid), self.src, 0, 20), "")

    def test_skipped_for_your_hook_your_decision_or_a_second_pass(self):
        yours = self.clip(hook="my hook")
        decided = self.clip(decided_by="human")
        again = self.clip(ai_notes=json.dumps({"precheck": {"shows": "gta6"}}))
        for cid in (yours, decided, again):
            pre, *_ = self.run_clip(cid)
            pre.assert_not_called()

    def test_off_with_the_switch_or_without_the_video_check(self):
        self.cfg["gemini"]["precheck"] = False
        pre, *_ = self.run_clip(self.clip())
        pre.assert_not_called()
        self.cfg["gemini"].update(precheck=True, enabled=False)
        pre, *_ = self.run_clip(self.clip())
        pre.assert_not_called()


class ClaimTests(unittest.TestCase):
    def test_two_connections_never_get_the_same_clip(self):
        tmp = tempfile.mkdtemp()
        a, b = DB(Path(tmp) / "t.db"), DB(Path(tmp) / "t.db")
        now = time.time()
        a.insert("clips", channel="x", status="queued", score=1, created_at=now, updated_at=now)
        real_execute, real_commit = a.conn.execute, a.conn.commit

        class Racer:  # the other connection grabs the clip between our SELECT and UPDATE
            won = None

            def __call__(self, sql, params=()):
                if sql.startswith("UPDATE") and self.won is None:
                    self.won = b.claim_clip("queued", "rendering")
                return real_execute(sql, params)

        racer = Racer()
        with mock.patch.object(a, "conn") as conn:
            conn.execute.side_effect = racer
            conn.commit = real_commit
            got = a.claim_clip("queued", "rendering")
        self.assertIsNotNone(racer.won)
        self.assertIsNone(got)  # before the fix both got clip 1


class FlakyYouTube(Publisher):
    name = "youtube"
    results: list = []
    calls = 0

    def ready(self):
        return True, "ok"

    def publish(self, video, meta):
        FlakyYouTube.calls += 1
        return FlakyYouTube.results.pop(0)


class PublishRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        sec = Path(self.tmp) / "secrets"
        sec.mkdir()
        (sec / "youtube_lacy.json").write_text("{}")
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": str(Path(self.tmp) / "data")},
            "posting": {"platforms": ["youtube"], "min_gap_minutes": 0, "quiet_hours": [],
                        "earliest_footage": "2026-01-01T00:00:00+00:00"},
            "auto_approve": {"veto_minutes": 0},
            "channels": [{"login": "lacy", "permission": "own", "youtube_token_file": str(sec / "youtube_lacy.json")}],
        })
        self.db = DB(Path(self.tmp) / "t.db")
        video = Path(self.tmp) / "v.mp4"
        video.write_bytes(b"x")
        now = time.time()
        self.cid = self.db.insert("clips", channel="lacy", status="approved", final_path=str(video),
                                  spoiler_risk="none", caption="c", quality=8, wall_start=now,
                                  decided_by="human", created_at=now, updated_at=now)
        FlakyYouTube.calls = 0
        p = mock.patch("clipper.publishers.get_publisher", return_value=FlakyYouTube(self.cfg))
        p.start()
        self.addCleanup(p.stop)
        n = mock.patch("clipper.submit_tracker.send_batch")
        n.start()
        self.addCleanup(n.stop)

    def post(self):
        return self.db.one("SELECT * FROM posts WHERE clip_id = ?", (self.cid,))

    def test_safe_failure_is_retried_then_posts(self):
        FlakyYouTube.results = [PostResult(False, "youtube", status="error", error="quota", retry=True),
                                PostResult(True, "youtube", remote_id="v1", url="u", status="published")]
        lines = scheduler.post_once(self.db, self.cfg)
        self.assertIn("trying again at", lines[0])
        self.assertIsNotNone(self.post()["retry_at"])
        scheduler.post_once(self.db, self.cfg)
        self.assertEqual(FlakyYouTube.calls, 1)  # not before retry_at
        self.db.execute("UPDATE posts SET retry_at = ?", (time.time() - 1,))
        scheduler.post_once(self.db, self.cfg)
        p = self.post()
        self.assertEqual((p["status"], p["attempts"], p["remote_id"]), ("published", 2, "v1"))
        self.assertEqual(row(self.db, self.cid)["status"], "posted")

    def test_unclear_failure_is_never_retried(self):
        FlakyYouTube.results = [PostResult(False, "youtube", status="error", error="failed after retries")]
        lines = scheduler.post_once(self.db, self.cfg)
        self.assertIn("not retried automatically", lines[0])
        self.assertIsNone(self.post()["retry_at"])
        scheduler.post_once(self.db, self.cfg)
        self.assertEqual(FlakyYouTube.calls, 1)

    def test_retries_stop_after_the_schedule(self):
        self.cfg["posting"]["retry_minutes"] = [1]
        FlakyYouTube.results = [PostResult(False, "youtube", status="error", error="x", retry=True)] * 2
        scheduler.post_once(self.db, self.cfg)
        self.db.execute("UPDATE posts SET retry_at = ?", (time.time() - 1,))
        lines = scheduler.post_once(self.db, self.cfg)
        self.assertIn("gave up after 2 tries", lines[0])
        self.assertIsNone(self.post()["retry_at"])

    def test_second_posting_pass_waits_for_the_first(self):
        FlakyYouTube.results = [PostResult(True, "youtube", remote_id="v1", url="u", status="published")]
        lock = config_mod.data_dir(self.cfg) / "posting.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "w") as fh:  # e.g. the service, mid-upload
            fcntl.flock(fh, fcntl.LOCK_EX)
            lines = scheduler.post_once(self.db, self.cfg)
        self.assertIn("another posting pass is running", lines[0])
        self.assertEqual(FlakyYouTube.calls, 0)

    def test_undo_just_before_upload_is_respected(self):
        FlakyYouTube.results = [PostResult(True, "youtube", remote_id="v1", url="u", status="published")]
        db, cid = self.db, self.cid

        class UndoneMidPass(FlakyYouTube):
            def ready(self):
                db.update("clips", cid, status="ready", decided_by="human")  # you tapped Undo
                return True, "ok"

        with mock.patch("clipper.publishers.get_publisher", return_value=UndoneMidPass(self.cfg)):
            scheduler.post_once(self.db, self.cfg)
        self.assertEqual(FlakyYouTube.calls, 0)
        self.assertIsNone(self.post())


class PublisherRetryFlagTests(unittest.TestCase):
    cfg = config_mod._merge(config_mod.DEFAULTS, {})

    def setUp(self):
        self.video = Path(tempfile.mkdtemp()) / "v.mp4"
        self.video.write_bytes(b"x" * 10)

    def test_youtube_marks_only_failures_before_a_video_exists(self):
        from clipper.publishers.base import PostMeta
        pub = youtube.YouTubePublisher(self.cfg)
        meta = PostMeta(title="t", caption="c")
        with mock.patch.object(pub, "_start_session", side_effect=youtube._ApiError("quotaExceeded")):
            self.assertTrue(pub.publish(self.video, meta).retry)
        with mock.patch.object(pub, "_start_session", return_value="https://up"), \
                mock.patch.object(pub, "_upload", side_effect=youtube._ApiError("YouTube upload failed after retries")):
            r = pub.publish(self.video, meta)
            self.assertFalse(r.retry)  # the last bytes may have landed: a retry could double-post
        with mock.patch.object(pub, "_start_session", return_value="https://up"), \
                mock.patch.object(pub, "_upload", side_effect=youtube._SessionExpired()):
            self.assertTrue(pub.publish(self.video, meta).retry)

    def test_instagram_marks_only_failures_before_media_publish(self):
        from clipper.publishers.base import PostMeta
        cfg = config_mod._merge(config_mod.DEFAULTS, {"instagram": {"ig_user_id": "178"}})
        pub = instagram.InstagramPublisher(cfg)
        meta = PostMeta(title="t", caption="c")
        with mock.patch.object(pub, "ready", return_value=(True, "")), \
                mock.patch.object(pub, "_prepare", side_effect=instagram._ApiError("processing ERROR")):
            self.assertTrue(pub.publish(self.video, meta).retry)
        with mock.patch.object(pub, "ready", return_value=(True, "")), \
                mock.patch.object(pub, "_prepare", return_value="C1"), \
                mock.patch.object(pub, "_post", side_effect=instagram._ApiError("connection error", transient=True)):
            self.assertFalse(pub.publish(self.video, meta).retry)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {"paths": {"data_dir": str(self.tmp)}})
        self.db = DB(self.tmp / "t.db")
        self.old = time.time() - 30 * 86400

    def clip(self, status, name, source=None, updated=None):
        raw = Path(source) if source else self.tmp / "raw" / "lacy" / f"{name}.ts"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"r" * 1000)
        out = self.tmp / "clips" / name
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.mp4").write_bytes(b"v" * 1000)
        (out / "thumb.jpg").write_bytes(b"t")
        t = updated or self.old
        self.db.insert("clips", channel="lacy", status=status, source=str(raw), final_path=str(out / "final.mp4"),
                       created_at=t, updated_at=t)
        return raw, out

    def test_done_clips_lose_media_live_ones_keep_it(self):
        posted_raw, posted_out = self.clip("posted", "a")
        ready_raw, ready_out = self.clip("ready", "b")
        fresh_raw, _ = self.clip("rejected", "c", updated=time.time())
        outside = Path(tempfile.mkdtemp()) / "mine.mp4"
        mine_raw, _ = self.clip("rejected", "d", source=str(outside))
        out = retention.prune_media(self.db, self.cfg, low_disk=False)
        self.assertFalse(posted_raw.exists())
        self.assertFalse((posted_out / "final.mp4").exists())
        self.assertTrue((posted_out / "thumb.jpg").exists())  # the dashboard still shows it
        self.assertTrue(ready_raw.exists() and (ready_out / "final.mp4").exists())
        self.assertTrue(fresh_raw.exists())  # rejected an hour ago: not yet
        self.assertTrue(mine_raw.exists())  # never outside data/raw
        self.assertEqual(out["raw"], 1)

    def test_raw_cut_shared_with_a_live_clip_is_kept(self):
        raw, _ = self.clip("rejected", "a")
        self.clip("approved", "b", source=str(raw))
        retention.prune_media(self.db, self.cfg, low_disk=False)
        self.assertTrue(raw.exists())

    def test_low_disk_deletes_right_away_and_alerts(self):
        raw, _ = self.clip("rejected", "a", updated=time.time())
        sent = []
        with mock.patch.object(retention, "free_gb", return_value=1.0):
            retention._last_alert = 0.0
            retention.housekeeping(self.db, self.cfg, send=sent.append)
        self.assertFalse(raw.exists())
        self.assertIn("Disk almost full", sent[0])

    def test_posted_clip_loses_every_copy_once_it_is_on_youtube(self):
        now = time.time()
        raw, out = self.clip("posted", "up", updated=now)  # just posted: no waiting period
        exp = self.tmp / "export" / "2026-11-24" / "1_up.mp4"
        exp.parent.mkdir(parents=True)
        exp.write_bytes(b"e")
        exp.with_suffix(".txt").write_text("caption")
        self.db.insert("posts", clip_id=1, platform="youtube", status="published",
                       url="https://youtube.com/shorts/abc123", posted_at=now)
        self.db.insert("posts", clip_id=1, platform="export", status="exported", remote_id=str(exp),
                       url=exp.as_uri(), posted_at=now)
        local_raw, local_out = self.clip("posted", "export-only", updated=now)  # nowhere else to watch it
        self.db.insert("posts", clip_id=2, platform="export", status="exported", url="file:///x", posted_at=now)
        retention.prune_media(self.db, self.cfg, low_disk=False)
        self.assertFalse(raw.exists() or (out / "final.mp4").exists())
        self.assertFalse(exp.exists() or exp.with_suffix(".txt").exists())
        self.assertTrue((out / "thumb.jpg").exists())
        self.assertTrue(local_raw.exists() and (local_out / "final.mp4").exists())

    def test_housekeeping_expires_unreviewed_clips_with_auto_approve_off(self):
        self.cfg["auto_approve"]["enabled"] = False
        self.clip("ready", "old")  # created 30 days ago
        self.clip("ready", "new", updated=time.time())
        with mock.patch.object(retention, "free_gb", return_value=100.0):
            retention.housekeeping(self.db, self.cfg)
        statuses = sorted(r["status"] for r in self.db.query("SELECT status FROM clips"))
        self.assertEqual(statuses, ["ready"])  # expired, then purged as rejected

    def test_rejected_clips_are_deleted_completely(self):
        raw, out = self.clip("rejected", "bad", updated=time.time())  # id 1
        self.db.insert("posts", clip_id=1, platform="youtube", status="error")
        self.db.execute("UPDATE clips SET drive_url = 'https://drive.google.com/file/d/AAA/view' WHERE id = 1")
        stuck_raw, _ = self.clip("rejected", "drive-down", updated=time.time())  # id 2
        self.db.execute("UPDATE clips SET drive_url = 'https://drive.google.com/file/d/BBB/view' WHERE id = 2")
        self.clip("ready", "keep", updated=time.time())  # id 3
        newer_raw, _ = self.clip("rejected", "newest", updated=time.time())  # id 4: its id could be reused
        deleted = []

        def drive_delete(link):
            if "BBB" in link:
                raise RuntimeError("Drive HTTP 500")
            deleted.append(link)

        self.assertEqual(retention.purge_rejected(self.db, self.cfg, drive_delete=drive_delete), 1)
        self.assertEqual(deleted, ["https://drive.google.com/file/d/AAA/view"])
        self.assertFalse(raw.exists() or out.exists())  # thumbnail folder too
        self.assertIsNone(self.db.one("SELECT id FROM clips WHERE id = 1"))
        self.assertIsNone(self.db.one("SELECT id FROM posts WHERE clip_id = 1"))
        self.assertTrue(stuck_raw.exists())  # Drive copy not gone yet: keep the row and its link
        self.assertTrue(newer_raw.exists())

    def test_ended_session_segments_are_all_pruned(self):
        seg_dir = self.tmp / "rec" / "lacy" / "00001"
        seg_dir.mkdir(parents=True)
        start = time.time() - 3 * 3600
        for i in range(3):
            p = seg_dir / (time.strftime(recorder.SEG_FMT, time.gmtime(start + i * 30)) + ".ts")
            p.write_bytes(b"s")
            os.utime(p, (start + i * 30, start + i * 30))
        self.assertEqual(recorder.prune(seg_dir, 90 * 60), 3)
        self.assertFalse(seg_dir.exists())

    def test_live_session_keeps_its_newest_segments(self):
        seg_dir = self.tmp / "rec" / "lacy" / "00002"
        seg_dir.mkdir(parents=True)
        start = time.time() - 3 * 3600
        for i in range(3):
            p = seg_dir / (time.strftime(recorder.SEG_FMT, time.gmtime(start + i * 30)) + ".ts")
            p.write_bytes(b"s")
        # written just now (mtime = now): still recording
        self.assertEqual(recorder.prune(seg_dir, 90 * 60), 1)


class BatchedYouTubeReadTests(unittest.TestCase):
    cfg = config_mod._merge(config_mod.DEFAULTS, {})

    def test_120_videos_cost_3_calls(self):
        calls = []

        def fake(method, url, **kw):
            ids = url.split("id=")[1].split("%2C")
            calls.append(ids)
            items = [{"id": i, "statistics": {"viewCount": "7"}, "status": {"uploadStatus": "processed"},
                      "contentDetails": {}} for i in ids if i != "v5"]
            return 200, {}, json.dumps({"items": items}).encode()

        ids = [f"v{i}" for i in range(120)]
        with mock.patch.object(youtube, "_access_token", return_value="tok"), \
                mock.patch.object(youtube, "_request", side_effect=fake):
            pub = youtube.YouTubePublisher(self.cfg)
            stats = pub.fetch_stats_many(ids)
            states = pub.check_status_many(ids)
        self.assertEqual([len(c) for c in calls], [50, 50, 20, 50, 50, 20])
        self.assertEqual((len(stats), stats["v0"].views), (119, 7))
        self.assertEqual(states["v5"][0], "removed")
        self.assertEqual(states["v6"], ("ok", ""))

    def test_failed_batch_is_unknown_not_removed(self):
        with mock.patch.object(youtube, "_access_token", return_value="tok"), \
                mock.patch.object(youtube, "_request", return_value=(403, {}, b'{"error": {"message": "quota"}}')):
            states = youtube.YouTubePublisher(self.cfg).check_status_many(["a", "b"])
        self.assertEqual({s for s, _ in states.values()}, {"unknown"})

    def test_publishers_without_a_batch_method_still_work(self):
        class One:
            def check_status(self, rid):
                if rid == "bad":
                    raise RuntimeError("boom")
                return "ok", ""

        self.assertEqual(status_many(One(), ["a", "bad"]), {"a": ("ok", ""), "bad": ("unknown", "boom")})


if __name__ == "__main__":
    unittest.main()
