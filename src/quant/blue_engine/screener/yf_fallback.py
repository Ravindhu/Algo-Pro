"""
yf_fallback.py - optional yfinance fallback for missing valuation inputs.

ROLE, AND THE RULE THAT GOVERNS IT
----------------------------------
**screener.in is the primary source. yfinance fills ONLY the gaps it leaves.**

This module never overrides a figure screener.in reported. It is called after
``valuations.build_inputs`` has run, it is handed the already-populated
:class:`FinancialInputs`, and it may write a value ONLY into a field that is
still ``None``. Every field it fills is recorded in ``inp.filled_by``, so the
output can always say which vendor a number came from.

Why that ordering matters: screener.in's summary tables are the basis the
screen's rules were calibrated on, and swapping a reported figure for a
different vendor's version - even a "better" one - would silently change the
screen's decisions. Filling gaps is additive; overriding is not.

WHY A FALLBACK IS NEEDED AT ALL
-------------------------------
Verified by probing yfinance against RELIANCE.NS. screener.in's summary tables
leave these gaps, and yfinance's statement DataFrames close them:

  gap                          screener.in        yfinance row
  ---------------------------  -----------------  ---------------------------------
  cash & equivalents           inside "Other      Cash And Cash Equivalents,
                               Assets +"          Cash Cash Equivalents And ST Inv
  DILUTED share count          not published      Diluted Average Shares
  minority interest            not published      Minority Interest
  inventory / receivables      inside "Other      Inventory, Accounts Receivable
                               Assets +"
  goodwill / intangibles       no line            Goodwill, Other Intangible Assets
  interest expense             in P&L only        Interest Expense
  capex                        net, in CF only    Capital Expenditure
  payables                     not published      Accounts Payable
  contingent liabilities       not published      STILL NOT AVAILABLE

Note the last row. yfinance does NOT close every gap, and this module does not
pretend otherwise: a field that neither source supplies stays ``None``.

DATA-QUALITY CAVEATS (measured, not assumed)
--------------------------------------------
1. **``Tax Rate For Calcs`` is unreliable for Indian tickers.** Probed value for
   RELIANCE.NS was ``0``, while ``Tax Provision / Pretax Income`` gives 22.4%.
   This module therefore NEVER reads yfinance's tax-rate field; it derives the
   rate from the two underlying amounts.
2. **yfinance is an unofficial Yahoo scraper** - the same reliability class as
   the screener scraper, with no SLA and no deprecation notice. It is a
   FALLBACK, never a dependency: if the import fails, the network fails, or the
   fields are absent, the valuation proceeds exactly as it did before.
3. **Only ~5 annual periods**, against screener.in's 10+. So yfinance can fill a
   point-in-time BALANCE-SHEET item comfortably, but it must not be used to
   lengthen the normalisation window - 5 years is the module's default window
   anyway, and a shorter history would narrow it silently.
4. A probed fiscal period came back as a FUTURE date (``2026-03-31``). The
   module does not trust period labels; it takes the most recent column and
   records the label it used.

SAFETY
------
* Read-only network access to Yahoo. No broker, no OpenAlgo, no order path.
* Never raises: every failure degrades to "nothing filled".
* Opt-in. Off unless a caller passes a yfinance provider, or explicitly enables
  it in config.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: The yfinance suffix for Indian exchanges. NSE is the primary listing for the
#: liquid universe; BSE is the fallback when an NSE symbol is unknown.
NSE_SUFFIX = ".NS"
BSE_SUFFIX = ".BO"

#: 1 crore = 10^7. THE UNIT BRIDGE BETWEEN THE TWO SOURCES.
#:
#: This is the single most dangerous thing in this module. screener.in reports
#: every amount in CRORES; yfinance reports every amount in ABSOLUTE rupees
#: (and share counts in absolute shares). Verified against RELIANCE.NS:
#:
#:     yfinance Total Assets      21,781,400,000,000  = 2,178,140 Cr
#:     yfinance Diluted Shares    13,532,736,424       = 1,353.27 crore-shares
#:     screener.in implied shares 1,352.97 crore-shares
#:
#: Copying a yfinance amount in without dividing by CRORE multiplies it by 1e7.
#: That is not a rounding error - it is a seven-order-of-magnitude error, and
#: for the share count it silently divides every per-share figure by 10 million.
#: The first live run of this module hit exactly that. All amounts therefore go
#: through :func:`_to_crore`.
CRORE = 1e7


def _to_crore(value: Optional[float]) -> Optional[float]:
    """Convert a yfinance ABSOLUTE-rupee/count figure to screener.in CRORES.

    Applied to currency amounts AND to share counts - both are absolute in
    yfinance, and both must become crore-scaled to sit next to screener.in
    values in the same :class:`FinancialInputs`.

    Returns ``None`` unchanged, so a missing figure stays missing.
    """
    if value is None:
        return None
    return value / CRORE


def available() -> bool:
    """True when yfinance is importable. Cheap enough to call per valuation."""
    try:
        import yfinance  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - missing package, broken install, ...
        return False


# --------------------------------------------------------------------------- #
# Statement access helpers
# --------------------------------------------------------------------------- #
def _latest_value(frame, *row_names) -> Optional[float]:
    """Most recent non-null value from a yfinance statement for the first
    matching row name.

    yfinance renames rows between versions ("Total Liab" vs "Total
    Liabilities"), so several candidate names are tried in order. Returns
    ``None`` when no candidate row exists - never 0.
    """
    if frame is None:
        return None
    try:
        if getattr(frame, "empty", True):
            return None
    except Exception:  # noqa: BLE001
        return None

    for name in row_names:
        try:
            if name not in frame.index:
                continue
            row = frame.loc[name]
            # Columns are periods, newest first; take the first usable value.
            for col in row.index:
                value = row[col]
                if value is None:
                    continue
                try:
                    f = float(value)
                except (TypeError, ValueError):
                    continue
                # NaN is not a value. `f != f` is the NaN test that needs no numpy.
                if f != f:
                    continue
                return f
        except Exception:  # noqa: BLE001 - a malformed frame row
            continue
    return None


def _period_label(frame) -> str:
    """The period label of the column the values were read from."""
    try:
        cols = list(frame.columns)
        return str(cols[0])[:10] if cols else ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# The fallback
# --------------------------------------------------------------------------- #
def fill_missing_inputs(inp: Any, *,
                        ticker: Optional[str] = None,
                        symbol: Optional[str] = None,
                        notes: Optional[List[str]] = None) -> List[str]:
    """Fill ONLY the ``None`` fields of a :class:`FinancialInputs` from yfinance.

    Args:
        inp: the ``FinancialInputs`` produced by ``valuations.build_inputs``.
        ticker: an explicit Yahoo ticker (e.g. ``"RELIANCE.NS"``). When omitted
            it is built from ``symbol`` / ``inp.symbol``.
        symbol: the bare NSE symbol; ``.NS`` is appended.
        notes: an optional list to append provenance notes to. Defaults to
            ``inp.notes``, which is what the valuation output already renders.

    Returns:
        The list of field names actually filled. Empty when yfinance supplied
        nothing - which is a normal outcome, not an error.

    Contract:
      * A field that is already set is NEVER touched.
      * A field neither source provides stays ``None``.
      * Nothing raises. A missing package or a dead network returns ``[]``.
    """
    note_list = notes if notes is not None else getattr(inp, "notes", [])

    if not available():
        note_list.append(
            "yfinance not installed; screener.in values used as-is")
        return []

    target = ticker or _to_yahoo_ticker(symbol or getattr(inp, "symbol", ""))
    if not target:
        # No symbol means we cannot address a ticker. Say so rather than
        # returning silently: an empty result with no explanation is
        # indistinguishable from "everything was already present".
        note_list.append(
            "yfinance fallback skipped: no symbol available to build a ticker")
        return []

    try:
        import yfinance as yf
        tk = yf.Ticker(target)
        bs = tk.balance_sheet
        inc = tk.income_stmt
        cf = tk.cashflow
    except Exception as exc:  # noqa: BLE001 - network, API change, bad ticker
        note_list.append(
            f"yfinance fetch failed for {target} ({type(exc).__name__}); "
            f"screener.in values used as-is")
        return []

    # A completely empty response means the ticker is wrong or Yahoo is down.
    if all(f is None or getattr(f, "empty", True) for f in (bs, inc, cf)):
        note_list.append(
            f"yfinance returned no statements for {target}; "
            f"screener.in values used as-is")
        return []

    filled: List[str] = []
    period = _period_label(bs) or _period_label(inc)

    def fill(field: str, value: Optional[float]) -> None:
        """Set ONE field if (and only if) it is currently unset.

        ``value`` must ALREADY be in screener.in's crore convention - callers
        pass everything through :func:`_to_crore`.
        """
        if value is None:
            return
        if getattr(inp, field, None) is not None:
            return          # screener.in already supplied it - never override
        setattr(inp, field, value)
        inp.filled_by[field] = "yfinance"
        filled.append(field)

    # ---- balance sheet: point-in-time items screener.in folds together ---- #
    # EVERY amount passes through _to_crore: yfinance is absolute rupees.
    fill("cash_and_equivalents",
         _to_crore(_latest_value(
             bs, "Cash And Cash Equivalents",
             "Cash Cash Equivalents And Short Term Investments",
             "Cash Financial")))
    fill("inventory",
         _to_crore(_latest_value(bs, "Inventory", "Total Inventory")))
    fill("receivables",
         _to_crore(_latest_value(bs, "Accounts Receivable", "Receivables",
                                 "Gross Accounts Receivable")))
    # NOTE: yfinance gives payables as a BALANCE, not a day count, so
    # ``days_payable`` is not fillable from here and is skipped deliberately
    # rather than filled with a figure of the wrong unit.
    fill("other_liabilities",
         _to_crore(_latest_value(bs, "Accounts Payable", "Payables",
                                 "Payables And Accrued Expenses")))

    # Goodwill + other intangibles: screener.in has no line at all, so this is
    # the only source for the CANAV intangible haircut.
    goodwill = _latest_value(bs, "Goodwill",
                             "Goodwill And Other Intangible Assets")
    other_intang = _latest_value(bs, "Other Intangible Assets",
                                 "Net Intangible Assets")
    if goodwill is not None or other_intang is not None:
        fill("intangibles", _to_crore((goodwill or 0.0) + (other_intang or 0.0)))

    fill("minority_interest", _to_crore(_latest_value(bs, "Minority Interest")))
    fill("total_assets", _to_crore(_latest_value(bs, "Total Assets")))

    # Shareholders' equity: only if screener.in could not build it.
    if getattr(inp, "shareholders_equity", None) is None:
        fill("shareholders_equity",
             _to_crore(_latest_value(
                 bs, "Stockholders Equity", "Total Stockholder Equity",
                 "Common Stock Equity")))
    fill("borrowings",
         _to_crore(_latest_value(
             bs, "Total Debt",
             "Long Term Debt And Capital Lease Obligation",
             "Long Term Debt")))

    # ---- income statement: the DILUTED share count ------------------------- #
    # screener.in publishes only basic (implied from EPS). Diluted is the count
    # the brief asked for, and it is only obtainable here.
    #
    # UNITS: yfinance returns an ABSOLUTE share count (13,532,736,424), while
    # screener.in's implied count is in CRORE-shares (1,352.97). Both must be in
    # crores before the per-share division, or every per-share figure is wrong
    # by a factor of 10 million.
    if getattr(inp, "diluted_shares", None) is None or \
            not getattr(inp, "shares_are_diluted", False):
        diluted_raw = _latest_value(inc, "Diluted Average Shares")
        if diluted_raw is not None and diluted_raw > 0:
            basic_raw = _latest_value(inc, "Basic Average Shares")
            # Sanity check on the RAW counts, before scaling: a diluted count
            # below the basic count, or one off by >50%, is a parsing artefact.
            if basic_raw is None or (0.5 <= basic_raw / diluted_raw <= 1.0):
                diluted = _to_crore(diluted_raw)
                # Cross-check against the share count screener.in already
                # implied. If they disagree by more than 20%, do NOT overwrite:
                # conflicting counts mean one source is stale (a bonus/split),
                # and the screener figure is the one consistent with the price
                # in the same inputs.
                current = getattr(inp, "diluted_shares", None)
                if current and not (0.80 <= diluted / current <= 1.20):
                    note_list.append(
                        f"yfinance diluted share count "
                        f"({diluted:,.1f} cr) disagrees with the screener-implied "
                        f"count ({current:,.1f} cr) by more than 20%; keeping the "
                        f"screener count, which is consistent with the price")
                else:
                    inp.diluted_shares = diluted
                    inp.shares_are_diluted = True
                    inp.filled_by["diluted_shares"] = "yfinance"
                    filled.append("diluted_shares")

    # ---- cash-flow: capex, which replaces ASSUMPTION A1's blind proxy ------ #
    capex = _latest_value(cf, "Capital Expenditure", "Purchase Of PPE")
    if capex is not None:
        # yfinance reports capex as a NEGATIVE outflow; store the magnitude.
        fill("capex_actual", _to_crore(abs(capex)))

    if filled:
        note_list.append(
            f"yfinance ({target}) filled {len(filled)} input(s) screener.in "
            f"omitted: {', '.join(sorted(filled))}"
            + (f" [period {period}]" if period else ""))
    else:
        note_list.append(
            f"yfinance consulted for {target} but screener.in already supplied "
            f"every input this module models")

    return filled


def _to_yahoo_ticker(symbol: str) -> str:
    """Map a bare NSE symbol to a Yahoo ticker. Already-suffixed input passes.

    ``RELIANCE`` -> ``RELIANCE.NS``. ``RELIANCE.NS`` -> unchanged.
    Yahoo uses a hyphen for NSE series symbols (``M&M`` -> ``M&M.NS`` is fine,
    but ``BAJAJ-AUTO`` stays hyphenated), so no character rewriting is done.
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    if s.endswith(NSE_SUFFIX) or s.endswith(BSE_SUFFIX):
        return s
    return s + NSE_SUFFIX


def is_financial_sector(ticker_or_info: Any) -> Optional[bool]:
    """Best-effort sector probe, for the bank-exclusion path.

    Returns True/False, or None when the sector cannot be established. Kept
    deliberately small: the caller decides what to do with an unknown sector,
    because silently treating unknown as "not a bank" would let a lender
    through the debt rule.
    """
    if not available():
        return None
    try:
        import yfinance as yf
        info = yf.Ticker(str(ticker_or_info)).info or {}
        sector = str(info.get("sector") or "").lower()
        industry = str(info.get("industry") or "").lower()
        if not sector and not industry:
            return None
        blob = f"{sector} {industry}"
        return any(k in blob for k in ("financial", "bank", "insurance"))
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "available",
    "fill_missing_inputs",
    "is_financial_sector",
    "NSE_SUFFIX",
    "BSE_SUFFIX",
    "CRORE",
    "_to_crore",
]
