"""
providers/fii_provider.py - Indian institutional-flow & volatility signals.

The single most-watched institutional signal in Indian equities is **FII net
flow** (cash + index-futures). This provider fetches that, plus DII flow, the
put-call ratio (PCR) and India VIX, from public NSE JSON endpoints.

Why a separate provider
-----------------------
Flow data has a *different cadence and freshness* from macro prints (intraday /
daily) and a different failure mode (NSE frequently requires a session cookie
and rate-limits aggressively). Isolating it lets the caller give it a short TTL
while the macro provider gets a long one.

Degradation
-----------
No ``requests`` / no network / NSE blocks us -> warnings + partial dict. The
Market engine already treats FII as optional (``fii_score`` may be None), so a
flow outage simply drops risk-appetite to breadth-only.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .base import MacroProvider, value_block

try:  # optional dependency
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:  # noqa: BLE001
    requests = None  # type: ignore
    _HAS_REQUESTS = False


class FiiFlowProvider(MacroProvider):
    """FII/DII flows, PCR and India VIX from NSE public endpoints."""

    name = "fii"

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 http_get: Optional[Callable[..., Any]] = None):
        super().__init__(config, log)
        self._http_get = http_get
        self.timeout: float = float(self.config.get("timeout", 8.0))

        # Public NSE endpoints. Kept in config so a schema change is a config
        # edit, not a code change.
        self.endpoints: Dict[str, str] = dict(self.config.get("endpoints") or {
            "fii_dii": "https://www.nseindia.com/api/fiidiiTradeReact",
            "pcr": "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
            "vix": "https://www.nseindia.com/api/allIndices",
        })
        self._headers = {
            "User-Agent": ("Mozilla/5.0 (compatible; blue-engine/1.0; "
                           "+https://openalgo.in)"),
            "Accept": "application/json",
        }

    def _get_json(self, url: str) -> Optional[Any]:
        getter = self._http_get
        if getter is None:
            if not _HAS_REQUESTS:
                return None
            getter = requests.get  # type: ignore[union-attr]
        try:
            resp = getter(url, timeout=self.timeout, headers=self._headers)
            return resp.json() if hasattr(resp, "json") else resp
        except Exception as exc:  # noqa: BLE001
            self.log(f"http GET failed ({exc!r}) for {url}")
            return None

    # ---- feeds ---------------------------------------------------------- #
    @staticmethod
    def _parse_fii_dii(payload: Any) -> Dict[str, Any]:
        """Normalise the fiidiiTradeReact payload to net cash/futures."""
        out: Dict[str, Any] = {}
        rows = payload if isinstance(payload, list) else (
            (payload or {}).get("data") if isinstance(payload, dict) else None)
        if not isinstance(rows, list):
            return out
        for row in rows:
            if not isinstance(row, dict):
                continue
            cat = str(row.get("category", "")).upper()
            net = row.get("netValue")
            if net is None:
                # NSE sometimes returns only buy/sell.
                buy = row.get("buyValue")
                sell = row.get("sellValue")
                if buy is not None and sell is not None:
                    try:
                        net = float(buy) - float(sell)
                    except (TypeError, ValueError):
                        net = None
            if net is None:
                continue
            if "FII" in cat or "FPI" in cat:
                out["net_cash"] = net
            elif "DII" in cat:
                out["dii_net_cash"] = net
        return out

    def _fetch(self) -> Dict[str, Any]:
        warnings: List[str] = []
        out: Dict[str, Any] = {}

        if not _HAS_REQUESTS and self._http_get is None:
            warnings.append("requests not installed; FII/PCR/VIX skipped")
            out["meta"] = {"ok": False, "warnings": warnings,
                           "source": self.name}
            return out

        fii = self._get_json(self.endpoints.get("fii_dii", ""))
        parsed = self._parse_fii_dii(fii)
        if parsed.get("net_cash") is not None:
            out["fii"] = {"net_cash": parsed["net_cash"],
                          "net_futures": parsed.get("net_futures")}
        else:
            warnings.append("FII/DII flow unavailable")

        if parsed.get("dii_net_cash") is not None:
            out["dii"] = {"net_cash": parsed["dii_net_cash"]}

        # India VIX from the allIndices payload.
        allidx = self._get_json(self.endpoints.get("vix", ""))
        vix = None
        if isinstance(allidx, dict):
            for row in (allidx.get("data") or []):
                if isinstance(row, dict) and \
                        str(row.get("index", "")).upper() == "INDIA VIX":
                    vix = row.get("last")
                    break
        if vix is not None:
            out["vix"] = value_block(vix, unit="index", source="nse")
        else:
            warnings.append("India VIX unavailable")

        out["meta"] = {"ok": True, "warnings": warnings, "source": self.name}
        return out


__all__ = ["FiiFlowProvider"]