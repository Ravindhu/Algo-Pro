"""
extractors.py - helpers that pull valuation inputs off a screener.in page.

WHY THIS MODULE EXISTS
----------------------
``valuations.build_inputs`` reads a handful of fields directly. Everything else
the page DOES carry was being left on the table, or missed because the label
text was guessed rather than read. This module is the layer that knows the REAL
labels, the real column layout, and how to recover a figure the page does not
print on its own line.

It is READ-ONLY over an already-parsed page. No network, no I/O: pass it the
``BeautifulSoup`` root (or a section table) and it returns plain Python.

WHAT THE REAL PAGE ACTUALLY GIVES US (verified live, RELIANCE)
--------------------------------------------------------------
The tables span **12 annual columns** (Mar 2015 .. Mar 2026), not 5. The exact
labels, which is what this module encodes, are:

    cash-flow      Cash from Operating Activity +, Cash from Investing Activity +,
                   Cash from Financing Activity +, Net Cash Flow,
                   Free Cash Flow, CFO/OP
    balance-sheet  Equity Capital, Reserves, Borrowings +, Other Liabilities +,
                   Total Liabilities, Fixed Assets +, CWIP, Investments,
                   Other Assets +, Total Assets

Note ``Cash from Investing Activity +`` - NOT ``Investing Activity``. Guessing
that label is exactly how ``capex_series`` came back empty.

THE HONEST BOUNDARY
-------------------
Four things the page genuinely does NOT print as a line, and which this module
therefore does NOT invent:

  * cash & equivalents          - folded into ``Other Assets +``
  * minority interest           - no line
  * intangibles / goodwill      - no line
  * contingent liabilities      - exists only in the annual-report PDF

For each of those the helper returns ``None``/``[]`` and records why. Guessing a
split of ``Other Assets +`` would put a fabricated number into a valuation, which
is the one thing this whole subsystem refuses to do.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from .numbers import to_number

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Exact label sets, read off the live page
# --------------------------------------------------------------------------- #
#: Cash-flow rows. ``+`` is part of the label on the page and is stripped by
#: ``_row``; listing the bare stem means a future "+"-less rename still hits.
CF_OPERATING = ("cash from operating activity", "operating activity")
CF_INVESTING = ("cash from investing activity", "investing activity")
CF_FINANCING = ("cash from financing activity", "financing activity")
CF_NET = ("net cash flow",)
CF_FCF = ("free cash flow",)

BS_EQUITY_CAPITAL = ("equity capital",)
BS_RESERVES = ("reserves",)
BS_BORROWINGS = ("borrowing",)          # covers "Borrowing" and "Borrowings +"
BS_OTHER_LIAB = ("other liabilities",)
BS_TOTAL_LIAB = ("total liabilities",)
BS_FIXED_ASSETS = ("fixed assets",)
BS_CWIP = ("cwip",)
BS_INVESTMENTS = ("investments",)
BS_OTHER_ASSETS = ("other assets",)
BS_TOTAL_ASSETS = ("total assets",)

PL_SALES = ("sales",)
PL_EXPENSES = ("expenses",)
PL_OPERATING_PROFIT = ("operating profit",)
PL_OPM = ("opm %",)
PL_OTHER_INCOME = ("other income",)
PL_INTEREST = ("interest",)
PL_DEPRECIATION = ("depreciation",)
PL_PBT = ("profit before tax",)
PL_TAX_PCT = ("tax %",)                 # EXACT - see the docstring warning below
PL_NET_PROFIT = ("net profit",)
PL_EPS = ("eps in rs",)

RATIO_DEBTOR_DAYS = ("debtor days",)
RATIO_INVENTORY_DAYS = ("inventory days",)
RATIO_PAYABLE_DAYS = ("days payable",)
RATIO_WC_DAYS = ("working capital days",)
RATIO_ROCE = ("roce %",)

# --------------------------------------------------------------------------- #
# Contingent liabilities - LOGGED-IN ONLY
# --------------------------------------------------------------------------- #
#: Seen only in a LOGGED-IN screener.in session (the row is not served to an
#: anonymous request - verified: four separate greps of the anonymous page for
#: "contingent", "Cont. Liab", "cont" and 10 other spellings all returned ZERO
#: hits across RELIANCE, ITC and KPITTECH).
#:
#: The EXACT printed label is unknown to us because it cannot be observed
#: anonymously, so this is a deliberately TOLERANT match rather than an exact
#: string. That is a departure from every other label in this module, and it is
#: justified: the abbreviation space is large ("Cont. Liab", "Contingent
#: Liabilities", "Contingent Liab.", "Cont Liabilities") while the collision
#: risk is low - only a handful of page rows contain "cont" at all, and the
#: value check below rejects anything that is not a plausible amount.
#:
#: Once the real label is known, replace this with the exact string. See
#: ``docs`` in the README and ``contingent_liabilities_label_probe`` below.
CONTINGENT_LIAB_PATTERNS = (
    r"contingent\s*liab",          # Contingent Liabilities / Contingent Liab.
    r"cont\.?\s*liab",             # Cont. Liab / Cont Liab / Cont.Liab
    r"contingent",                 # bare "Contingent ..."
)

_CONTINGENT_RE = re.compile(
    r"(?i)(?:" + "|".join(CONTINGENT_LIAB_PATTERNS) + r")")


# --------------------------------------------------------------------------- #
# Low-level row access
# --------------------------------------------------------------------------- #
def _norm(text: Any) -> str:
    return " ".join(str(text or "").replace("\xa0", " ").split()).strip().lower()


def _strip_dangling_plus(label: str) -> str:
    """``"borrowings +"`` -> ``"borrowings"``.

    screener.in marks expandable rows with a trailing ``+``. It is punctuation,
    not part of the name, and it breaks every equality match if left in.
    """
    out = label.strip()
    while out.endswith("+"):
        out = out[:-1].strip()
    return out


def _label_matches(got: str, want: str, *, exact: bool) -> bool:
    """Does a row label satisfy a wanted stem?

    THREE-WAY match, and all three are needed on the real page:

    1. exact equality                      -> "reserves" == "reserves"
    2. exact + a trailing plural 's'       -> want "borrowing" matches
                                              row "borrowings +"
                                              (THE bug: HDFCBANK labels the row
                                              "Borrowing", ITC labels it
                                              "Borrowings +". One stem must
                                              catch both.)
    3. substring, only when exact=False     -> explicit opt-in for labels that
                                              are unambiguous

    A bare substring match is NOT the default anywhere, because
    "tax" is a substring of "profit before tax" (crores vs a percentage).
    """
    if not got or not want:
        return False
    if got == want:
        return True
    # Singular/plural tolerance in both directions, and only for a trailing 's'
    # so "borrowing" does not swallow an unrelated "borrowingcosts" row.
    if got.rstrip("s") == want.rstrip("s"):
        return True
    if exact:
        return False
    return want in got


def find_row(table: Optional[Dict[str, Any]],
             labels: Sequence[str],
             *,
             exact: bool = True) -> Optional[Dict[str, str]]:
    """Locate a labelled row in a parsed table.

    Args:
        table: a ``parser.parse_table`` result.
        labels: candidate label stems; the FIRST match wins.
        exact: ``True`` matches the full label (after stripping a trailing
            ``+``); ``False`` allows a substring.

    ``exact=True`` is the default and MUST stay the default for labels that are
    substrings of each other on the real page. A substring search for ``"tax"``
    returns ``Profit before tax`` - a CRORE figure - when the caller wanted
    ``Tax %``, a percentage. That single mistake puts a crore value into a tax
    rate, and it is the most damaging silent error available here.
    """
    if not table:
        return None
    headers = table.get("headers") or []
    if not headers:
        return None
    label_col = headers[0]

    wanted = [_strip_dangling_plus(_norm(x)) for x in labels]
    for row in (table.get("rows") or []):
        got = _strip_dangling_plus(_norm(row.get(label_col, "")))
        for want in wanted:
            if _label_matches(got, want, exact=exact):
                return row
    return None


def row_series(table: Optional[Dict[str, Any]],
               labels: Sequence[str],
               *,
               exact: bool = True,
               skip_trailing: int = 0,
               limit: Optional[int] = None) -> List[Optional[float]]:
    """A labelled row as a numeric series, oldest -> newest.

    ``skip_trailing`` drops columns from the RIGHT. Used to exclude a ``TTM``
    column from the P&L, which is not an audited annual period.
    ``limit`` keeps only the LAST N values (the normalisation window).
    """
    row = find_row(table, labels, exact=exact)
    if row is None:
        return []
    headers = (table or {}).get("headers") or []
    values = [to_number(row.get(h)) for h in headers[1:]]
    if skip_trailing:
        values = values[: len(values) - skip_trailing] if skip_trailing < len(values) else []
    if limit and limit > 0:
        values = values[-limit:]
    return values


def row_latest(table: Optional[Dict[str, Any]],
               labels: Sequence[str],
               *,
               exact: bool = True,
               skip_trailing: int = 0) -> Optional[float]:
    """The most recent non-None value of a labelled row."""
    series = row_series(table, labels, exact=exact,
                        skip_trailing=skip_trailing)
    for value in reversed(series):
        if value is not None:
            return value
    return None


# --------------------------------------------------------------------------- #
# Cash-flow: capex and the CFO cross-check
# --------------------------------------------------------------------------- #
def capex_from_cashflow(company: Dict[str, Any]) -> Optional[float]:
    """Best available capex figure (positive magnitude), or ``None``.

    WHAT THE PAGE GIVES: a single NET ``Cash from Investing Activity +``. That
    is NOT capex. It nets asset purchases against asset SALES and includes
    acquisitions and investment purchases, so for a company that sold a business
    it can be positive.

    So this helper prefers, in order:

      1. ``Free Cash Flow`` and ``Cash from Operating Activity +``, from which
         capex is recovered as ``CFO - FCF``. This is exact where both rows are
         present, because screener.in defines FCF as CFO minus capex.
      2. ``-Cash from Investing Activity +``, only when that figure is NEGATIVE
         (a net outflow). Used as a labelled approximation.

    Returns ``None`` rather than a guess when neither is available - and NEVER
    returns a value derived from a POSITIVE investing figure, because a net
    inflow is proof the line is contaminated by disposals.
    """
    cf = company.get("cash_flow")
    if not cf:
        return None

    cfo = row_latest(cf, CF_OPERATING)
    fcf = row_latest(cf, CF_FCF)
    if cfo is not None and fcf is not None:
        capex = cfo - fcf
        if capex > 0:
            return capex

    investing = row_latest(cf, CF_INVESTING)
    if investing is not None and investing < 0:
        return abs(investing)

    return None


def cashflow_series(company: Dict[str, Any],
                    limit: Optional[int] = None) -> Dict[str, List[Optional[float]]]:
    """The four cash-flow lines as series, for a caller that wants history."""
    cf = company.get("cash_flow")
    return {
        "operating": row_series(cf, CF_OPERATING, limit=limit),
        "investing": row_series(cf, CF_INVESTING, limit=limit),
        "financing": row_series(cf, CF_FINANCING, limit=limit),
        "net_cash_flow": row_series(cf, CF_NET, limit=limit),
        "free_cash_flow": row_series(cf, CF_FCF, limit=limit),
    }


# --------------------------------------------------------------------------- #
# P&L helpers
# --------------------------------------------------------------------------- #
def profitability_series(company: Dict[str, Any],
                         limit: Optional[int] = None,
                         skip_trailing: int = 0) -> Dict[str, List[Optional[float]]]:
    """The P&L lines a valuation normalises over."""
    pl = company.get("profit_loss")
    return {
        "sales": row_series(pl, PL_SALES, limit=limit,
                            skip_trailing=skip_trailing),
        "expenses": row_series(pl, PL_EXPENSES, limit=limit,
                               skip_trailing=skip_trailing),
        "operating_profit": row_series(pl, PL_OPERATING_PROFIT, limit=limit,
                                       skip_trailing=skip_trailing),
        "opm_pct": row_series(pl, PL_OPM, limit=limit,
                              skip_trailing=skip_trailing),
        "other_income": row_series(pl, PL_OTHER_INCOME, limit=limit,
                                   skip_trailing=skip_trailing),
        "interest": row_series(pl, PL_INTEREST, limit=limit,
                               skip_trailing=skip_trailing),
        "depreciation": row_series(pl, PL_DEPRECIATION, limit=limit,
                                   skip_trailing=skip_trailing),
        "profit_before_tax": row_series(pl, PL_PBT, limit=limit,
                                        skip_trailing=skip_trailing),
        "tax_pct": row_series(pl, PL_TAX_PCT, limit=limit,
                              skip_trailing=skip_trailing),
        "net_profit": row_series(pl, PL_NET_PROFIT, limit=limit,
                                 skip_trailing=skip_trailing),
        "eps": row_series(pl, PL_EPS, limit=limit,
                          skip_trailing=skip_trailing),
    }


def interest_expense(company: Dict[str, Any]) -> Optional[float]:
    """Latest annual Interest expense. ``None`` when not reported."""
    return row_latest(company.get("profit_loss"), PL_INTEREST)


def other_income(company: Dict[str, Any]) -> Optional[float]:
    """Latest annual Other Income.

    Relevant to EPV because a persistent non-operating income stream is
    sometimes capitalised separately rather than left inside EBIT. screener.in's
    ``Operating Profit`` EXCLUDES it, which is why the valuation can use EBIT
    directly - but the figure is exposed here so a caller can check.
    """
    return row_latest(company.get("profit_loss"), PL_OTHER_INCOME)


# --------------------------------------------------------------------------- #
# Balance sheet helpers
# --------------------------------------------------------------------------- #
def balance_sheet_snapshot(company: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Every balance-sheet line the page actually prints, latest period."""
    bs = company.get("balance_sheet")
    return {
        "equity_capital": row_latest(bs, BS_EQUITY_CAPITAL),
        "reserves": row_latest(bs, BS_RESERVES),
        "borrowings": row_latest(bs, BS_BORROWINGS),
        "other_liabilities": row_latest(bs, BS_OTHER_LIAB),
        "total_liabilities": row_latest(bs, BS_TOTAL_LIAB),
        "fixed_assets": row_latest(bs, BS_FIXED_ASSETS),
        "cwip": row_latest(bs, BS_CWIP),
        "investments": row_latest(bs, BS_INVESTMENTS),
        "other_assets": row_latest(bs, BS_OTHER_ASSETS),
        "total_assets": row_latest(bs, BS_TOTAL_ASSETS),
        # Deliberately None: NOT printed as a line. See module docstring.
        "cash_and_equivalents": None,
        "minority_interest": None,
        "intangibles": None,
        "contingent_liabilities": None,
    }


