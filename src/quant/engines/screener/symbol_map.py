"""Resolve Screener.in company symbols to tradable (symbol, exchange) pairs.

Screener.in links a company to NSE when it lists there, otherwise to BSE -
in which case the "symbol" we extract is a bare BSE security code
("500319") rather than an NSE trading symbol. Downstream stages
(OpenAlgo history, backtests, watchlists) need a proper exchange tag and,
where possible, the NSE trading symbol.

Resolution strategy, in order of reliability:
1. Pure-alphanumeric symbols that match the NSE master -> NSE as-is.
2. BSE security codes: look the code up in the BSE master, then match the
   issuer name against the NSE master (normalised). NSE master rows also
   carry an ISIN, but the BSE master in the vendor checkout does not, so
   name matching is the practical join key.
3. No match -> return the original symbol with exchange "BSE" (a valid
   OpenAlgo exchange for BSE codes) and mark it unresolved.

Data source: the pinned vendor checkout
``Blue_engine/database/vendor/indian-market-historical-data/stock_list_*.csv``.
Lists are refreshed by the vendor pin updater; a stale list simply means
very recently listed names resolve as BSE/unresolved until updated.
"""
from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

LOG = logging.getLogger(__name__)

VENDOR_DIR = (Path(__file__).resolve().parents[1] / "database" / "vendor"
              / "indian-market-historical-data")
NSE_LIST = VENDOR_DIR / "stock_list_nse.csv"
BSE_LIST = VENDOR_DIR / "stock_list_bse.csv"

_CODE_RE = re.compile(r"^\d{6}$")  # BSE security codes are 6 digits


@dataclass(frozen=True)
class ResolvedSymbol:
    """A screener symbol resolved to a tradable instrument."""
    original: str        # exactly what the screen/export produced
    symbol: str          # trading symbol (NSE symbol or BSE code)
    exchange: str        # "NSE" or "BSE"
    name: str            # company name from the matched master, "" if none
    isin: str            # ISIN from the NSE master, "" if unknown
    resolved: bool       # False = no master match found (informational)

    def as_dict(self) -> Dict[str, str]:
        return {
            "original": self.original, "symbol": self.symbol,
            "exchange": self.exchange, "name": self.name,
            "isin": self.isin, "resolved": "true" if self.resolved else "false",
        }


def _norm_name(name: str) -> str:
    """Normalise a company name for cross-exchange matching.

    Screener shows short forms ("KNR Construct.", "Advani Hotels.") while
    masters carry legal names ("KNR Constructions Limited"). Keep
    alphanumeric tokens only, drop legal suffixes, and compare on the
    first few tokens so short forms still match.
    """
    text = re.sub(r"[^A-Za-z0-9 ]", " ", (name or "")).upper()
    tokens = [t for t in text.split() if t]
    drop = {"LIMITED", "LTD", "INDIA", "THE", "AND", "&", "CORPORATION",
            "CORPN", "COMPANY", "CO"}
    kept = [t for t in tokens if t not in drop]
    return " ".join(kept[:4]) if kept else " ".join(tokens[:4])


class SymbolResolver:
    """Lazy-loading resolver over the vendor NSE/BSE master lists."""

    def __init__(self, nse_path: Path = NSE_LIST, bse_path: Path = BSE_LIST):
        self._nse_path = nse_path
        self._bse_path = bse_path
        self._nse_by_symbol: Dict[str, Tuple[str, str]] = {}   # sym -> (name, isin)
        self._nse_by_name: Dict[str, str] = {}                 # norm name -> sym
        self._bse_by_code: Dict[str, str] = {}                 # code -> issuer name
        self._loaded = False

    def _load(self) -> bool:
        if self._loaded:
            return True
        try:
            with self._nse_path.open(encoding="utf-8", errors="replace", newline="") as fh:
                for row in csv.DictReader(fh):
                    sym = (row.get("SYMBOL") or "").strip().upper()
                    if not sym:
                        continue
                    name = (row.get("NAME OF COMPANY") or "").strip()
                    isin = (row.get("ISIN NUMBER") or "").strip()
                    self._nse_by_symbol[sym] = (name, isin)
                    self._nse_by_name.setdefault(_norm_name(name), sym)
            with self._bse_path.open(encoding="utf-8", errors="replace", newline="") as fh:
                for row in csv.DictReader(fh):
                    code = (row.get("Security Code") or "").strip()
                    if not code:
                        continue
                    self._bse_by_code[code] = (row.get("Issuer Name") or "").strip()
        except OSError as exc:
            LOG.warning("symbol master lists unavailable: %s", exc)
            return False
        self._loaded = True
        return True

    def resolve(self, screener_symbol: str) -> ResolvedSymbol:
        """Resolve one screener symbol to (symbol, exchange)."""
        original = (screener_symbol or "").strip().upper()
        if not original:
            return ResolvedSymbol(original, "", "NSE", "", "", False)
        if not self._load():
            # Masters unavailable: keep alphanumeric symbols as NSE (the
            # common case) and flag numeric codes as unresolved BSE.
            if _CODE_RE.fullmatch(original):
                return ResolvedSymbol(original, original, "BSE", "", "", False)
            return ResolvedSymbol(original, original, "NSE", "", "", True)

        if not _CODE_RE.fullmatch(original):
            # Alphanumeric: NSE trading symbol when present, else unknown.
            if original in self._nse_by_symbol:
                name, isin = self._nse_by_symbol[original]
                return ResolvedSymbol(original, original, "NSE", name, isin, True)
            return ResolvedSymbol(original, original, "NSE", "", "", False)

        # BSE security code: identify the issuer, then look for its NSE listing.
        issuer = self._bse_by_code.get(original, "")
        if issuer:
            nse_sym = self._nse_by_name.get(_norm_name(issuer))
            if nse_sym:
                name, isin = self._nse_by_symbol[nse_sym]
                return ResolvedSymbol(original, nse_sym, "NSE", name, isin, True)
            # BSE-only listing: tradeable on BSE via the code itself.
            return ResolvedSymbol(original, original, "BSE", issuer, "", True)
        return ResolvedSymbol(original, original, "BSE", "", "", False)


_DEFAULT: Optional[SymbolResolver] = None


def resolve_symbol(screener_symbol: str) -> ResolvedSymbol:
    """Module-level convenience using the shared default resolver."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SymbolResolver()
    return _DEFAULT.resolve(screener_symbol)


def resolve_many(symbols) -> Dict[str, ResolvedSymbol]:
    """Resolve a sequence of screener symbols to a dict keyed by original."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = SymbolResolver()
    return {sym: _DEFAULT.resolve(sym) for sym in symbols}


__all__ = ["ResolvedSymbol", "SymbolResolver", "resolve_symbol", "resolve_many"]
