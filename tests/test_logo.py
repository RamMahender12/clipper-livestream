import unittest

from clipper import config as config_mod, guardrails
from clipper.render import build_filtergraph


class LogoTest(unittest.TestCase):
    def test_logo_overlaid_before_captions(self):
        g = build_filtergraph("blur", logo=True)
        self.assertIn("[1:v]scale=", g)            # logo is the 2nd input
        self.assertIn("[1:v]scale=540:-1,format=rgba[lg]", g)  # half width, not recoloured
        self.assertIn("[base][lg]overlay=W-w-", g)            # bottom-right
        self.assertIn("[branded]null[v]", g)      # branded frame is what gets output
        self.assertNotIn("[1:v]", build_filtergraph("blur"))

    def test_clipup_channels_resolve_existing_logo(self):
        cfg = config_mod.load()
        chans = [c for c in cfg["channels"] if c.get("permission") == "campaign:clipup"]
        self.assertTrue(chans)
        for ch in chans:
            camp = guardrails.campaign_for(cfg, ch)
            self.assertTrue(config_mod.resolve(cfg, camp["logo"]).is_file(), ch.get("login"))


if __name__ == "__main__":
    unittest.main()
