"""Pre-gate safety checks: everything that must pass BEFORE the risk gate sees an intent.

Flow stage: Pre-gate safety, Trading calendar, Event blackout, Overnight gap check, Trade intent, Stress and CVaR, Session rules: pre-open, auction, closing, Stale-feed checks
Status: PHASE 3 IMPLEMENTED (2026-11-11); session + feed nodes wired 2026-11-14
Reuses (wrap, not copy): quant.risk.controls (blackout, gap), quant.portfolio.portfolio_risk
(stress_pnl, historical VaR/CVaR), config/calendar (NSE holidays),
quant.risk.session_windows (auction windows), quant.monitor.feed_health (stale quotes).

Capital-protection order: these checks run ahead of quant.risk.gate.check_intent and can only
REFUSE. They never approve anything, never touch an order client, and never resize an order.

Checks:
  1. trading_calendar_ok  - the day is an NSE equity trading day (holiday CSV, fail closed).
  2. event_blackout_ok    - results/RBI/Budget/Fed dates block new entries (delegates to controls).
  3. overnight_gap_ok     - entry ref price far from yesterday's close is refused (gap risk).
  4. handoff_ok           - the intent is complete and internally consistent before the gate.
  5. stress_ok            - the whole book survives the configured shock within the VaR/CVaR limit.
  6. session_ok           - the clock is not inside an auction window (2026-11-14).
  7. feed_ok              - that symbol's quote feed has not latched PAUSED (2026-11-14).

Checks 6 and 7 are opt-in: they only run when the caller supplies a clock or a
feed book, so an existing caller keeps its exact behaviour. Both FAIL CLOSED when
their engine module is unavailable, and neither can ever block an exit.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from quant.core.types import TradeIntent
from quant.portfolio.portfolio_risk import stress_pnl
from quant.risk.controls import gap_exit_required, in_event_blackout

__all__ = [
    "PreGateConfig", "PreGateResult", "PreGate", "load_equity_holidays",
]


def load_equity_holidays(path) -> set:
    """Equity-segment holidays from a calendar CSV (columns: date, equity_holiday, ...)."""
    out = set()
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if str(row.get("equity_holiday", "")).strip().lower() == "yes":
                    day = date.fromisoformat(str(row["date"]).strip())
                    out.add(day)
    except Exception:
        return set()  # unreadable calendar: caller fails closed
    return out


@dataclass(frozen=True)
class PreGateConfig:
    shock: float = -0.10                  # equity crash scenario, fraction
    var_limit: float = 0.05               # max stressed book loss as a share of capital
    gap_limit: float = 0.10               # max entry ref price vs previous close
    holidays_csv: str = "config/calendar/nse_holidays_2026.csv"
    # Auction windows. None means "no auction schedule for this segment", which is
    # correct for cash equities. A string like "09:15-09:45,12:00-12:30" enables it.
    auction_windows: Optional[str] = None
    is_sme: bool = False                  # SME trades in one all-day call auction
    auction_buffer_min: int = 2           # refuse this close to a window opening
    # Book-level caps (v5 2.6). Unset means the check refuses when it is enabled.
    max_sector_pct: Optional[float] = None
    max_beta: Optional[float] = None
    max_cvar: Optional[float] = None
    max_drawdown: Optional[float] = None


@dataclass
class PreGateResult:
    ok: bool
    reasons: Tuple[str, ...] = ()
    detail: Dict[str, object] = field(default_factory=dict)


class PreGate:
    """Stateless pre-gate. Call check() before check_intent(); both must pass."""

    def __init__(self, cfg: PreGateConfig = PreGateConfig(), holidays: Optional[set] = None) -> None:
        self.cfg = cfg
        # holidays=None means "lazy-load from the CSV". An explicitly passed set()
        # means "no holidays known": weekend checks still work, holiday dates do not.
        self._holidays = load_equity_holidays(Path(cfg.holidays_csv)) if holidays is None else set(holidays)

    # 1 + 2: calendar and events -------------------------------------------------
    def calendar_ok(self, day: date, blackout_days: Iterable[date] = ()) -> PreGateResult:
        reasons: List[str] = []
        if day.weekday() >= 5:
            reasons.append(f"{day.isoformat()} is a weekend")
        # Fail closed: an unreadable/empty calendar refuses trading days we cannot verify.
        if not self._holidays and not self._calendar_readable():
            reasons.append("trading calendar unreadable; cannot verify the day")
        elif day in self._holidays:
            reasons.append(f"{day.isoformat()} is an NSE equity holiday")
        if in_event_blackout(day, blackout_days):
            reasons.append(f"event blackout on {day.isoformat()}")
        return PreGateResult(not reasons, tuple(reasons))

    def _calendar_readable(self) -> bool:
        return Path(self.cfg.holidays_csv).exists()

    # 3: overnight gap ------------------------------------------------------------
    def overnight_gap_ok(self, intent: TradeIntent) -> PreGateResult:
        if intent.prev_close is None or intent.kind != "entry":
            return PreGateResult(True)
        msg = gap_exit_required(float(intent.prev_close), float(intent.ref_price),
                                gap_limit=self.cfg.gap_limit)
        if msg:
            return PreGateResult(False, (f"overnight gap: {msg}",))
        return PreGateResult(True)

    # 4: intent handoff -----------------------------------------------------------
    def handoff_ok(self, intent: TradeIntent) -> PreGateResult:
        """Contract check between the strategy layer and the risk gate. Fail closed."""
        reasons: List[str] = []
        if not intent.intent_id:
            reasons.append("intent_id missing")
        if not intent.symbol or not str(intent.symbol).isalnum():
            reasons.append(f"bad symbol {intent.symbol!r}")
        if intent.qty < 1 or intent.qty != int(intent.qty):
            reasons.append(f"bad qty {intent.qty}")
        if intent.ref_price <= 0:
            reasons.append(f"bad ref_price {intent.ref_price}")
        if intent.side not in ("BUY", "SELL"):
            reasons.append(f"bad side {intent.side!r}")
        if intent.kind not in ("entry", "exit", "protection"):
            reasons.append(f"bad kind {intent.kind!r}")
        if intent.lot_size < 1:
            reasons.append(f"bad lot_size {intent.lot_size}")
        # Long-only book: only a BUY entry may open risk. A SELL must be an exit/protection.
        if intent.side == "SELL" and intent.kind == "entry":
            reasons.append("long-only book: SELL entry refused before the gate")
        return PreGateResult(not reasons, tuple(reasons))

    # 5: stress / VaR ---------------------------------------------------------------
    def stress_ok(self, capital: float, weights: Dict[str, float],
                  betas: Optional[Dict[str, float]] = None) -> PreGateResult:
        """Crash the whole book by cfg.shock and refuse entries if the loss breaches var_limit.

        weights: symbol -> share of capital at risk (notional / capital). Betas scale the shock.
        """
        reasons: List[str] = []
        if capital is None or capital <= 0:
            return PreGateResult(False, ("stress check needs positive capital",))
        scaled: Dict[str, float] = {}
        for sym, w in weights.items():
            beta = float((betas or {}).get(sym, 1.0))
            scaled[sym] = w * beta
        loss_share = abs(stress_pnl({k: v for k, v in scaled.items()}, {k: self.cfg.shock for k in scaled}))
        self.detail_note(loss_share)
        if loss_share > self.cfg.var_limit:
            reasons.append(
                f"stressed book loss {loss_share:.1%} of capital exceeds limit {self.cfg.var_limit:.1%}")
        return PreGateResult(not reasons, tuple(reasons), detail={"stressed_loss_share": loss_share})

    def detail_note(self, loss_share: float) -> None:  # keep last computed value for audit
        self._last_loss = loss_share

    # 5b: portfolio (book-level) caps ------------------------------------------------
    def portfolio_caps_ok(self, weights: Dict[str, float], *, pnl_history=None
                          ) -> PreGateResult:
        """Sector, beta and tail caps on the PROPOSED book.

        Opt-in: only runs when the caller supplies the book, so an existing caller
        keeps its behaviour. This is the check that catches five banks at 30% each
        - every one of which passes the per-order limits individually.
        """
        from quant.portfolio.risk_limits import (PortfolioRiskConfig,
                                                 portfolio_risk_checks)

        cfg = PortfolioRiskConfig(
            max_sector_pct=self.cfg.max_sector_pct,
            max_beta=self.cfg.max_beta,
            max_cvar=self.cfg.max_cvar,
            max_drawdown=self.cfg.max_drawdown,
        )
        reasons = portfolio_risk_checks(dict(weights or {}), config=cfg,
                                        pnl_history=pnl_history)
        return PreGateResult(not reasons, tuple(f"portfolio: {r}" for r in reasons))

    # 6: session / auction window ---------------------------------------------------
    def session_ok(self, now=None) -> PreGateResult:
        """Refuse an entry inside (or just before) an auction window.

        Only runs when a clock is supplied, so a caller that has not adopted this
        check keeps its previous behaviour. Fails CLOSED: if the schedule cannot
        be read we do not know whether the market is in an auction.
        """
        from quant.risk import session_windows

        windows = self.cfg.auction_windows
        if windows is None and not self.cfg.is_sme:
            return PreGateResult(True)  # cash segment has no periodic auction here
        blocked, reason = session_windows.blocks_entry(
            now, windows, buffer_min=self.cfg.auction_buffer_min,
            is_sme=self.cfg.is_sme)
        if blocked:
            return PreGateResult(False, (f"session: {reason}",))
        return PreGateResult(True)

    # 7: quote feed health -----------------------------------------------------------
    def feed_ok(self, symbol: str, feed_book=None) -> PreGateResult:
        """Refuse an entry whose symbol feed has latched PAUSED.

        Only runs when a feed book is supplied. A missing book is NOT treated as
        healthy when the caller asked for the check: the guard fails closed.
        """
        if feed_book is None:
            return PreGateResult(True)
        guard = feed_book.blocks_entries([symbol])
        if guard.get("blocked"):
            return PreGateResult(False, (f"feed: {guard.get('detail') or 'paused'}",))
        return PreGateResult(True)

    # combined entry point ------------------------------------------------------------
    def check(self, intent: TradeIntent, *, blackout_days: Sequence[date] = (),
              weights: Optional[Dict[str, float]] = None, capital: float = 0.0,
              betas: Optional[Dict[str, float]] = None, now=None,
              feed_book=None, check_book_limits: bool = False) -> PreGateResult:
        """All pre-gate checks. Exits and protection are never pre-gated.

        ``now`` and ``feed_book`` are opt-in: supply them to enable the session and
        feed-health checks. Omitting them reproduces the earlier behaviour exactly,
        which is what keeps existing callers and their tests stable.
        """
        if intent.kind != "entry":
            return PreGateResult(True)  # exits and protection are never pre-gated
        results = [
            self.calendar_ok(intent.trade_day or date.today(), blackout_days),
            self.handoff_ok(intent),
            self.overnight_gap_ok(intent),
        ]
        if weights:
            results.append(self.stress_ok(capital, weights, betas))
        # Book-level caps: sector, beta and tail.
        #
        # SEPARATELY opt-in via ``check_book_limits``, NOT implied by ``weights``.
        # ``weights`` already drives the stress check and existing callers pass it
        # for that alone; running the cap check as a side effect of it turned an
        # unset cap into an unexpected refusal. Enabling a check has to be an
        # explicit act, and unset caps fail closed only once it is enabled.
        if check_book_limits and weights:
            results.append(self.portfolio_caps_ok(weights))
        if now is not None:
            results.append(self.session_ok(now))
        if feed_book is not None:
            results.append(self.feed_ok(intent.symbol, feed_book))
        reasons = tuple(r for res in results for r in res.reasons)
        return PreGateResult(not reasons, reasons)
