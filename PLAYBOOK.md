# GTA 6 Clipping Playbook

How to turn GTA 6 launch weeks into income with this tool, legally and without losing accounts. Written 18 Nov 2026, the day before launch. Numbers are from public campaign boards and clipping-tool blogs, which have a commercial interest, so treat them as rough.

## What the three reels describe

All three describe the same idea, plus one packaging layer.

| Reel | Claim | What it means for us |
|---|---|---|
| @theromanknox, @bizibeast | Don't download the full stream after it ends; record it live, count chat every 10 s, cut ~30 s around each spike, caption with Whisper. Free tools (streamlink, ffmpeg, whisper). Only works while the streamer is live. | Built: `clipper run` does exactly this, unattended. |
| @nateherkai | Run a GTA 6 page with a daily mix of news, tips and credited clips; track every post; plan launch night (New Zealand plays first). Gaming campaigns pay a median of about $1.13 per 1,000 views, so ~9M views a month makes ~$10K. Instagram stops recommending accounts that just repost, so every clip needs a hook, captions and your own take. | Built: AI hooks/titles/captions + "your take" line, a scoreboard (`clipper stats`), launch-time table. The content plan prompt is in `prompts/content_manager.md`. |

What the reels leave out, and what this playbook adds: **you only get paid for clips you're allowed to post.** Clipping random streamers gets copyright strikes and bans. Paid clipping campaigns exist because streamers want clips and pay for them; that permission is the business.

## Where the money comes from

Ranked by how fast the first dollar arrives.

