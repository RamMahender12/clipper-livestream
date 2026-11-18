"""New-campaign alerts: ping Telegram when a new GTA 6 campaign appears on
Content Rewards, so you can join before its budget runs out.

How: every `interval_minutes`, read Content Rewards' public sitemap (the
list of pages it publishes for search engines; robots.txt allows it). For
each campaign link not seen before, read that one public campaign page,
pull the campaign's own data (name, payout, platforms, budget, status) and
alert if it matches the keywords and pays on a platform you post to.
No login, no private API, a polite pace, and nothing about other clippers
is stored.
"""
from __future__ import annotations

import html as html_mod
import logging
import re
import threading
import time
import urllib.error
import urllib.request

from . import notify
from .db import DB

log = logging.getLogger(__name__)

SITEMAP = "https://contentrewards.com/sitemap.xml"
CAMPAIGN_URL = "https://contentrewards.com/c/campaigns/{id}"
USER_AGENT = "Mozilla/5.0 (compatible; gta6clipper-campaign-watch/1.0; personal use)"
_CAMPAIGN_RE = re.compile(r"/c/campaigns/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_campaigns (
    id TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    name TEXT DEFAULT '',
    matched INTEGER DEFAULT 0,
    alerted INTEGER DEFAULT 0
);
"""


def _fetch(url: str, timeout: float = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def sitemap_campaign_ids(xml: str) -> list[str]:
    """Campaign ids in sitemap order, de-duplicated."""
    out: list[str] = []
    for cid in _CAMPAIGN_RE.findall(xml):
        if cid not in out:
            out.append(cid)
    return out


def _first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


def _clean(text: str) -> str:
    """React's serialized data escapes a leading '$' as '$$'."""
    text = html_mod.unescape(text or "").strip()
    return "$" + text[2:] if text.startswith("$$") else text


def parse_campaign(page: str) -> dict:
    """The campaign's own fields from its public page (server-rendered data)."""
    raw = page.replace('\\"', '"')
    title = _first(r'property="og:title"[^>]*content="([^"]*)"', page) or ""
    desc = _first(r'property="og:description"[^>]*content="([^"]*)"', page) or ""
    # the campaign object carries payoutType; read the fields near it
    anchor = raw.find('"payoutType"')
    block = raw[max(0, anchor - 3000): anchor + 3000] if anchor >= 0 else raw

    def num(key: str) -> int | None:
        v = _first(r'"%s"\s*:\s*(-?\d+)' % key, block)
        return int(v) if v is not None else None

    platforms = _first(r'"platforms"\s*:\s*\[([^\]]*)\]', raw) or ""
    budget = num("budgetCents")
    if budget is None:
        b = _first(r'"budget"\s*:\s*(\d+)', raw)
        budget = int(b) if b else None
    return {
        "title": html_mod.unescape(title).strip(),
        "description": html_mod.unescape(desc).strip(),
        "name": _clean(_first(r'"name"\s*:\s*"([^"]{1,200})"\s*,\s*"organizationExperienceId"', raw) or "")
                or _clean(title),
        "organization": _clean(_first(r'"organizationName"\s*:\s*"([^"]{1,120})"', block) or ""),
        "payout_type": _first(r'"payoutType"\s*:\s*"([^"]+)"', block) or "",
        "rate_cents": num("rateCents"),
        "max_payout_cents": num("maxPayoutCents"),
        "min_payout_cents": num("minPayoutCents"),
        "amount_cents": num("amountCents"),
        "budget_cents": budget,
        "status": _first(r'"status"\s*:\s*"(active|paused|ended|scheduled|draft|completed)"', block) or "",
        "requires_application": _first(r'"requiresApplication"\s*:\s*(true|false)', block) == "true",
        "platforms": [p.strip().strip('"') for p in platforms.split(",") if p.strip().strip('"')],
    }


def matches(c: dict, wcfg: dict) -> bool:
    text = f"{c['title']} {c['name']} {c['description']}".lower()
    if not any(k.lower() in text for k in wcfg.get("keywords", [])):
        return False
    wanted = [p.lower() for p in wcfg.get("platforms_any", [])]
    if wanted and not any(p in wanted for p in c["platforms"]):
        return False
    if c["status"] and c["status"] not in ("active", "scheduled"):
        return False
    return True


