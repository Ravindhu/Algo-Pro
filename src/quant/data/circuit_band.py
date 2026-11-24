"""Price band + surveillance for the screener rows (circuit_pct and asm_gsm).

Flow stage: Liquidity screener -> screen file (input enrichment)
Status: IMPLEMENTED 2026-11-24 (reuses execution/pov_guards, no order path)

THE GAP THIS CLOSES
-------------------
``data/screener_engine.screen_rows`` drops any symbol without ``circuit_pct`` and
``asm_gsm`` - correctly, because a price band the bot cannot verify is a risk it
must not take. But the Screener.in payload never carries either field, so every
row was dropped and ``data/screened/latest.json`` was written as
``{"results": []}``. The bot then had no symbols at all and no strategy could
fire. Measured on the real 14-row screen: ``considered 14, selected 0,
dropped 14, all "circuit_pct missing"``.

SOURCE OF TRUTH
---------------
The broker's own quote reply. OpenAlgo's ``/api/v1/quotes`` returns
``upper_circuit_limit`` and ``lower_circuit_limit`` for cash equities, and the
band is symmetric around the previous close:

    circuit_pct = (upper_limit / prev_close) - 1        (fraction, e.g. 0.20)

ASM/GSM is NOT in the quote reply for most brokers. It is read from the
instrument master (``/api/v1/search``) when the broker publishes it, and is
otherwise reported as UNKNOWN - never guessed as False. An unknown flag keeps
the symbol out, because ``screen_rows`` treats ASM as a hard exclusion.

RULES (each fails closed)
-------------------------
1. No reader, a raised exception, a failed reply, or a missing field -> the
   symbol is returned with ``circuit_pct=None`` and a reason. It is not dropped
   here; ``screen_rows`` does that and records why.
2. A band is accepted only when it is symmetric (``|up| - 1`` within
   ``SYMMETRY_TOL`` of ``1 - |down|``), inside ``(0, MAX_CIRCUIT]``, and built
   from a positive previous close. An asymmetric or absurd band is a data
   fault, not a 20% stock.
3. Nothing here places, cancels or modifies an order, and no LLM is involved.

WHY prev_close COMES FROM THE DAILY BARS
----------------------------------------
The quote reply's band already encodes the previous close the exchange used.
Reading the previous close back out of the same reply (``prev_close`` or
``close``) keeps the ratio self-consistent even when the two are taken from the
same snapshot. A caller that already holds verified daily bars may pass its own
previous close instead, which is the preferred path (it is the same figure the
bot's own price-band check uses).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, Optional

__all__ = [
    "MAX_CIRCUIT",
    "SYMMETRY_TOL",
    "QuoteReader",
    "band_from_quote",
    "enrich_candidates",
]

#: No NSE cash equity has a band wider than +-20%. Anything above this is a
#: scaling error (paise vs rupees, or a derivative) and must not be trusted.
MAX_CIRCUIT = 0.20
#: Upper and lower band must mirror each other this closely, as a fraction.
SYMMETRY_TOL = 0.005

#: ``reader(symbol, exchange) -> raw OpenAlgo quotes reply``.
QuoteReader = Callable[[str, str], Dict[str, Any]]


def _num(value: Any) -> Optional[float]:
    """Finite float, else None. Booleans and strings that are not numbers -> None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _quote_data(reply: Any) -> Optional[Dict[str, Any]]:
    """The ``data`` block of a successful reply, else None."""
    if not isinstance(reply, dict) or reply.get("status") != "success":
        return None
    data = reply.get("data")
    return data if isinstance(data, dict) else None