1. **Per-view clipping campaigns** (Whop Content Rewards and similar networks). A streamer or brand funds a budget, sets a rate per 1,000 views and a cap per clip, and pays for approved clips. Benchmarks from Content Rewards (15 Nov 2026, 39 live gaming/streamer campaigns): median **$1.00 per 1k views**, middle half $0.69 to $2.00, median cap **$150 per clip**. As of 16 Nov, the GTAVIclipper board listed only two open GTA 6 campaigns (a music promo at $0.70/1k with a $35 cap, and X's native clipping program at $1.23 per 1k verified impressions). Expect many more once the big streamers go live on launch day.
2. **X native clipping program**: listed at $1.23 per 1k verified impressions for 5 to 60 s clips from videos over 2 minutes. Check eligibility (it's an X program, not a campaign).
3. **Platform payouts** (slower): YouTube Shorts revenue share needs YPP (1,000 subscribers plus 10M Shorts views in 90 days). Facebook has its own content monetization. A viral launch week can get you there, but this is month 2+ money.
4. **Your own gameplay** (if you buy GTA 6 and a PS5 or Xbox Series): the most original and lowest-risk footage. It also fixes Instagram's originality rule. Use `clipper ingest your_recording.mp4 -c <your_login>`.

### Honest math

Realized earnings are lower than headline rates. One tracker reports a blended **$0.39 per 1k** realized across clippers and about **$305 average lifetime earnings** per clipper; most people quit early. You're aiming to be in the top slice by running a system all day while others clip by hand.

| Target / month | Paid views needed at $1/1k | Per day | Example output |
|---|---|---|---|
| $1,000 | 1M | ~33K | 8 clips/day × 3 platforms × ~1.4K avg views |
| $3,000 | 3M | ~100K | 12 clips/day × 3 platforms × ~2.8K avg views |
| $10,000 | 10M | ~333K | needs several viral hits per week, multiple campaigns |

**Caps change the strategy.** A 5M-view clip on a $150 cap pays $150, the same as a 150K-view clip. So many solid clips beat one viral clip: aim for each clip to reach its cap, spread across several campaigns, and prefer campaigns with high caps and big remaining budgets.

## Rules that keep you legal and unbanned

The tool enforces the first four. The rest are on you.

1. **Permission for every channel.** Each channel in `config.toml` needs `permission = "campaign:<id>" | "written:<link>" | "own" | "test"`. No permission, no recording. `test` is never posted.
2. **A human approves every post.** Nothing goes out until you click Approve in the review UI.
3. **No spoilers.** Rockstar's policy (support.rockstargames.com, updated 10 Jan 2025) has takedowns for story endings, isolated cutscenes, edited cutscene compilations and big plot reveals, plus in-game TV/comedy shows posted on their own, cheating/glitch/mod-menu content, and **any pre-release footage**. Claude flags spoiler risk on each clip; high-risk clips can't be posted. Launch week is peak spoiler-anger season: clip the player's reactions and chaos, not the plot.
4. **Official APIs only, paced posting.** No bots, no browser automation, no follow/like schemes, daily caps and a minimum gap between posts.
5. **Rockstar's fine print.** Its written policy covers *non-commercial* fan use and reserves the right to take down anything. Monetized GTA gameplay on YouTube and Twitch has been tolerated for a decade, but it's tolerance, not a license. Don't build on anything Rockstar could object to (spoilers, leaks, music, mods), and keep a backlog so one takedown doesn't stall you.
6. **Disclose paid clips.** Campaign clips are ads: put `#ad` first and switch on each platform's paid-partnership toggle. Campaign briefs increasingly require it.
7. **Music.** Mute or avoid in-game radio. Licensed songs get your clips muted or claimed. Many briefs require it too.
8. **Originality on Instagram.** Since 30 Apr 2026, an account loses recommendations if most of what it posted in the last 30 days is other people's content. Watermarks and speed changes don't count as original; commentary and real edits do. Mix your own gameplay, news or tips videos in your own voice, and reaction voiceovers so that more than half of what you post is original. YouTube Shorts and X are friendlier to clips.
9. **No fake views.** Campaigns bot-score every clip. Bought views or engagement pods mean no payout and a ban.
10. **One account per platform per page.** Posting the same clip across a network of accounts is spam on every platform.
11. **First 7 seconds clean.** YouTube's September 2026 rules make Shorts with graphic violence in the first 7 seconds ineligible for ads; it's also where viewers decide to scroll. Hook text, not gore.

### India-specific notes

- **TikTok has been blocked in India since 2020.** Don't route around the ban with a VPN; that conflicts with "legal and not banned". Use YouTube Shorts, Instagram Reels, X and Facebook, and choose campaigns that pay on those platforms.
- Some campaigns only count views from Tier 1 countries (US/UK/CA/AU). Write English captions and post when the US is awake: 05:30-10:30 IST is US evening prime time.
- Campaign payouts are taxable income. Keep the `clipper stats` export and the Whop payout records for your CA.

## Launch timeline (IST)

`clipper launch-times` prints this live. Reported unlock is midnight local time (PlayStation); confirm on your store page.

| When (IST) | What happens | Your move |
|---|---|---|
| Wed 18 Nov, before 16:30 | Setup | Join campaigns, fill `config.toml`, run `clipper doctor`, test on a live channel |
| **Wed 18 Nov 16:30** | **New Zealand unlocks.** Streamers flew there to play first (xQc's title says so). First real GTA 6 gameplay anywhere. | Run `clipper run`. These are the scarcest clips of the whole launch. |
| Wed 18 Nov 18:30 / 20:30 | Australia, then Japan unlock | Keep approving; post at a steady pace |
| Thu 19 Nov 00:00 | India unlocks | If you own the game, record your own first hour |
| Thu 19 Nov 05:30 | UK unlocks; US evening audience is awake | Peak posting window for Tier 1 views |
| Thu 19 Nov 10:30 / 13:30 | US East / West unlock: the big US streamers go live | The biggest wave: watch for new campaigns opening |

## Daily routine (full-time)

1. **Morning (30 min):** check campaign boards (Whop Content Rewards discover, GTAVIclipper board, clipping Discords) for new GTA 6 or streamer campaigns. Add each to `[[campaigns]]` and its streamer to `[[channels]]` with `permission = "campaign:<id>"`.
2. **All day:** leave `clipper run` going. It records whoever is live, cuts spikes, captions them and writes hooks.
3. **Every 30-60 min:** open http://127.0.0.1:8765. Approve the best clips, fix weak hooks (hook edits re-render), reject anything spoilery, mean or boring. Post the AI's "take" as a pinned comment.
4. **Submit:** campaigns pay only for posts you submit in their dashboard. After posting, submit each URL (`clipper stats` lists them by campaign).
5. **Evening:** make one original piece (your gameplay, a tips video, a news recap in your voice) to keep the account original.
6. **Weekly:** `clipper stats --refresh`. Drop campaigns that don't approve your clips, raise volume on the ones that pay, and retune `[detect]` per streamer if clips start too early or late.

## Week 1 targets

- 3 platforms connected (YouTube, Instagram, X/Facebook manual export).
- 3+ active campaigns, 10-20 approved clips a day.
- Track approval rate per campaign. Under 50% means the brief isn't being followed: read it again.
