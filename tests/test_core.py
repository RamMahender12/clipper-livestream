import tempfile
import time
import unittest
from pathlib import Path

from clipper import captions, guardrails, highlights, recorder
from clipper.db import DB
from clipper.spikes import SpikeDetector
from clipper.twitch_chat import parse_privmsg


class SpikeTests(unittest.TestCase):
    def make(self, **kw):
        base = dict(window_seconds=10, baseline_windows=30, warmup_windows=6, min_ratio=2.5,
                    min_z=3.0, min_messages=12, clip_mentions=3, cooldown_seconds=45,
                    keywords=["kekw", "lul"])
        base.update(kw)
        return SpikeDetector(**base)

    def feed_rate(self, det, start, seconds, per_window, text="hello", users=None):
        spikes = []
        for w in range(seconds // 10):
            for i in range(per_window):
                ts = start + w * 10 + i * (10 / max(per_window, 1))
                spikes += det.feed(ts, f"u{i % (users or per_window)}", text)
        return spikes

    def test_quiet_chat_has_no_spikes(self):
        det = self.make()
        self.assertEqual(self.feed_rate(det, 1000, 300, 5), [])

    def test_burst_is_detected_once_with_cooldown(self):
        det = self.make()
        t = 1000
        self.feed_rate(det, t, 120, 5)
        spikes = self.feed_rate(det, t + 120, 30, 40, text="KEKW KEKW")
        spikes += det.tick(t + 200)
        self.assertEqual(len(spikes), 1)
        sp = spikes[0]
        self.assertGreaterEqual(sp.ratio, 2.5)
        self.assertIn("kekw", sp.top_terms)
        self.assertGreater(sp.score, sp.ratio)  # keyword boost

    def test_warmup_blocks_early_spikes(self):
        det = self.make()
        spikes = self.feed_rate(det, 1000, 30, 50)
        self.assertEqual(spikes, [])

    def test_clip_it_mentions_trigger(self):
        det = self.make(min_messages=12)
        t = 1000
        self.feed_rate(det, t, 120, 6)
        spikes = []
        for i in range(6):
            spikes += det.feed(t + 121 + i, f"x{i}", "CLIP IT")
        spikes += det.tick(t + 140)
        self.assertEqual(len(spikes), 1)
        self.assertGreaterEqual(spikes[0].clip_mentions, 3)

    def test_silence_counts_as_zero_baseline(self):
        det = self.make()
        self.feed_rate(det, 1000, 60, 5)
        det.tick(1000 + 600)  # 9 minutes of silence
        self.assertEqual(list(det.history)[-1], 0)


class ChatParseTests(unittest.TestCase):
    def test_parse_plain(self):
        self.assertEqual(
            parse_privmsg(":bob!bob@bob.tmi.twitch.tv PRIVMSG #xqc :KEKW no way"),
            ("bob", "KEKW no way"))

    def test_parse_tags_and_action(self):
        line = "@badge-info=;color=#FF0000 :amy!amy@amy.tmi.twitch.tv PRIVMSG #xqc :\x01ACTION dances\x01"
        self.assertEqual(parse_privmsg(line), ("amy", "dances"))

    def test_non_privmsg(self):
        self.assertIsNone(parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome"))


class CaptionTests(unittest.TestCase):
    def words(self):
        return [captions.Word(0.0, 0.4, "no"), captions.Word(0.4, 0.8, "way"),
                captions.Word(0.8, 1.2, "he"), captions.Word(1.2, 1.6, "did"),
                captions.Word(2.8, 3.1, "that!"), captions.Word(3.2, 3.5, "bro")]

    def test_chunking_breaks_on_size_gap_and_punctuation(self):
        chunks = captions.chunk_words(self.words(), per_chunk=3)
        self.assertEqual([[w.text for w in c] for c in chunks],
                         [["no", "way", "he"], ["did"], ["that!"], ["bro"]])

    def test_ass_has_hook_credit_and_highlight(self):
        ass = captions.build_ass(self.words(), hook="chat lost it", credit="twitch.tv/x {evil}", duration=10)
        self.assertIn("Style: Cap", ass)
        self.assertIn("CHAT LOST IT", ass)
        self.assertIn("twitch.tv/x (evil)", ass)  # braces escaped
        self.assertIn("{\\c&H0000E5FF}NO{\\c&H00FFFFFF} WAY HE", ass)

    def test_whisper_json_parsing_drops_noise(self):
        data = {"transcription": [
            {"offsets": {"from": 0, "to": 500}, "text": " [Music]"},
            {"offsets": {"from": 500, "to": 900}, "text": " hello"},
            {"offsets": {"from": 900, "to": 950}, "text": "."},
        ]}
        ws = captions.words_from_whisper(data)
        self.assertEqual([w.text for w in ws], ["hello."])


class GuardrailTests(unittest.TestCase):
    cfg = {
        "campaigns": [{"id": "c1", "platforms": ["tiktok"], "rate_per_1k": 1.0,
                       "cap_per_clip": 50, "min_views": 1000}],
        "posting": {"max_posts_per_day": {"tiktok": 2}, "min_gap_minutes": 30,
                    "quiet_hours": [], "timezone": "UTC"},
    }

    def test_permission_required(self):
        self.assertFalse(guardrails.can_record({"login": "a"})[0])
        self.assertFalse(guardrails.can_record({"login": "a", "permission": "written:"})[0])
        self.assertTrue(guardrails.can_record({"login": "a", "permission": "own"})[0])

    def test_post_rules(self):
        clip = {"status": "approved", "spoiler_risk": "none"}
        camp_ch = {"login": "a", "permission": "campaign:c1"}
        self.assertTrue(guardrails.can_post(self.cfg, camp_ch, clip, "tiktok")[0])
        self.assertFalse(guardrails.can_post(self.cfg, camp_ch, clip, "youtube")[0])
        self.assertFalse(guardrails.can_post(self.cfg, {"login": "a", "permission": "test"}, clip, "tiktok")[0])
        self.assertFalse(guardrails.can_post(self.cfg, camp_ch, {**clip, "spoiler_risk": "high"}, "tiktok")[0])
        self.assertFalse(guardrails.can_post(self.cfg, camp_ch, {**clip, "status": "ready"}, "tiktok")[0])

    def test_earnings(self):
        camp = self.cfg["campaigns"][0]
        self.assertEqual(guardrails.estimated_earnings(999, camp), 0.0)
        self.assertEqual(guardrails.estimated_earnings(20_000, camp), 20.0)
        self.assertEqual(guardrails.estimated_earnings(2_000_000, camp), 50.0)

    def test_pacing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "t.db")
            now = time.time()
            self.assertTrue(guardrails.pacing_ok(self.cfg, db, "tiktok", now)[0])
            db.insert("posts", clip_id=1, platform="tiktok", status="published", posted_at=now - 600)
            ok, why = guardrails.pacing_ok(self.cfg, db, "tiktok", now)
            self.assertFalse(ok)
            self.assertIn("next slot", why)
            db.insert("posts", clip_id=2, platform="tiktok", status="published", posted_at=now - 7200)
            self.assertIn("daily cap", guardrails.pacing_ok(self.cfg, db, "tiktok", now + 3600)[1])


class LaunchGuardAndCaptionRuleTests(unittest.TestCase):
    def test_pre_release_footage_is_blocked(self):
        cfg = {"posting": {"earliest_footage": "2026-11-18T11:00:00+00:00"}, "campaigns": []}
        ch = {"login": "lacy", "permission": "own"}
        cutoff = 1794999600.0  # 2026-11-18T11:00:00Z
        early = {"status": "approved", "spoiler_risk": "none", "wall_start": cutoff - 60}
        late = {**early, "wall_start": cutoff + 60}
        manual = {**early, "wall_start": None}  # ingested/manual cuts have no wall clock
        self.assertFalse(guardrails.can_post(cfg, ch, early, "export")[0])
        self.assertIn("pre-release", guardrails.can_post(cfg, ch, early, "export")[1])
        self.assertTrue(guardrails.can_post(cfg, ch, late, "export")[0])
        self.assertTrue(guardrails.can_post(cfg, ch, manual, "export")[0])

    def test_channel_caption_rules(self):
        from clipper import metadata

        ch = {"login": "lacy", "display_name": "LACY", "credit": "twitch.tv/lacy",
              "required_tags": ["#LACY"], "caption_must_include": ["LACY", "#LACY"]}
        meta = metadata.template_meta(ch, None, ["kekw"], "hello")
        self.assertEqual(meta.hashtags[0], "LACY")  # required tag first, never truncated away
        self.assertIn("LACY", meta.caption)
        self.assertIn("#LACY", meta.caption)
        many = metadata._merge_tags(["LACY"], [f"t{i}" for i in range(10)])
        self.assertEqual(many[0], "LACY")
        self.assertEqual(len(many), 6)


class SegmentTests(unittest.TestCase):
    def test_covering_and_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name in ("20261118-110000", "20261118-110030", "20261118-110100"):
                (d / f"{name}.ts").write_bytes(b"x")
            t = recorder.segment_start(d / "20261118-110030.ts")
            picked, closed = recorder.covering(d, t + 5, t + 25)
            self.assertEqual([p.stem for _, p in picked], ["20261118-110030"])
            self.assertTrue(closed)
            picked, closed = recorder.covering(d, t + 20, t + 40)
            self.assertEqual(len(picked), 2)
            self.assertFalse(closed)  # newest segment may still be growing


class HighlightTests(unittest.TestCase):
    def test_finds_loud_runs(self):
        series = [-30.0] * 300
        for i in range(100, 104):
            series[i] = -14.0
        for i in range(200, 201):
            series[i] = -10.0  # 1 step only: too short
        moments = highlights.find_moments(series, min_len=2)
        self.assertEqual(len(moments), 1)
        self.assertAlmostEqual(moments[0].at, 100, delta=4)

class CleanCaptionTests(unittest.TestCase):
    def test_masks_and_mutes_swearing_only(self):
        from clipper import captions, render
        self.assertEqual(captions.mask("Are you fucking deadass? SHIT"), "Are you f*****g deadass? S**T")
        self.assertEqual(captions.mask("Dickens flew the cockpit past Scunthorpe, assess it"),
                         "Dickens flew the cockpit past Scunthorpe, assess it")
        W = captions.Word
        words = [W(0.5, 0.8, "fucking"), W(1.0, 1.2, "nice"), W(2.0, 2.3, "Shit!")]
        ass = captions.build_ass(words, hook="what the fuck", duration=5)
        self.assertNotIn("FUCK", ass)
        self.assertIn("F*****G", ass)
        self.assertIn("WHAT THE F**K", ass)
        self.assertIn("FUCKING", captions.build_ass(words, duration=5, censor=False))
        spans = captions.profane_spans(words)
        self.assertEqual(len(spans), 2)
        af = render.audio_filter(spans)
        self.assertTrue(af.startswith("volume=enable='between(t,0.460,0.840)+between(t,1.960,2.340)':volume=0,"))
        self.assertEqual(render.audio_filter([]), "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000")

if __name__ == "__main__":
    unittest.main()
