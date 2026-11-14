"""Sector, cluster and beta caps on the whole book. Advisory-check, gate-enforced.

Flow stage: Portfolio risk, Stress and CVaR, Live intraday VaR/CVaR
Status: PHASE 6 IMPLEMENTED (2026-11-14)
Existing code to reuse or wrap (do not copy):
    quant.portfolio.portfolio_risk   (group_exposure, cap_violations,
        beta_exposure, stress_pnl, historical_cvar - parity-tested locally)
    quant.portfolio.tail_risk        (VaR/CVaR at 95 and 99, five named India
        stress scenarios, drawdown - the engine wrapper)

THE GAP THIS CLOSES
-------------------
v5 2.6 lists "Portfolio risk: sector, cluster and beta caps" as a stage. The
functions existed in ``portfolio_risk.py`` and NOTHING on the trade path called
them: ``cap_violations`` was referenced only by tests. A book could hold five
banks at 30% each and every individual order would pass every existing check,
because every existing check looks at ONE order at a time.

``tail_risk.py`` had the same problem: 186 lines of VaR, CVaR and named India
scenarios, wired to nothing.

This module is the enforcement point. It combines the two and answers one
question the pre-gate can call: *"would adding this order breach a book limit?"*

FAIL-CLOSED RULES
-----------------
- A cap that is not set blocks the entry, exactly like every other unset limit.
  "No sector cap configured" must not mean "no sector risk".
- An unmapped symbol is grouped as ``UNKNOWN`` and capped like any other group,
  because a sector bet you cannot name is still a sector bet.
- If the book cannot be measured (no weights, no capital), the check REFUSES.
  It never returns "fine" because it could not look.

WHAT IT DOES NOT DO
-------------------
It does not read the broker. Weights are passed in. It does not place orders:
``risk/gate.py`` remains the only path to an order.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from quant.portfolio import portfolio_risk
from quant.portfolio import tail_risk

logger = logging.getLogger(__name__)

__all__ = [
    "PortfolioRiskConfig",
    "portfolio_risk_checks",
    "book_summary",
    "proposed_weights",
    "sector_map",
]

UNKNOWN_GROUP = "UNKNOWN"


class PortfolioRiskConfig:
    """Caps and tail limits. Every ``None`` means UNSET, and unset fails closed."""

    __slots__ = ("max_sector_pct", "max_beta", "max_cvar", "max_drawdown",
                 "sector_map", "betas")

    def __init__(self, *, max_sector_pct: Optional[float] = None,
                 max_beta: Optional[float] = None,
                 max_cvar: Optional[float] = None,
                 max_drawdown: Optional[float] = None,
                 sector_map: Optional[Mapping[str, str]] = None,
                 betas: Optional[Mapping[str, float]] = None) -> None:
        for name, value in (("max_sector_pct", max_sector_pct), ("max_beta", max_beta),
                            ("max_cvar", max_cvar), ("max_drawdown", max_drawdown)):
            if value is not None and (not isinstance(value, (int, float)) or value <= 0):
                raise ValueError(f"{name} must be a positive number or None, got {value!r}")
        self.max_sector_pct = max_sector_pct
        self.max_beta = max_beta
        self.max_cvar = max_cvar
        self.max_drawdown = max_drawdown
        self.sector_map = dict(sector_map or {})
        self.betas = dict(betas or {})


def sector_map(screen_path=None) -> Dict[str, str]:
    """symbol -> sector, from the screener output.

    Source: ``data/screened/latest.json``, whose rows carry the sector the
    screener assigned. That is the only SYMBOL-keyed sector source available:
    the engine's ``INDEX_TO_SCREENER_SECTORS`` is keyed by ISIN, not by symbol,
    so it cannot answer this question and is deliberately not used here.

    Returns an empty dict when there is no screen. Callers then group everything
    as UNKNOWN, which is CAPPED like any other group rather than quietly exempted
    - an unnameable sector bet is still a sector bet.
    """
    import json
    from pathlib import Path

    path = Path(screen_path) if screen_path else (
        Path(__file__).resolve().parents[3] / "data" / "screened" / "latest.json")
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("screen unreadable for the sector map: %r", exc)
        return {}

    rows = payload.get("symbols") if isinstance(payload, dict) else payload
    out: Dict[str, str] = {}
    for row in rows or []:
        if isinstance(row, dict):
            sym = str(row.get("symbol") or "").strip().upper()
            sector = str(row.get("sector") or "").strip().upper()
            if sym and sector:
                out[sym] = sector
    return out


def proposed_weights(weights: Mapping[str, float], symbol: str, add_weight: float,
                     *, capital: float, notional: Optional[float] = None
                     ) -> Dict[str, float]:
    """The book AFTER adding this order. Does not mutate the input.

    ``add_weight`` is a fraction of capital. When ``notional`` is given it wins,
    because a caller that knows the order value should not have it re-derived.
    """
    out = {str(k).upper(): float(v) for k, v in (weights or {}).items()}
    key = str(symbol).strip().upper()
    if notional is not None and capital:
        delta = float(notional) / float(capital)
    else:
        delta = float(add_weight or 0.0)
    out[key] = out.get(key, 0.0) + delta
    return out


def portfolio_risk_checks(
    weights: Mapping[str, float],
    *,
    config: Optional[PortfolioRiskConfig] = None,
    pnl_history=None,
    sector_group: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """Reasons to REFUSE an entry. An empty list means nothing is breached.

    Args:
        weights: symbol -> share of capital, the PROPOSED book.
        config: caps. ``None`` uses an all-unset config, which refuses (fail
            closed) rather than passing.
        pnl_history: returns series for the tail metrics. Without it the CVaR and
            drawdown checks cannot run, and an unset-or-unmeasurable tail limit
            refuses.
        sector_group: symbol -> sector. Defaults to the engine map.

    Returns a list of human-readable reasons, never raises for a limit breach.
    """
    cfg = config or PortfolioRiskConfig()
    reasons: List[str] = []

    if not weights:
        return ["portfolio risk: no book to measure (fail closed)"]

    group_map = dict(sector_group or cfg.sector_map or sector_map())

    # 1. sector / cluster caps -------------------------------------------------
    if cfg.max_sector_pct is None:
        reasons.append("limit not set: max_sector_pct")
    else:
        reasons.extend(portfolio_risk.cap_violations(
            dict(weights), group_map, cap=float(cfg.max_sector_pct)))

    # 2. beta cap ---------------------------------------------------------------
    if cfg.max_beta is None:
        reasons.append("limit not set: max_beta")
    else:
        beta = abs(portfolio_risk.beta_exposure(dict(weights), dict(cfg.betas)))
        if beta > float(cfg.max_beta):
            reasons.append(f"book beta {beta:.2f} above cap {float(cfg.max_beta):.2f}")

    # 3. tail risk (CVaR / drawdown) -------------------------------------------
    if cfg.max_cvar is not None or cfg.max_drawdown is not None:
        if not pnl_history:
            reasons.append("tail risk: no P&L history, cannot measure (fail closed)")
        else:
            tail = tail_risk.check_limits(
                pnl_history, max_cvar=cfg.max_cvar, max_drawdown=cfg.max_drawdown)
            for reason in tail.get("reasons") or []:
                reasons.append(f"tail risk: {reason}")

    return reasons


def book_summary(weights: Mapping[str, float], *,
                 config: Optional[PortfolioRiskConfig] = None,
                 pnl_history=None,
                 sector_group: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """One dict for the dashboard: group exposure, beta and tail metrics.

    Read-only and cheap. A missing input is reported as unmeasurable, never as a
    comfortable zero.
    """
    cfg = config or PortfolioRiskConfig()
    group_map = dict(sector_group or cfg.sector_map or sector_map())
    book = {str(k).upper(): float(v) for k, v in (weights or {}).items()}

    groups = portfolio_risk.group_exposure(book, group_map) if book else {}
    beta = abs(portfolio_risk.beta_exposure(book, dict(cfg.betas))) if book else None

    tail = tail_risk.panel(pnl_history) if pnl_history else {
        "summary": {"n": 0, "note": "no P&L history supplied"},
        "stress": tail_risk.stress(1.0),
        "scenarios_available": len(tail_risk.DEFAULT_STRESS),
    }

    return {
        "weights": book,
        "groups": {k: round(v, 6) for k, v in sorted(groups.items())},
        "unmapped": sorted(s for s in book if s not in group_map),
        "beta": None if beta is None else round(beta, 4),
        "caps": {"max_sector_pct": cfg.max_sector_pct, "max_beta": cfg.max_beta,
                 "max_cvar": cfg.max_cvar, "max_drawdown": cfg.max_drawdown},
        "tail": tail,
    }
