"""Offline tests for the publishers' pure helpers (no network calls)."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from clipper import config
from clipper import publishers
from clipper.publishers import export, instagram, tiktok, youtube
from clipper.publishers.base import PostMeta, caption_with_tags

MB = 1000 * 1000


def make_cfg(tmp: str) -> dict:
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["paths"]["data_dir"] = str(Path(tmp) / "data")
    cfg["youtube"]["client_secrets_file"] = str(Path(tmp) / "secrets" / "yt_client.json")
    cfg["youtube"]["token_file"] = str(Path(tmp) / "secrets" / "yt_token.json")
    cfg["tiktok"]["token_file"] = str(Path(tmp) / "secrets" / "tt_token.json")
    cfg["tiktok"]["client_key_env"] = "TEST_TT_KEY_UNSET"
    cfg["tiktok"]["client_secret_env"] = "TEST_TT_SECRET_UNSET"
    cfg["instagram"]["access_token_env"] = "TEST_IG_TOKEN_UNSET"
    return cfg


class TempCfgCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.cfg = make_cfg(self.tmp)
        for var in ("TEST_TT_KEY_UNSET", "TEST_TT_SECRET_UNSET", "TEST_IG_TOKEN_UNSET"):
            os.environ.pop(var, None)

    def tearDown(self):
        self._tmp.cleanup()

    def video(self, size: int = 2048) -> Path:
        p = Path(self.tmp) / "in.mp4"
        p.write_bytes(os.urandom(size))
        return p


class RegistryTests(unittest.TestCase):
    def test_all_and_lookup(self):
        self.assertEqual(publishers.ALL, ["export", "youtube", "instagram", "tiktok"])
        cfg = copy.deepcopy(config.DEFAULTS)
        for name in publishers.ALL:
            pub = publishers.get_publisher(name, cfg)
            self.assertEqual(pub.name, name)
        self.assertIsInstance(publishers.get_publisher(" YouTube ", cfg), youtube.YouTubePublisher)
        with self.assertRaises(ValueError):
            publishers.get_publisher("myspace", cfg)


class ExportTests(TempCfgCase):
    def test_writes_video_and_caption(self):
        src = self.video()
        meta = PostMeta(title="He Stole The POLICE Heli?!", caption="Credit: @streamer",
                        hashtags=["gta6", "#gaming"], is_paid_promotion=True, clip_id=42)
        pub = export.ExportPublisher(self.cfg)
        self.assertEqual(pub.ready()[0], True)
        res = pub.publish(src, meta)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.status, "exported")
        dest = Path(res.remote_id)
        self.assertEqual(dest.parent.parent, Path(self.cfg["paths"]["data_dir"]) / "export")
        self.assertRegex(dest.parent.name, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(dest.name, "42_he-stole-the-police-heli.mp4")
        self.assertEqual(dest.read_bytes(), src.read_bytes())
        text = dest.with_suffix(".txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("He Stole The POLICE Heli?!\n"))
        self.assertIn("Credit: @streamer\n\n#gta6 #gaming", text)
        self.assertIn("PAID PROMOTION", text)

    def test_missing_video_is_error_not_exception(self):
        res = export.ExportPublisher(self.cfg).publish(Path(self.tmp) / "nope.mp4", PostMeta("t", "c"))
        self.assertFalse(res.ok)
        self.assertEqual(res.status, "error")

    def test_slugify(self):
        self.assertEqual(export.slugify("  ***  "), "clip")
        self.assertEqual(export.slugify("GTA 6: LOL!!"), "gta-6-lol")
        self.assertLessEqual(len(export.slugify("x" * 200)), 50)


class TikTokChunkTests(unittest.TestCase):
    def check_plan(self, size: int):
        chunk, count = tiktok.tiktok_chunk_plan(size)
        self.assertTrue(1 <= count <= tiktok.TT_MAX_CHUNKS)
        ranges = tiktok.tiktok_chunk_ranges(size, chunk, count)
        self.assertEqual(len(ranges), count)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], size - 1)
        for (a0, a1), (b0, _) in zip(ranges, ranges[1:]):
            self.assertEqual(a1 + 1, b0)  # contiguous, no overlap
        lengths = [e - s + 1 for s, e in ranges]
        if count == 1:
            self.assertEqual(chunk, size)
            self.assertLessEqual(size, tiktok.TT_MAX_CHUNK)
        else:
            self.assertGreaterEqual(chunk, tiktok.TT_MIN_CHUNK)
            self.assertLessEqual(chunk, tiktok.TT_MAX_CHUNK)
            self.assertTrue(all(n == chunk for n in lengths[:-1]))
            self.assertGreaterEqual(lengths[-1], chunk)
            self.assertLessEqual(lengths[-1], tiktok.TT_MAX_FINAL_CHUNK)
            self.assertEqual(count, size // chunk)  # floor rule from the media transfer guide
        if size < tiktok.TT_MIN_CHUNK:
            self.assertEqual((chunk, count), (size, 1))
        if size > tiktok.TT_MAX_CHUNK:
            self.assertGreaterEqual(count, 2)

    def test_doc_example(self):
        # Media transfer guide example: 50,000,123 bytes at 10,000,000 -> 5 chunks.
        self.assertEqual(tiktok.tiktok_chunk_plan(50_000_123), (10_000_000, 5))
        last = tiktok.tiktok_chunk_ranges(50_000_123, 10_000_000, 5)[-1]
        self.assertEqual(last, (40_000_000, 50_000_122))

    def test_small_file_single_chunk(self):
        self.assertEqual(tiktok.tiktok_chunk_plan(3 * MB), (3 * MB, 1))
        self.assertEqual(tiktok.tiktok_chunk_plan(15 * MB), (15 * MB, 1))

    def test_big_preferred_chunk_still_splits_over_64mb(self):
        chunk, count = tiktok.tiktok_chunk_plan(100 * MB, preferred=64 * MB)
        self.assertEqual(count, 2)
        self.assertLessEqual(chunk, tiktok.TT_MAX_CHUNK)

    def test_huge_file_capped_at_1000_chunks(self):
        chunk, count = tiktok.tiktok_chunk_plan(20_000 * MB)
        self.assertLessEqual(count, 1000)
        self.check_plan(20_000 * MB)

    def test_invariants_across_sizes(self):
        for size in [1, 5 * MB, tiktok.TT_MIN_CHUNK - 1, tiktok.TT_MIN_CHUNK, 19_999_999,
                     20 * MB, 63 * MB, 64 * MB, 64 * MB + 1, 99 * MB, 127 * MB, 128 * MB + 7,
                     500 * MB + 3, 4 * 1024 * MB]:
            with self.subTest(size=size):
                self.check_plan(size)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            tiktok.tiktok_chunk_plan(0)

    def test_source_info(self):
        info = tiktok.tiktok_source_info(50_000_123)
        self.assertEqual(info, {"source": "FILE_UPLOAD", "video_size": 50_000_123,
                                "chunk_size": 10_000_000, "total_chunk_count": 5})


class CaptionTests(unittest.TestCase):
    def test_caption_with_tags_limit(self):
        meta = PostMeta(title="t", caption="a" * 3000, hashtags=["x"])
        self.assertEqual(len(caption_with_tags(meta)), 2200)
        self.assertEqual(len(caption_with_tags(meta, 100)), 100)

    def test_youtube_title_and_description(self):
        meta = PostMeta(title="<b>" + "T" * 150, caption="é" * 4000 + " <script>", hashtags=["gta6"])
        body = youtube.youtube_video_body(meta, config.DEFAULTS["youtube"])
        title = body["snippet"]["title"]
        self.assertLessEqual(len(title), 100)
        self.assertNotIn("<", title)
        desc = body["snippet"]["description"]
        self.assertLessEqual(len(desc.encode("utf-8")), 5000)  # 5000 BYTES, é is 2 bytes
        self.assertNotIn("<", desc)
        self.assertNotIn(">", desc)

    def test_youtube_empty_title_fallback(self):
        body = youtube.youtube_video_body(PostMeta(title="  <> ", caption="c"), {})
        self.assertTrue(body["snippet"]["title"])

    def test_instagram_caption_limit(self):
        meta = PostMeta(title="t", caption="z" * 5000)
        params = instagram.instagram_container_params(meta, {})
        self.assertEqual(len(params["caption"]), 2200)

    def test_tiktok_utf16_trim(self):
        text = "😀" * 1500  # 3000 UTF-16 units
        trimmed = tiktok.trim_utf16(text, 2200)
        self.assertEqual(len(trimmed.encode("utf-16-le")) // 2, 2200)
        self.assertEqual(tiktok.trim_utf16("abc", 2200), "abc")


class YouTubeRequestTests(unittest.TestCase):
    def test_init_request(self):
        ycfg = dict(config.DEFAULTS["youtube"], privacy="unlisted", category_id="20")
        meta = PostMeta(title="Clip", caption="cap", hashtags=["gta6"], is_paid_promotion=True)
        url, headers, body = youtube.youtube_init_request(meta, ycfg, 123456)
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                         "https://www.googleapis.com/upload/youtube/v3/videos")
        q = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(q["uploadType"], ["resumable"])
        self.assertEqual(q["part"], ["snippet,status,paidProductPlacementDetails"])
        self.assertEqual(headers["X-Upload-Content-Length"], "123456")
        self.assertEqual(headers["X-Upload-Content-Type"], "video/mp4")
        self.assertNotIn("Authorization", headers)
        data = json.loads(body)
        self.assertEqual(data["snippet"]["categoryId"], "20")
        self.assertEqual(data["snippet"]["tags"], ["gta6"])
        self.assertIn("#gta6", data["snippet"]["description"])
        self.assertEqual(data["status"]["privacyStatus"], "unlisted")
        self.assertIs(data["status"]["selfDeclaredMadeForKids"], False)
        self.assertIs(data["paidProductPlacementDetails"]["hasPaidProductPlacement"], True)

    def test_paid_flag_explicit_false(self):
        body = youtube.youtube_video_body(PostMeta("t", "c"), {})
        self.assertIs(body["paidProductPlacementDetails"]["hasPaidProductPlacement"], False)

    def test_chunk_size_multiple_of_256k(self):
        self.assertEqual(youtube.CHUNK_SIZE % (256 * 1024), 0)

    def test_ranges(self):
        self.assertEqual(youtube.content_range(0, 524288, 2000000), "bytes 0-524287/2000000")
        self.assertEqual(youtube.next_offset("bytes=0-999999"), 1000000)
        self.assertEqual(youtube.next_offset(None), 0)

    def test_pkce(self):
        verifier, challenge = youtube.pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
        self.assertEqual(challenge, expected.decode())
        self.assertNotIn("=", challenge)


class TikTokRequestTests(unittest.TestCase):
    def test_hex_challenge(self):
        self.assertEqual(tiktok.tiktok_code_challenge("abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_post_info_disclosure(self):
        creator = {"comment_disabled": True}
        paid = tiktok.tiktok_post_info(PostMeta("t", "cap", ["gta6"], True), {}, creator, "PUBLIC_TO_EVERYONE")
        self.assertIs(paid["brand_content_toggle"], True)
        self.assertIs(paid["brand_organic_toggle"], False)
        self.assertIs(paid["disable_comment"], True)
        self.assertEqual(paid["title"], "cap\n\n#gta6")
        free = tiktok.tiktok_post_info(PostMeta("t", "cap"), {}, {}, "SELF_ONLY")
        self.assertIs(free["brand_content_toggle"], False)

    def test_scopes_by_mode(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        self.assertNotIn("video.publish", tiktok._scopes(cfg))
        cfg["tiktok"]["mode"] = "direct"
        self.assertIn("video.publish", tiktok._scopes(cfg))


class InstagramHelperTests(unittest.TestCase):
    def test_container_params(self):
        p = instagram.instagram_container_params(PostMeta("t", "c", is_paid_promotion=True),
                                                 {"share_to_feed": False})
        self.assertEqual(p["media_type"], "REELS")
        self.assertEqual(p["upload_type"], "resumable")
        self.assertEqual(p["share_to_feed"], "false")
        self.assertEqual(p["is_paid_partnership"], "true")
        self.assertNotIn("is_paid_partnership", instagram.instagram_container_params(PostMeta("t", "c"), {}))

    def test_quota_parsing(self):
        resp = {"data": [{"quota_usage": 7, "config": {"quota_total": 100, "quota_duration": 86400}}]}
        self.assertEqual(instagram.publishing_quota(resp), (7, 100))
        self.assertEqual(instagram.publishing_quota({"quota_usage": 2, "config": {"quota_total": 50}}), (2, 50))

    def test_insights_parsing(self):
        resp = {"data": [{"name": "views", "values": [{"value": 1234}]},
                         {"name": "shares", "total_value": {"value": 5}}]}
        self.assertEqual(instagram.parse_insights(resp), {"views": 1234, "shares": 5})


class ReadinessTests(TempCfgCase):
    """Missing credentials -> (False, steps) and publish() returns an error result, no network."""

    def test_youtube_not_ready(self):
        pub = youtube.YouTubePublisher(self.cfg)
        ok, msg = pub.ready()
        self.assertFalse(ok)
        self.assertIn("Desktop app", msg)
        res = pub.publish(self.video(), PostMeta("t", "c"))
        self.assertFalse(res.ok)
        self.assertEqual(res.status, "error")

    def test_youtube_web_client_rejected(self):
        path = Path(self.cfg["youtube"]["client_secrets_file"])
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"web": {"client_id": "x"}}))
        ok, msg = youtube.YouTubePublisher(self.cfg).ready()
        self.assertFalse(ok)
        self.assertIn("installed", msg)

    def test_instagram_not_ready(self):
        pub = instagram.InstagramPublisher(self.cfg)
        ok, msg = pub.ready()
        self.assertFalse(ok)
        self.assertIn("ig_user_id", msg)
        self.cfg["instagram"]["ig_user_id"] = "1789"
        ok, msg = pub.ready()
        self.assertFalse(ok)
        self.assertIn("TEST_IG_TOKEN_UNSET", msg)
        with mock.patch.dict(os.environ, {"TEST_IG_TOKEN_UNSET": "IGAAfake"}):
            ok, msg = pub.ready()
            self.assertFalse(ok)
            self.assertIn("Facebook Login", msg)
        res = pub.publish(self.video(), PostMeta("t", "c"))
        self.assertFalse(res.ok)
        self.assertEqual(res.status, "error")

    def test_tiktok_not_ready_and_direct_warning(self):
        pub = tiktok.TikTokPublisher(self.cfg)
        ok, msg = pub.ready()
        self.assertFalse(ok)
        self.assertIn("Login Kit", msg)
        res = pub.publish(self.video(), PostMeta("t", "c"))
        self.assertFalse(res.ok)
        self.assertEqual(res.status, "error")
        env = {"TEST_TT_KEY_UNSET": "k", "TEST_TT_SECRET_UNSET": "s"}
        token = Path(self.cfg["tiktok"]["token_file"])
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(json.dumps({"refresh_token": "r", "scope": "user.info.basic,video.upload,video.publish"}))
        with mock.patch.dict(os.environ, env):
            ok, msg = pub.ready()
            self.assertTrue(ok, msg)
            self.cfg["tiktok"]["mode"] = "direct"
            ok, msg = pub.ready()
            self.assertTrue(ok)
            self.assertIn("SELF_ONLY", msg)


if __name__ == "__main__":
    unittest.main()