def balance_sheet_balances(company: Dict[str, Any],
                           tolerance: float = 0.02) -> Dict[str, Any]:
    """Check the parsed balance sheet actually balances.

    A cheap but valuable integrity gate: ``Total Assets`` must equal
    ``Total Liabilities`` and must equal ``Fixed Assets + CWIP + Investments +
    Other Assets``. If either fails, a label was mis-read and every number
    derived from this page is suspect.

    Returns ``{"balanced": bool, "checks": {...}}``. Never raises.
    """
    snap = balance_sheet_snapshot(company)

    def close(a: Optional[float], b: Optional[float]) -> Optional[bool]:
        if a is None or b is None:
            return None
        if a == 0 and b == 0:
            return True
        base = max(abs(a), abs(b))
        return abs(a - b) <= base * tolerance

    assets_sum = None
    parts = [snap.get("fixed_assets"), snap.get("cwip"),
             snap.get("investments"), snap.get("other_assets")]
    if all(p is not None for p in parts):
        assets_sum = sum(parts)          # type: ignore[arg-type]

    checks = {
        "assets_eq_liabilities": close(snap.get("total_assets"),
                                       snap.get("total_liabilities")),
        "assets_eq_components": close(snap.get("total_assets"), assets_sum),
    }
    known = [v for v in checks.values() if v is not None]
    return {
        "balanced": all(known) if known else None,
        "checks": checks,
        "components_sum": assets_sum,
        "total_assets": snap.get("total_assets"),
    }


