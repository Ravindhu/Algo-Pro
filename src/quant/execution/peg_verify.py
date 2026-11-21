"""Verify a high-volume bid level before a peg entry (2026-11-21).

Flow stage: Order pricing
Status: PHASE 3 IMPLEMENTED (pure checks; the depth and quote reads stay in the runner)

The peg chooses a bid level by its resting quantity. Before resting an order there, the
verifier checks the level over recent depth polls:

  1. Real absorption: sellers traded into the level and it held. Absorbed sell qty must reach
     ``absorb_ratio`` x the initial bid qty.
  2. Low pull ratio: bid qty that left the level without trading (pulled) must stay at or
     below ``max_pull_ratio`` of (initial qty + absorbed qty).
  3. Dwell and bounce: price touched the level, then recovered above it, and stayed above it
     for at least ``dwell_seconds`` up to the latest poll.

Limits (stated): samples are polls, not ticks. A trade classifies as sell pressure when the
LTP at the poll is at or below the level. Activity inside one poll is not seen.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Tuple

_EPS = 1e-6


@dataclass(frozen=True)
class VerifyConfig:
    window_seconds: float = 60.0    # samples older than this are ignored
    stale_seconds: float = 30.0     # latest sample older than this fails the check
    dwell_seconds: float = 10.0
    absorb_ratio: float = 0.5
    max_pull_ratio: float = 0.3
    min_samples: int = 3
    max_snapshots: int = 40         # memory cap per symbol


@dataclass(frozen=True)
class BidSnapshot:
    ts: float
    bids: Tuple[Tuple[float, float], ...]   # (price, qty), best first
    ltp: Optional[float]
    volume: Optional[float]                  # cumulative day volume


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: Tuple[str, ...] = ()
    metrics: Dict[str, Any] = field(default_factory=dict)


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def snapshot_from_replies(ts: float, depth_reply: Any, quote_reply: Any) -> Optional[BidSnapshot]:
    """Build a snapshot from one depth and one quote reply. None when the depth is unusable."""
    if not isinstance(depth_reply, dict) or str(depth_reply.get("status", "")).lower() != "success":
        return None
    data = depth_reply.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("bids"), list):
        return None
    bids = []
    for lvl in data["bids"]:
        if not isinstance(lvl, dict):
            continue
        px, qty = _num(lvl.get("price")), _num(lvl.get("quantity", lvl.get("qty")))
        if px is not None and px > 0 and qty is not None and qty >= 0:
            bids.append((px, qty))
    if not bids:
        return None
    ltp = volume = None
    if isinstance(quote_reply, dict) and str(quote_reply.get("status", "")).lower() == "success" \
            and isinstance(quote_reply.get("data"), dict):
        q = quote_reply["data"]
        ltp = _num(q.get("ltp"))
        volume = _num(q.get("volume"))
    return BidSnapshot(ts=float(ts), bids=tuple(bids), ltp=ltp, volume=volume)


def _qty_at(snap: BidSnapshot, level: float) -> float:
    for px, qty in snap.bids:
        if abs(px - level) < _EPS:
            return qty
    return 0.0


class BidVerifier:
    """Keeps recent snapshots per symbol and checks a bid level on demand."""

    def __init__(self, cfg: Optional[VerifyConfig] = None) -> None:
        self.cfg = cfg or VerifyConfig()
        self._snaps: Dict[str, Deque[BidSnapshot]] = {}

    def observe(self, symbol: str, ts: float, depth_reply: Any, quote_reply: Any) -> bool:
        """Store one poll. Returns False when the depth reply was unusable (nothing stored)."""
        snap = snapshot_from_replies(ts, depth_reply, quote_reply)
        if snap is None:
            return False
        dq = self._snaps.setdefault(str(symbol), deque(maxlen=self.cfg.max_snapshots))
        dq.append(snap)
        while dq and dq[0].ts < snap.ts - self.cfg.window_seconds:
            dq.popleft()
        return True

    def verdict(self, symbol: str, level: float, now_ts: float) -> Verdict:
        """Check one bid level. ok only when all three checks pass."""
        cfg = self.cfg
        snaps = list(self._snaps.get(str(symbol), ()))
        snaps = [s for s in snaps if s.ts >= now_ts - cfg.window_seconds]
        if len(snaps) < cfg.min_samples:
            return Verdict(False, ("insufficient_samples",), {"samples": len(snaps)})
        latest = snaps[-1]
        if now_ts - latest.ts > cfg.stale_seconds:
            return Verdict(False, ("stale_samples",), {"age_s": round(now_ts - latest.ts, 1)})
        if _qty_at(latest, level) <= 0:
            return Verdict(False, ("level_gone",), {"level": level})
        first = next((i for i, s in enumerate(snaps) if _qty_at(s, level) > 0), None)
        if first is None:
            return Verdict(False, ("level_gone",), {"level": level})
        seq = snaps[first:]
        initial = _qty_at(seq[0], level)
        absorbed = pulled = 0.0
        for a, b in zip(seq, seq[1:]):
            qa, qb = _qty_at(a, level), _qty_at(b, level)
            if qa <= 0 or qb <= 0:
                continue
            vol_delta = 0.0
            if a.volume is not None and b.volume is not None and b.volume > a.volume:
                vol_delta = b.volume - a.volume
            traded = vol_delta if (b.ltp is not None and b.ltp <= level + _EPS) else 0.0
            absorbed += traded
            pulled += max(0.0, max(0.0, qa - qb) - traded)
        metrics: Dict[str, Any] = {"level": level, "initial_qty": initial,
                                   "absorbed_qty": absorbed, "pulled_qty": pulled}
        reasons = []
        absorb_ratio = absorbed / initial if initial > 0 else 0.0
        metrics["absorb_ratio"] = round(absorb_ratio, 4)
        if absorb_ratio < cfg.absorb_ratio:
            reasons.append("absorption_low")
        denom = initial + absorbed
        pull_ratio = pulled / denom if denom > 0 else 1.0
        metrics["pull_ratio"] = round(pull_ratio, 4)
        if pull_ratio > cfg.max_pull_ratio:
            reasons.append("pull_high")
        # Dwell and bounce: last touch at or below the level, then a recovery above it.
        last_touch = None
        for i, s in enumerate(seq):
            if s.ltp is not None and s.ltp <= level + _EPS:
                last_touch = i
        recover = None
        if last_touch is None:
            reasons.append("no_touch")
        else:
            recover = next((i for i in range(last_touch + 1, len(seq))
                            if seq[i].ltp is not None and seq[i].ltp > level + _EPS), None)
            if recover is None or seq[-1].ltp is None or seq[-1].ltp <= level + _EPS:
                reasons.append("no_bounce")
        if recover is not None and "no_bounce" not in reasons:
            dwell = latest.ts - seq[recover].ts
            metrics["dwell_s"] = round(dwell, 1)
            if dwell < cfg.dwell_seconds:
                reasons.append("dwell_short")
        return Verdict(not reasons, tuple(reasons), metrics)



__all__ = ["BidVerifier", "BidSnapshot", "VerifyConfig", "Verdict", "snapshot_from_replies"]
