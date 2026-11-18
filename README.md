# GTA 6 live clipper

Records Twitch streams you're allowed to clip, watches chat, cuts every moment chat explodes into a captioned vertical short, writes the hook and caption with Claude, and posts the clips that you (or the AI auto-approver) approve through official platform APIs.

Strategy, money math, legal rules and the launch-night timeline are in **[PLAYBOOK.md](PLAYBOOK.md)**.

```
streamlink ──► 30 s segments on disk (UTC-named)            ┌──────────────┐
                                    ▲                       │ review UI    │  you or AI approve
Twitch chat (anonymous, read-only)  │ cut [t-20s, t+10s]    │ 127.0.0.1    │──────────────┐
   └► 10 s windows ► spike? ────────┘                       └──────▲───────┘              ▼
                                       whisper.cpp captions        │              scheduler (paced)
                                       Claude: hook/title/caption ─┘              YouTube · Instagram
                                       ffmpeg 9:16 render + burn-in              TikTok draft · export
```

## Setup (10 minutes)

```bash
brew install streamlink whisper-cpp ffmpeg-full
```

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

```bash
mkdir -p models && curl -L -o models/ggml-base.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

```bash
.venv/bin/clipper init
```

Edit `config.toml`: add each campaign you've joined under `[[campaigns]]` and its streamer under `[[channels]]` with `permission = "campaign:<id>"`. For better hooks and spoiler checks, export your Anthropic key; without it the tool uses simple templates.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

```bash
.venv/bin/clipper doctor
```

## Run it

```bash
.venv/bin/clipper run
```

Then open http://127.0.0.1:8765. Clips appear under **ready** about a minute after a spike. Edit the hook (Save + re-render burns the new one in), fix the caption, then **Approve**. The scheduler posts approved clips to each platform in `[posting].platforms`, one at a time, respecting the daily caps and minimum gap.

## Auto-approve

With `[auto_approve] enabled = true` and `ANTHROPIC_API_KEY` set, the AI approves clips for you every few minutes. Each ready clip gets two opinions:

1. **Your track record.** Every post older than `mature_hours` with known views is ranked against your other posts on the same platform. A new clip's expected rank comes from how past clips did with the same streamer, the same AI quality score and a similar chat-spike strength. A factor needs about 5 past clips before it moves the estimate much, so one lucky viral clip doesn't skew it. The card shows the result as "~N views expected".
2. **A Claude review.** Claude sees the clip, the campaign brief, and your best and worst past posts, and votes approve, hold or reject, with a confidence score.

A clip is approved only when Claude approves with confidence of at least `min_confidence`, the brief is met, and history predicts at least `min_percentile`, up to `max_per_day`. Until you have `min_history` posts with views (cold start), the bar is AI quality of at least `cold_start_min_quality`, with at most `cold_start_max_per_day` approvals. Clips both opinions call weak are rejected; everything else waits for you with the reason on the card.

These are never auto-approved, whatever the settings: `test` footage, clips flagged as spoilers or unsafe, clips without an AI spoiler check, and channels without permission. Auto-approved clips wait `veto_minutes` (default 10) before posting. **Undo approve** on the card stops them. Ready clips still unposted after `expire_hours` are dropped, because launch-week moments go stale.

The model learns from view counts. API-posted clips update automatically (`clipper stats --refresh`, also run every 2 hours). For clips you post by hand from `data/export/`, type the post URL and its views into the card's **Posts** row once it's a day old. `clipper stats` compares views per clip for AI-approved and human-approved clips, so you can see whether the AI is earning its place.

```bash
.venv/bin/clipper autoreview --dry-run
```

shows what it would decide right now without changing anything. Each review costs a Claude call at high effort, a few cents per clip.

Other commands:

| Command | Use |
|---|---|
| `clipper live` | Which configured channels are live, and in what category |
| `clipper launch-times` | GTA 6 unlock time per region in your timezone |
| `clipper run --no-post` | Record and render only; post manually from `data/export/` |
| `clipper review` | Just the review UI and renderer |
| `clipper ingest my_gameplay.mp4 -c mylogin --render` | Find loud moments in your own recording (or a VOD a campaign named) |
| `clipper cut vod.mp4 -c login --start 1:02:30 --duration 30 --render` | Cut one clip by hand |
| `clipper autoreview [--dry-run]` | One auto-approval pass now (dry run changes nothing) |
| `clipper post` | One posting pass now |
| `clipper stats --refresh` | Views and estimated campaign earnings, by campaign |
| `clipper auth youtube` / `clipper auth tiktok` | One-time OAuth consent |

## Permissions are enforced

Every channel needs `permission`:

- `campaign:<id>`: the streamer pays clippers through that campaign. Clips only post to the platforms the campaign pays on.
- `written:<link>`: the streamer said yes in writing; link the message.
- `own`: your own footage.
- `test`: record and render locally, never posted.

A channel without one is never recorded. Clips flagged as a high spoiler risk or unsafe can't be posted. Nothing posts unless you approved it or the auto-approver did under the rules above.

## Posting platforms

Start with `platforms = ["export"]`: every approved clip lands in `data/export/<date>/` with a `.txt` caption, ready to post from your phone. Native app posting also lets you add trending sounds and the paid-partnership toggle by hand. Switch on APIs once the accounts are warmed up:

- **YouTube Shorts**: Google Cloud project, YouTube Data API v3, OAuth client (Desktop). Save the JSON as `secrets/youtube_client_secret.json`, then `clipper auth youtube`.
- **Instagram Reels**: a Professional (Creator/Business) account linked to a Facebook Page, and a Business-type Meta app using Facebook Login for Business with `instagram_basic`, `instagram_content_publish`, `instagram_manage_insights` and the `pages_*` permissions. Local-file upload needs that Facebook Login token (starts with `EAA`), not an Instagram Login token. Put `ig_user_id` in config and the token in `IG_ACCESS_TOKEN`. Posting to your own account needs no App Review. Limit: 100 API posts per 24 h.
- **TikTok**: blocked in India. Elsewhere, a TikTok developer app with Login Kit, the Content Posting API and the Display API. Unaudited apps can only post privately, so the default `mode = "inbox"` sends clips to your TikTok drafts. You add the caption and the paid-partnership toggle in the app, because the inbox API takes neither. About 5 pending drafts per day.
- **YouTube**: publish the OAuth consent screen to "In production" (in Testing, refresh tokens expire after 7 days). Uploads have a separate quota of 100 per day per Cloud project. The paid-promotion flag is set automatically for campaign clips.

API posting was tested against mock servers, not the live platforms. Before leaving the scheduler running, approve a single clip, run `clipper post` once, and check the result on each platform.

Each publisher's `ready()` check prints the exact missing step; `clipper doctor` shows them all.

## Tuning

- **Clips start too late or too early:** adjust `[clip].chat_delay_seconds` (chat usually reacts 2-6 s after the moment) and `pre_seconds`.
- **Too many or too few spikes:** raise or lower `[detect].min_ratio` and `min_z`. Big chats (50k+ viewers) need lower ratios; small chats need a higher `min_messages`.
- **Streamer face cam:** set `layout = "facecam_split"` and `facecam = [x, y, w, h]` (fractions of the frame) on the channel.
- **Faster renders:** `encoder = "h264_videotoolbox"`.
- **Better captions:** download `ggml-small.en.bin` and point `whisper_model` at it.
- **Disk:** about 3.5 GB per hour per channel at 1080p60; segments older than `retain_minutes` are deleted automatically. Cut clips are kept in `data/raw/` and `data/clips/`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```
