"""Chat-spike detection. Pure logic, no I/O, so it is unit-testable.

Messages are bucketed into fixed windows (default 10 s). A window is a spike
when it is well above the recent baseline (ratio to the median AND/OR z-score)
and above an absolute floor, or when several people type "clip it".
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, deque
from dataclasses import dataclass, field

_WORD = re.compile(r"[a-z0-9?!']+")
_CLIP_IT = re.compile(r"\bclip\s*(it|that|this)\b|\bclipped\b|\bclip\b", re.I)


@dataclass
class Spike:
    at: float  # wall-clock end of the spiking window (unix seconds)
    window_start: float
    count: int
    unique: int
    baseline: float
    ratio: float
    z: float
    clip_mentions: int
    score: float
    top_terms: list[str] = field(default_factory=list)
    sample: list[str] = field(default_factory=list)


@dataclass
class _Window:
    start: float
    count: int = 0
    users: set = field(default_factory=set)
    terms: Counter = field(default_factory=Counter)
    clip_mentions: int = 0
    keyword_hits: int = 0
    sample: list = field(default_factory=list)


class SpikeDetector:
    def __init__(
        self,
        window_seconds: float = 10,
        baseline_windows: int = 30,
        warmup_windows: int = 6,
        min_ratio: float = 2.5,
        min_z: float = 3.0,
        min_messages: int = 12,
        clip_mentions: int = 3,
        cooldown_seconds: float = 45,
        keywords: list[str] | None = None,
        **_ignored,
    ):
        self.window = float(window_seconds)
        self.history: deque[int] = deque(maxlen=int(baseline_windows))
        self.warmup = int(warmup_windows)
        self.min_ratio = float(min_ratio)
        self.min_z = float(min_z)
        self.min_messages = int(min_messages)
        self.clip_threshold = int(clip_mentions)
        self.cooldown = float(cooldown_seconds)
        self.keywords = {k.lower() for k in (keywords or [])}
        self._cur: _Window | None = None
        self._last_spike_at = float("-inf")

    @classmethod
    def from_config(cls, cfg: dict, channel_cfg: dict | None = None) -> "SpikeDetector":
        """[detect] settings, with the streamer's own `detect = {...}` on top: a 200-viewer chat
        never reaches thresholds sized for one with thousands."""
        return cls(**{**cfg["detect"], **((channel_cfg or {}).get("detect") or {})})

    def _open(self, ts: float) -> None:
        start = ts - (ts % self.window)
        self._cur = _Window(start=start)

    def feed(self, ts: float, user: str, text: str) -> list[Spike]:
        """Add one chat message. Returns spikes from any windows that closed."""
        spikes = self.tick(ts)
        if self._cur is None:
            self._open(ts)
        w = self._cur
        w.count += 1
        w.users.add(user.lower())
        low = text.lower()
        words = _WORD.findall(low)
        w.terms.update(set(words))  # count each term once per message
        if _CLIP_IT.search(low):
            w.clip_mentions += 1
        if any(k in words or (" " in k and k in low) for k in self.keywords):
            w.keyword_hits += 1
        if len(w.sample) < 12 and 0 < len(text) <= 80:
            w.sample.append(text)
        return spikes

    def tick(self, now: float) -> list[Spike]:
        """Close every window that ended before `now`. Call this periodically
        even when chat is silent so quiet windows enter the baseline."""
        out: list[Spike] = []
        while self._cur is not None and now >= self._cur.start + self.window:
            closed = self._cur
            spike = self._evaluate(closed)
            if spike:
                out.append(spike)
            self.history.append(closed.count)
            nxt = closed.start + self.window
            self._cur = _Window(start=nxt) if now < nxt + self.window else None
            if self._cur is None:
                # long silence: account for skipped empty windows (bounded)
                gap = int((now - nxt) // self.window)
                for _ in range(min(gap, self.history.maxlen or gap)):
                    self.history.append(0)
                self._open(now)
        return out

    def _evaluate(self, w: _Window) -> Spike | None:
        if len(self.history) < self.warmup:
            return None
        hist = list(self.history)
        median = statistics.median(hist)
        mean = statistics.fmean(hist)
        stdev = statistics.pstdev(hist) if len(hist) > 1 else 0.0
        baseline = max(median, 1.0)
        ratio = w.count / baseline
        z = (w.count - mean) / stdev if stdev > 0 else (float("inf") if w.count > mean else 0.0)
        end = w.start + self.window

        volume_spike = w.count >= self.min_messages and (ratio >= self.min_ratio or z >= self.min_z)
        clip_spike = w.clip_mentions >= self.clip_threshold and w.count >= max(3, self.min_messages // 3)
        if not (volume_spike or clip_spike):
            return None
        if end - self._last_spike_at < self.cooldown:
            return None
        self._last_spike_at = end

        keyword_density = w.keyword_hits / max(w.count, 1)
        score = min(ratio, 20.0) * (1.0 + keyword_density) + 1.5 * w.clip_mentions
        top = [t for t, _ in w.terms.most_common(8) if len(t) > 1 or t in ("w",)][:5]
        return Spike(
            at=end,
            window_start=w.start,
            count=w.count,
            unique=len(w.users),
            baseline=round(baseline, 2),
            ratio=round(ratio, 2),
            z=round(z, 2) if z != float("inf") else 99.0,
            clip_mentions=w.clip_mentions,
            score=round(score, 2),
            top_terms=top,
            sample=w.sample,
        )
