"""Hand-checked tests for POV Phase D (paper-only engine loop). Fake clock and sleep, no waiting."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quant.core.types import GateDecision, TradeIntent
from quant.execution.pov_engine import PaperChildSender, PovEngineLoop, PovRunConfig, PovSnapshot
from quant.execution.pov_executor import PovExecutor
from quant.execution.pov_guards import TopOfBook

IST = ZoneInfo("Asia/Kolkata")
NOON = datetime(2026, 11, 9, 12, 0, tzinfo=IST)  # outside every auction window


def _top(bid, ask, bid_qty=10_000):
    return TopOfBook(best_bid=bid, bid_qty=bid_qty, best_ask=ask, ask_qty=10_000, ltp=bid,
                     upper_circuit=ask * 1.2, lower_circuit=bid * 0.8, tick_size=0.05,
                     auction_price=None, auction_volume=None, source="depth")


class Feed:
    """Returns the scripted snapshots in order; the last one repeats; None means a miss."""

    def __init__(self, *snaps):
        self._snaps = list(snaps)
        self._i = 0

    def snapshot(self, symbol):
        s = self._snaps[min(self._i, len(self._snaps) - 1)]
        self._i += 1
        return s


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Audit:
    def __init__(self):
        self.rows = []

    def append(self, kind, payload):
        self.rows.append((kind, payload))
        return str(len(self.rows))


def _parent(qty=100, side="BUY"):
    return TradeIntent(intent_id="P1", strategy_id="S", symbol="INFY", side=side, qty=qty,
                       ref_price=100.0, kind="entry", lot_size=1, trade_day=None)


def _run(feed, *, qty=100, side="BUY", approve=True, cfg=None, sender=None):
    clock = Clock()
    audit = Audit()
    sender = sender or PaperChildSender()
    gate = lambda i: GateDecision(intent_id=i.intent_id, approved=approve,
                                  reasons=() if approve else ("blocked by test",))
    ex = PovExecutor(gate=gate, sender=sender, api_key="k", audit=audit)
    base = {"now": NOON}
    cfg = cfg or PovRunConfig(pov_pct=10.0, max_polls=5, poll_seconds=1.0, guard=base)
    cfg.guard = {**base, **cfg.guard}
    loop = PovEngineLoop(executor=ex, sender=sender, api_key="k", source=feed, audit=audit,
                         config=cfg, clock=clock, sleep=clock.sleep)
    return loop.run(_parent(qty, side)), sender, audit


def test_non_paper_sender_is_refused():
    class Live:
        def place_child(self, *a, **k):
            raise AssertionError("must not be called")

    ex = PovExecutor(gate=lambda i: GateDecision(i.intent_id, True), sender=Live(), api_key="k",
                     audit=Audit())
    with pytest.raises(TypeError, match="paper only"):
        PovEngineLoop(executor=ex, sender=Live(), api_key="k", source=Feed(None), audit=Audit(),
                      config=PovRunConfig())


def test_passive_child_rests_then_fills_when_ask_drops_to_limit():
    # Poll 1: bid 100.00 / ask 100.10, delta 500 -> slice 10% = 50. Passive BUY rests at 100.00.
    # Poll 2: ask drops to 99.95 (<= 100.00 limit) -> resting child fills 50 at reconcile.
    # Then the peg resets and a new passive child of 50 rests at the new bid 99.90.
    # Poll 3 holds the same peg. max_polls ends the run: the resting child is cancelled.
    feed = Feed(PovSnapshot(_top(100.00, 100.10), 500.0),
                PovSnapshot(_top(99.90, 99.95), 500.0),
                PovSnapshot(_top(99.90, 99.95), 500.0))
    res, sender, _ = _run(feed, qty=100)
    assert res.status == "timeout"
    assert res.filled == 50
    assert res.remaining == 50
    assert res.child_count == 2
    kinds = [e["kind"] for e in res.events]
    assert kinds.index("rest") < kinds.index("fill") < kinds.index("cancel")
    assert sender.open_order_ids == ()  # timeout cleanup left nothing resting


def test_peg_cross_on_patience_replaces_resting_child_and_completes():
    # Patience 0.5 s, poll 1 s. Poll 1: passive child of 100 rests at 100.00.
    # Poll 2 (t=1): patience over, peg crosses at ask 100.10 + 1 tick = 100.15.
    # The stale resting child is cancelled, and the new 100-share LIMIT at 100.15 crosses the ask
    # and fills in full. Remaining 0 -> complete.
    cfg = PovRunConfig(pov_pct=10.0, max_polls=5, poll_seconds=1.0,
                       peg={"peg_patience_seconds": 0.5, "peg_escape_buffer_ticks": 1})
    feed = Feed(PovSnapshot(_top(100.00, 100.10), 1000.0))
    res, sender, _ = _run(feed, qty=100, cfg=cfg)
    assert res.status == "complete"
    assert res.filled == 100 and res.remaining == 0
    assert res.child_count == 2
    kinds = [e["kind"] for e in res.events]
    assert "cancel" in kinds
    assert kinds[-1] == "fill"
    assert sender.open_order_ids == ()


def test_gate_refusal_stops_run_and_places_nothing():
    res, sender, audit = _run(Feed(PovSnapshot(_top(100.00, 100.10), 500.0)), approve=False)
    assert res.status == "refused"
    assert res.filled == 0 and res.remaining == 100
    assert res.child_count == 0
    assert sender.open_order_ids == ()
    assert any(k == "gate" for k, _ in audit.rows)


def test_missing_snapshots_fail_closed_after_limit():
    cfg = PovRunConfig(pov_pct=10.0, max_polls=20, poll_seconds=1.0, max_missing_snapshots=2)
    res, sender, _ = _run(Feed(None), qty=100, cfg=cfg)
    assert res.status == "data_unavailable"  # misses 1, 2 allowed; the 3rd stops the run
    assert res.child_count == 0 and res.filled == 0
    assert [e["count"] for e in res.events if e["kind"] == "snapshot_missing"] == [1, 2, 3]


def test_zero_volume_never_places_a_child_and_times_out():
    res, sender, _ = _run(Feed(PovSnapshot(_top(100.00, 100.10), 0.0)), qty=100)
    assert res.status == "timeout"
    assert res.child_count == 0 and res.remaining == 100


def test_child_qty_capped_by_max_slice():
    cfg = PovRunConfig(pov_pct=10.0, max_slice=20, max_polls=1, poll_seconds=1.0)
    res, _, _ = _run(Feed(PovSnapshot(_top(100.00, 100.10), 5000.0)), qty=100, cfg=cfg)
    rest = [e for e in res.events if e["kind"] == "rest"]
    assert rest and rest[0]["qty"] == 20  # 10% of 5000 = 500, capped to 20


def test_sell_side_fills_when_bid_rises_to_limit():
    # SELL passive at ask 100.10. Next poll the bid rises to 100.15 (>= limit) -> SELL limit
    # 100.10 <= bid 100.15 crosses -> fills 50.
    feed = Feed(PovSnapshot(_top(100.00, 100.10), 500.0),
                PovSnapshot(_top(100.15, 100.20), 500.0))
    cfg = PovRunConfig(pov_pct=10.0, max_polls=2, poll_seconds=1.0, guard={"now": NOON})
    res, _, _ = _run(feed, qty=100, side="SELL", cfg=cfg)
    assert res.filled == 50
