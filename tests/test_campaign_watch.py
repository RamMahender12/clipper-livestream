import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipper import campaign_watch as cw, config as config_mod
from clipper.db import DB

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"


def sitemap(*ids):
    return "<urlset>" + "".join(
        f"<url><loc>https://contentrewards.com/c/campaigns/{i}</loc></url>" for i in ids) + "</urlset>"


def page(name, platforms, payout="cpm", rate=150, status="active", org="Agency"):
    plats = ",".join(f'\\"{p}\\"' for p in platforms)
    return (f'<meta property="og:title" content="{name} by {org}">'
            f'<meta property="og:description" content="Clip the GTA 6 stream">'
            f'<script>self.__next_f.push([1,"{{\\"platforms\\":[{plats}],\\"budget\\":250000,'
            f'\\"name\\":\\"{name}\\",\\"organizationExperienceId\\":\\"exp_1\\",'
            f'\\"organizationName\\":\\"{org}\\",\\"maxPayoutCents\\":20000,\\"minPayoutCents\\":500,'
            f'\\"payoutType\\":\\"{payout}\\",\\"rateCents\\":{rate},\\"amountCents\\":null,'
            f'\\"requiresApplication\\":false,\\"status\\":\\"{status}\\"}}"])</script>')


class ParseTests(unittest.TestCase):
    cfg = config_mod.DEFAULTS["campaign_watch"]

    def test_sitemap_ids(self):
        xml = sitemap(A, B) + f"<loc>https://contentrewards.com/c/campaigns/{A}</loc>"
        self.assertEqual(cw.sitemap_campaign_ids(xml), [A, B])

    def test_parse_and_match(self):
        c = cw.parse_campaign(page("$$5,000 Kai GTA VI Clipping", ["youtube", "tiktok"]))
        self.assertEqual(c["name"], "$5,000 Kai GTA VI Clipping")
        self.assertEqual((c["payout_type"], c["rate_cents"], c["max_payout_cents"], c["budget_cents"]),
                         ("cpm", 150, 20000, 250000))
        self.assertEqual(c["platforms"], ["youtube", "tiktok"])
        self.assertTrue(cw.matches(c, self.cfg))
        text = cw.alert_text(c, A)
        self.assertIn("$1.50 per 1K views, up to $200 per clip (min $5)", text)
        self.assertIn("Platforms: YouTube, TikTok", text)
        self.assertIn(A, text)

    def test_filters(self):
        tiktok_only = cw.parse_campaign(page("GTA 6 Clips", ["tiktok"]))
        not_gta = cw.parse_campaign(page("Podcast clipping", ["youtube"]).replace("GTA 6", "podcast"))
        ended = cw.parse_campaign(page("GTA 6 Clips", ["youtube"], status="ended"))
        for c in (tiktok_only, not_gta, ended):
            self.assertFalse(cw.matches(c, self.cfg), c["name"])


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.db = DB(Path(tmp) / "t.db")
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {"campaign_watch": {"page_delay_seconds": 0}})
        self.sent = []

    def run_with(self, pages):
        def fake_fetch(url, timeout=30):
            return pages[url]
        with mock.patch.object(cw, "_fetch", side_effect=fake_fetch):
            return cw.run_once(self.db, self.cfg, send=lambda t: self.sent.append(t) or True)

    def test_seed_then_alert_only_new_matches(self):
        self.run_with({cw.SITEMAP: sitemap(A)})
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Campaign alerts are on", self.sent[0])
        pages = {cw.SITEMAP: sitemap(A, B, C),
                 cw.CAMPAIGN_URL.format(id=B): page("Lacy GTA VI Round 2", ["instagram", "youtube"]),
                 cw.CAMPAIGN_URL.format(id=C): page("Skincare UGC", ["tiktok"]).replace("GTA 6", "skin")}
        self.run_with(pages)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Lacy GTA VI Round 2", self.sent[1])
        self.run_with(pages)  # nothing new the second time
        self.assertEqual(len(self.sent), 2)


if __name__ == "__main__":
    unittest.main()
