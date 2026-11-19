import tempfile
import time
import unittest
from pathlib import Path

from clipper import config as config_mod, twitch_clips as tc
from clipper.db import DB
from clipper.spikes import SpikeDetector


def replay(det, per_window, start=1_000_000.0):
    """Feed one message per slot, `n` messages in each 10 s window; return the spikes."""
    spikes = []
    for i, n in enumerate(per_window):
        for k in range(n):
            spikes += det.feed(start + i * 10 + k * (9 / max(n, 1)), f"u{i}_{k}", "LOL")
    spikes += det.tick(start + len(per_window) * 10 + 1)
    return spikes


class SmallChatTests(unittest.TestCase):
    cfg = config_mod._merge(config_mod.DEFAULTS, {})
    chat = [1, 2, 1, 0, 1, 2, 1, 1, 0, 1, 2, 1, 1, 0, 1, 6, 1, 1]  # ~1 per 10 s, then a burst of 6

    def test_small_chat_spikes_only_with_its_own_bar(self):
        default = SpikeDetector.from_config(self.cfg, {"login": "big"})
        self.assertEqual(replay(default, self.chat), [])  # 12-message floor: never
        small = SpikeDetector.from_config(self.cfg, {"login": "asianjeff",
                                                     "detect": {"min_messages": 4, "clip_mentions": 2}})
        self.assertEqual(len(replay(small, self.chat)), 1)


class FakeHelix:
    def broadcaster_id(self, login):
        return {"asianjeff": "1", "lacy": "2"}[login]

    def clips(self, bid, since, first=50):
        now = time.time()
        return [{"id": f"c{bid}", "url": "u", "view_count": 4, "title": "lol", "duration": 20.0,
                 "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 600))}]


class SmallViewerClipTests(unittest.TestCase):
    def test_per_streamer_view_minimum(self):
        tmp = tempfile.mkdtemp()
        cfg = config_mod._merge(config_mod.DEFAULTS, {
            "paths": {"data_dir": tmp}, "posting": {"earliest_footage": ""}, "campaigns": [{"id": "c"}],
            "channels": [{"login": "asianjeff", "permission": "campaign:c", "twitch_min_views": 3},
                         {"login": "lacy", "permission": "campaign:c"}]})
        fetched = []

        def fetch(clip, out):
            fetched.append(clip["id"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")
            return out

        tc.harvest_once(DB(Path(tmp) / "t.db"), cfg, FakeHelix(), fetch=fetch)
        self.assertEqual(fetched, ["c1"])  # AsianJeff's 4-view clip counts; LACY's default bar is higher


if __name__ == "__main__":
    unittest.main()
