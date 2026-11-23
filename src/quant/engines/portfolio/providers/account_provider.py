"""
providers/account_provider.py - the live book from ``positionbook`` / ``funds``.

Assembles ``current_weights`` (the actual book, mapped to sleeves) and
``capital`` (available funds) so the optimiser knows where it is starting from
and can size correctly. Read-only: this module never places an order.

Source methods (public SDK, injected client):
    client.positionbook()   -> open positions (net qty, avg price, ltp, pnl)
    client.holdings()       -> delivery holdings
    client.funds()          -> available cash / margin

A position's market value / total capital = its weight. Symbols are mapped back
to sleeves via ``universe.sleeve_for_symbol`` so a NIFTY BANK futures position
counts toward the BANKNIFTY sleeve.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from .base import PortfolioProvider
from . import universe as universe_mod
from ...core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = ["AccountProvider", "net_contributed_from_funds"]


def _positions(client: Any) -> List[Dict[str, Any]]:
    """Read the position book (best-effort; [] on any failure)."""
    try:
        resp = client.positionbook()
    except Exception as exc:  # noqa: BLE001
        logger.debug("positionbook failed: %s", exc)
        return []
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return []
    data = resp.get("data") or []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _holdings(client: Any) -> List[Dict[str, Any]]:
    try:
        resp = client.holdings()
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return []
    data = resp.get("data") or []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _funds(client: Any) -> Dict[str, Any]:
    try:
        resp = client.funds()
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(resp, dict) and resp.get("status") == "success":
        d = resp.get("data")
        return d if isinstance(d, dict) else {}
    return {}


def _row_value(row: Dict[str, Any]) -> float:
    """Market value of a position/holding row (price * qty)."""
    qty = safe_float(row.get("quantity"))
    if qty is None:
        qty = safe_float(row.get("netqty"))
    if qty is None:
        qty = safe_float(row.get("position"))
    if qty is None:
        qty = 0.0
    price = safe_float(row.get("ltp"))
    if price is None:
        price = safe_float(row.get("avg_price")) or safe_float(
            row.get("average_price")) or 0.0
    return qty * price


#: Broker funds fields that report NET CAPITAL PAID IN (deposits minus
#: withdrawals) on a cumulative basis. Brokers spell this differently, so we
#: probe a set of known names rather than hard-coding one. A match here is the
#: most reliable flow source: it is the broker's own record of money the client
#: moved in, independent of how the book performed.
_DEPOSIT_FIELDS = (
    "netdeposits", "net_deposits", "payin", "pay_in",
    "total_deposits", "deposits", "funds_added", "capital_added",
)


def net_contributed_from_funds(funds: Dict[str, Any]) -> Optional[float]:
    """Cumulative net capital paid in, from the broker's funds payload.

    Returns ``None`` when the broker exposes no such field, so the caller can
    fall back to the capital-delta method. This is deliberately separate from
    ``availablecash``: available cash moves with trading P&L, whereas a true
    deposit ledger does not, which is exactly the distinction XIRR needs.

    Field names vary by broker, so we probe a known set case-insensitively.
    """
    if not isinstance(funds, dict):
        return None
    lowered = {str(k).strip().lower(): v for k, v in funds.items()}
    for name in _DEPOSIT_FIELDS:
        if name in lowered:
            v = safe_float(lowered.get(name))
            if v is not None:
                return v
    return None


class AccountProvider(PortfolioProvider):
    """Live book (current weights) + capital from the SDK."""

    name = "account"

    def __init__(self, client: Any, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None):
        super().__init__(config, log)
        self.client = client
        self.include_holdings: bool = bool(self.config.get("include_holdings", True))

    def _fetch(self) -> Dict[str, Any]:
        warnings: List[str] = []
        if self.client is None:
            return {"meta": {"ok": False, "warnings": ["no SDK client supplied"]}}

        rows: List[Dict[str, Any]] = list(_positions(self.client))
        if self.include_holdings:
            rows += _holdings(self.client)

        by_sleeve: Dict[str, float] = {}
        for r in rows:
            sym = str(r.get("symbol") or r.get("tradingsymbol") or "")
            sleeve = universe_mod.sleeve_for_symbol(sym, self.config)
            if sleeve is None:
                # Unknown symbol: keep it as its own pseudo-sleeve so it is not
                # silently ignored (the engine can decide to exclude it).
                sleeve = sym or "UNKNOWN"
                warnings.append(f"{sym}: no sleeve mapping; kept as its own")
            by_sleeve[sleeve] = by_sleeve.get(sleeve, 0.0) + _row_value(r)

        funds = _funds(self.client)
        available = safe_float(funds.get("availablecash"))
        if available is None:
            available = safe_float(funds.get("available_balance"))
        if available is None:
            available = safe_float(funds.get("cash")) or 0.0

        holdings_value = sum(by_sleeve.values())
        capital = holdings_value + available

        # Cumulative net capital the broker records as paid in, when it exposes
        # one. This is the PREFERRED flow source for XIRR: unlike `capital`, it
        # is not moved by trading P&L, so a top-up is separable from a gain.
        net_contributed = net_contributed_from_funds(funds)

        current_weights: Dict[str, float] = {}
        if capital > 0:
            current_weights = {s: round(v / capital, 6)
                               for s, v in by_sleeve.items() if v > 0}

        self.log(f"sleeves={len(current_weights)} capital={capital:,.0f} "
                 f"cash={available:,.0f}")
        return {
            "current_weights": current_weights,
            "capital": round(capital, 2),
            "available_cash": round(available, 2),
            "holdings_value": round(holdings_value, 2),
            "positions_value": {s: round(v, 2) for s, v in by_sleeve.items()},
            "n_positions": len(rows),
            "net_contributed": (None if net_contributed is None
                                else round(net_contributed, 2)),
            "funds": funds,
            "meta": {"ok": True, "stale": False, "warnings": warnings,
                     "source": self.name},
        }