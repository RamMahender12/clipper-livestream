"""clipper: live GTA 6 stream clipper with a human review queue.

  clipper init                 create config.toml from the example
  clipper doctor               check tools, model, credentials, channel permissions
  clipper live                 show which configured channels are live right now
  clipper run                  record live channels, cut spikes, review UI, post approved clips
  clipper review               review UI + renderer only (no recording, no posting)
  clipper ingest FILE -c LOGIN find loud moments in a local video and queue clips
  clipper cut FILE -c LOGIN --start 1:23:45 --duration 30
  clipper autoreview [--dry-run]  one AI auto-approval pass over ready clips
  clipper post                 one posting pass for approved clips
  clipper stats [--refresh]    views and estimated campaign earnings
  clipper launch-times         GTA 6 regional unlock times in your timezone
  clipper auth youtube --channel LOGIN   link a streamer's YouTube channel (one-time)
  clipper instagram-accounts   list Instagram Business account IDs for the token
  clipper telegram setup|test  connect and test phone alerts
  clipper service install|status|restart|stop|start|uninstall
                               run unattended in the background (macOS launchd)
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as config_mod, guardrails, pipeline, tools
from .db import open_db

log = logging.getLogger("clipper")


def _setup_logging(cfg: dict | None, verbose: bool) -> None:
    from logging.handlers import RotatingFileHandler

    from .service import SERVICE_ENV

    handlers: list[logging.Handler] = []
    if not os.environ.get(SERVICE_ENV):  # launchd already captures stdout to service.log
        handlers.append(logging.StreamHandler(sys.stdout))
    if cfg:
        handlers.append(RotatingFileHandler(config_mod.data_dir(cfg) / "clipper.log",
                                            maxBytes=10_000_000, backupCount=5))
    if not handlers:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def _parse_time(s: str) -> float:
    parts = [float(p) for p in s.split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


# ---------------------------------------------------------------- commands
def cmd_init(args) -> int:
    dst = config_mod.ROOT / "config.toml"
    if dst.exists():
        print(f"{dst} already exists; not overwriting.")
        return 0
    shutil.copy(config_mod.ROOT / "config.example.toml", dst)
    print(f"Created {dst}. Edit channels/campaigns, then run: clipper doctor")
    return 0


def cmd_doctor(args, cfg) -> int:
    ok = True

    def line(good: bool, what: str, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  [{'ok' if good else '!!'}] {what}{(' - ' + detail) if detail else ''}")

    print("Tools")
    try:
        ff = tools.ffmpeg()
        line(tools.has_filter(ff, "ass"), "ffmpeg with libass (captions)", ff)
    except tools.ToolMissing as e:
        line(False, "ffmpeg", str(e))
    for name, fn in (("streamlink", tools.streamlink), ("whisper.cpp", tools.whisper_cli)):
        try:
            line(True, name, fn())
        except tools.ToolMissing as e:
            line(False, name, str(e))
    model = config_mod.resolve(cfg, cfg["paths"]["whisper_model"])
    line(model.exists(), "whisper model", str(model))
    print("AI packaging")
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    print(f"  [{'ok' if have_key else '--'}] Anthropic credentials"
          f"{'' if have_key else ' - not set: hooks/titles fall back to templates (export ANTHROPIC_API_KEY=...)'}")
    from . import service

    env_path = config_mod.ROOT / service.ENV_FILE
    print(f"  [{'ok' if env_path.exists() else '--'}] secrets file {service.ENV_FILE}"
          f"{'' if env_path.exists() else ' (created by `clipper service install`)'}")
    st = service.status(cfg)
    if sys.platform == "darwin":
        print(f"  [{'ok' if st['state'] in ('running', 'active') else '--'}] background service: "
              f"{'installed, ' + st['state'] if st['installed'] else 'not installed (clipper service install)'}")
    acfg = cfg["auto_approve"]
    if acfg.get("enabled"):
        print(f"  [{'ok' if have_key else '!!'}] auto-approve ON"
              f"{'' if have_key else ' - needs Anthropic credentials, otherwise every clip is held for you'}")
    else:
        print("  [--] auto-approve off (set [auto_approve] enabled = true)")
    print("Channels")
    if not cfg["channels"]:
        line(False, "no channels configured")
    for ch in cfg["channels"]:
        good, why = guardrails.can_record(ch)
        camp = guardrails.campaign_for(cfg, ch)
        extra = f" -> campaign {camp['id']} ${camp.get('rate_per_1k')}/1k cap ${camp.get('cap_per_clip')}" if camp else ""
        line(good, ch.get("login", "?"), why + extra)
    print("Publishing")
    from . import notify
    from .publishers import channel_account, get_publisher

    for p in cfg["posting"]["platforms"]:
        if p == "export":
            line(True, "export", "writes files to data/export/<date>/")
            continue
        for ch in cfg["channels"]:
            kind, _ = guardrails.permission(ch)
            if kind in (None, "test"):
                continue
            account = channel_account(cfg, ch, p)
            if not account:
                hint = (f"run: clipper auth youtube --channel {ch['login']}" if p == "youtube"
                        else f"set instagram_user_id on the channel (clipper instagram-accounts)"
                        if p == "instagram" else "no account set")
                line(False, f"{p} for {ch['login']}", hint)
                continue
            try:
                good, msg = get_publisher(p, cfg, ch).ready()
            except Exception as exc:  # noqa: BLE001
                good, msg = False, str(exc)
            line(good, f"{p} for {ch['login']}", (msg or "").splitlines()[0] if msg else account)
    print(f"  [{'ok' if notify.configured() else '--'}] Telegram alerts"
          f"{'' if notify.configured() else ' - not set up (clipper telegram setup)'}")
    print("\nAll good." if ok else "\nFix the !! items above.")
    return 0 if ok else 1


def cmd_live(args, cfg) -> int:
    from .recorder import check_live

    for ch in cfg["channels"]:
        info = check_live(ch["login"])
        state = f"LIVE [{info.category}] {info.title[:70]}" if info.live else "offline"
        print(f"{ch['login']:<20} {state}")
    return 0


def _install_signals(stop: threading.Event) -> None:
    def handler(*_):
        print("\nstopping...")
        stop.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def cmd_run(args, cfg, review_only: bool = False) -> int:
    from . import recorder, review_server, scheduler, service, watcher

    host, port = cfg["review"]["host"], int(cfg["review"]["port"])
    if service.port_in_use(host, port):
        print(f"Port {port} is busy: clipper is probably already running "
              f"(check `clipper service status`). Not starting a second copy.")
        return 1
    if not review_only:
        stray = recorder.kill_orphans(config_mod.data_dir(cfg) / "rec")
        if stray:
            log.warning("stopped %d recorder(s) left over from a previous run", stray)
        if cfg["record"].get("keep_awake", True) and service.keep_awake():
            log.info("keeping the Mac awake while recording (display may still sleep)")
    db = open_db(cfg)
    stop = threading.Event()
    _install_signals(stop)
    # clips interrupted mid-render last time go back to the queue
    db.execute("UPDATE clips SET status = 'queued' WHERE status = 'rendering'")
    watchers = []
    if not review_only and not args.no_watch:
        for ch in cfg["channels"]:
            w = watcher.ChannelWatcher(db, cfg, ch, stop)
            w.start()
            watchers.append(w)
        recorder.Pruner(config_mod.data_dir(cfg) / "rec",
                        float(cfg["record"]["retain_minutes"]) * 60, stop).start()
    for i in range(int(cfg["clip"]["render_workers"])):
        pipeline.RenderWorker(db, cfg, stop, i).start()
    if not review_only and not args.no_post:
        scheduler.Scheduler(db, cfg, stop).start()
    if not review_only and cfg["auto_approve"].get("enabled"):
        from .autoapprove import AutoApprover

        AutoApprover(db, cfg, stop).start()
        log.info("auto-approve is ON (max %s/day, %s min undo window)",
                 cfg["auto_approve"]["max_per_day"], cfg["auto_approve"]["veto_minutes"])
    review_server.serve(db, cfg, watchers, stop)
    print(f"Review clips at http://{cfg['review']['host']}:{cfg['review']['port']}  (Ctrl-C to stop)")
    while not stop.wait(30):
        for w in watchers:
            log.debug("%s: %s", w.login, w.status)
    time.sleep(2)
    return 0


def cmd_ingest(args, cfg) -> int:
    from . import highlights

    src = Path(args.file).resolve()
    ch = config_mod.channel(cfg, args.channel)
    good, why = guardrails.can_record(ch)
    if not good:
        print(f"{args.channel}: {why}")
        return 1
    db = open_db(cfg)
    print(f"Scanning loudness in {src.name} ...")
    series = highlights.loudness_series(src)
    moments = highlights.find_moments(series, top=args.top)
    dur = tools.duration(src)
    for m in moments:
        start = max(0.0, m.at - args.pre)
        length = min(args.pre + args.post, dur - start)
        cid = pipeline.queue_clip(db, cfg, channel=args.channel, source=src,
                                  start_s=start, duration_s=length, score=m.score)
        print(f"  queued clip {cid} at {timedelta(seconds=int(m.at))} (+{m.score} dB)")
    if not moments:
        print("No loud moments found; try clipper cut with manual timestamps.")
    if args.render:
        print(f"Rendered {pipeline.drain_queue(db, cfg)} clips.")
    return 0


def cmd_cut(args, cfg) -> int:
    ch = config_mod.channel(cfg, args.channel)
    good, why = guardrails.can_record(ch)
    if not good:
        print(f"{args.channel}: {why}")
        return 1
    db = open_db(cfg)
    cid = pipeline.queue_clip(db, cfg, channel=args.channel, source=Path(args.file).resolve(),
                              start_s=_parse_time(args.start), duration_s=args.duration, score=5.0)
    print(f"queued clip {cid}")
    if args.render:
        pipeline.drain_queue(db, cfg)
        row = db.one("SELECT status, final_path, error FROM clips WHERE id = ?", (cid,))
        print(f"clip {cid}: {row['status']} {row['final_path'] or row['error']}")
    return 0


def cmd_autoreview(args, cfg) -> int:
    from . import autoapprove

    db = open_db(cfg)
    st = autoapprove.status(db, cfg)
    print(f"history: {st['history']} posts with view data (stats model needs {st['min_history']}); "
          f"auto-approved in last 24h: {st['today']}/{st['limit']}")
    lines = autoapprove.run_once(db, cfg, dry_run=args.dry_run)
    for line in lines or ["no new clips to review"]:
        print(line)
    reviewed = {int(line.split(":")[0].split()[-1]) for line in lines if line.startswith("clip ")}
    waiting = [r for r in autoapprove.waiting_for_human(db) if r["id"] not in reviewed]
    if waiting:
        print(f"\n{len(waiting)} ready clip(s) already reviewed and waiting for you in the review UI "
              "(re-reviewed only after they change):")
        for r in waiting[:10]:
            note = (r["decision_note"] or "").split(" | ")[0]
            print(f"  clip {r['id']} ({r['channel']}): {note or 'held by you'}")
        if len(waiting) > 10:
            print(f"  ... and {len(waiting) - 10} more")
    if args.dry_run:
        print("(dry run: nothing changed)")
    return 0


def cmd_service(args, cfg) -> int:
    from . import service

    action = args.action
    if action == "install":
        path = service.install(cfg)
        print(f"Installed {path}")
        print("It starts now and at every login, restarts if it crashes, and keeps the Mac awake.")
        print(f"Put API keys in {service.ENV_FILE}, then: .venv/bin/clipper service restart")
    elif action == "start":
        service.start()
    elif action == "stop":
        service.stop()
        print("Stopped. It stays stopped until `clipper service start` (or the next install).")
    elif action == "restart":
        service.restart()
    elif action == "uninstall":
        service.uninstall()
        print("Stopped and removed the background service.")
    if action in ("stop", "uninstall"):
        for _ in range(20):  # launchd unloads asynchronously
            if not service.is_loaded():
                break
            time.sleep(0.25)
    st = service.status(cfg)
    print(f"service: {'installed' if st['installed'] else 'not installed'}, {st['state']}"
          f"{', pid ' + st['pid'] if st['pid'] else ''}"
          f"{', last exit ' + st['last_exit'] if st['last_exit'] else ''}")
    print(f"logs: {st['log']}  and  {config_mod.data_dir(cfg) / 'clipper.log'}")
    return 0


def cmd_post(args, cfg) -> int:
    from . import scheduler

    for line in scheduler.post_once(open_db(cfg), cfg) or ["nothing to post"]:
        print(line)
    return 0


def cmd_stats(args, cfg) -> int:
    from . import scheduler

    db = open_db(cfg)
    if args.refresh:
        print(f"refreshed {scheduler.refresh_stats(db, cfg)} posts")
    s = scheduler.earnings_summary(db, cfg)
    print(f"clips: {s['clips']}")
    print(f"posts: {s['posts']}  views: {s['views']:,}  estimated campaign earnings: ${s['est_usd']:.2f}")
    for camp, agg in sorted(s["by_campaign"].items()):
        print(f"  {camp:<28} {agg['posts']:>4} posts {agg['views']:>12,} views  ${agg['est_usd']:.2f}")
    print("Estimates assume every view qualifies; campaigns verify and may pay less.")
    for who, agg in sorted(s.get("by_decider", {}).items()):
        print(f"  approved by {who:<6} {agg['clips']:>4} clips  {agg['views_per_clip']:>10,} views/clip")
    rows = db.query("SELECT campaign_id, platform, url, posted_at FROM posts "
                    "WHERE campaign_id != '' AND url != '' AND status != 'error' ORDER BY campaign_id, posted_at")
    if rows:
        print("\nPost URLs to submit in each campaign dashboard (campaigns only pay for submitted posts):")
        current = None
        for r in rows:
            if r["campaign_id"] != current:
                current = r["campaign_id"]
                print(f"  [{current}]")
            print(f"    {r['platform']:<10} {r['url']}")
    return 0


LAUNCH_ZONES = [
    ("New Zealand", "Pacific/Auckland"), ("Australia (Sydney)", "Australia/Sydney"),
    ("Japan", "Asia/Tokyo"), ("India", "Asia/Kolkata"), ("UAE", "Asia/Dubai"),
    ("UK", "Europe/London"), ("Brazil (Sao Paulo)", "America/Sao_Paulo"),
    ("US East", "America/New_York"), ("US West", "America/Los_Angeles"),
]


def cmd_launch_times(args, cfg) -> int:
    tz = ZoneInfo(cfg["posting"]["timezone"] if cfg else "Asia/Kolkata")
    now = datetime.now(tz)
    print(f"GTA 6 regional unlock (midnight local on 19 Nov 2026, as reported for PlayStation;"
          f" check the store page for your platform). Shown in {tz.key}:")
    for name, zone in LAUNCH_ZONES:
        unlock = datetime(2026, 11, 19, 0, 0, tzinfo=ZoneInfo(zone)).astimezone(tz)
        delta = unlock - now
        rel = "unlocked" if delta.total_seconds() <= 0 else f"in {int(delta.total_seconds() // 3600)}h {int(delta.total_seconds() % 3600 // 60)}m"
        print(f"  {name:<20} {unlock:%a %d %b %H:%M}  ({rel})")
    return 0


def cmd_auth(args, cfg) -> int:
    if args.platform == "youtube":
        from .publishers import youtube, youtube_token_path

        if args.channel:
            ch = config_mod.channel(cfg, args.channel)
            if not ch:
                print(f"No channel '{args.channel}' in config.toml")
                return 1
            token = youtube_token_path(cfg, ch)
            acfg = dict(cfg)
            acfg["youtube"] = {**cfg["youtube"], "token_file": token}
            print(f"Sign in and pick the YouTube channel for {ch.get('display_name') or ch['login']}'s clips.")
            youtube.authorize(acfg)
            print(f"Saved {token}. Redeploy to copy it to the server.")
        else:
            youtube.authorize(cfg)
    elif args.platform == "tiktok":
        from .publishers import tiktok

        tiktok.authorize(cfg)
    else:
        print("Instagram uses a long-lived token: set the env var named in [instagram].access_token_env")
        return 1
    return 0


def cmd_instagram_accounts(args, cfg) -> int:
    """List the Instagram Business accounts the token can post to, with their IDs."""
    import json as _json
    import urllib.parse
    import urllib.request

    ig = cfg["instagram"]
    token = os.environ.get(ig.get("access_token_env", "IG_ACCESS_TOKEN"), "").strip()
    if not token:
        print(f"Set {ig.get('access_token_env', 'IG_ACCESS_TOKEN')} in secrets/clipper.env first.")
        return 1
    url = (f"https://graph.facebook.com/{ig.get('graph_version', 'v27.0')}/me/accounts?"
           + urllib.parse.urlencode({"fields": "name,instagram_business_account{id,username}",
                                     "limit": 100, "access_token": token}))
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = _json.load(resp)
    except Exception as exc:  # noqa: BLE001
        print(f"Graph API error: {exc}")
        return 1
    found = 0
    for page in data.get("data", []):
        acct = page.get("instagram_business_account")
        if acct:
            found += 1
            print(f"  @{acct.get('username', '?'):<24} instagram_user_id = \"{acct['id']}\"   (Facebook Page: {page.get('name')})")
    if not found:
        print("No Instagram Business accounts found. Each one must be linked to a Facebook Page you manage.")
    return 0


def cmd_telegram(args, cfg) -> int:
    from . import notify, service

    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("Add TELEGRAM_BOT_TOKEN=... (from @BotFather) to secrets/clipper.env first.")
        return 1
    if args.action == "setup":
        chats = notify.find_chat_ids()
        if not chats:
            print("No messages found. Open your bot in Telegram, press Start (or send 'hi'), then run this again.")
            return 1
        chat_id, name = chats[-1]
        env_path = config_mod.ROOT / service.ENV_FILE
        text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        lines = [l for l in text.splitlines() if not l.strip().startswith("TELEGRAM_CHAT_ID=")]
        lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
        os.environ["TELEGRAM_CHAT_ID"] = chat_id
        print(f"Saved TELEGRAM_CHAT_ID for chat '{name}'.")
    ok = notify.send("Clipper alerts are connected. You'll get a message with links to submit "
                     "every time new clips go live.")
    print("Test message sent." if ok else "Sending failed: check the bot token and that you pressed Start.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipper", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("doctor")
    sub.add_parser("live")
    r = sub.add_parser("run")
    r.add_argument("--no-watch", action="store_true", help="don't record; only render/review/post")
    r.add_argument("--no-post", action="store_true", help="don't post; only record/render/review")
    sub.add_parser("review")
    i = sub.add_parser("ingest")
    i.add_argument("file")
    i.add_argument("-c", "--channel", required=True)
    i.add_argument("--top", type=int, default=8)
    i.add_argument("--pre", type=float, default=20)
    i.add_argument("--post", type=float, default=10)
    i.add_argument("--render", action="store_true", help="render now instead of leaving it to `run`")
    c = sub.add_parser("cut")
    c.add_argument("file")
    c.add_argument("-c", "--channel", required=True)
    c.add_argument("--start", required=True)
    c.add_argument("--duration", type=float, default=30)
    c.add_argument("--render", action="store_true")
    ar = sub.add_parser("autoreview")
    ar.add_argument("--dry-run", action="store_true", help="show decisions without changing anything")
    sub.add_parser("post")
    s = sub.add_parser("stats")
    s.add_argument("--refresh", action="store_true")
    sub.add_parser("launch-times")
    sv = sub.add_parser("service")
    sv.add_argument("action", choices=["install", "status", "start", "stop", "restart", "uninstall"])
    a = sub.add_parser("auth")
    a.add_argument("platform", choices=["youtube", "tiktok", "instagram"])
    a.add_argument("--channel", help="link this streamer's own account (e.g. lacy)")
    sub.add_parser("instagram-accounts")
    tg = sub.add_parser("telegram")
    tg.add_argument("action", choices=["setup", "test"])
    args = ap.parse_args(argv)

    if args.cmd == "init":
        return cmd_init(args)
    from .service import load_env_file

    load_env_file()
    cfg = None
    if args.cmd != "launch-times" or (config_mod.ROOT / "config.toml").exists() or args.config:
        cfg = config_mod.load(args.config)
    _setup_logging(cfg, args.verbose)
    handlers = {
        "doctor": cmd_doctor, "live": cmd_live, "run": cmd_run, "ingest": cmd_ingest,
        "cut": cmd_cut, "post": cmd_post, "stats": cmd_stats, "launch-times": cmd_launch_times,
        "auth": cmd_auth, "autoreview": cmd_autoreview, "service": cmd_service,
        "instagram-accounts": cmd_instagram_accounts, "telegram": cmd_telegram,
    }
    if args.cmd == "review":
        args.no_watch = args.no_post = True
        return cmd_run(args, cfg, review_only=True)
    return handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
