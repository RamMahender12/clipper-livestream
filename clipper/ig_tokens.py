"""Instagram tokens that don't run out.

The token you pasted (IG_ACCESS_TOKEN) is a 60-day Facebook user token, and Meta documents no way
for a server to extend one. A Page access token made from it does not expire ("Long-lived Page
access token do not have an expiration date", developers.facebook.com/docs/facebook-login/guides/
access-tokens/get-long-lived), so each Instagram account posts with its Page's token instead,
kept in secrets/instagram_tokens.json.

TokenKeeper runs every few hours: it (re)makes Page tokens while the user token still works, drops
any that stopped working (posting then falls back to the user token), and warns on Telegram well
before anything needs you: the user token's expiry and Meta's 90-day "data access" window, after
which you log in to the app again.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config as config_mod

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com"

RENEW_STEPS = (
    "To renew: developers.facebook.com/tools/explorer -> app GTA6 Clipper -> Generate Access Token "
    "(same permissions) -> open it in the Access Token Debugger -> Extend Access Token -> put the new "
    "token in secrets/clipper.env as IG_ACCESS_TOKEN and redeploy. The Page tokens are then remade "
    "automatically.")


def tokens_path(cfg: dict) -> Path:
    return config_mod.resolve(cfg, cfg.get("instagram", {}).get("tokens_file", "secrets/instagram_tokens.json"))


def load(cfg: dict) -> dict:
    try:
        return json.loads(tokens_path(cfg).read_text())
    except (OSError, ValueError):
        return {}


def save(cfg: dict, data: dict) -> None:
    path = tokens_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def user_token(cfg: dict) -> str:
    return os.environ.get(cfg.get("instagram", {}).get("access_token_env", "IG_ACCESS_TOKEN"), "").strip()


def token_for(cfg: dict, ig_user_id: str) -> str:
    """The token to post with for one Instagram account: its Page token, else the user token."""
    entry = load(cfg).get(str(ig_user_id)) or {}
    return entry.get("token") or user_token(cfg)


def _get(cfg: dict, path: str, **params) -> dict:
    ver = cfg.get("instagram", {}).get("graph_version", "v27.0")
    url = f"{GRAPH}/{ver}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def debug(cfg: dict, token: str) -> dict:
    """Meta's view of a token: type, is_valid, expires_at (0 = never), data_access_expires_at."""
    return _get(cfg, "debug_token", input_token=token, access_token=token).get("data", {})


def ig_accounts(cfg: dict) -> dict[str, str]:
    """Instagram account id -> channel login, for every channel that posts to Instagram."""
    out = {}
    for ch in cfg.get("channels", []):
        ig = str(ch.get("instagram_user_id") or "").strip()
        if ig:
            out[ig] = ch["login"]
    if not out and str(cfg.get("instagram", {}).get("ig_user_id", "")).strip():
        out[str(cfg["instagram"]["ig_user_id"])] = "default"
    return out


def make_page_tokens(cfg: dict) -> list[str]:
    """Ask Meta for each Page's token (needs a working user token) and store the ones we post with."""
    tok = user_token(cfg)
    if not tok:
        return ["no IG_ACCESS_TOKEN: nothing to make Page tokens from"]
    wanted = ig_accounts(cfg)
    pages = _get(cfg, "me/accounts", fields="name,access_token,instagram_business_account",
                 limit="100", access_token=tok).get("data", [])
    data = load(cfg)
    lines = []
    for p in pages:
        ig = str((p.get("instagram_business_account") or {}).get("id", ""))
        if ig in wanted and p.get("access_token"):
            data[ig] = {"page": p.get("name", ""), "channel": wanted[ig], "token": p["access_token"],
                        "made_at": time.time()}
            lines.append(f"{wanted[ig]}: Page token for '{p.get('name', '')}' saved")
    for ig, login in wanted.items():
        if ig not in data:
            lines.append(f"{login}: no Page linked to Instagram account ...{ig[-4:]} (posting uses the user token)")
    save(cfg, data)
    return lines


