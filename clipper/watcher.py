"""Per-channel live loop: wait for live -> record + read chat -> cut every spike."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod, guardrails, pipeline, recorder
from .db import DB
from .spikes import Spike, SpikeDetector
from .twitch_chat import ChatReader

log = logging.getLogger(__name__)


@dataclass
class PendingCut:
    spike: Spike
    spike_id: int
    t0: float
    t1: float
    created: float


class ChannelWatcher(threading.Thread):
    def __init__(self, db: DB, cfg: dict, channel_cfg: dict, stop: threading.Event):
        super().__init__(daemon=True, name=f"watch-{channel_cfg['login']}")
        self.db, self.cfg, self.ch, self.stop_event = db, cfg, channel_cfg, stop
        self.login = channel_cfg["login"].lower()
        self.status = "starting"

    # ---- main loop -------------------------------------------------------
    def run(self) -> None:
        ok, why = guardrails.can_record(self.ch)
        if not ok:
            self.status = f"skipped: {why}"
            log.warning("%s %s", self.login, self.status)
            return
        poll = float(self.cfg["record"]["poll_seconds"])
        wanted = [c.lower() for c in self.ch.get("categories", [])]
        while not self.stop_event.is_set():
            info = recorder.check_live(self.login)
            if info.live and (not wanted or info.category.lower() in wanted):
                try:
                    self._session(info)
                except Exception:
                    log.exception("%s: session crashed", self.login)
            else:
                self.status = "offline" if not info.live else f"live in {info.category} (waiting for {wanted})"
            self.stop_event.wait(poll)

    def _session(self, info: recorder.LiveInfo) -> None:
        rcfg, dcfg, ccfg = self.cfg["record"], self.cfg["detect"], self.cfg["clip"]
        root = config_mod.data_dir(self.cfg) / "rec" / self.login
        started = time.time()
        session_id = self.db.insert(
            "sessions", channel=self.login, started_at=started,
            segment_dir="", title=info.title, status="recording",
        )
        seg_dir = root / f"{session_id:05d}"
        self.db.update("sessions", session_id, segment_dir=str(seg_dir))
        rec = recorder.Recorder(self.login, seg_dir, rcfg["quality"], rcfg["segment_seconds"])
        rec.start()

        msgs: queue.Queue = queue.Queue(maxsize=200000)
        chat_stop = threading.Event()

        def on_msg(ts: float, user: str, text: str) -> None:
            try:
                msgs.put_nowait((ts, user, text))
            except queue.Full:
                pass

        chat = ChatReader(self.login, on_msg, chat_stop)
        chat.start()
        det = SpikeDetector.from_config(self.cfg, config_mod.channel(self.cfg, self.login))
        pending: list[PendingCut] = []
        recent_clips: deque[float] = deque()
        max_per_hour = int(ccfg["max_per_hour_per_channel"])
        self.status = f"LIVE: recording + reading chat ({info.title[:60]})"
        log.info("%s is live: %s", self.login, info.title)
        n_msgs = 0
        try:
            while not self.stop_event.is_set() and rec.alive():
                now = time.time()
                spikes: list[Spike] = []
                while True:
                    try:
                        ts, user, text = msgs.get_nowait()
                    except queue.Empty:
                        break
                    n_msgs += 1
                    spikes.extend(det.feed(ts, user, text))
                spikes.extend(det.tick(now))
                for sp in spikes:
                    while recent_clips and now - recent_clips[0] > 3600:
                        recent_clips.popleft()
                    if len(recent_clips) >= max_per_hour:
                        log.info("%s: hourly clip cap reached, skipping spike %.1f", self.login, sp.score)
                        continue
                    recent_clips.append(now)
                    pending.append(self._register(session_id, sp))
                pending = self._try_cuts(seg_dir, pending, done=False)
                if now - started > 120 and rec.last_write_age() > 420:
                    log.warning("%s: no video for 7 min (ad break or stall); restarting recorder",
                                self.login)
                    rec.stop()
                    rec.start()
                    started = now
                self.status = (f"LIVE: {n_msgs} chat msgs, {len(recent_clips)} clips/hr, "
                               f"{len(pending)} waiting to cut")
                self.stop_event.wait(1.0)
        finally:
            chat_stop.set()
            rec.stop()
            err = rec.errors()
            if err:
                log.info("%s recorder exit: %s", self.login, err[-400:])
            self._try_cuts(seg_dir, pending, done=True)
            self.db.update("sessions", session_id, ended_at=time.time(), status="ended")
            self.status = "stream ended"
            log.info("%s: session %s ended", self.login, session_id)

    # ---- helpers ---------------------------------------------------------
    def _register(self, session_id: int, sp: Spike) -> PendingCut:
        ccfg, window = self.cfg["clip"], float(self.cfg["detect"]["window_seconds"])
        moment = sp.window_start + window / 2 - float(ccfg["chat_delay_seconds"])
        t0 = moment - float(ccfg["pre_seconds"])
        t1 = moment + float(ccfg["post_seconds"])
        spike_id = self.db.insert(
            "spikes", session_id=session_id, channel=self.login, at=sp.at, score=sp.score,
            count=sp.count, baseline=sp.baseline, ratio=sp.ratio, z=sp.z,
            clip_mentions=sp.clip_mentions, top_terms=",".join(sp.top_terms),
            sample=json.dumps(sp.sample),
        )
        log.info("%s spike #%s score=%.1f count=%d (x%.1f) terms=%s",
                 self.login, spike_id, sp.score, sp.count, sp.ratio, sp.top_terms)
        return PendingCut(sp, spike_id, t0, t1, time.time())

    def _try_cuts(self, seg_dir: Path, pending: list[PendingCut], done: bool) -> list[PendingCut]:
        keep = []
        raw_dir = config_mod.data_dir(self.cfg) / "raw" / self.login
        for p in pending:
            out = raw_dir / f"spike{p.spike_id:06d}.ts"
            try:
                offset = recorder.extract_window(seg_dir, p.t0, p.t1, out, recording_done=done)
            except Exception as exc:
                log.warning("%s: cut for spike %s failed: %s", self.login, p.spike_id, exc)
                offset = None
                if time.time() - p.created > 300:
                    continue
            if offset is None:
                if not done and time.time() - p.created < 600:
                    keep.append(p)
                continue
            clip_id = pipeline.queue_clip(
                self.db, self.cfg, channel=self.login, source=out,
                start_s=offset, duration_s=p.t1 - p.t0, score=p.spike.score,
                spike_id=p.spike_id, wall_start=p.t0,
            )
            log.info("%s: queued clip %s from spike %s", self.login, clip_id, p.spike_id)
        return keep
