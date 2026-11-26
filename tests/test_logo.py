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


    def test_clipup_caption_always_has_clipup_tag(self):
        from clipper.publishers.base import PostMeta, caption_with_tags
        from clipper.scheduler import with_required_tags
        cfg = config_mod.load()
        ch = next(c for c in cfg["channels"] if c.get("permission") == "campaign:clipup")
        camp = guardrails.campaign_for(cfg, ch)
        for stored in ("funny,gaming", "Clipup,gta6", ""):  # a review edit dropped it / kept it / cleared all
            tags = with_required_tags(ch, camp, stored)
            self.assertEqual(sum(t.lower() == "clipup" for t in tags), 1, stored)
            text = caption_with_tags(PostMeta(title="t", caption="lol", hashtags=tags))
            self.assertIn("#clipup", text.lower())


if __name__ == "__main__":
    unittest.main()
