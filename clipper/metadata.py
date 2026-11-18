"""Hooks, titles, captions and a spoiler/safety check for each clip, written by Claude.

This is the "originality layer": platforms demote plain re-uploads, so every
clip gets its own hook, captions and a suggested one-line take. Without an
API key it falls back to simple templates so the pipeline still runs.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You write packaging for short vertical clips (TikTok, Reels, Shorts) cut from live Twitch streams of GTA 6 during its launch weeks. A human reviews everything you write before it is posted.

What you get: the streamer's credit line, the clip transcript (auto-generated, may contain errors), what chat was spamming at that moment, and the campaign rules for this clip, if any.

Write:
- hook: on-screen text for the first 3 seconds, at most 7 words. It must make a scrolling viewer stop, and it must be true to what happens in the clip. No clickbait that the clip doesn't pay off.
- title: at most 70 characters, for YouTube Shorts. Include "GTA 6".
- caption: 1-2 short lines for TikTok/Instagram, then the credit line on its own line. If the campaign requires a disclosure (for example "#ad"), put it at the very start of the caption.
- hashtags: 3-5 tags without spaces, always including gta6. Include any tags the campaign requires.
- take: one line of original commentary the page owner could add as a pinned comment or voiceover. It should add a real opinion or context, not restate the clip.
- spoiler_risk: "high" if the clip or your text reveals story events (mission outcomes, character deaths or betrayals, plot twists, the ending, or an isolated cutscene); "low" if it shows early story context; otherwise "none". Rockstar has spoiler footage taken down, and spoilers make viewers angry, so when unsure choose the higher level. Never put spoilers in the hook, title or caption.
- unsafe: true if the clip itself contains slurs, harassment of a real person, sexual content, or anything that breaks TikTok, Instagram or YouTube community guidelines.
- quality: 1-10, how likely a stranger who doesn't know this streamer watches to the end. An empty or meaningless transcript with weak chat signals scores low.
- why: one short sentence on what the moment is.

Keep the voice short, punchy and funny, never mean. Don't invent events that the transcript and chat don't support."""

SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string"},
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "take": {"type": "string"},
        "spoiler_risk": {"type": "string", "enum": ["none", "low", "high"]},
        "unsafe": {"type": "boolean"},
        "quality": {"type": "integer"},
        "why": {"type": "string"},
    },
    "required": ["hook", "title", "caption", "hashtags", "take", "spoiler_risk", "unsafe", "quality", "why"],
    "additionalProperties": False,
}


@dataclass
class ClipMeta:
    hook: str
    title: str
    caption: str
    hashtags: list[str] = field(default_factory=list)
    take: str = ""
    spoiler_risk: str = "unknown"
    unsafe: bool = False
    quality: int = 0
    why: str = ""
    source: str = "template"

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def _credit_line(channel_cfg: dict) -> str:
    credit = channel_cfg.get("credit") or f"twitch.tv/{channel_cfg.get('login', '')}"
    return f"Credit: {credit}"


def template_meta(channel_cfg: dict, campaign: dict | None, top_terms: list[str], transcript: str) -> ClipMeta:
    name = channel_cfg.get("display_name") or channel_cfg.get("login", "streamer")
    terms = {t.lower() for t in top_terms}
    if terms & {"kekw", "lul", "lmao", "lmfao", "omegalul", "icant", "dead", "lol"}:
        hook = "Chat could not stop laughing"
    elif terms & {"wtf", "???", "no way", "noway", "holy", "omg"}:
        hook = "Nobody expected this"
    elif terms & {"clip", "w", "pog", "poggers", "insane"}:
        hook = "Chat said clip it instantly"
    else:
        hook = f"{name} in GTA 6"
    disclosure = (campaign or {}).get("disclosure", "")
    lines = [disclosure, f"{name} on GTA 6 launch week", _credit_line(channel_cfg)]
    tags = ["gta6", "gtavi", "gaming"] + [t.lstrip("#") for t in (campaign or {}).get("required_tags", []) if t.startswith("#")]
    return ClipMeta(
        hook=hook,
        title=f"{name} in GTA 6"[:70],
        caption="\n".join(l for l in lines if l),
        hashtags=tags[:5],
        take="",
        spoiler_risk="unknown",
        quality=3 if transcript.strip() else 1,
        why="template (no ANTHROPIC_API_KEY)",
    )