def band_from_quote(reply: Dict[str, Any], prev_close: Optional[float] = None
                    ) -> Dict[str, Any]:
    """``{circuit_pct, prev_close, reason}`` from one quote reply. Never raises.

    ``circuit_pct`` is None with a ``reason`` when the band cannot be trusted.
    """
    data = _quote_data(reply)
    if data is None:
        return {"circuit_pct": None, "prev_close": prev_close,
                "reason": "quote reply not successful"}

    upper = _num(data.get("upper_circuit_limit", data.get("upper_circuit")))
    lower = _num(data.get("lower_circuit_limit", data.get("lower_circuit")))
    if upper is None or lower is None:
        return {"circuit_pct": None, "prev_close": prev_close,
                "reason": "quote carries no circuit limits"}
    if upper <= 0 or lower <= 0:
        return {"circuit_pct": None, "prev_close": prev_close,
                "reason": "circuit limits not positive"}

    close = _num(prev_close)
    if close is None or close <= 0:
        close = _num(data.get("prev_close", data.get("close")))
    if close is None or close <= 0:
        return {"circuit_pct": None, "prev_close": None,
                "reason": "no positive previous close to derive the band from"}

    up_frac = upper / close - 1.0
    down_frac = 1.0 - lower / close
    if up_frac <= 0 or down_frac <= 0:
        return {"circuit_pct": None, "prev_close": close,
                "reason": "previous close outside the quoted band"}
    if abs(up_frac - down_frac) > SYMMETRY_TOL:
        return {"circuit_pct": None, "prev_close": close,
                "reason": f"asymmetric band (up {up_frac:.4f} vs down {down_frac:.4f})"}

    circuit = round((up_frac + down_frac) / 2.0, 6)
    if not 0 < circuit <= MAX_CIRCUIT:
        return {"circuit_pct": None, "prev_close": close,
                "reason": f"implausible circuit {circuit:.4f}"}
    return {"circuit_pct": circuit, "prev_close": close, "reason": ""}


def _surveillance(data: Optional[Dict[str, Any]]) -> Optional[bool]:
    """ASM/GSM flag from a quote block, or None when the broker does not publish it.

    None is deliberately distinct from False: an unknown flag must keep the
    symbol out, and collapsing it to False would let an unflagged-looking symbol
    through the screen.
    """
    if not isinstance(data, dict):
        return None
    for key in ("asm", "gsm", "asm_gsm"):
        if key in data and data[key] is not None:
            return bool(data[key])
    return None


def enrich_candidates(candidates: Any, reader: Optional[QuoteReader],
                      exchange: str = "NSE",
                      prev_close_fn: Optional[Callable[[str], Optional[float]]] = None,
                      ) -> Dict[str, Any]:
    """Add ``circuit_pct`` / ``asm_gsm`` to each candidate from the broker quote.

    Returns ``{"candidates": [...], "enriched": n, "failed": [{symbol, reason}]}``.
    A candidate that cannot be enriched is KEPT, with ``circuit_pct=None`` and a
    ``band_reason``; ``screen_rows`` applies the exclusion and records the reason
    in one place. ``reader=None`` means no broker source is configured: every
    candidate is returned unenriched with that reason, and nothing is invented.
    """
    rows = [dict(c) for c in (candidates or []) if isinstance(c, dict) and c.get("symbol")]
    failed = []
    enriched = 0
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if reader is None:
            row.setdefault("circuit_pct", None)
            row.setdefault("asm_gsm", None)
            row["band_reason"] = "no quote source configured"
            failed.append({"symbol": symbol, "reason": "no quote source configured"})
            continue
        close = None
        if prev_close_fn is not None:
            try:
                close = _num(prev_close_fn(symbol))
            except Exception:  # noqa: BLE001 - a price lookup must not stop the screen
                close = None
        try:
            reply = reader(symbol, exchange)
        except Exception as exc:  # noqa: BLE001 - one symbol must not stop the others
            row.setdefault("circuit_pct", None)
            row.setdefault("asm_gsm", None)
            row["band_reason"] = f"quote raised {type(exc).__name__}"
            failed.append({"symbol": symbol, "reason": row["band_reason"]})
            continue

        band = band_from_quote(reply, prev_close=close)
        data = _quote_data(reply)
        flag = _surveillance(data)
        row["circuit_pct"] = band["circuit_pct"]
        # Unknown surveillance stays unknown; screen_rows refuses it.
        row["asm_gsm"] = bool(flag) if flag is not None else None
        if band["reason"]:
            row["band_reason"] = band["reason"]
            failed.append({"symbol": symbol, "reason": band["reason"]})
            continue
        if flag is None:
            row["band_reason"] = "ASM/GSM not published by the broker"
            failed.append({"symbol": symbol, "reason": row["band_reason"]})
            continue
        enriched += 1
    return {"candidates": rows, "enriched": enriched, "failed": failed}
