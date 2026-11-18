import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from clipper import ai, config as config_mod


class FakeResponse:
    def __init__(self, model, text='{"ok": true}', inp=2000, out=300):
        self.model = model
        self.stop_reason = "end_turn"
        self.usage = types.SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0,
                                           cache_creation_input_tokens=0)
        self.content = [types.SimpleNamespace(type="text", text=text)]


class FakeAnthropic(types.ModuleType):
    """Stands in for the anthropic SDK: records request kwargs, can raise the credit error."""

    class APIStatusError(Exception):
        def __init__(self, message, status_code=400):
            super().__init__(message)
            self.message, self.status_code = message, status_code

    class AuthenticationError(Exception): pass
    class RateLimitError(Exception): pass
    class APIConnectionError(Exception): pass

    def __init__(self):
        super().__init__("anthropic")
        self.calls, self.fail_with = [], None
        outer = self

        class Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                if outer.fail_with:
                    raise outer.fail_with
                return FakeResponse(kw["model"])

        class Client:
            def __init__(self):
                self.beta = types.SimpleNamespace(messages=Messages())

        self.Anthropic = Client


class CostTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config_mod._merge(config_mod.DEFAULTS, {"paths": {"data_dir": tempfile.mkdtemp()}})
        self.sdk = FakeAnthropic()
        p = mock.patch.dict(sys.modules, {"anthropic": self.sdk})
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"})
        e.start()
        self.addCleanup(e.stop)
        n = mock.patch("clipper.notify.send", return_value=True)
        self.send = n.start()
        self.addCleanup(n.stop)
        ai._alerted.clear()

    def test_prices_and_cost(self):
        self.assertAlmostEqual(ai.cost_usd("claude-haiku-5-5", 2000, 300), 0.00035)
        self.assertAlmostEqual(ai.cost_usd("claude-opus-5-5", 2000, 1500), 0.038)
        self.assertEqual(ai.price("claude-mystery-9"), ai.DEFAULT_PRICE)  # unknown: priced like Opus

    def test_per_model_request_settings_and_metering(self):
        data, model = ai.call_json(self.cfg, "sys", "hi", {"type": "object"}, purpose="packaging", thinking="off")
        self.assertEqual((data, model), ({"ok": True}, "claude-haiku-5-5"))
        haiku = self.sdk.calls[-1]
        self.assertEqual(haiku["thinking"], {"type": "disabled"})
        self.assertNotIn("fallbacks", haiku)  # Haiku 5.5 has no server-side fallback
        self.assertEqual(haiku["output_config"]["effort"], "low")
        ai.call_json(self.cfg, "sys", "hi", {}, model="claude-sonnet-5-5", purpose="judge", thinking="off")
        sonnet = self.sdk.calls[-1]
        self.assertEqual(sonnet["thinking"], {"type": "between_tools"})
        self.assertEqual(sonnet["fallbacks"], "default")
        ai.call_json(self.cfg, "sys", "hi", {}, model="claude-opus-5-5", thinking="off")
        self.assertNotIn("thinking", self.sdk.calls[-1])  # Opus can't switch it off
        spent = ai.spent_today(self.cfg)
        self.assertAlmostEqual(spent, ai.cost_usd("claude-haiku-5-5", 2000, 300) + ai.cost_usd("claude-sonnet-5-5", 2000, 300)
                               + ai.cost_usd("claude-opus-5-5", 2000, 300), places=6)

    def test_daily_cap_stops_calls_and_alerts_once(self):
        self.cfg["ai"]["daily_budget_usd"] = 0.01
        for _ in range(3):
            ai.call_json(self.cfg, "s", "u", {}, model="claude-opus-5-5")  # $0.014 each
        self.assertEqual(len(self.sdk.calls), 1)  # the first went through; then the cap held
        self.assertEqual(ai.call_json(self.cfg, "s", "u", {})[1], "budget")
        texts = [c.args[0] for c in self.send.call_args_list]
        self.assertEqual(sum("today's cap" in t for t in texts), 1)

    def test_out_of_credits_is_named_and_alerted(self):
        self.sdk.fail_with = FakeAnthropic.APIStatusError("Your credit balance is too low to access the Anthropic API.")
        self.assertEqual(ai.call_json(self.cfg, "s", "u", {}), (None, "no-credit"))
        self.assertEqual(ai.call_json(self.cfg, "s", "u", {}), (None, "no-credit"))
        self.assertEqual(sum("credits ran out" in c.args[0] for c in self.send.call_args_list), 1)


if __name__ == "__main__":
    unittest.main()