def _days(ts: float | int | None, now: float) -> float | None:
    return None if not ts else (float(ts) - now) / 86400


def check(cfg: dict, now: float | None = None) -> tuple[list[str], list[str]]:
    """(status lines, warnings). Drops Page tokens Meta no longer accepts."""
    now = now or time.time()
    warn_days = float(cfg.get("instagram", {}).get("warn_days", 14))
    lines, warnings = [], []
    tok = user_token(cfg)
    if tok:
        d = debug(cfg, tok)
        exp, data_exp = _days(d.get("expires_at"), now), _days(d.get("data_access_expires_at"), now)
        lines.append(f"user token: {'valid' if d.get('is_valid') else 'INVALID'}"
                     f", expires {'never' if exp is None else f'in {exp:.0f} days'}"
                     f", data access {'n/a' if data_exp is None else f'for {data_exp:.0f} more days'}")
        if not d.get("is_valid"):
            warnings.append("The Instagram user token no longer works. " + RENEW_STEPS)
        elif exp is not None and exp < warn_days:
            warnings.append(f"The Instagram user token expires in {exp:.0f} days. Posting keeps working on "
                            f"the Page tokens, but renew it so they can be remade if needed. " + RENEW_STEPS)
    data = load(cfg)
    changed = False
    for ig, login in ig_accounts(cfg).items():
        entry = data.get(ig)
        if not entry:
            lines.append(f"{login}: no Page token (posting uses the user token)")
            continue
        d = debug(cfg, entry["token"])
        exp, data_exp = _days(d.get("expires_at"), now), _days(d.get("data_access_expires_at"), now)
        if not d.get("is_valid"):
            data.pop(ig)
            changed = True
            warnings.append(f"{login}: the Instagram Page token stopped working; posting falls back to the "
                            "user token and a new Page token is made on the next check.")
            continue
        lines.append(f"{login}: Page token valid, expires {'never' if exp is None else f'in {exp:.0f} days'}"
                     f", data access {'n/a' if data_exp is None else f'for {data_exp:.0f} more days'}")
        if exp is not None and exp < warn_days:
            warnings.append(f"{login}: the Instagram Page token expires in {exp:.0f} days. " + RENEW_STEPS)
        elif data_exp is not None and data_exp < warn_days:
            warnings.append(f"{login}: Meta's 90-day data access for Instagram ends in {data_exp:.0f} days; "
                            "after that posting stops until you log in to the app again. " + RENEW_STEPS)
    if changed:
        save(cfg, data)
    return lines, warnings


def keep_once(cfg: dict, now: float | None = None) -> tuple[list[str], list[str]]:
    """Make missing Page tokens while the user token works, then check everything."""
    lines: list[str] = []
    have = load(cfg)
    if user_token(cfg) and any(ig not in have for ig in ig_accounts(cfg)):
        try:
            lines += make_page_tokens(cfg)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            lines.append(f"couldn't make Page tokens: {str(exc)[:120]}")
    more, warnings = check(cfg, now)
    return lines + more, warnings


class TokenKeeper(threading.Thread):
    def __init__(self, cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name="ig-tokens")
        self.cfg, self.stop_event = cfg, stop
        self.last_warned: dict[str, float] = {}

    def run(self) -> None:
        hours = float(self.cfg.get("instagram", {}).get("check_hours", 6))
        self.stop_event.wait(60)
        while not self.stop_event.is_set():
            try:
                lines, warnings = keep_once(self.cfg)
                for line in lines:
                    log.info("instagram tokens: %s", line)
                self._warn(warnings)
            except Exception:
                log.exception("instagram token check failed")
            self.stop_event.wait(hours * 3600)

    def _warn(self, warnings: list[str]) -> None:
        from . import notify

        now = time.time()
        for w in warnings:
            key = w[:60]
            if now - self.last_warned.get(key, 0) < 86400:  # once a day per problem
                continue
            if notify.send("⚠️ " + w):
                self.last_warned[key] = now
