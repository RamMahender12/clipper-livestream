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
import urllib.error
import urllib.request

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
_ctx = threading.local()


def set_channel(channel: str | None) -> None:
    """Which streamer the following Claude calls on this thread are for (spend per campaign)."""
    _ctx.channel = channel or ""


def current_channel() -> str:
    return getattr(_ctx, "channel", "") or ""

# call_json failures that say nothing about the clip: the same call can work later (the cap resets at
# 00:00 UTC, credits get topped up, the network or a key comes back). These cost nothing, so callers
# try again; refusal, bad-json and other 4xx answers are about the clip and wait for a human.
TEMPORARY = {"budget", "no-credit", "rate-limit", "connection", "auth", "api-429", "api-529"}


def temporary(reason: str) -> bool:
    return reason in TEMPORARY or reason.startswith("api-5")


def available(cfg: dict) -> tuple[bool, str]:
    """Would a Claude call go out right now? Makes no call."""
    ai = cfg["ai"]
    if not ai.get("enabled") or not have_credentials():
        return False, "no-credentials"
    budget = float(ai.get("daily_budget_usd", 0) or 0)
    if budget and spent_today(cfg) >= budget:
        return False, "budget"
    return True, ""


def use_openrouter() -> bool:
    """OpenRouter (free models) is used when its key is set and there's no Anthropic key."""
    return bool(os.environ.get("OPENROUTER_API_KEY")) and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _openrouter_content(user):
    """Anthropic content blocks -> OpenAI-style parts (images become data URIs)."""
    if isinstance(user, str):
        return user
    parts = []
    for b in user:
        if b.get("type") == "image":
            s = b["source"]
            parts.append({"type": "image_url", "image_url": {"url": f"data:{s['media_type']};base64,{s['data']}"}})
        else:
            parts.append(b)
    return parts


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        return json.loads(text[a:b + 1])  # raises JSONDecodeError if there's no object


# ponytail: free model ids churn; override with [ai].openrouter_fallbacks when one disappears
OPENROUTER_FALLBACKS = ["nvidia/nemotron-3.5-super-vl:free", "particle-ai/particle-1:free",
                        "google/gemma-4-26b-a4b-it:free", "inclusionai/ling-3.2-flash:free"]


def _call_openrouter(cfg: dict, system: str, user, schema: dict, max_tokens: int, purpose: str):
    ai = cfg["ai"]
    # free models are often rate-limited upstream (429), so try a few that take images and return JSON;
    # openrouter/free (random model, often bad JSON) only as a last resort
    models = list(dict.fromkeys([ai.get("openrouter_model", "google/gemma-4-31b-it:free"),
                                 *ai.get("openrouter_fallbacks", OPENROUTER_FALLBACKS), "openrouter/free"]))
    sys_msg = system + "\n\nReply with ONLY a JSON object matching this JSON Schema, no other text:\n" + json.dumps(schema)
    last = "connection"
    for model in models:
        payload = json.dumps({"model": model, "max_tokens": min(max_tokens, 4000),
                              "response_format": {"type": "json_object"},
                              "messages": [{"role": "system", "content": sys_msg},
                                           {"role": "user", "content": _openrouter_content(user)}]}).encode()
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions", data=payload,
            headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                log.error("OpenRouter API key rejected")
                return None, "auth"
            log.warning("OpenRouter %s error %s", model, exc.code)
            last = "rate-limit" if exc.code == 429 else f"api-{exc.code}"
            continue
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            log.warning("OpenRouter unreachable")
            continue
        try:
            data = _extract_json(body["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, json.JSONDecodeError):
            msg = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            log.warning("unparseable JSON from OpenRouter model %s: %r", body.get("model", model), msg[:120])
            last = "bad-json"
            continue
        u = body.get("usage") or {}
        try:
            record(cfg, purpose, model, type("U", (), {"input_tokens": u.get("prompt_tokens", 0),
                                                       "output_tokens": u.get("completion_tokens", 0)})())
        except Exception:
            log.exception("couldn't record usage")
        return data, body.get("model", model)
    return None, last


def have_credentials() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
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
    if "channel" not in {r[1] for r in conn.execute("PRAGMA table_info(ai_usage)")}:
        conn.execute("ALTER TABLE ai_usage ADD COLUMN channel TEXT DEFAULT ''")
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
                         "cache_read_tokens, cost_usd, channel) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (time.time(), _today(), purpose, model, inp + cw, out, cr, c, current_channel()))
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
    if use_openrouter():
        return _call_openrouter(cfg, system, user, schema, max_tokens, purpose)
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