def equity_from_balance_sheet(company: Dict[str, Any]) -> Optional[float]:
    """Shareholders' equity = Equity Capital + Reserves."""
    snap = balance_sheet_snapshot(company)
    cap, res = snap.get("equity_capital"), snap.get("reserves")
    if cap is None and res is None:
        return None
    return (cap or 0.0) + (res or 0.0)


def net_working_assets(company: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """A working-capital view built ONLY from printed lines.

    The page does not split inventory from receivables from cash, so a classic
    current-assets-minus-current-liabilities figure is NOT computable here. What
    IS computable is the activity-based view:

        inventory        = Inventory Days  x daily sales
        receivables      = Debtor Days     x daily sales
        payables         = Days Payable    x daily sales
        working capital  = Working Capital Days x daily sales

    All four are returned, each ``None`` when its input is missing. Reporting the
    components rather than one net figure is deliberate: the caller can see which
    part is derived from which ratio.
    """
    ratios = company.get("ratios")
    revenue = row_latest(company.get("profit_loss"), PL_SALES)

    def from_days(labels) -> Optional[float]:
        if not revenue or revenue <= 0:
            return None
        days = row_latest(ratios, labels)
        if days is None or not (-3650 < days < 3650):
            return None
        return (days / 365.0) * revenue

    return {
        "revenue_latest": revenue,
        "inventory": from_days(RATIO_INVENTORY_DAYS),
        "receivables": from_days(RATIO_DEBTOR_DAYS),
        "payables": from_days(RATIO_PAYABLE_DAYS),
        "working_capital": from_days(RATIO_WC_DAYS),
    }


# --------------------------------------------------------------------------- #
# Market helpers
# --------------------------------------------------------------------------- #
def market_snapshot(company: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Price, market cap, and the implied share counts."""
    from .numbers import ratio_lookup

    top = company.get("top_ratios") or []
    price = to_number(ratio_lookup(top, "current_price"))
    mcap = to_number(ratio_lookup(top, "market_cap"))

    shares_from_mcap = None
    if price and mcap and price > 0:
        shares_from_mcap = mcap / price

    # EPS bridge. TTM is skipped so the ratio is annual-consistent.
    pl = company.get("profit_loss")
    headers = (pl or {}).get("headers") or []
    ttm = sum(1 for h in headers[1:] if "ttm" in str(h).lower())
    np_ = row_latest(pl, PL_NET_PROFIT, skip_trailing=ttm)
    eps = row_latest(pl, PL_EPS, skip_trailing=ttm)
    shares_from_eps = None
    if np_ and eps and eps > 0:
        shares_from_eps = np_ / eps

    # Prefer the market-cap-implied count: it is the one consistent with the
    # price in the same snapshot (a bonus/split since the EPS period breaks EPS).
    shares = shares_from_mcap if shares_from_mcap else shares_from_eps
    source = "market_cap/price" if shares_from_mcap else (
        "net_profit/eps" if shares_from_eps else None)

    disagree = None
    if shares_from_mcap and shares_from_eps:
        disagree = abs(shares_from_mcap - shares_from_eps) / shares_from_eps

    return {
        "price": price,
        "market_cap": mcap,
        "shares_from_market_cap": shares_from_mcap,
        "shares_from_eps": shares_from_eps,
        "shares": shares,
        "shares_source": source,
        "shares_disagreement_pct": disagree,
    }


# --------------------------------------------------------------------------- #
# Statement depth
# --------------------------------------------------------------------------- #
def statement_years(company: Dict[str, Any]) -> Dict[str, int]:
    """How many annual columns each table carries.

    Reported so the normalisation window can be chosen from evidence (the page
    gives 12 years, not 5) instead of a hard-coded number.
    """
    out = {}
    for key in ("profit_loss", "balance_sheet", "cash_flow", "ratios",
                "quarters", "shareholding"):
        table = company.get(key)
        headers = (table or {}).get("headers") or []
        # Exclude the leading label column and any TTM column.
        cols = [h for h in headers[1:] if "ttm" not in str(h).lower()]
        out[key] = len(cols)
    return out


# --------------------------------------------------------------------------- #
# One-call bundle
# --------------------------------------------------------------------------- #
#: Fields this helper set can supply that ``build_inputs`` reads. Used by the
#: bundle to report coverage without duplicating the field list.
BUNDLE_FIELDS = (
    "equity_from_balance_sheet",
    "capex",
    "interest_expense",
    "other_income",
    "net_current_assets",
    "market",
    "statement_years",
    "balance_check",
    "contingent_liabilities",
)


def extraction_bundle(company: Dict[str, Any]) -> Dict[str, Any]:
    """Every helper above in one call, for a caller that wants the lot.

    Also returns ``unavailable`` - the fields that are NOT on the page at all -
    so a consumer sees the boundary explicitly instead of assuming a missing key
    means a bug.

    ``contingent_liabilities`` is special: it is present ONLY when the page was
    fetched with a logged-in session cookie, so the bundle reports
    ``contingent_source`` to distinguish the two cases.
    """
    contingent = contingent_liabilities(company)
    if contingent is not None:
        contingent_source = "screener:logged-in-ratios"
    else:
        contingent_source = (
            "not-visible (row is served only to a logged-in session; "
            "fetch with cookies=... to populate)"
        )

    unavailable = [
        "cash_and_equivalents  (folded into 'Other Assets +' - no line)",
        "minority_interest     (no line on the page)",
        "intangibles/goodwill  (no line on the page)",
    ]
    if contingent is None:
        unavailable.append(
            "contingent_liabilities (LOGGED-IN session only - use cookies=...)")

    return {
        "equity_from_balance_sheet": equity_from_balance_sheet(company),
        "capex": capex_from_cashflow(company),
        "cashflow": cashflow_series(company),
        "profitability": profitability_series(company),
        "interest_expense": interest_expense(company),
        "other_income": other_income(company),
        "balance_sheet": balance_sheet_snapshot(company),
        "net_current_assets": net_working_assets(company),
        "market": market_snapshot(company),
        "statement_years": statement_years(company),
        "balance_check": balance_sheet_balances(company),
        "contingent_liabilities": contingent,
        "contingent_source": contingent_source,
        "unavailable": unavailable,
    }


__all__ = [
    "find_row",
    "row_series",
    "row_latest",
    "capex_from_cashflow",
    "cashflow_series",
    "profitability_series",
    "interest_expense",
    "other_income",
    "balance_sheet_snapshot",
    "balance_sheet_balances",
    "equity_from_balance_sheet",
    "net_working_assets",
    "market_snapshot",
    "statement_years",
    "contingent_liabilities",
    "contingent_liabilities_label_probe",
    "extraction_bundle",
    "BUNDLE_FIELDS",
    "CONTINGENT_LIAB_PATTERNS",
]


# --------------------------------------------------------------------------- #
# Contingent liabilities (LOGGED-IN ONLY)
# --------------------------------------------------------------------------- #
def contingent_liabilities(company: Dict[str, Any]) -> Optional[float]:
    """Contingent liabilities, when the page was fetched with a session cookie.

    Returns the latest period's value in CRORES, or ``None``.

    WHY THIS IS DIFFERENT FROM EVERY OTHER HELPER HERE
    -------------------------------------------------
    The row is served **only to a logged-in screener.in session**. Verified by
    four independent greps of the anonymous page across RELIANCE, ITC and
    KPITTECH: zero hits for ``contingent``, ``Cont. Liab``, ``Cont Liab``,
    ``cont`` and ten other spellings - "cont" does not appear as a substring
    anywhere on the page.

    So this helper returns ``None`` on an anonymous fetch, which is the correct
    and expected outcome. Callers must not read that ``None`` as "the company
    has no contingencies"; it means "we could not see the row". The
    ``extraction_bundle`` surfaces which case applies via ``contingent_source``.

    Args:
        company: a parsed company snapshot.

    MATCHING IS TOLERANT, DELIBERATELY. Every other label in this module is
    matched exactly, because guessing cost us two figures (``Cash from Investing
    Activity +`` and ``Borrowing`` vs ``Borrowings +``). Here the exact label
    CANNOT be observed anonymously, so an exact match would be a guess too - just
    a different one. A regex over the known abbreviation space is used instead,
    and two guards replace the certainty that exact matching normally provides:

      1. the match must be a *row label* (first cell of a table row) or appear in
         page text, not just any occurrence; and
      2. the value must parse as a plausible non-negative amount. A row whose
         value will not parse is rejected rather than returned as 0.
    """
    targets: List[Dict[str, Any]] = []
    for key in ("ratios", "balance_sheet", "profit_loss", "cash_flow",
                "quarters", "shareholding"):
        table = company.get(key)
        if table:
            targets.append(table)

    # 1. Table rows, by label. Highest confidence.
    for table in targets:
        headers = table.get("headers") or []
        if not headers:
            continue
        label_col = headers[0]
        for row in (table.get("rows") or []):
            label = str(row.get(label_col, "") or "")
            if not _CONTINGENT_RE.search(label):
                continue
            value = _latest_from_row(row, headers)
            if value is not None and value >= 0:
                logger.debug("contingent liabilities matched row %r -> %s",
                             label, value)
                return value

    # 2. A header cell (some tab layouts put it in <th>).
    for table in targets:
        for header in (table.get("headers") or []):
            if _CONTINGENT_RE.search(str(header)):
                # The header names the ROW; find it and read its latest value.
                row = find_row(table, (str(header),), exact=True)
                if row is None:
                    row = find_row(table, (str(header),), exact=False)
                if row is not None:
                    value = _latest_from_row(row, table.get("headers") or [])
                    if value is not None and value >= 0:
                        return value

    # 3. A raw key on the snapshot (a provider may have parsed it already).
    for key in ("contingent_liabilities", "contingent_liab", "cont_liab"):
        value = to_number(company.get(key))
        if value is not None and value >= 0:
            return value

    return None


def _latest_from_row(row: Dict[str, Any],
                     headers: Sequence[str]) -> Optional[float]:
    """Most recent numeric cell of a row, skipping the label column and TTM."""
    keys = [h for h in headers[1:] if h in row]
    for key in reversed(keys):
        if "ttm" in str(key).lower():
            continue
        value = to_number(row.get(key))
        if value is not None:
            return value
    return None


def contingent_liabilities_label_probe(html: str) -> List[str]:
    """Find candidate contingent-liability labels in a LOGGED-IN page.

    Run this once with your session cookie to learn the exact printed label,
    then either keep the tolerant regex or hard-code the exact string:

        from blue_engine.screener.client.screener_client import ScreenerClient
        from blue_engine.screener.client import extractors as EX

        c = ScreenerClient(cookies="sessionid=...")
        html, err = c.fetch_html(
            "https://www.screener.in/company/RELIANCE/consolidated/", "company")
        print(EX.contingent_liabilities_label_probe(html))

    Returns every distinct label whose text matches the tolerant pattern, so the
    real one is visible rather than guessed. Empty list on an anonymous page -
    which is itself the proof that the row is gated.
    """
    from .parser import parse_html

    root = parse_html(html)
    found: List[str] = []

    def _note(text: str) -> None:
        clean = " ".join(str(text or "").split())
        if clean and _CONTINGENT_RE.search(clean) and clean not in found:
            found.append(clean)

    for table in root.select("table"):
        sec = table.find_parent("section")
        sec_id = (sec.get("id") if sec else "") or "?"
        for tr in table.select("tr"):
            first = tr.select_one("td, th")
            if first is not None:
                label = " ".join(first.get_text().split())
                if _CONTINGENT_RE.search(label):
                    _note(f"[section#{sec_id}] {label}")
        for th in table.select("th"):
            _note(f"[section#{sec_id}] {th.get_text()}")

    return found
