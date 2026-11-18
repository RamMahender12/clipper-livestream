"""Shared Claude call that returns schema-validated JSON."""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)


def have_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.path.exists(os.path.expanduser("~/.config/anthropic")))


def call_json(
    cfg: dict,
    system: str,
    user: str | list,
    schema: dict,
    effort: str | None = None,
    model: str | None = None,
    max_tokens: int = 16000,
) -> tuple[dict | None, str]:
    """Return (data, model) on success, or (None, reason) where reason is one of
    no-credentials, no-sdk, auth, rate-limit, api-<status>, connection, refusal, bad-json."""
    ai = cfg["ai"]
    if not ai.get("enabled") or not have_credentials():
        return None, "no-credentials"
    try:
        import anthropic
    except ImportError:
        return None, "no-sdk"

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=model or ai.get("model", "claude-opus-5-5"),
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": effort or ai.get("effort", "medium"),
                "format": {"type": "json_schema", "schema": schema},
            },
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        log.error("Anthropic API key rejected")
        return None, "auth"
    except anthropic.RateLimitError:
        log.warning("Anthropic rate limit")
        return None, "rate-limit"
    except anthropic.APIStatusError as exc:
        log.warning("Anthropic API error %s", exc.status_code)
        return None, f"api-{exc.status_code}"
    except anthropic.APIConnectionError:
        log.warning("Anthropic API unreachable")
        return None, "connection"

    if response.stop_reason == "refusal":
        return None, "refusal"
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        return json.loads(text), response.model
    except json.JSONDecodeError:
        log.warning("unparseable JSON from Claude (stop_reason=%s)", response.stop_reason)
        return None, "bad-json"