def _usd(cents: int | None) -> str:
    if cents is None:
        return "?"
    return f"${cents / 100:,.2f}".replace(".00", "")


def alert_text(c: dict, cid: str) -> str:
    pay = c["payout_type"]
    if pay == "cpm" and c["rate_cents"] is not None:
        terms = f"{_usd(c['rate_cents'])} per 1K views"
        if c["max_payout_cents"]:
            terms += f", up to {_usd(c['max_payout_cents'])} per clip"
        if c["min_payout_cents"]:
            terms += f" (min {_usd(c['min_payout_cents'])})"
    elif "pool" in pay.lower():
        terms = f"Pool {_usd(c['amount_cents'] or c['budget_cents'])}, split by views"
    else:
        terms = pay or "see page"
    nice = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok", "x": "X"}
    lines = [
        "New GTA 6 campaign",
        f"{c['name'] or c['title']}" + (f" (by {c['organization']})" if c["organization"] else ""),
        f"Pays: {terms}",
    ]
    if c["budget_cents"]:
        lines.append(f"Budget: {_usd(c['budget_cents'])}")
    if c["platforms"]:
        lines.append("Platforms: " + ", ".join(nice.get(p, p) for p in c["platforms"]))
    lines.append("Needs an application" if c["requires_application"] else "Instant join")
    if c["status"] and c["status"] != "active":
        lines.append(f"Status: {c['status']}")
    lines += [CAMPAIGN_URL.format(id=cid), "",
              "Joined? Tell Claude the streamer and campaign to add it "
              "(Reach-style briefs need a fresh YouTube + Instagram pair)."]
    return "\n".join(lines)


def run_once(db: DB, cfg: dict, send=notify.send) -> list[str]:
    wcfg = cfg["campaign_watch"]
    db.conn.executescript(SCHEMA)
    xml = _fetch(SITEMAP)
    ids = sitemap_campaign_ids(xml)
    seen = {r["id"] for r in db.query("SELECT id FROM seen_campaigns")}
    now = time.time()
    lines: list[str] = []
    if not seen:
        # first run: remember everything that exists today, alert only on newer ones
        with db._lock:
            db.conn.executemany("INSERT OR IGNORE INTO seen_campaigns (id, first_seen) VALUES (?, ?)",
                                [(cid, now) for cid in ids])
            db.conn.commit()
        msg = (f"Campaign alerts are on. Watching Content Rewards ({len(ids)} campaigns today). "
               f"You'll get a message when a new campaign matching {', '.join(wcfg['keywords'][:3])} "
               f"appears that pays on {' or '.join(p.title() for p in wcfg['platforms_any'])}.")
        send(msg)
        return [f"seeded {len(ids)} existing campaigns"]
    new = [cid for cid in ids if cid not in seen][: int(wcfg.get("max_pages_per_run", 15))]
    for cid in new:
        try:
            c = parse_campaign(_fetch(CAMPAIGN_URL.format(id=cid)))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("campaign %s fetch failed: %s", cid, exc)
            continue  # retried next run (not marked seen)
        hit = matches(c, wcfg)
        alerted = bool(hit and send(alert_text(c, cid)))
        db.execute("INSERT OR REPLACE INTO seen_campaigns (id, first_seen, name, matched, alerted) "
                   "VALUES (?, ?, ?, ?, ?)", (cid, now, c["name"] or c["title"], int(hit), int(alerted)))
        lines.append(f"{'MATCH' if hit else 'skip '} {c['name'] or c['title']} [{', '.join(c['platforms'])}]")
        time.sleep(float(wcfg.get("page_delay_seconds", 2)))
    return lines


class CampaignWatcher(threading.Thread):
    def __init__(self, db: DB, cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name="campaign-watch")
        self.db, self.cfg, self.stop_event = db, cfg, stop

    def run(self) -> None:
        interval = float(self.cfg["campaign_watch"].get("interval_minutes", 20)) * 60
        self.stop_event.wait(30)  # let the rest of the app start first
        while not self.stop_event.is_set():
            try:
                for line in run_once(self.db, self.cfg):
                    log.info("campaign watch: %s", line)
            except Exception:
                log.exception("campaign watch pass failed")
            self.stop_event.wait(interval)
