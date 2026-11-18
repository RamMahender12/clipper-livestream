import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clipper import config as config_mod, ig_tokens


class FakeGraph:
    def __init__(self, now):
        self.now = now
        self.valid = {"USER": True, "PAGE-L": True, "PAGE-C": True}
        self.expires = {"USER": now + 60 * 86400, "PAGE-L": 0, "PAGE-C": 0}
        self.data_expires = {"USER": now + 90 * 86400, "PAGE-L": now + 90 * 86400, "PAGE-C": now + 90 * 86400}

    def __call__(self, cfg, path, **params):
        if path == "me/accounts":
            return {"data": [
                {"name": "LACY GTA Clips", "access_token": "PAGE-L", "instagram_business_account": {"id": "111"}},
                {"name": "Clix GTA Clips", "access_token": "PAGE-C", "instagram_business_account": {"id": "222"}},
                {"name": "Unrelated", "access_token": "PAGE-X"}]}
        if path == "debug_token":
            t = params["input_token"]
            return {"data": {"is_valid": self.valid.get(t, False), "expires_at": self.expires.get(t, 0),
                             "data_access_expires_at": self.data_expires.get(t, 0)}}
        raise AssertionError(path)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {
            "instagram": {"tokens_file": str(self.tmp / "ig.json")},
            "channels": [{"login": "lacy", "instagram_user_id": "111"},
                         {"login": "clix", "instagram_user_id": "222"}]})
        self.now = time.time()
        self.graph = FakeGraph(self.now)
        env = mock.patch.dict(os.environ, {"IG_ACCESS_TOKEN": "USER"})
        env.start()
        self.addCleanup(env.stop)
        get = mock.patch.object(ig_tokens, "_get", side_effect=self.graph)
        get.start()
        self.addCleanup(get.stop)

    def test_page_tokens_are_made_used_and_private(self):
        self.assertEqual(ig_tokens.token_for(self.cfg, "111"), "USER")  # before: the user token
        lines = ig_tokens.make_page_tokens(self.cfg)
        self.assertEqual(len(lines), 2)
        self.assertEqual(ig_tokens.token_for(self.cfg, "111"), "PAGE-L")
        self.assertEqual(ig_tokens.token_for(self.cfg, "222"), "PAGE-C")
        self.assertEqual(ig_tokens.token_for(self.cfg, "999"), "USER")
        self.assertNotIn("PAGE-X", (self.tmp / "ig.json").read_text())
        self.assertEqual(oct((self.tmp / "ig.json").stat().st_mode & 0o777), "0o600")

    def test_publisher_posts_with_the_page_token(self):
        from clipper.publishers import get_publisher
        ig_tokens.make_page_tokens(self.cfg)
        pub = get_publisher("instagram", self.cfg, self.cfg["channels"][1])
        self.assertEqual(pub._token(), "PAGE-C")

    def test_all_good_no_warnings(self):
        lines, warnings = ig_tokens.keep_once(self.cfg, self.now)  # makes the missing Page tokens too
        self.assertEqual(warnings, [])
        self.assertTrue(any("lacy: Page token valid, expires never" in l for l in lines))

    def test_dead_page_token_is_dropped_and_remade(self):
        ig_tokens.make_page_tokens(self.cfg)
        self.graph.valid["PAGE-L"] = False
        lines, warnings = ig_tokens.check(self.cfg, self.now)
        self.assertTrue(any("lacy" in w and "stopped working" in w for w in warnings))
        self.assertEqual(ig_tokens.token_for(self.cfg, "111"), "USER")  # falls back right away
        self.graph.valid["PAGE-L"] = True
        ig_tokens.keep_once(self.cfg, self.now)
        self.assertEqual(ig_tokens.token_for(self.cfg, "111"), "PAGE-L")

    def test_warns_two_weeks_ahead(self):
        ig_tokens.make_page_tokens(self.cfg)
        self.graph.expires["USER"] = self.now + 10 * 86400
        self.graph.data_expires["PAGE-C"] = self.now + 5 * 86400
        _, warnings = ig_tokens.check(self.cfg, self.now)
        self.assertTrue(any("user token expires in 10 days" in w for w in warnings))
        self.assertTrue(any(w.startswith("clix: Meta's 90-day data access") for w in warnings))
        self.assertTrue(all("Access Token Debugger" in w for w in warnings))  # says how to fix it

    def test_keeper_warns_once_a_day(self):
        keeper = ig_tokens.TokenKeeper(self.cfg, __import__("threading").Event())
        with mock.patch("clipper.notify.send", return_value=True) as send:
            keeper._warn(["The Instagram user token expires in 3 days."])
            keeper._warn(["The Instagram user token expires in 3 days."])
        self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
