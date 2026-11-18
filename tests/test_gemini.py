import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipper import config as config_mod, gemini, guardrails, pipeline
from clipper.db import DB

GEM = {"quality": 8, "what_happens": "car flips into the river", "spoiler": "none", "cutscene": False,
       "unsafe": False, "hook_matches": True, "better_hook": "", "reason": "chaos", "model": "gemini-3.8-flash"}


class CombineTests(unittest.TestCase):
    def test_weighted_quality_and_no_gemini(self):
        self.assertEqual(gemini.combine(5, "none", False, None)["quality"], 5)
        out = gemini.combine(6, "none", False, GEM)  # 0.6*8 + 0.4*6 = 7.2
        self.assertEqual((out["quality"], out["spoiler"], out["unsafe"], out["disagree"]), (7, "none", False, False))

    def test_cautious_spoiler_cutscene_and_unsafe(self):
        self.assertEqual(gemini.combine(6, "none", False, {**GEM, "spoiler": "low"})["spoiler"], "low")
        self.assertEqual(gemini.combine(6, "high", False, GEM)["spoiler"], "high")
        self.assertEqual(gemini.combine(6, "none", False, {**GEM, "cutscene": True})["spoiler"], "high")
        self.assertTrue(gemini.combine(6, "none", False, {**GEM, "unsafe": True})["unsafe"])
        self.assertTrue(gemini.combine(6, "none", True, GEM)["unsafe"])

    def test_disagreement_and_claude_unavailable(self):
        self.assertTrue(gemini.combine(4, "none", False, GEM)["disagree"])     # 4 vs 8
        self.assertFalse(gemini.combine(6, "none", False, GEM)["disagree"])    # 6 vs 8
        out = gemini.combine(3, "unknown", False, GEM)                          # template packaging
        self.assertEqual((out["quality"], out["spoiler"], out["disagree"]), (8, "none", False))


class TiebreakTests(unittest.TestCase):
    def test_views_settle_a_split_but_never_safety(self):
        split = gemini.combine(3, "none", False, {**GEM, "quality": 6})
        self.assertTrue(split["disagree"])
        strong = gemini.tiebreak(split, 3, 6, "strong", "1,616 views on Twitch")
        self.assertEqual((strong["quality"], strong["disagree"]), (6, False))
        self.assertIn("Gemini's 6/10", strong["tiebreak"])
        weak = gemini.tiebreak(split, 3, 6, "weak")
        self.assertEqual((weak["quality"], weak["disagree"]), (3, False))
        self.assertIn("Claude's 3/10", weak["tiebreak"])
        for unsure in ("neutral", None):  # no clear audience signal: still your call
            self.assertTrue(gemini.tiebreak(split, 3, 6, unsure)["disagree"])
        agreed = gemini.combine(6, "none", False, GEM)
        self.assertIs(gemini.tiebreak(agreed, 6, 8, "strong"), agreed)
        risky = gemini.combine(3, "none", True, {**GEM, "quality": 6, "spoiler": "high"})
        out = gemini.tiebreak(risky, 3, 6, "strong")
        self.assertEqual((out["unsafe"], out["spoiler"]), (True, "high"))


class ParseTests(unittest.TestCase):
    def test_response_shapes(self):
        resp = {"status": "completed", "steps": [
            {"type": "thought", "content": [{"type": "text", "text": "thinking"}]},
            {"type": "model_output", "content": [{"type": "text", "text": '{"a": 1}'}]}]}
        self.assertEqual(gemini.interaction_text(resp), '{"a": 1}')
        self.assertEqual(gemini.interaction_text({"outputs": [{"text": "legacy"}]}), "legacy")
        gc = {"candidates": [{"content": {"parts": [{"text": '{"b": 2}'}]}}]}
        self.assertEqual(gemini.generate_content_text(gc), '{"b": 2}')
        self.assertEqual(gemini._parse_json('```json\n{"c": 3}\n```'), {"c": 3})


