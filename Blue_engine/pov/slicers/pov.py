"""pov.py - Percentage-of-Volume (POV) execution slicer."""
from __future__ import annotations
import time, uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from blue_engine.core.auction_windows import is_within_auction_window
from blue_engine.core.band import BandConfig, compute_target_pov_rate
from blue_engine.core.benchmark import ArrivalPriceWindow, SlippageAccumulator
from blue_engine.core.lot import lot_quantity, resolve_lot_size
from blue_engine.core.mode import read_mode
from blue_engine.core.quote_health import QuoteHealthMonitor, quote_frame_is_valid
from blue_engine.core.reverse_exit import resolve_restricted_flag, same_day_reverse_exit_blocked
from blue_engine.core.symbol_meta import is_sme_periodic_call_auction
from blue_engine.core.throttle import should_throttle_order
from blue_engine.core.time_utils import ist_now, is_past_cutoff
from blue_engine.core.volume_cache import get_quote as _get_quote_from_ws
from .base import BaseSlicer
from .guards import (AuctionPriceGuard, AuctionWindowGuard, CircuitGuard,
    DepthPricingGuard, DepthSliceCapGuard, evaluate_guards, fetch_top_of_book)
_DEFAULT_LIMIT_EXPIRY_PCT = 80.0
_LAST_ORDER_AT: Dict[str, datetime] = {}
_LAST_CANCEL_AT: Dict[str, datetime] = {}
class _LocalThrottleSnapshot:
    def __init__(self, *, order_count: int, filled_quantity: int,
                 last_order_at=None, last_cancel_at=None,
                 exit_filled_quantity: int = 0) -> None:
        self.order_count = int(order_count)
        self.filled_quantity = int(filled_quantity)
        self.exit_filled_quantity = int(exit_filled_quantity)
        self.last_order_at = last_order_at
        self.last_cancel_at = last_cancel_at

