"""
providers/fii_provider.py - Indian institutional-flow & volatility signals.

The single most-watched institutional signal in Indian equities is **FII net
flow** (cash + index-futures). This provider fetches that, plus DII flow, the
put-call ratio (PCR) and India VIX.

Two sources, in strict priority order
-------------------------------------
1. **Vendor parquet** (``database/vendor/indian-market-historical-data/data/FLOWS``)
   - the pinned Aswin checkout, updated by the vendor pin updater. Fast, no
     network, no scraping, and carries FII *and* DII cash plus the full F&O
     breakdown. Used first whenever the files are present and readable.

2. **NSE public JSON** - ``fiidiiTradeReact`` for cash flow, ``allIndices`` for
   VIX. Kept as the fallback because NSE requires a session cookie, rate-limits
   aggressively, and (observed 2026-10-22) frequently returns nothing at all.

**Freshness is not assumed.** The parquet's last date is compared against the
market calendar; a file whose newest row is older than
``max_lag_trading_days`` is served but tagged ``meta.stale = True``, because a
silently stale flow file is exactly the failure this layer exists to prevent
(see audit F-08: ``stale=False`` was reported for a 10-month-old series).

Why a separate provider
-----------------------
Flow data has a *different cadence and freshness* from macro prints (intraday /
daily) and a different failure mode (NSE frequently requires a session cookie
and rate-limits aggressively). Isolating it lets the caller give it a short TTL
while the macro provider gets a long one.

Degradation
-----------
No vendor files / no ``requests`` / NSE blocks us -> warnings + partial dict. The
Market engine already treats FII as optional (``fii_score`` may be None), so a
flow outage simply drops risk-appetite to breadth-only.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .base import MacroProvider, value_block

try:  # optional dependency
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:  # noqa: BLE001
    requests = None  # type: ignore
    _HAS_REQUESTS = False

#: Pinned vendor checkout.
#: Depth is computed by walking up to the folder that HOLDS the ``blue_engine``
#: package, not by a fixed ``parents[N]``. A fixed depth broke once already when
#: the engine moved from ``Blue_engine/`` to ``src/quant/blue_engine/``.
def _engine_root() -> Path:
    """The ``blue_engine`` package folder, found by name from this file upward."""
    for parent in Path(__file__).resolve().parents:
        if parent.name == "blue_engine" and (parent / "__init__.py").is_file():
            return parent
    raise RuntimeError("blue_engine package root not found")


ENGINE_DIR = _engine_root()
VENDOR_DIR = (ENGINE_DIR / "database" / "vendor" / "indian-market-historical-data")
DEFAULT_FLOWS_DIR = VENDOR_DIR / "data" / "FLOWS"
DEFAULT_INDICES_DIR = VENDOR_DIR / "data" / "INDICES"


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
    # ---- vendor parquet: the primary, offline source -------------------- #
    def _flows_dir(self) -> Optional[Path]:
        raw = self.config.get("flows_dir")
        d = Path(raw) if raw else DEFAULT_FLOWS_DIR
        return d if d.is_dir() else None

    def _read_vendor(self) -> tuple:
        """Read the pinned vendor FLOWS parquets.

        Returns ``(out, warnings)``. Deliberately tolerant: a missing file, a
        missing ``pyarrow``/``pandas`` or a corrupt column is a *warning*, never
        an exception - the provider contract is degrade-never-halt.

        Numeric columns are read from the newest row whose ``Date`` parses.
        Rows are sorted by date rather than trusted in file order, because a
        vendor file appended out of order would otherwise serve a random day.
        """
        warnings: List[str] = []
        out: Dict[str, Any] = {}
        flows_dir = self._flows_dir()
        if flows_dir is None:
            return out, [f"vendor flows dir missing ({DEFAULT_FLOWS_DIR})"]

        try:
            import pandas as pd  # noqa: F401 - optional dependency
        except Exception:  # noqa: BLE001
            return out, ["pandas not installed; vendor flows skipped"]

        cash_file = flows_dir / "fii_dii_cash.parquet"
        deriv_file = flows_dir / "fii_derivatives.parquet"

        def _latest(path: Path):
            """Newest row of a parquet as a plain dict, or None."""
            if not path.is_file():
                warnings.append(f"vendor file missing: {path.name}")
                return None
            try:
                df = pd.read_parquet(path)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"vendor read failed ({path.name}): {exc!r}")
                return None
            if "Date" not in df.columns or df.empty:
                warnings.append(f"vendor file has no usable Date column: {path.name}")
                return None
            df = df.assign(_d=pd.to_datetime(df["Date"], errors="coerce"))
            df = df.dropna(subset=["_d"]).sort_values("_d")
            if df.empty:
                warnings.append(f"vendor file has no parseable dates: {path.name}")
                return None
            row = df.iloc[-1].to_dict()
            row["_as_of"] = df["_d"].iloc[-1].date()
            row["_prev"] = df.iloc[-2].to_dict() if len(df) > 1 else None
            return row

        cash = _latest(cash_file)
        if cash is not None:
            def _num(row: Optional[dict], key: str) -> Optional[float]:
                if not row:
                    return None
                try:
                    v = float(row.get(key))
                except (TypeError, ValueError):
                    return None
                return v if v == v else None  # NaN guard

            fii_net = _num(cash, "FII_Net_Cr")
            dii_net = _num(cash, "DII_Net_Cr")
            if fii_net is not None:
                out["fii"] = {"net_cash": fii_net,
                              "net_futures": None,
                              "source": "vendor:indian-market-historical-data"}
                # Recent daily history (oldest first) so the Market engine can
                # compute a rolling z-score. Without this the flow is only a
                # point value and "/is today unusual?" cannot be asked at all.
                hist_n = int(self.config.get("history_rows", 60))
                if hist_n > 0:
                    hist = self._recent_net(
                        cash_file, "FII_Net_Cr", hist_n)
                    if hist:
                        out["fii"]["history"] = hist
            if dii_net is not None:
                out["dii"] = {"net_cash": dii_net,
                              "source": "vendor:indian-market-historical-data"}
            # Net cash is in RUPEES CRORE; the macro block wants a bare number
            # for scoring, so expose a signal-shaped block too. 1 Cr = 1e7.
            if fii_net is not None:
                out["fii_inflow"] = value_block(
                    fii_net * 1e7,
                    previous=(_num(cash.get("_prev"), "FII_Net_Cr") or 0) * 1e7
                    if cash.get("_prev") else None,
                    unit="inr", as_of=cash["_as_of"].isoformat(),
                    source="vendor FII net cash (Cr)")
            out["_cash_as_of"] = cash["_as_of"]

        deriv = _latest(deriv_file)
        if deriv is not None:
            try:
                out["fii_long_ratio_pct"] = value_block(
                    float(deriv.get("FII_Long_Ratio_Pct")),
                    as_of=deriv["_as_of"].isoformat(),
                    source="vendor FII index-futures long ratio")
            except (TypeError, ValueError):
                pass
            try:
                out["fii_index_futures_net"] = value_block(
                    float(deriv.get("FII_Index_Futures_Net")),
                    as_of=deriv["_as_of"].isoformat(),
                    source="vendor FII index-futures net OI")
            except (TypeError, ValueError):
                pass
            out["_deriv_as_of"] = deriv["_as_of"]

        return out, warnings

    def _staleness(self, as_of: Optional[date]) -> Dict[str, Any]:
        """Classify a vendor ``as_of`` date against the market calendar.

        The vendor updates on trading days, so the comparison is in *business*
        days. A Friday file read on Monday is ~1 trading day old, not 3. This is
        the check that stops a quietly frozen parquet from being scored as if
        it were today's flow.
        """
        if as_of is None:
            return {"stale": False, "age_days": None, "note": "no vendor date"}
        today = date.today()
        age = (today - as_of).days
        if age <= 0:
            return {"stale": False, "age_days": age, "note": "same day"}
        # Approximate trading days: 5 of every 7 calendar days, ignoring holidays
        # (a holiday makes the estimate *stricter*, which is the safe direction).
        approx_trading = round(age * 5 / 7)
        limit = int(self.config.get("max_lag_trading_days", 2))
        return {
            "stale": approx_trading > limit,
            "age_days": age,
            "approx_trading_days": approx_trading,
            "max_lag_trading_days": limit,
            "note": (f"vendor as_of {as_of.isoformat()} is ~{approx_trading} "
                     f"trading days old (limit {limit})"),
        }

    @staticmethod
    def _recent_net(path: Path, column: str, n: int) -> List[float]:
        """The last ``n`` numeric values of ``column``, oldest first.

        Used for the rolling z-score. Rows are date-sorted first, so the series
        handed downstream is chronological regardless of file order, and any
        unparseable value is skipped rather than become a zero (a zero would
        silently look like "no flow" and distort the mean).
        """
        try:
            import pandas as pd
            df = pd.read_parquet(path)
            if "Date" not in df.columns or column not in df.columns:
                return []
            df = df.assign(_d=pd.to_datetime(df["Date"], errors="coerce"))
            df = df.dropna(subset=["_d"]).sort_values("_d")
            vals = pd.to_numeric(df[column], errors="coerce")
            vals = vals[vals.notna()].tolist()
            return [float(v) for v in vals[-int(n):]]
        except Exception:  # noqa: BLE001 - history is optional
            return []

    def _read_vendor_vix(self, warnings: List[str]) -> Optional[Dict[str, Any]]:
        """India VIX from the vendor INDICES parquet.

        The last bar of these files is frequently a partial row with a NaN
        ``Close`` and zero volume (observed: NIFTY_50 and INDIA_VIX both end on
        2026-10-13 with Close=NaN). Reading it blindly would inject NaN into the
        volatility state, so rows with a non-positive/NaN close are dropped and
        the newest *complete* bar is used.
        """
        raw = self.config.get("indices_dir")
        d = Path(raw) if raw else DEFAULT_INDICES_DIR
        path = d / "INDIA_VIX.parquet"
        if not path.is_file():
            return None
        try:
            import pandas as pd
            df = pd.read_parquet(path)
            close_col = next((c for c in df.columns
                              if str(c).lower() in ("close", "ltp", "last")), None)
            if not close_col:
                warnings.append("vendor INDIA_VIX has no close column")
                return None
            # The vendor INDICES files carry the date as the ROW INDEX, not as a
            # column (verified: columns are Open/High/Low/Close/Adj Close/Volume
            # and ``df.index`` holds 2026-10-12, 2026-10-13, ...). Accept either
            # shape so a future vendor change does not break this silently.
            date_col = next((c for c in df.columns
                             if str(c).lower() in ("date", "datetime", "timestamp")),
                            None)
            dates = df[date_col] if date_col is not None else df.index
            work = df.assign(
                _d=pd.to_datetime(dates, errors="coerce"),
                _c=pd.to_numeric(df[close_col], errors="coerce"))
            work = work.dropna(subset=["_d", "_c"]).query("_c > 0")
            if work.empty:
                warnings.append("vendor INDIA_VIX has no complete bars")
                return None
            work = work.sort_values("_d")
            last = work.iloc[-1]
            return value_block(float(last["_c"]), unit="index",
                               as_of=last["_d"].date().isoformat(),
                               source="vendor INDIA_VIX.parquet")
        except Exception as exc:  # noqa: BLE001 - never halt on a VIX read
            warnings.append(f"vendor VIX read failed: {exc!r}")
            return None

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

        # ---- 1. vendor parquet (primary) ------------------------------- #
        vendor_out, vendor_warnings = self._read_vendor()
        warnings.extend(vendor_warnings)
        stale_info = {"stale": False, "age_days": None}
        if vendor_out:
            stale_info = self._staleness(vendor_out.get("_cash_as_of"))
            if stale_info.get("stale"):
                # Serve it, but never pretend it is fresh.
                warnings.append(
                    f"vendor flows STALE: {stale_info.get('note')}; "
                    f"values are dated, not today's")
            out.update(vendor_out)

        have_flow = "fii" in out

        # ---- 2. NSE JSON (fallback only) ------------------------------- #
        # Only contacted when the vendor could not supply the cash flow. This
        # removes the 19-second NSE stall observed on 2026-10-22 from the
        # normal path.
        nse_used = False
        if not have_flow:
            if not _HAS_REQUESTS and self._http_get is None:
                warnings.append("requests not installed; FII/PCR/VIX skipped")
            else:
                nse_used = True
                fii = self._get_json(self.endpoints.get("fii_dii", ""))
                parsed = self._parse_fii_dii(fii)
                if parsed.get("net_cash") is not None:
                    out["fii"] = {"net_cash": parsed["net_cash"],
                                  "net_futures": parsed.get("net_futures"),
                                  "source": "nse:fiidiiTradeReact"}
                else:
                    warnings.append("FII/DII flow unavailable (vendor and NSE)")
                if parsed.get("dii_net_cash") is not None:
                    out["dii"] = {"net_cash": parsed["dii_net_cash"],
                                  "source": "nse:fiidiiTradeReact"}

        # ---- 3. India VIX -------------------------------------------------
        # Vendor first: ``data/INDICES/INDIA_VIX.parquet`` exists in the same
        # checkout and costs ~0.1 s. NSE's ``allIndices`` endpoint was measured
        # at 9 s returning nothing (2026-10-22), so it is the fallback only.
        if out.get("vix") is None and not self.config.get("disable_vix"):
            vix_block = self._read_vendor_vix(warnings)
            if vix_block is not None:
                out["vix"] = vix_block
            elif _HAS_REQUESTS or self._http_get is not None:
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

        out["meta"] = {
            "ok": True,
            "warnings": warnings,
            "source": self.name,
            "stale": bool(stale_info.get("stale")),
            "flow_source": (out.get("fii") or {}).get("source"),
            "network_used": nse_used,
            "as_of": (vendor_out.get("_cash_as_of").isoformat()
                      if vendor_out.get("_cash_as_of") else None),
        }
        return out


__all__ = ["FiiFlowProvider"]