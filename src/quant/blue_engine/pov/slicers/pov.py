"""pov.py - Percentage-of-Volume (POV) execution slicer."""
from __future__ import annotations
import time, uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
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
        # Duration/interval are coerced with max(1, int(...)) rather than a
        # bare int(). A bare int() truncated any sub-second value to 0, and a
        # duration of 0 makes the execution loop's end_ts land in the past
        # before the first quote fetch - so the loop body never ran, no slice
        # was attempted, and a caller asking for a fast run silently got NO
        # run at all. Rounding up to 1s keeps every caller to at least one
        # poll, which is the only behaviour that can be called "executing".
        self.duration: int = max(1, int(
            config.get("duration") or config.get("duration_seconds", 300)))
        self.interval: int = max(1, int(
            config.get("interval") or config.get("poll_interval_seconds", 15)))
        self.symbol: str = str(config.get("symbol", ""))
        self.exchange: str = str(config.get("exchange", ""))
        self.product: str = str(config.get("product", "CNC")).upper()
        self.order_mode: str = str(config.get("order_mode", "MARKET")).upper()
        if self.order_mode not in ("MARKET", "LIMIT", "PEG"):
            raise ValueError(
                f"order_mode must be MARKET, LIMIT or PEG, got {self.order_mode!r}")
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
        self.target_price: Optional[float] = (
            float(config["target_price"])
            if config.get("target_price") is not None else None)
        # The operator's tag. Callers (config_loader, runner, pov_engine) inject
        # this as "strategy_label"; some legacy configs used "strategy". Accept
        # both, preferring the canonical "strategy_label", so the tag sent to the
        # OpenAlgo orderbook and the ledger execution_id actually match the run.
        self.strategy_label: str = str(
            config.get("strategy_label") or config.get("strategy") or "POV")
        #: Exact orderbook strategy tags THIS slicer may cancel or treat as its
        #: own working children. Default is only its own full label; the old
        #: first-dash-prefix fallback cancelled sibling strategies' orders.
        extra_labels = config.get("allowed_strategy_labels") or []
        if isinstance(extra_labels, str):
            extra_labels = [extra_labels]
        self.allowed_strategy_labels: set = {
            str(lbl) for lbl in ([self.strategy_label] + list(extra_labels)) if lbl}
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
        # Keep strategy-provided protection at the parent execution and
        # expose it to the slicer loop. For OCL the target may be None in
        # DYNAMIC mode; the trailing logic is controlled by the strategy.
        stop_loss = intent.get("stop_loss") if intent else None
        target_level = intent.get("target") if intent else None
        intent_meta = (intent or {}).get("_meta") or {}
        if stop_loss is not None:
            self.stop_loss_price = float(stop_loss)
        if target_level is not None:
            self.target_price = float(target_level)
        else:
            self.target_price = None
        try:
            from blue_engine.core.protective_orders import register
            register(
                execution_id, symbol=self.symbol, exchange=self.exchange,
                action=self.action,
                stop_loss=self.stop_loss_price,
                target=self.target_price,
                strategy=self.strategy_label, meta=intent_meta)
            if self.stop_loss_price is not None or self.target_price is not None:
                self.log(
                    f"protective levels registered: SL={self.stop_loss_price}, "
                    f"TP={self.target_price}, "
                    f"exit_mode={intent_meta.get('exit_mode')}"
                )
        except Exception as exc:                # noqa: BLE001
            self.log(f"protective-level registry failed (non-fatal): {exc}")
        # The intent carries the signal's confirmation band (daily buyer VWAP
        # + ceiling) at _meta.band. It must be passed INTO _run explicitly:
        # _run has no `intent` in scope, and reaching for it there raised
        # NameError, which silently prevented the band from ever reaching
        # the slicer.
        band_meta = intent_meta.get("band") or {}
        summary = self._run(execution_id=execution_id, mode=mode,
                           lot_size=lot,
                           upper_circuit=upper_circuit,
                           lower_circuit=lower_circuit,
                           band_meta=band_meta)
        summary["execution_id"] = execution_id
        summary["stop_loss"] = self.stop_loss_price
        summary["target_price"] = self.target_price
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
             lower_circuit: Optional[float],
             band_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        throttle: Dict[str, Any] = self.config.copy()
        prev_volume: Optional[int] = None
        session_vwap_sum: float = 0.0
        session_vwap_vol: int = 0
        # Buyer-initiated VWAP (LTP >= ask). The band is anchored on this.
        buyer_vwap_sum: float = 0.0
        buyer_vwap_vol: int = 0
        # CONFIRMATION BAND (from the signal's intent meta).
        # When the signal supplies a daily buyer-initiated VWAP band, that
        # band - not the session VWAP - drives the POV rate, and its ceiling
        # is a HARD HALT checked on every poll. Values are None when the
        # signal did not supply a band, in which case the slicer falls back
        # to its own session-VWAP band exactly as before.
        band_info = band_meta or {}
        band_vwap: Optional[float] = _safe_num(band_info.get("buyer_vwap"))
        band_ceiling: Optional[float] = _safe_num(band_info.get("ceiling_price"))
        band_ceiling_pct: float = _safe_num(
            band_info.get("ceiling_pct"), default=0.5)
        band_halt_count = 0
        if band_vwap is not None:
            self.log(
                f"confirmation band active: buyer_vwap={band_vwap:.2f} "
                f"ceiling={band_ceiling if band_ceiling is None else f'{band_ceiling:.2f}'} "
                f"({band_info.get('classification', 'unspecified')})"
            )
        arrival_window = ArrivalPriceWindow(max_ticks=20)
        slippage_acc = SlippageAccumulator(window=10)
        # PEG-1: passive/peg pricing engine. Only instantiated for PEG runs;
        # MARKET/LIMIT runs are untouched by the peg state machine.
        peg_engine = None
        peg_open_child = False   # a resting PEG child we believe is still working
        if self.order_mode == "PEG":
            from .peg import PegEngine
            peg_engine = PegEngine(self.config)
        sme_cfg = (
            self.sme_periodic_call_auction
            if self.sme_periodic_call_auction is not None
            else is_sme_periodic_call_auction(self.symbol_info))
        restricted = resolve_restricted_flag(self.symbol_info, self.config)
        target_qty = self.quantity
        filled_qty = 0
        child_count = 0
        #: order_id -> placement time, for orders THIS run placed only.
        own_oids: Dict[str, datetime] = {}
        child_log: List[Dict[str, Any]] = []
        phase = "accumulating"
        #: Tracks the phase the PEG engine last saw, so a flip into the exit
        #: phase resets the peg anchor. Declared here (after `phase` exists)
        #: because referring to `phase` above this point raised
        #: UnboundLocalError in a live run.
        peg_phase = phase
        poll_sec = self.interval
        #: Date of the first valid quote in this run; used to detect a session
        #: rollover (see the quote block below).
        trading_day_seen: Optional[str] = None
        summary_reason: Optional[str] = None
        quote_health = QuoteHealthMonitor(
            threshold=int(self.config.get("quote_failure_threshold", 3)))
        end_ts = (
            datetime.now() + timedelta(seconds=self.duration)).timestamp()
        #: AUDIT (2026-10-22, P1-3): the single wall-clock deadline is correct for
        #: a same-day POV run but wrong for a run launched after the square-off
        #: (e.g. after 15:20 IST, or into an SME/PCAS name outside the auction
        #: window). Those runs have nothing to trade and must fail closed rather
        #: than spin for the full ``duration`` and then report "filled=0" as if
        #: it were a market outcome.
        _now_ist = ist_now()
        if self.cutoff and is_past_cutoff(self.cutoff, now=_now_ist):
            self.log(f"cutoff {self.cutoff} already passed; nothing to do")
            return {"filled": 0, "target": target_qty,
                    "child_orders": 0, "mode": mode,
                    "phase": "aborted",
                    "reason": "cutoff_reached_at_start"}
        if self.squareoff and is_past_cutoff(self.squareoff, now=_now_ist):
            self.log(f"squareoff {self.squareoff} already passed; nothing to do")
            return {"filled": 0, "target": target_qty,
                    "child_orders": 0, "mode": mode,
                    "phase": "aborted",
                    "reason": "squareoff_reached_at_start"}
        now_ts = datetime.now().timestamp()
        self.log(f"loop starts | target={target_qty} | phase={phase}")
        while now_ts < end_ts and filled_qty < target_qty:
            # PAUSED: skip poll when quote feed is consistently down.
            if quote_health.should_pause():
                snap = quote_health.snapshot()
                self.log(f"PAUSED after {snap['failures']} failures; terminated")
                data_failure_handler = getattr(self, "on_data_failure", None)
                if callable(data_failure_handler):
                    data_failure_handler(
                        f"quote feed paused after {snap['failures']} failures: "
                        f"{snap['last_reason']}")
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
                    data_failure_handler = getattr(
                        self, "on_data_failure", None)
                    if callable(data_failure_handler):
                        data_failure_handler(
                            f"quote feed paused after "
                            f"{quote_health.snapshot()['failures']} failures: "
                            f"{reason}")
                    break
                self.log(f"quote invalid ({reason}); sleep {poll_sec}s")
                time.sleep(poll_sec)
                now_ts = datetime.now().timestamp()
                continue
            quote_health.record_success()
            qd = quote.get("data") or {}
            ltp = _safe_float(qd.get("ltp"))
            # AUDIT (2026-10-22, P1-4): ``volume`` is the broker's cumulative
            # traded volume FOR THE DAY. Delta-volume slicing and VWAP are only
            # valid inside one session; the moment the cache rolls over to the
            # next trading day, ``volume`` restarts near zero, the delta goes
            # negative and the POV loop silently stops slicing for the rest of
            # the run (or, worse, treats the reset as a huge positive delta if
            # the cache briefly reports a stale figure). A day rollover aborts
            # the execution leg so a human re-plans it.
            quote_day = str(
                qd.get("date") or qd.get("trading_date") or qd.get("ts_date") or "")
            if quote_day and trading_day_seen is None:
                trading_day_seen = quote_day
            elif quote_day and quote_day != trading_day_seen:
                self.log(f"session rollover detected ({trading_day_seen} -> "
                         f"{quote_day}); aborting before volume maths go wrong")
                data_failure_handler = getattr(self, "on_data_failure", None)
                if callable(data_failure_handler):
                    data_failure_handler(f"session rollover to {quote_day}")
                summary_reason = "session_rollover"
                break
            volume = _safe_int(qd.get("volume"))
            ask = _safe_float(qd.get("ask"))
            monitor_observer = getattr(self, "monitor_observer", None)
            if callable(monitor_observer):
                try:
                    safe_to_continue = monitor_observer()
                except Exception as exc:
                    self.log(f"live monitor failed: {exc!r}; "
                             "aborting execution")
                    break
                if not safe_to_continue:
                    self.log("live monitor could not verify safety; "
                             "aborting execution")
                    break
            # Time-of-day phase transitions.
            now_ist = ist_now()
            now_ts = now_ist.timestamp()
            # AUDIT (2026-10-22, P1-2): entering the exit phase used to flip the
            # working side but leave ``target_qty`` at the ENTRY quantity, so a
            # square-off of a partially filled entry tried to sell the full
            # parent size. If the entry had only filled 300 of 1,000 shares, the
            # exit phase would sell 1,000 - opening an unhedged short with real
            # money. The exit target is now pinned to what the entry actually
            # filled, and an exit with nothing filled terminates instead of
            # looping to the duration limit.
            if (self.squareoff and is_past_cutoff(self.squareoff, now=now_ist)) \
                    or (self.cutoff and is_past_cutoff(self.cutoff, now=now_ist)):
                if phase != "exiting":
                    phase = "exiting"
                    exit_target = filled_qty
                    self.log(
                        f"{'squareoff' if self.squareoff else 'cutoff'}_time "
                        f"reached; exit phase for {exit_target} filled shares")
                    target_qty = exit_target
                    if target_qty <= 0:
                        self.log("nothing was filled; no exit required")
                        break
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
            # Price-side LIMIT guards (PEG places LIMITs, so it obeys them too).
            if slice_qty > 0 and self.order_mode in ("LIMIT", "PEG"):
                if self.action == "BUY" and self.ceiling_price                         and ltp > self.ceiling_price:
                    self.log(f"BUY LIMIT ltp={ltp}>{self.ceiling_price}; skip")
                    slice_qty = 0
                if self.action == "SELL" and self.ceiling_price                         and ltp < self.ceiling_price:
                    self.log(f"SELL LIMIT ltp={ltp}<{self.ceiling_price}; skip")
                    slice_qty = 0
            # Protective levels are attached to the execution record and
            # consumed by the signal/position manager for post-entry exits.

            # ---- CEILING HALT: checked on EVERY poll, before any slicing ----
            # "price above ceiling -> stop slicing". This runs even when
            # delta_vol produced no slice this poll, because a run that has
            # stopped trading must still be seen to be halting rather than
            # silently idling. The ceiling is rescaled from the reference
            # VWAP so the halt stays meaningful as that reference ages.
            live_ceiling = band_ceiling
            if band_ceiling is None and band_vwap is not None:
                live_ceiling = band_vwap * (1.0 + band_ceiling_pct / 100.0)
            if live_ceiling is not None and _above(live_ceiling, ltp):
                band_halt_count += 1
                self.log(
                    f"CEILING HALT: ltp={ltp:.2f} > ceiling={live_ceiling:.2f} "
                    f"(halts={band_halt_count}); slicing stopped this poll"
                )
                slice_qty = 0
            # Feature 13: band-based dynamic rate.
            # AUDIT (2026-10-22, P1-1): ``arrival_window`` and ``slippage_acc``
            # were constructed but never used - the LIVE slippage feedback loop
            # (Phase 2) was advertised in the config/dashboard and silently
            # absent from the executor, so ``max_slippage_bps`` had no effect on
            # a live run. Implemented here.
            ref_price = arrival_window.vwap()
            rate_for_poll = self.pct if phase == "accumulating" else self.exit_pct
            if slice_qty > 0:
                # Prefer the signal's confirmation band when present; fall
                # back to the slicer's own session/buyer VWAP otherwise so
                # non-confirmed runs behave exactly as before.
                band_cfg = BandConfig(
                    vwap=(band_vwap if band_vwap is not None else
                          (buyer_vwap_sum/buyer_vwap_vol
                           if buyer_vwap_vol > 0
                           else (session_vwap_sum/session_vwap_vol
                                 if session_vwap_vol > 0 else None))),
                    vwap_ceiling_pct=(band_ceiling_pct if band_vwap is not None
                                      else float(self.config.get("vwap_ceiling_pct",0.5))),
                    vwap_floor_pct=float(self.config.get("vwap_floor_pct",3.0)),
                    min_pov_pct=float(self.config.get("min_pov_pct",5.0)),
                    max_pov_pct=float(self.config.get("max_pov_pct",25.0)),
                    part_pct=self.pct,
                    passthrough_floor=self.config.get("passthrough_floor","part_pct"),
                    action=self.action)
                decision = compute_target_pov_rate(ltp=ltp, config=band_cfg)
                self.log(f"rate={decision.rate:.2f}% [{decision.reason}]")
                rate_for_poll = decision.rate
                slice_qty = int((delta_vol * decision.rate) / 100.0)
            # ---- SLIPPAGE FEEDBACK (Phase 2) -------------------------------
            # Realised slippage per child = (fill-side move vs the arrival VWAP
            # of that slice). The rolling mean of the last N samples gates the
            # participation rate: over the cap, multiply by the penalty factor
            # and never go below min_pov_pct.
            if slice_qty > 0 and bool(self.config.get("slippage_guard_enabled", True)):
                window_bps = _safe_float(self.config.get("max_slippage_bps"), 50.0)
                current_bps = slippage_acc.current_bps()
                if window_bps > 0 and slippage_acc.is_over_threshold(window_bps):
                    penalty = _safe_float(
                        self.config.get("slippage_penalty_factor"), 0.5)
                    floor_pct = _safe_float(
                        self.config.get("min_pov_pct"), 5.0)
                    penalised = max(floor_pct, rate_for_poll * max(0.0, penalty))
                    self.log(
                        f"slippage {current_bps:.1f}bps > {window_bps:.1f}bps; "
                        f"rate {rate_for_poll:.2f}% -> {penalised:.2f}%")
                    slice_qty = int((delta_vol * penalised) / 100.0)
            # Re-anchor the fill count from the durable ledger so partial and
            # delayed fills are accounted for. The in-memory counter is only a
            # fallback for when the ledger has nothing to report.
            filled_before_sync = filled_qty
            try:
                from blue_engine.core import ledger as _ledger
                durable_filled = _ledger.recover_filled_quantity(execution_id)
            except Exception:
                durable_filled = None
            if durable_filled:
                if durable_filled != filled_qty:
                    self.log(f"fill sync: {filled_qty} -> {durable_filled}")
                filled_qty = durable_filled
            # PEG-1: a detected fill means the current slice is done; reset the
            # peg anchor and patience window so the next slice starts passive
            # again instead of inheriting a stale chase cap.
            if peg_engine is not None and filled_qty > filled_before_sync:
                self.log("peg: fill detected; peg state reset for next slice")
                peg_engine.reset_for_new_slice()
                peg_open_child = False
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
                if self.order_mode in ("LIMIT", "PEG") and self.config.get("limit_price") is not None:
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
                    execution_id=execution_id,
                    allowed_strategy_labels=self.allowed_strategy_labels,
                    own_oids=own_oids,
                    min_age_sec=self.limit_expiry_pct / 100.0 * poll_sec)
            price_type = self.order_mode
            # MUST be initialised before the branches below: MARKET runs never
            # enter the PEG/LIMIT arms, and reading an unassigned name here
            # raised UnboundLocalError - crashing every MARKET slice, which is
            # the default for a confirmation-band execution.
            place_price: Optional[float] = None
            if slice_qty > 0:
                if self.order_mode == "PEG":
                    # PEG-1 decides the price from the live top-of-book; the
                    # depth cap from the guards still applies to quantity.
                    if peg_phase != phase:
                        self.log("peg: phase change; peg state reset")
                        peg_engine.reset_for_new_slice()
                        peg_open_child = False
                        peg_phase = phase
                    decision = peg_engine.decide(
                        tob, action=self.order_action, now_ts=now_ts)
                    if decision.price is None:
                        self.log(f"peg skip ({decision.reason}); no child")
                    elif peg_open_child and decision.reason == "passive_at_touch":
                        # A resting child is already working at this price;
                        # never stack a second passive order on top of it.
                        self.log("peg holding resting child; skip placement")
                    else:
                        if peg_open_child:
                            # Peg moved (re-peg or cross): retire the old child.
                            self._cancel_stale_limits(
                                client=self.client, symbol=self.symbol,
                                exchange=self.exchange,
                                strategy_label=self.strategy_label,
                                limit_expiry_pct=self.limit_expiry_pct,
                                execution_id=execution_id,
                                allowed_strategy_labels=self.allowed_strategy_labels,
                                own_oids=own_oids, min_age_sec=0.0)
                            peg_open_child = False
                        place_price = decision.price
                        price_type = "LIMIT"   # PEG always works as LIMIT
                        self.log(f"peg: {decision.reason} @ {place_price}")
                elif self.order_mode == "LIMIT":
                    place_price = (
                        guard_price if guard_price is not None
                        else (float(self.config["limit_price"])
                              if self.config.get("limit_price") is not None else None))
                pre_order_check = getattr(self, "pre_order_check", None)
                # Kill-switch re-check on EVERY child (audit P1-6). The runner
                # only verified the switch once, before the signal wait.
                try:
                    halted = bool(self.kill_tripped and self.kill_tripped())
                    extra_halt = self.halt_check
                    if callable(extra_halt):
                        halted = halted or bool(extra_halt())
                except Exception as exc:  # noqa: BLE001 - fail closed
                    self.log(f"halt check failed: {exc!r}; aborting execution")
                    summary_reason = "halt_check_failed"
                    break
                if halted:
                    self.log("kill switch / halt active at child submission; "
                             "aborting execution")
                    summary_reason = "halted_by_kill_switch"
                    break
                if callable(pre_order_check):
                    try:
                        authorized_qty = pre_order_check(
                            phase=phase, quantity=slice_qty,
                            action=self.order_action, price=(
                                place_price if place_price is not None else ltp))
                    except Exception as exc:
                        self.log(f"pre-order safety check failed: {exc!r}; "
                                 "aborting execution")
                        break
                    if authorized_qty is None:
                        self.log("pre-order safety check blocked execution")
                        break
                    if (not isinstance(authorized_qty, int)
                            or isinstance(authorized_qty, bool)
                            or authorized_qty < 0
                            or authorized_qty > slice_qty):
                        self.log("pre-order safety check returned invalid "
                                 "quantity; aborting execution")
                        break
                    if authorized_qty == 0:
                        self.log("pre-order safety check paused this entry")
                        time.sleep(poll_sec)
                        now_ts = datetime.now().timestamp()
                        continue
                    slice_qty = authorized_qty
                self.log(f"[poll {child_count+1}] {self.order_action} {slice_qty} "
                          f"{self.symbol}/{self.exchange} {self.order_mode} "
                          f"@ {place_price or 'MARKET'}")
                try:
                    arrival_window.clear()
                    attempt_ts = datetime.now()
                    resp = self.client.placeorder(
                        strategy=self.strategy_label, symbol=self.symbol,
                        action=self.order_action, exchange=self.exchange,
                    price_type=("LIMIT" if self.order_mode == "PEG"
                                else self.order_mode), product=self.product,
                        quantity=slice_qty, price=place_price)
                except Exception as exc:
                    # A timeout can hide an order the broker accepted. Check the
                    # broker before any retry, or the retry doubles the exposure.
                    self.log(f"placeorder raised {exc!r}; reconciling with broker")
                    state, found_oid = self._reconcile_failed_placement(
                        client=self.client, symbol=self.symbol, exchange=self.exchange,
                        strategy_label=self.strategy_label, action=self.order_action,
                        quantity=slice_qty, known_oids=set(own_oids))
                    if state == "placed":
                        self.log(f"reconcile: order {found_oid} found at broker; not retrying")
                        resp = {"status": "success", "order_id": found_oid,
                                "data": {"order_id": found_oid}}
                    elif state == "absent":
                        time.sleep(poll_sec); now_ts = datetime.now().timestamp(); continue
                    else:
                        self.log("reconcile: broker state unknown; halting execution")
                        summary_reason = "placement_unreconciled"
                        break
                self._record_child(execution_id, resp, slice_qty, ltp, mode)
                if isinstance(resp, dict) and resp.get("status") == "success":
                    oid = resp.get("order_id") or (resp.get("data") or {}).get("order_id")
                    child_count += 1; _LAST_ORDER_AT[execution_id] = datetime.now()
                    if oid:
                        own_oids[str(oid)] = datetime.now()
                    # Record the execution's realised slippage against the
                    # arrival VWAP of the slice just placed (see Phase 2 above).
                    try:
                        from blue_engine.core.benchmark import compute_slippage_bps
                        session_vwap = (session_vwap_sum / session_vwap_vol
                                        if session_vwap_vol > 0 else None)
                        # Audit (2026-10-22): ``resolve_expected_price`` returns
                        # the LARGER of the two VWAPs, a benchmark choice, not a
                        # side-aware expectation. For a BUY whose fill is at the
                        # touch, expected = max(...) guarantees
                        # actual - expected <= 0, i.e. structurally "favorable"
                        # slippage, so the guard below could never trigger. The
                        # sign convention is side-aware here instead.
                        expected = (session_vwap
                                    if session_vwap is not None else ref_price)
                        fill_px = _safe_float(
                            (resp.get("data") or {}).get("average_price"), ltp)
                        if expected and expected > 0 and fill_px > 0:
                            slippage_acc.record(compute_slippage_bps(
                                expected_price=expected, actual_price=fill_px,
                                side=self.order_action))
                    except Exception as exc:  # noqa: BLE001 - never block a child
                        self.log(f"slippage sample skipped: {exc!r}")
                    if self.order_mode == "PEG":
                        peg_open_child = True
                    child_log.append({"attempted": slice_qty, "order_id": oid,
                                      "ltp_at_place": ltp, "phase": phase})
                    self.log(f"child placed: oid={oid}")
                else:
                    msg = resp.get("message") if isinstance(resp, dict) else str(resp)
                    self.log(f"rejected: {msg}")
            time.sleep(poll_sec); now_ts = datetime.now().timestamp()
        self.log(f"done: filled={filled_qty}/{target_qty} children={child_count} phase={phase}"
                 + (f" | ceiling halts={band_halt_count}" if band_halt_count else "")
                 + (f" | reason={summary_reason}" if summary_reason else ""))
        summary = {"filled":filled_qty, "target":target_qty, "child_orders":child_count,
                   "mode":mode, "phase":phase, "execution_id":execution_id,
                   "ceiling_halts": band_halt_count, "children":child_log}
        if summary_reason:
            summary["reason"] = summary_reason
        return summary

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

    def _reconcile_failed_placement(
        self, *, client: Any, symbol: str, exchange: str, strategy_label: str,
        action: str, quantity: int, known_oids: set) -> Tuple[str, Optional[str]]:
        """After a ``placeorder`` exception, check the broker before any retry.

        Returns ``("placed", oid)`` when a matching order exists that this run
        has not recorded, ``("absent", None)`` when none exists so a retry is
        safe, or ``("unknown", None)`` when the orderbook cannot be read, in
        which case the caller must halt rather than retry.
        """
        try:
            ob = client.orderbook()
        except Exception as exc:  # noqa: BLE001 - reconciliation must not raise
            self.log(f"reconcile: orderbook read failed: {exc!r}")
            return "unknown", None
        if not isinstance(ob, dict) or ob.get("status") != "success":
            return "unknown", None
        rows = ob.get("data", [])
        if isinstance(rows, dict):
            rows = rows.get("orders", []) or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            oid = row.get("orderid")
            if not oid or str(oid) in known_oids:
                continue
            if (row.get("symbol") == symbol and row.get("exchange") == exchange
                    and row.get("strategy") == strategy_label
                    and str(row.get("action", "")).upper() == action.upper()
                    and _safe_float(row.get("quantity"), -1.0) == float(quantity)):
                return "placed", str(oid)
        return "absent", None

    def _cancel_stale_limits(
        self, *, client: Any, symbol: str, exchange: str,
        strategy_label: str, limit_expiry_pct: float,
        execution_id: Optional[str] = None,
        allowed_strategy_labels: Optional[set] = None,
        own_oids: Optional[Dict[str, datetime]] = None,
        min_age_sec: float = 0.0) -> None:
        """Cancel this run's resting LIMIT children older than ``min_age_sec``.

        Only orders in ``own_oids`` are eligible, so a shared orderbook's other
        orders are never touched. With no ``own_oids`` nothing is cancelled.
        """
        if own_oids is None:
            return
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
            # AUDIT (2026-10-22, P1-5): the strategy-name comparison used
            # ``strategy_label.split("-")[0]``, i.e. "POV" for "POV-INFY-NSE".
            # On a shared orderbook (a screener bridge running three strategies,
            # or two concurrent POV runs on different symbols) the broadened
            # match cancelled OTHER strategies' working LIMITs as "stale". The
            # comparison is now an explicit allow-list that includes the exact
            # label and the full label, never a truncated token.
            allowed_labels = allowed_strategy_labels or {strategy_label}
            if (row.get("symbol") == symbol and row.get("exchange") == exchange
                    and row.get("strategy") in allowed_labels
                    and pt == "limit"
                    and os_ in ("open", "trigger pending", "pending")):
                oid = row.get("orderid")
                if not oid or str(oid) not in own_oids: continue
                age = (datetime.now() - own_oids[str(oid)]).total_seconds()
                if age < min_age_sec: continue
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
    """Integer parse with the same fail-soft contract as ``_safe_float``."""
    try:
        if value is None or value == "": return default
        return int(value)
    except (TypeError, ValueError): return default


def _safe_num(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Parse an optional numeric field, preserving None as "absent".

    Unlike _safe_float (which defaults to 0.0 for the volume/price maths),
    this keeps None distinct from 0.0. That matters for the confirmation
    band: a missing ceiling must mean "no band information" and NOT
    "ceiling is zero", because a zero ceiling would halt every run.
    """
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _above(ceiling: float, price: float, tick: float = 0.05) -> bool:
    """True when ``price`` is meaningfully ABOVE ``ceiling``.

    A naive ``price > ceiling`` is wrong here because the ceiling is
    computed as ``vwap * (1 + pct/100)``. In IEEE-754 that product is
    imprecise: 100.0 * 1.005 == 100.50000000000001, so a price of exactly
    100.50 - a real, quotable tick - would be judged to be above the
    ceiling and halt slicing. Measured live in this codebase, that turned
    a boundary case into a spurious total halt.

    Half a tick of tolerance makes the boundary ``>= ceiling`` behave as
    intended while leaving a genuine one-tick breach (which is a full tick
    above the ceiling) firmly inside the halt.
    """
    return price > (ceiling + tick / 2.0)