class POVSlicer(BaseSlicer):
    """Percentage-of-Volume execution slicer."""

    def __init__(self, client: Any, config: Dict[str, Any]) -> None:
        super().__init__(client, config)
        self.action: str = str(config.get("action", "BUY")).upper()
        if self.action not in ("BUY", "SELL"):
            raise ValueError(f"action must be BUY or SELL, got {self.action!r}")
        self.quantity: int = int(
            config.get("quantity") or config.get("total_quantity")
            or config.get("target_quantity") or 0)
        if self.quantity <= 0:
            raise ValueError("quantity must be a positive integer")
        self.pct: float = float(config.get("participation_pct", 10.0))
        if not (0 < self.pct <= 100):
            raise ValueError("participation_pct must be between 0 and 100")
        self.duration: int = int(
            config.get("duration") or config.get("duration_seconds", 300))
        self.interval: int = int(
            config.get("interval") or config.get("poll_interval_seconds", 15))
        self.symbol: str = str(config.get("symbol", ""))
        self.exchange: str = str(config.get("exchange", ""))
        self.product: str = str(config.get("product", "CNC")).upper()
        self.order_mode: str = str(config.get("order_mode", "MARKET")).upper()
        self.max_slice: Optional[int] = (
            int(config["max_slice_quantity"])
            if config.get("max_slice_quantity") is not None else None)
        self.exit_pct: float = float(
            config.get("exit_participation_pct", 30.0))
        self.limit_expiry_pct: float = float(
            config.get("limit_expiry_pct", _DEFAULT_LIMIT_EXPIRY_PCT))
        self.cutoff: Optional[str] = config.get("cutoff_time")
        self.squareoff: Optional[str] = config.get("squareoff_time")
        self.ceiling_price: Optional[float] = (
            float(config["ceiling_price"])
            if config.get("ceiling_price") is not None else None)
        self.stop_loss_price: Optional[float] = (
            float(config["stop_loss_price"])
            if config.get("stop_loss_price") is not None else None)
        # The operator's tag. Callers (config_loader, runner, pov_engine) inject
        # this as "strategy_label"; some legacy configs used "strategy". Accept
        # both, preferring the canonical "strategy_label", so the tag sent to the
        # OpenAlgo orderbook and the ledger execution_id actually match the run.
        self.strategy_label: str = str(
            config.get("strategy_label") or config.get("strategy") or "POV")
        self.sme_periodic_call_auction: Optional[bool] = (
            None if config.get("sme_periodic_call_auction") is None
            else bool(config["sme_periodic_call_auction"]))
        self.symbol_info: Any = config.get("symbol_info")
        #: The action used for the *current* order. Starts as the entry action
        #: and flips to the opposite side once the run enters the exit phase,
        #: so a stop-loss / square-off actually closes the position instead of
        #: buying (or selling) more.
        self.order_action: str = self.action
    def _entry_action(self) -> str:
        return self.action
    def _exit_action(self) -> str:
        return "SELL" if self.action == "BUY" else "BUY"

    def _sync_order_action(self, phase: str) -> None:
        '''Point ``order_action`` at the side the phase requires.'''
        self.order_action = (
            self._exit_action() if phase == "exiting" else self._entry_action()
        )

    def _cap_child_quantity(
        self, slice_qty: int, remaining_qty: int, target_qty: int
    ) -> int:
        '''Allow specialized slicers to cap a proposed child quantity.'''
        return slice_qty
    def execute(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        mode = read_mode(self.client)
        self.log(f"starting POV | mode={mode} | {self.symbol}/{self.exchange} "
                 f"| {self.action} {self.quantity}@{self.pct}% "
                 f"every {self.interval}s for {self.duration}s")
        now = ist_now()
        if self.cutoff and is_past_cutoff(self.cutoff, now=now):
            self.log(f"cutoff {self.cutoff} past; exiting")
            return {"filled": 0, "target": self.quantity,
                    "child_orders": 0, "mode": mode,
                    "reason": "cutoff_reached_at_start"}
        lot = resolve_lot_size(self.client, symbol=self.symbol,
                              exchange=self.exchange,
                              override=self.config.get("lot_size"))
        self.log(f"lot_size={lot} resolved" if lot
                  else "no lot size; rounding disabled")
        upper_circuit: Optional[float] = None
        lower_circuit: Optional[float] = None
        try:
            sym = self.symbol_info
            if sym is None:
                sym = getattr(self.client, "symbol",
                              lambda s, e: None)(self.symbol, self.exchange)
            if isinstance(sym, dict):
                upper_circuit = sym.get("upper_circuit")
                lower_circuit = sym.get("lower_circuit")
        except Exception as exc:
            self.log(f"circuit probe failed (non-fatal): {exc}")
        execution_id = self._create_execution_record(mode)
        summary = self._run(execution_id=execution_id, mode=mode,
                           lot_size=lot,
                           upper_circuit=upper_circuit,
                           lower_circuit=lower_circuit)
        summary["execution_id"] = execution_id
        return summary

    def _create_execution_record(self, mode: str) -> str:
        try:
            from blue_engine.core import ledger
            execution_id = f"{self.strategy_label}-{uuid.uuid4().hex[:8]}"
            ledger.create_execution(
                execution_id=execution_id,
                strategy=self.strategy_label,
                symbol=self.symbol, exchange=self.exchange,
                action=self.action, total_quantity=self.quantity,
                mode=mode, phase="accumulating")
            return execution_id
        except Exception as exc:
            self.log(f"ledger.create_execution failed: {exc!r}")
            return f"{self.strategy_label}-{uuid.uuid4().hex[:8]}"

    def _run(self, *, execution_id: str, mode: str,
             lot_size: Optional[int],
             upper_circuit: Optional[float],
             lower_circuit: Optional[float]) -> Dict[str, Any]:
        throttle: Dict[str, Any] = self.config.copy()
        prev_volume: Optional[int] = None
        session_vwap_sum: float = 0.0
        session_vwap_vol: int = 0
        # Buyer-initiated VWAP (LTP >= ask). The band is anchored on this.
        buyer_vwap_sum: float = 0.0
        buyer_vwap_vol: int = 0
        arrival_window = ArrivalPriceWindow(max_ticks=20)
        slippage_acc = SlippageAccumulator(window=10)
        sme_cfg = (
            self.sme_periodic_call_auction
            if self.sme_periodic_call_auction is not None
            else is_sme_periodic_call_auction(self.symbol_info))
        restricted = resolve_restricted_flag(self.symbol_info, self.config)
        target_qty = self.quantity
        filled_qty = 0
        child_count = 0
        child_log: List[Dict[str, Any]] = []
        phase = "accumulating"
        poll_sec = self.interval
        quote_health = QuoteHealthMonitor(
            threshold=int(self.config.get("quote_failure_threshold", 3)))
        end_ts = (
            datetime.now() + timedelta(seconds=self.duration)).timestamp()
        now_ts = datetime.now().timestamp()
        self.log(f"loop starts | target={target_qty} | phase={phase}")
        while now_ts < end_ts and filled_qty < target_qty:
            # PAUSED: skip poll when quote feed is consistently down.
            if quote_health.should_pause():
                snap = quote_health.snapshot()
                self.log(f"PAUSED after {snap['failures']} failures; terminated")
                break
            # Feature X: WS-backed volume cache (live vol; REST fallback if empty).
            quote = _get_quote_from_ws(
                symbol=self.symbol, exchange=self.exchange, client=self.client)
            # Feature 12: quote validity check.
            if not quote_frame_is_valid(quote):
                reason = (quote.get("message", "invalid")
                          if isinstance(quote, dict) else "non-dict")
                if quote_health.record_failure(str(reason)):
                    self.log(f"quote health -> PAUSED")
                    break
                self.log(f"quote invalid ({reason}); sleep {poll_sec}s")
                time.sleep(poll_sec)
                now_ts = datetime.now().timestamp()
                continue
            quote_health.record_success()
            qd = quote.get("data") or {}
            ltp = _safe_float(qd.get("ltp"))
            volume = _safe_int(qd.get("volume"))
            ask = _safe_float(qd.get("ask"))
            # Time-of-day phase transitions.
            now_ist = ist_now()
            now_ts = now_ist.timestamp()
            if self.squareoff and is_past_cutoff(self.squareoff, now=now_ist):
                self.log("squareoff_time reached; exiting")
                phase = "exiting"
            elif self.cutoff and is_past_cutoff(self.cutoff, now=now_ist):
                self.log("cutoff_time reached; exit phase")
                phase = "exiting"
            # Keep the working side in step with the phase so exits reverse.
            self._sync_order_action(phase)
            # Core POV volume gate.
            delta_vol = 0
            slice_qty = 0
            if prev_volume is None:
                self.log(f"first poll: vol={volume}")
            elif volume > prev_volume:
                delta_vol = volume - prev_volume
                pct = self.exit_pct if phase == "exiting" else self.pct
                slice_qty = int((delta_vol * pct) / 100.0)
            else:
                self.log(f"vol stalled {volume}<={prev_volume}; no slice")
            prev_volume = volume
            # Session VWAP accumulation (Feature 13).
            if delta_vol > 0 and ltp > 0:
                session_vwap_sum += ltp * delta_vol
                session_vwap_vol += delta_vol
                arrival_window.record(price=ltp, volume_weight=delta_vol)
            # Buyer-initiated VWAP accumulation: only ticks where LTP >= ask.
            # This drives the band anchors passed into BandConfig below.
            if delta_vol > 0 and ltp > 0 and ask > 0 and ltp >= ask:
                buyer_vwap_sum += ltp * delta_vol
                buyer_vwap_vol += delta_vol
            # Feature 5: SME/PCAS window guard.
            if sme_cfg and not is_within_auction_window():
                self.log(f"SME/PCAS window inactive; skip at ltp={ltp}")
                time.sleep(poll_sec)
                now_ts = datetime.now().timestamp()
                continue
            # Price-side LIMIT guards.
            if slice_qty > 0 and self.order_mode == "LIMIT":
                if self.action == "BUY" and self.ceiling_price                         and ltp > self.ceiling_price:
                    self.log(f"BUY LIMIT ltp={ltp}>{self.ceiling_price}; skip")
                    slice_qty = 0
                if self.action == "SELL" and self.ceiling_price                         and ltp < self.ceiling_price:
                    self.log(f"SELL LIMIT ltp={ltp}<{self.ceiling_price}; skip")
                    slice_qty = 0
            # Stop-loss -> exit phase.
            if slice_qty > 0 and self.stop_loss_price:
                if self.action == "BUY" and ltp <= self.stop_loss_price:
                    self.log(f"stop_loss ltp={ltp}<={self.stop_loss_price}; exit")
                    phase = "exiting"
                    self._sync_order_action(phase)
                    slice_qty = 0
                elif self.action == "SELL" and ltp >= self.stop_loss_price:
                    self.log(f"stop_loss ltp={ltp}>={self.stop_loss_price}; exit")
                    phase = "exiting"
                    self._sync_order_action(phase)
                    slice_qty = 0
            # Feature 13: band-based dynamic rate.
            if slice_qty > 0:
                band_cfg = BandConfig(
                    vwap=(buyer_vwap_sum/buyer_vwap_vol
                          if buyer_vwap_vol > 0
                          else (session_vwap_sum/session_vwap_vol
                                if session_vwap_vol > 0 else None)),
                    vwap_ceiling_pct=float(self.config.get("vwap_ceiling_pct",0.5)),
                    vwap_floor_pct=float(self.config.get("vwap_floor_pct",3.0)),
                    min_pov_pct=float(self.config.get("min_pov_pct",5.0)),
                    max_pov_pct=float(self.config.get("max_pov_pct",25.0)),
                    part_pct=self.pct,
                    passthrough_floor=self.config.get("passthrough_floor","part_pct"),
                    action=self.action)
                decision = compute_target_pov_rate(ltp=ltp, config=band_cfg)
                self.log(f"rate={decision.rate:.2f}% [{decision.reason}]")
                slice_qty = int((delta_vol * decision.rate) / 100.0)
            # Re-anchor the fill count from the durable ledger so partial and
            # delayed fills are accounted for. The in-memory counter is only a
            # fallback for when the ledger has nothing to report.
            try:
                from blue_engine.core import ledger as _ledger
                durable_filled = _ledger.recover_filled_quantity(execution_id)
            except Exception:
                durable_filled = None
            if durable_filled:
                if durable_filled != filled_qty:
                    self.log(f"fill sync: {filled_qty} -> {durable_filled}")
                filled_qty = durable_filled
            if slice_qty > 0:
                remaining = target_qty - filled_qty
                if slice_qty > remaining: slice_qty = remaining
            if slice_qty > 0 and self.max_slice and slice_qty > self.max_slice:
                slice_qty = self.max_slice
            if slice_qty > 0 and lot_size:
                slice_qty = lot_quantity(slice_qty, lot_size)
                if slice_qty == 0: self.log("lot rounding consumed slice")
            if slice_qty > 0:
                slice_qty = self._cap_child_quantity(
                    slice_qty, target_qty - filled_qty, target_qty
                )

            if slice_qty > 0 and restricted:
                if same_day_reverse_exit_blocked(request_data=self.config,
                    last_fill_trade_date=None, intent_action=self.action,
                    current_phase=phase):
                    self.log("same-day reverse exit blocked; skip slice")
                    slice_qty = 0
            guard_price: Optional[float] = None
            if slice_qty > 0:
                # Fetch depth + quote once; keyword-only (see guards.fetch_top_of_book).
                tob = fetch_top_of_book(
                    self.client, symbol=self.symbol,
                    exchange=self.exchange, cfg=self.config)
                # Note: the DepthPricingGuard/DepthSliceCapGuard/CircuitGuard below
                # are constructed with a *top-of-book snapshot* argument in their
                # ``tob`` field, but their ``evaluate()`` signature is the unified
                # ``(top, *, action, cfg)``. We therefore drive them through
                # ``evaluate_guards`` with the snapshot, not via the ctors.
                guards = [
                    CircuitGuard(),
                    DepthPricingGuard(),
                    DepthSliceCapGuard(),
                ]
                if sme_cfg:
                    guards += [AuctionWindowGuard(), AuctionPriceGuard()]
                # Inject the computed slice_qty and the static limit price so the
                # depth-cap and pricing guards can actually see what they guard.
                guard_cfg = dict(self.config)
                guard_cfg["slice_qty"] = slice_qty
                guard_cfg["action"] = self.order_action
                if self.order_mode == "LIMIT" and self.config.get("limit_price") is not None:
                    guard_cfg["limit_price"] = float(self.config["limit_price"])
                result = evaluate_guards(
                    tob, action=self.order_action, cfg=guard_cfg,
                    guards=[g for g in guards if g])
                # 1. A pause (circuit / auction window) aborts this slice.
                if result.pause_reason:
                    self.log(f"guard paused: {result.pause_reason}; skip slice")
                    slice_qty = 0
                # 2. Apply the depth-aware LIMIT price when the guard produced one.
                #    AuctionPriceGuard wins (it is evaluated before DepthPricingGuard
                #    so the rightmost-wins merge already gave us the auction anchor).
                if slice_qty > 0 and result.new_price is not None:
                    guard_price = float(result.new_price)
                # 3. Apply the depth cap (best-opposite-tier displayed quantity).
                if slice_qty > 0 and result.capped_qty is not None:
                    if result.capped_qty <= 0:
                        self.log("guard capped slice to 0 (below min depth qty); skip")
                        slice_qty = 0
                    elif result.capped_qty < slice_qty:
                        self.log(f"guard capped slice {slice_qty} -> {result.capped_qty}")
                        slice_qty = result.capped_qty
            if slice_qty > 0:
                thsnap = _LocalThrottleSnapshot(
                    order_count=child_count, filled_quantity=filled_qty,
                    last_order_at=_LAST_ORDER_AT.get(execution_id),
                    last_cancel_at=_LAST_CANCEL_AT.get(execution_id))
                throttled, delay, reason = should_throttle_order(
                    execution=thsnap, request_data=throttle)
                if throttled:
                    self.log(f"throttled ({reason}); sleep {delay:.1f}s")
                    time.sleep(max(0.1, delay))
                    now_ts = datetime.now().timestamp(); continue
            if self.order_mode == "LIMIT" and child_count > 0:
                self._cancel_stale_limits(
                    client=self.client, symbol=self.symbol,
                    exchange=self.exchange, strategy_label=self.strategy_label,
                    limit_expiry_pct=self.limit_expiry_pct,
                    execution_id=execution_id)
            if slice_qty > 0:
                # Prefer the guard-computed depth-aware price; fall back to the
                # static limit_price for LIMIT runs, else None (MARKET).
                if self.order_mode == "LIMIT":
                    place_price = (
                        guard_price if guard_price is not None
                        else (float(self.config["limit_price"])
                              if self.config.get("limit_price") is not None else None))
                else:
                    place_price = None
                self.log(f"[poll {child_count+1}] {self.order_action} {slice_qty} "
                          f"{self.symbol}/{self.exchange} {self.order_mode} "
                          f"@ {place_price or 'MARKET'}")
                try:
                    arrival_window.clear()
                    resp = self.client.placeorder(
                        strategy=self.strategy_label, symbol=self.symbol,
                        action=self.order_action, exchange=self.exchange,
                        price_type=self.order_mode, product=self.product,
                        quantity=slice_qty, price=place_price)
                except Exception as exc:
                    self.log(f"placeorder raised {exc!r}")
                    time.sleep(poll_sec); now_ts = datetime.now().timestamp(); continue
                self._record_child(execution_id, resp, slice_qty, ltp, mode)
                if isinstance(resp, dict) and resp.get("status") == "success":
                    oid = resp.get("order_id") or (resp.get("data") or {}).get("order_id")
                    child_count += 1; _LAST_ORDER_AT[execution_id] = datetime.now()
                    child_log.append({"attempted": slice_qty, "order_id": oid,
                                      "ltp_at_place": ltp, "phase": phase})
                    self.log(f"child placed: oid={oid}")
                else:
                    msg = resp.get("message") if isinstance(resp, dict) else str(resp)
                    self.log(f"rejected: {msg}")
            time.sleep(poll_sec); now_ts = datetime.now().timestamp()
        self.log(f"done: filled={filled_qty}/{target_qty} children={child_count} phase={phase}")
        return {"filled":filled_qty, "target":target_qty, "child_orders":child_count,
                "mode":mode, "phase":phase, "execution_id":execution_id, "children":child_log}

    def _record_child(self, execution_id: str, response: Any,
                      quantity: int, ltp: float, mode: str) -> None:
        try:
            from blue_engine.core import ledger
            order_id: Optional[str] = None
            status = "open"
            avg_price = ltp
            if isinstance(response, dict):
                if response.get("status") == "success":
                    order_id = (response.get("order_id")
                                or (response.get("data") or {}).get("order_id"))
                    avg_price = _safe_float(
                        (response.get("data") or {}).get("average_price", ltp))
                else:
                    status = "rejected"
            ledger.record_child(
                execution_id=execution_id, order_id=order_id,
                quantity=quantity, ltp=ltp, status=status, avg_price=avg_price,
                # Only optimistically credit the fill when the operator has
                # explicitly opted in. Default False: an order ack is NOT a fill,
                # so filled_quantity advances only when a real order-update event
                # reports it. Pre-crediting the full quantity at ack time made the
                # loop believe the parent was complete before any shares traded.
                assume_fill=self._assume_fill_on_ack())
        except Exception as exc:
            self.log(f"ledger.record_child failed: {exc!r}")

    def _assume_fill_on_ack(self) -> bool:
        """Whether a broker order ACK may be counted as a full fill.

        Disabled by default. Operators on brokers whose order-update feed is
        unreliable can set ``assume_fill_on_ack: true`` in the JSON config to
        fall back to the legacy behaviour, but doing so risks declaring the
        parent complete on an open, unfilled child.
        """
        return bool(self.config.get("assume_fill_on_ack", False))

    def _cancel_stale_limits(
        self, *, client: Any, symbol: str, exchange: str,
        strategy_label: str, limit_expiry_pct: float,
        execution_id: Optional[str] = None) -> None:
        try: ob = client.orderbook()
        except Exception as exc:
            self.log(f"orderbook read failed: {exc}"); return
        if not isinstance(ob, dict) or ob.get("status") != "success": return
        rows = ob.get("data", [])
        if isinstance(rows, dict): rows = rows.get("orders", []) or []
        for row in rows:
            if not isinstance(row, dict): continue
            pt = str(row.get("pricetype") or row.get("price_type", "")).lower()
            os_ = str(row.get("order_status", "")).lower()
            if (row.get("symbol") == symbol and row.get("exchange") == exchange
                    and (row.get("strategy") == strategy_label
                         or row.get("strategy") == strategy_label.split("-")[0])
                    and pt == "limit"
                    and os_ in ("open", "trigger pending", "pending")):
                oid = row.get("orderid")
                if not oid: continue
                cresp = client.cancelorder(order_id=oid, strategy=strategy_label)
                if isinstance(cresp, dict) and cresp.get("status") == "success":
                    self.log(f"cancelled stale LIMIT child {oid}")
                    if execution_id:
                        _LAST_CANCEL_AT[execution_id] = datetime.now()
                else:
                    msg = cresp.get("message") if isinstance(cresp, dict) else cresp
                    self.log(f"cancel failed {oid}: {msg}")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "": return default
        return float(value)
    except (TypeError, ValueError): return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "": return default
        return int(float(value))
    except (TypeError, ValueError): return default