class ReviewFlowTests(unittest.TestCase):
    def test_upload_ask_delete(self):
        video = Path(tempfile.mkdtemp()) / "final.mp4"
        video.write_bytes(b"x" * 1000)
        answer = {"what_happens": "chaos", "visual_quality": 9, "spoiler_risk": "none", "is_cutscene": False,
                  "unsafe": False, "hook_matches": False, "better_hook": "He did NOT see that coming",
                  "reason": "funny"}
        calls = []

        def fake(method, url, key, body=None, headers=None, timeout=120):
            calls.append((method, url.split("googleapis.com")[-1], headers or {}))
            if url.endswith("/upload/v1beta/files"):
                return 200, {"X-Goog-Upload-URL": "https://up/1"}, b"{}"
            if url == "https://up/1":
                return 200, {}, json.dumps({"file": {"name": "files/abc", "uri": "https://f/abc",
                                                     "state": "PROCESSING", "mimeType": "video/mp4"}}).encode()
            if method == "GET":
                return 200, {}, json.dumps({"name": "files/abc", "uri": "https://f/abc", "state": "ACTIVE"}).encode()
            if url.endswith("/v1beta/interactions"):
                sent = json.loads(body)
                assert sent["input"][1] == {"type": "video", "uri": "https://f/abc", "mime_type": "video/mp4"}
                assert sent["response_format"]["schema"]["required"]
                return 200, {}, json.dumps({"status": "completed", "steps": [
                    {"type": "model_output", "content": [{"type": "text", "text": json.dumps(answer)}]}]}).encode()
            if method == "DELETE":
                return 200, {}, b"{}"
            raise AssertionError(url)

        cfg = config_mod._merge(config_mod.DEFAULTS, {"gemini": {"backend": "ai_studio"}})
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), \
                mock.patch.object(gemini, "_request", side_effect=fake), mock.patch.object(gemini.time, "sleep"):
            out = gemini.review(cfg, video, streamer="LACY", hook="cops", transcript="no way", chat_terms=["kekw"])
        self.assertEqual((out["quality"], out["hook_matches"], out["better_hook"]),
                         (9, False, "He did NOT see that coming"))
        self.assertEqual(calls[0][2]["X-Goog-Upload-Command"], "start")
        self.assertEqual(calls[1][2]["X-Goog-Upload-Command"], "upload, finalize")
        self.assertEqual(calls[-1][0], "DELETE")  # cleaned up


class VertexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "sa.json").write_text(json.dumps({"type": "service_account", "project_id": "proj-1",
                                                      "client_email": "x@proj-1.iam", "token_uri": "https://t"}))
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {"gemini": {"service_account_file": str(self.tmp / "sa.json")}})

    def test_configured_by_service_account_file(self):
        self.assertTrue(gemini.configured(self.cfg))
        self.cfg["gemini"]["service_account_file"] = str(self.tmp / "missing.json")
        self.assertFalse(gemini.configured(self.cfg))

    def test_request_and_cleanup(self):
        video = self.tmp / "final.mp4"
        video.write_bytes(b"full")
        answer = {"what_happens": "x", "visual_quality": 7, "spoiler_risk": "none", "is_cutscene": True,
                  "unsafe": False, "hook_matches": True, "better_hook": "", "reason": "r"}
        seen = {}

        class Resp:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(self.payload).encode()

        def fake_urlopen(req, timeout=0):
            seen["url"], seen["auth"] = req.full_url, req.headers.get("Authorization")
            seen["body"] = json.loads(req.data)
            return Resp({"modelVersion": "gemini-3.8-flash", "candidates": [
                {"content": {"parts": [{"text": json.dumps(answer)}]}}]})

        def fake_proxy(v):
            out = v.with_name("final.review.mp4")
            out.write_bytes(b"small")
            return out

        with mock.patch.object(gemini, "make_proxy", side_effect=fake_proxy), \
                mock.patch.object(gemini, "vertex_token", return_value="tok"), \
                mock.patch.object(gemini.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = gemini.review(self.cfg, video, streamer="LACY", hook="h", transcript="t", chat_terms=[])
        self.assertEqual(seen["url"], "https://aiplatform.googleapis.com/v1/projects/proj-1/locations/global/"
                                      "publishers/google/models/gemini-3.8-flash:generateContent")
        self.assertEqual(seen["auth"], "Bearer tok")
        parts = seen["body"]["contents"][0]["parts"]
        self.assertEqual(parts[0]["inlineData"]["mimeType"], "video/mp4")
        self.assertEqual(seen["body"]["generationConfig"]["responseJsonSchema"], gemini.SCHEMA)
        self.assertEqual((out["quality"], out["cutscene"]), (7, True))
        self.assertFalse((self.tmp / "final.review.mp4").exists())  # preview copy removed


class PipelineMergeTests(unittest.TestCase):
    def test_merge_into_clip_and_auto_gate(self):
        tmp = tempfile.mkdtemp()
        cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": tmp},
            "campaigns": [{"id": "c1"}],
            "channels": [{"login": "lacy", "permission": "campaign:c1"}],
        })
        db = DB(Path(tmp) / "t.db")
        video = Path(tmp) / "final.mp4"
        video.write_bytes(b"x")
        now = time.time()
        cid = db.insert("clips", channel="lacy", status="rendering", final_path=str(video), quality=4,
                        spoiler_risk="none", hook="h", transcript="t",
                        ai_notes=json.dumps({"source": "claude-opus-5-5", "claude_quality": 4,
                                             "claude_spoiler": "none", "claude_unsafe": False}),
                        created_at=now, updated_at=now)
        with mock.patch.object(gemini, "configured", return_value=True), \
                mock.patch.object(gemini, "review", return_value=dict(GEM)):
            self.assertTrue(pipeline.gemini_review(db, cfg, cid))
        row = dict(db.one("SELECT * FROM clips WHERE id = ?", (cid,)))
        notes = json.loads(row["ai_notes"])
        self.assertEqual((row["quality"], notes["claude_quality"], notes["gemini"]["quality"]), (6, 4, 8))
        self.assertTrue(notes["disagree"])  # 4 vs 8
        ok, why = guardrails.auto_gate(cfg, cfg["channels"][0], {**row, "final_path": str(video)}, notes)
        self.assertFalse(ok)
        self.assertIn("disagree", why)
        # Gemini failing keeps Claude's verdict and records the error
        with mock.patch.object(gemini, "configured", return_value=True), \
                mock.patch.object(gemini, "review", side_effect=gemini.GeminiError("quota")):
            self.assertFalse(pipeline.gemini_review(db, cfg, cid))
        self.assertIn("quota", json.loads(db.one("SELECT ai_notes FROM clips WHERE id = ?", (cid,))["ai_notes"])["gemini_error"])
        # a well-viewed Twitch viewer clip: the views break the tie and the gate opens
        db.update("clips", cid, source_views=1616, source_pct=0.97)
        with mock.patch.object(gemini, "configured", return_value=True), \
                mock.patch.object(gemini, "review", return_value=dict(GEM)):
            self.assertTrue(pipeline.gemini_review(db, cfg, cid))
        row = dict(db.one("SELECT * FROM clips WHERE id = ?", (cid,)))
        notes = json.loads(row["ai_notes"])
        self.assertEqual((row["quality"], notes["disagree"]), (8, False))
        self.assertIn("1,616 views", notes["tiebreak"])
        ok, why = guardrails.auto_gate(cfg, cfg["channels"][0], row, notes)
        self.assertTrue(ok, why)



class HookSwapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": str(self.tmp)},
            "campaigns": [{"id": "c1"}],
            "channels": [{"login": "lacy", "permission": "campaign:c1"}],
        })
        self.db = DB(self.tmp / "t.db")
        src = self.tmp / "src.mp4"
        src.write_bytes(b"src")
        self.video = self.tmp / "final.mp4"
        self.video.write_bytes(b"first render")
        now = time.time()
        miss = {**GEM, "quality": 7, "hook_matches": False, "better_hook": "Tiger ambush at the gas station"}
        self.cid = self.db.insert(
            "clips", channel="lacy", status="rendering", source=str(src), start_s=0, duration_s=20,
            final_path=str(self.video), quality=7, spoiler_risk="none", hook="LACY steals a jet", transcript="t",
            words_json="[]", ai_notes=json.dumps({"source": "claude-opus-5-5", "claude_quality": 7,
                                                  "claude_spoiler": "none", "claude_unsafe": False,
                                                  "gemini": miss}),
            created_at=now, updated_at=now)
        self.hooks_seen = []

    def review(self, cfg, path, streamer, hook, transcript, chat_terms):
        self.hooks_seen.append(hook)
        return {**GEM, "quality": 7, "hook_matches": True}

    def render(self, db, cfg, clip_id, ch, src, start, dur, words, hook):
        self.video.write_bytes(b"render with " + hook.encode())
        db.update("clips", clip_id, final_path=str(self.video))
        return self.video

    def test_swaps_once_and_rechecks(self):
        with mock.patch.object(gemini, "configured", return_value=True), \
                mock.patch.object(gemini, "review", side_effect=self.review), \
                mock.patch.object(pipeline, "render_final", side_effect=self.render):
            self.assertTrue(pipeline.swap_hook(self.db, self.cfg, self.cid))
            self.assertFalse(pipeline.swap_hook(self.db, self.cfg, self.cid))  # once per clip
        row = dict(self.db.one("SELECT * FROM clips WHERE id = ?", (self.cid,)))
        notes = json.loads(row["ai_notes"])
        self.assertEqual(row["hook"], "Tiger ambush at the gas station")
        self.assertEqual(notes["hook_swapped"], {"from": "LACY steals a jet", "to": "Tiger ambush at the gas station"})
        self.assertEqual(self.hooks_seen, ["Tiger ambush at the gas station"])  # Gemini re-checked the new one
        self.assertTrue(notes["gemini"]["hook_matches"])
        self.assertEqual(self.video.read_bytes(), b"render with Tiger ambush at the gas station")
        self.assertFalse((self.tmp / "final.first.mp4").exists())
        ok, why = guardrails.auto_gate(self.cfg, self.cfg["channels"][0], row, notes)
        self.assertTrue(ok, why)

    def test_leaves_your_clips_alone_and_survives_a_failed_render(self):
        self.db.update("clips", self.cid, decided_by="human")
        self.assertFalse(pipeline.swap_hook(self.db, self.cfg, self.cid))
        self.db.update("clips", self.cid, decided_by="")
        with mock.patch.object(pipeline, "render_final", side_effect=RuntimeError("ffmpeg")):
            self.assertFalse(pipeline.swap_hook(self.db, self.cfg, self.cid))
        row = dict(self.db.one("SELECT * FROM clips WHERE id = ?", (self.cid,)))
        self.assertEqual(row["hook"], "LACY steals a jet")
        self.assertNotIn("hook_swapped", json.loads(row["ai_notes"]))
        self.assertEqual(self.video.read_bytes(), b"first render")  # first render kept


if __name__ == "__main__":
    unittest.main()
