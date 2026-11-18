"""Shared Claude call that returns schema-validated JSON, with a spend meter and a daily cap.

Every call records its tokens and estimated cost (table ai_usage). Once today's spend (UTC) reaches
[ai].daily_budget_usd, calls stop until midnight UTC and Telegram says so once: the clipper keeps
running on template packaging and nothing is auto-approved, instead of quietly draining credits.
Running out of credits is reported the same way.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

# $ per million tokens (input, output); cache reads ~0.1x input. From the Claude API model notes.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
DEFAULT_PRICE = (4.0, 20.0)  # unknown model: assume Opus pricing, so the cap errs on the safe side

USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_usage (
    ts REAL NOT NULL,
    day TEXT NOT NULL,
    purpose TEXT DEFAULT '',
    model TEXT DEFAULT '',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ai_usage_day ON ai_usage(day);
"""
_lock = threading.Lock()
_alerted: dict[str, float] = {}


def have_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.path.exists(os.path.expanduser("~/.config/anthropic")))


def price(model: str) -> tuple[float, float]:
    for name, p in PRICES.items():
        if model.startswith(name):
            return p
    return DEFAULT_PRICE


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    pin, pout = price(model)
    return (input_tokens * pin + cache_write * pin * 1.25 + cache_read * pin * 0.1 + output_tokens * pout) / 1e6


def _db(cfg: dict) -> sqlite3.Connection:
    from .config import data_dir

    path = data_dir(cfg) / "clipper.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.executescript(USAGE_SCHEMA)
    return conn


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def spent_today(cfg: dict) -> float:
    with _lock:
        conn = _db(cfg)
        try:
            row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM ai_usage WHERE day = ?", (_today(),)).fetchone()
        finally:
            conn.close()
    return float(row[0] or 0)


def record(cfg: dict, purpose: str, model: str, usage) -> float:
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    c = cost_usd(model, inp, out, cr, cw)
    with _lock:
        conn = _db(cfg)
        try:
            conn.execute("INSERT INTO ai_usage (ts, day, purpose, model, input_tokens, output_tokens, "
                         "cache_read_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (time.time(), _today(), purpose, model, inp + cw, out, cr, c))
            conn.commit()
        finally:
            conn.close()
    return c


def _alert_once(key: str, text: str, every: float = 6 * 3600) -> None:
    now = time.time()
    if now - _alerted.get(key, 0) < every:
        return
    _alerted[key] = now
    log.error(text)
    try:
        from . import notify

        notify.send("⚠️ " + text)
    except Exception:
        pass


def _thinking(model: str, mode: str | None) -> dict | None:
    """Turn thinking off where the model allows it (fewer output tokens). Opus can't turn it off."""
    if mode != "off":
        return None
    if "haiku" in model:
        return {"type": "disabled"}
    if "sonnet" in model:
        return {"type": "between_tools"}  # Sonnet 5.5's way to switch thinking off (effort high or below)
    return None


def call_json(
    cfg: dict,
    system: str,
    user: str | list,
    schema: dict,
    effort: str | None = None,
    model: str | None = None,
    max_tokens: int = 16000,
    purpose: str = "",
    thinking: str | None = None,
) -> tuple[dict | None, str]:
    """Return (data, model) on success, or (None, reason) where reason is one of no-credentials,
    no-sdk, budget, no-credit, auth, rate-limit, api-<status>, connection, refusal, bad-json."""
    ai = cfg["ai"]
    if not ai.get("enabled") or not have_credentials():
        return None, "no-credentials"
    budget = float(ai.get("daily_budget_usd", 0) or 0)
    if budget:
        spent = spent_today(cfg)
        if spent >= budget:
            _alert_once("budget", f"Claude spend reached today's cap of ${budget:.2f} (${spent:.2f}); AI is paused "
                                  "until 00:00 UTC. Clips still render; nothing is auto-approved meanwhile. "
                                  "Raise [ai].daily_budget_usd if you want more.")
            return None, "budget"
        if spent >= 0.8 * budget:
            _alert_once("budget80", f"Claude spend today is ${spent:.2f}, 80% of the ${budget:.2f} daily cap.")
    try:
        import anthropic
    except ImportError:
        return None, "no-sdk"

    model = model or ai.get("model", "claude-haiku-5-5")
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        output_config={"effort": effort or ai.get("effort", "low"),
                       "format": {"type": "json_schema", "schema": schema}},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if (t := _thinking(model, thinking)) is not None:
        kwargs["thinking"] = t
    if "haiku" not in model:  # Haiku 5.5 has no server-side fallback
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        log.error("Anthropic API key rejected")
        return None, "auth"
    except anthropic.RateLimitError:
        log.warning("Anthropic rate limit")
        return None, "rate-limit"
    except anthropic.APIStatusError as exc:
        text = str(getattr(exc, "message", "") or exc).lower()
        if "credit balance" in text or "billing" in text:
            _alert_once("credit", "Anthropic API credits ran out: Claude calls are failing. Top up at "
                                  "console.anthropic.com -> Billing. Clips keep rendering meanwhile.")
            return None, "no-credit"
        log.warning("Anthropic API error %s", exc.status_code)
        return None, f"api-{exc.status_code}"
    except anthropic.APIConnectionError:
        log.warning("Anthropic API unreachable")
        return None, "connection"

    try:
        c = record(cfg, purpose, getattr(response, "model", model) or model, response.usage)
        log.info("claude %s %s: %s in / %s out tokens, ~$%.4f", purpose or "call", response.model,
                 response.usage.input_tokens, response.usage.output_tokens, c)
    except Exception:  # metering must never break a call
        log.exception("couldn't record Claude usage")
    if response.stop_reason == "refusal":
        return None, "refusal"
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        return json.loads(text), response.model
    except json.JSONDecodeError:
        log.warning("unparseable JSON from Claude (stop_reason=%s)", response.stop_reason)
        return None, "bad-json"