def _have_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.path.exists(os.path.expanduser("~/.config/anthropic")))


def generate(
    cfg: dict,
    channel_cfg: dict,
    campaign: dict | None,
    transcript: str,
    top_terms: list[str],
    chat_sample: list[str],
) -> ClipMeta:
    ai = cfg["ai"]
    if not ai.get("enabled") or not _have_credentials():
        return template_meta(channel_cfg, campaign, top_terms, transcript)
    try:
        import anthropic
    except ImportError:
        return template_meta(channel_cfg, campaign, top_terms, transcript)

    rules = "none (unpaid)"
    if campaign:
        rules = json.dumps({
            "campaign": campaign.get("name", campaign.get("id")),
            "required_tags": campaign.get("required_tags", []),
            "disclosure": campaign.get("disclosure", ""),
            "notes": campaign.get("notes", ""),
        })
    user = (
        f"Page: {ai.get('page_handle')} ({ai.get('niche')}). Voice: {ai.get('voice')}.\n"
        f"Streamer: {channel_cfg.get('display_name') or channel_cfg.get('login')}\n"
        f"Credit line (put it in the caption exactly): {_credit_line(channel_cfg)}\n"
        f"Campaign rules: {rules}\n"
        f"Chat was spamming: {', '.join(top_terms) or 'n/a'}\n"
        f"Sample chat lines: {json.dumps(chat_sample[:10])}\n"
        f"Transcript:\n<transcript>\n{transcript.strip() or '(no speech detected)'}\n</transcript>"
    )
    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=ai.get("model", "claude-opus-5-5"),
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": ai.get("effort", "medium"),
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        log.error("Anthropic API key rejected; using template metadata")
        return template_meta(channel_cfg, campaign, top_terms, transcript)
    except anthropic.RateLimitError:
        log.warning("Anthropic rate limit; using template metadata for this clip")
        return template_meta(channel_cfg, campaign, top_terms, transcript)
    except anthropic.APIStatusError as exc:
        log.warning("Anthropic API error %s; using template metadata", exc.status_code)
        return template_meta(channel_cfg, campaign, top_terms, transcript)
    except anthropic.APIConnectionError:
        log.warning("Anthropic API unreachable; using template metadata")
        return template_meta(channel_cfg, campaign, top_terms, transcript)

    if response.stop_reason == "refusal":
        meta = template_meta(channel_cfg, campaign, top_terms, transcript)
        meta.unsafe = True
        meta.why = "model declined to write copy for this clip; review manually"
        return meta
    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("unparseable metadata response; using template")
        return template_meta(channel_cfg, campaign, top_terms, transcript)

    caption = data["caption"]
    credit = _credit_line(channel_cfg)
    if credit not in caption:
        caption = f"{caption}\n{credit}"
    disclosure = (campaign or {}).get("disclosure", "")
    if disclosure and disclosure not in caption:
        caption = f"{disclosure} {caption}"
    tags = [t.lstrip("#").replace(" ", "") for t in data["hashtags"] if t.strip()]
    for req in (campaign or {}).get("required_tags", []):
        if req.startswith("#") and req.lstrip("#") not in tags:
            tags.append(req.lstrip("#"))
    return ClipMeta(
        hook=data["hook"][:60],
        title=data["title"][:100],
        caption=caption,
        hashtags=tags[:6],
        take=data["take"],
        spoiler_risk=data["spoiler_risk"],
        unsafe=bool(data["unsafe"]),
        quality=max(0, min(10, int(data["quality"]))),
        why=data["why"],
        source=response.model,
    )
