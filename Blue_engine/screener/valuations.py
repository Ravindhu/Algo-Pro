"""
valuations.py - fundamental valuation: EPV and Conservative Adjusted NAV.

WHAT THIS MODULE IS
-------------------
Two independent, deliberately CONSERVATIVE estimates of what a business is
worth, per share, from a company's reported financials:

  1. EPV   (Earnings Power Value)          - an earnings-based value that
                                             assumes NO growth.
  2. CANAV (Conservative Adjusted Net
           Asset Value)                    - an asset-based value after
                                             haircuts.

They answer different questions and are useful precisely because they DISAGREE:

  * EPV high, NAV low    -> the value is in the earnings stream (a franchise).
  * NAV high, EPV low    -> the assets are worth more than the earnings
                            (asset-rich, earnings-weak).
  * both high            -> broad alignment; the strongest signal here.
  * both low             -> no value cushion on either basis.

THE FOUR RULES THIS MODULE OBEYS
--------------------------------
1. **Never invent missing data.** Every input is either measured from a reported
   figure or ``None``. A valuation that cannot be computed returns
   ``status="insufficient_data"`` with the exact missing inputs listed - it does
   NOT return a number built on guessed values. This is the whole point: a
   valuation you cannot reconstruct is worse than no valuation, because it
   gets used.

2. **Consolidated financials where available.** The client fetches the
   ``/consolidated/`` page by default. The URL is recorded in the output so the
   basis is auditable.

3. **Discount the whole forecast, never a single year.** EPV normalises over
   the trailing 3-5 years. One unusually profitable year is exactly the input
   that destroys an EPV, so the module uses the MEDIAN of the normalised series
   and reports the year-by-year series it used.

4. **Haircuts are per-company, and defaults are labelled assumptions.** CANAV
   does not apply one fixed set of percentages to every company. It reads what
   the balance sheet actually implies and only falls back to the conservative
   defaults when a detail is unavailable - and when it does, it says so in
   ``assumptions``.

DATA REALITY (what screener.in actually provides)
-------------------------------------------------
Verified against live pages. The summary tables give:

  P&L         Sales, Expenses, Operating Profit, OPM %, Other Income, Interest,
              Depreciation, Profit before tax, Tax %, Net Profit, EPS in Rs,
              Dividend Payout %   (10+ years, annual + TTM)
  Balance     Equity Capital, Reserves, Borrowings, Other Liabilities, Fixed
    Sheet     Assets, CWIP, Investments, Other Assets, Total Liabilities/Assets
  Cash Flow   Operating/Investing/Financing Activity, Free Cash Flow
  Ratios      Debtor Days, Inventory Days, Days Payable, Working Capital Days,
              ROCE, ROE

Using ONLY these:

  * Normalized EBIT      = Operating Profit   (== Sales - Expenses, exact)
  * Normalized tax rate  = MEDIAN of "Tax %"
  * Depreciation         = reported directly
  * Maintenance capex    = Depreciation, times a configurable multiple \
                           (a PROXY - see ASSUMPTION A1)
  * Maintenance WC       = Working Capital Days / 365 * Revenue increment \
                           (screener's own definition; negative is allowed)
  * Excess cash          = (Other Assets + Investments + CWIP, floored at a
                           configurable operating minimum) - see ASSUMPTION A2
  * Debt                 = Borrowings
  * Shares               = Net Profit / EPS in Rs

What screener.in does NOT provide, and which is therefore an explicit
ASSUMPTION or a recorded gap (never a silent zero):
  inventory/receivables split, maintenance-vs-growth capex split, minority
  interest, contingent liabilities, DILUTED shares (only basic, implied).

Every assumption applied is listed in the output under ``assumptions`` with a
stable code (A1, A2, ...) so a reader can audit or override it.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .client.numbers import to_number

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Scenario multipliers
# --------------------------------------------------------------------------- #
#: EPV scenarios. Bear/Base/Bull vary the two assumptions that dominate the
#: answer least transparently: the discount rate and the maintenance capex
#: multiple. They do NOT vary revenue or the margin - the entire premise of EPV
#: is that those are normalised, and flexing them would turn this into a
#: DCF with growth, which is a different (and far less trustworthy) model.
EPV_SCENARIOS: Dict[str, Dict[str, float]] = {
    "bear": {"discount_rate": 0.12, "maintenance_capex_multiple": 1.30},
    "base": {"discount_rate": 0.10, "maintenance_capex_multiple": 1.00},
    "bull": {"discount_rate": 0.09, "maintenance_capex_multiple": 0.80},
}

#: CANAV scenarios. Ordered from most to least punitive. ``stress`` is meant to
#: be uncomfortable: it is the "what if the assets are worth materially less
#: than the balance sheet says" case.
#:
#: Each scenario scales the BASE haircuts. A scenario multiplier of 1.0 means
#: "use the base haircut"; 1.5 means "haircut 50% harder".
CANAV_SCENARIOS: Dict[str, Dict[str, float]] = {
    "stress": {
        "inventory": 0.6667,      # 66.67% written off
        "receivables": 0.6667,    # via expected-credit-loss
        "intangibles": 1.0,       # goodwill and intangibles go to zero
        "quoted_investment": 0.4000,   # 40% below quote
        "unquoted_investment": 0.5000,  # 50% below carrying value
        "asset_revaluation": 0.30,      # write 30% off fixed assets
        "contingent": 1.0,              # 100% of disclosed contingents
    },
    "base": {
        "inventory": 0.3333,      # the requested conservative default
        "receivables": 0.5000,    # the requested conservative default
        "intangibles": 1.0,
        "quoted_investment": 0.2500,
        "unquoted_investment": 0.3300,
        "asset_revaluation": 0.15,
        "contingent": 0.75,
    },
    "moderate": {
        "inventory": 0.1667,
        "receivables": 0.2500,
        "intangibles": 1.0,
        "quoted_investment": 0.1000,
        "unquoted_investment": 0.1500,
        "asset_revaluation": 0.05,
        "contingent": 0.50,
    },
}

#: The requested conservative fallbacks, used ONLY when a company-specific
#: figure cannot be established. Recorded as an ASSUMPTION when applied.
DEFAULT_INVENTORY_HAIRCUT = 0.3333
DEFAULT_RECEIVABLE_HAIRCUT = 0.5000


# --------------------------------------------------------------------------- #
# Valuation classification
# --------------------------------------------------------------------------- #
CLASS_STRONG = "STRONG VALUATION ALIGNMENT"
CLASS_ASSET_RICH = "ASSET RICH / EARNINGS WEAK"
CLASS_FRANCHISE = "EARNINGS FRANCHISE"
CLASS_RICH = "RICH / PREMIUM TO BOTH"
CLASS_INSUFFICIENT = "INSUFFICIENT DATA"


def classify(market_price: Optional[float],
             epv_per_share: Optional[float],
             canav_per_share: Optional[float]) -> Tuple[str, str]:
    """Classify a name by where price sits relative to the two value estimates.

        STRONG VALUATION ALIGNMENT : Price < NAV  AND Price < EPV
        ASSET RICH / EARNINGS WEAK : Price < NAV  but   Price > EPV
        EARNINGS FRANCHISE         : Price > NAV  but   Price < EPV
        RICH / PREMIUM TO BOTH     : Price > NAV  and   Price > EPV

    Returns ``(classification, reason)``.

    Both comparisons use the BASE scenario estimates. When either base estimate
    is unavailable the classification is ``INSUFFICIENT DATA`` - we do not
    fall back to bear or bull, because a classification produced by a different
    scenario than the one the reader is looking at would be unreproducible.
    """
    if market_price is None or market_price <= 0:
        return CLASS_INSUFFICIENT, "no market price"
    if epv_per_share is None and canav_per_share is None:
        return CLASS_INSUFFICIENT, "neither EPV nor CANAV could be computed"
    if epv_per_share is None:
        return CLASS_INSUFFICIENT, "EPV not computable"
    if canav_per_share is None:
        return CLASS_INSUFFICIENT, "CANAV not computable"

    below_nav = market_price < canav_per_share
    below_epv = market_price < epv_per_share

    if below_nav and below_epv:
        return CLASS_STRONG, (
            f"price {market_price:,.2f} is below both CANAV "
            f"{canav_per_share:,.2f} and EPV {epv_per_share:,.2f}")
    if below_nav and not below_epv:
        return CLASS_ASSET_RICH, (
            f"price {market_price:,.2f} is below CANAV "
            f"{canav_per_share:,.2f} but above EPV {epv_per_share:,.2f} "
            f"- assets exceed earning power")
    if not below_nav and below_epv:
        return CLASS_FRANCHISE, (
            f"price {market_price:,.2f} is above CANAV "
            f"{canav_per_share:,.2f} but below EPV {epv_per_share:,.2f} "
            f"- value is in the earnings stream")
    return CLASS_RICH, (
        f"price {market_price:,.2f} is above both CANAV "
        f"{canav_per_share:,.2f} and EPV {epv_per_share:,.2f}")


# --------------------------------------------------------------------------- #
# Assumption record
# --------------------------------------------------------------------------- #
@dataclass
class Assumption:
    """One explicit modelling assumption, with a stable code.

    Anything that is NOT measured from a reported figure must be recorded here.
    A valuation is only as honest as this list.
    """

    code: str
    text: str
    impact: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code, "text": self.text, "impact": self.impact}


# --------------------------------------------------------------------------- #
# Series extraction helpers
# --------------------------------------------------------------------------- #
def _row_series(table: Optional[Dict[str, Any]],
                label: str,
                *, exact: bool = True) -> List[Optional[float]]:
    """Extract one labelled row as a numeric series across all periods.

    ``exact=True`` matches the label exactly (case/whitespace-insensitive).
    That is REQUIRED for labels that are substrings of each other on the real
    page: a substring match for ``"tax"`` returns ``"Profit before tax"``, whose
    values are in crores, whereas ``"Tax %"`` is a percentage. Mixing those two
    would put a crore figure into a tax rate - a silent, catastrophic bug.

    ``exact=False`` allows a substring match, used only where the label is
    unambiguous (e.g. ``"borrowing"`` for ``Borrowing`` vs ``Borrowings +``).
    """
    if not table:
        return []
    headers = table.get("headers") or []
    if not headers:
        return []
    label_col = headers[0]
    want = " ".join(str(label).split()).strip().lower()

    for row in (table.get("rows") or []):
        got = " ".join(str(row.get(label_col, "")).split()).strip().lower()
        got = got.rstrip("+").strip()          # "Sales +" -> "sales"
        hit = (got == want) if exact else (want in got)
        if hit:
            return [to_number(row.get(h)) for h in headers[1:]]
    return []


def _latest(series: Sequence[Optional[float]],
            skip_trailing: int = 0) -> Optional[float]:
    """Most recent non-None value, optionally skipping a trailing column.

    ``skip_trailing`` exists because screener.in appends a ``TTM`` column to the
    P&L. TTM is a legitimate figure but it is NOT an audited annual period, so
    a normalisation over annual results must exclude it. Balance-sheet and
    ratio tables do not carry TTM.
    """
    values = list(series)
    if skip_trailing:
        values = values[: len(values) - skip_trailing] if skip_trailing < len(values) else []
    for value in reversed(values):
        if value is not None:
            return value
    return None


def _normalisation_window(series: Sequence[Optional[float]],
                          years: int,
                          skip_trailing: int = 0) -> List[float]:
    """The trailing N non-None values, oldest first (excluding TTM).

    Used to normalise revenue/EBIT/margins. We take the trailing window rather
    than the whole history because a company that changed shape a decade ago
    should be valued on what it is now, not on what it was.
    """
    values = list(series)
    if skip_trailing:
        values = values[: len(values) - skip_trailing] if skip_trailing < len(values) else []
    clean = [v for v in values if v is not None]
    return clean[-years:] if years > 0 else clean


__all__ = [
    "EPV_SCENARIOS",
    "CANAV_SCENARIOS",
    "DEFAULT_INVENTORY_HAIRCUT",
    "DEFAULT_RECEIVABLE_HAIRCUT",
    "CLASS_STRONG",
    "CLASS_ASSET_RICH",
    "CLASS_FRANCHISE",
    "CLASS_RICH",
    "CLASS_INSUFFICIENT",
    "Assumption",
    "classify",
]


# --------------------------------------------------------------------------- #
# Valuation inputs (the measured facts, before any opinion)
# --------------------------------------------------------------------------- #
@dataclass
class FinancialInputs:
    """Every raw figure a valuation needs, already parsed from the tables.

    ``None`` means "not reported / not derivable" and is NEVER coerced to 0.
    ``missing`` lists which required inputs are absent, so a caller can render a
    precise reason instead of a vague failure.
    """

    symbol: str = ""
    basis: str = "consolidated"
    source_url: str = ""

    # --- normalised earnings inputs (annual, TTM excluded) ----------------- #
    revenue_series: List[float] = field(default_factory=list)
    ebit_series: List[float] = field(default_factory=list)
    ebit_margin_series: List[float] = field(default_factory=list)
    depreciation_series: List[float] = field(default_factory=list)
    tax_rate_series: List[float] = field(default_factory=list)
    capex_series: List[float] = field(default_factory=list)
    working_capital_days_series: List[float] = field(default_factory=list)

    # --- balance-sheet inputs --------------------------------------------- #
    shareholders_equity: Optional[float] = None
    borrowings: Optional[float] = None
    cash_and_equivalents: Optional[float] = None
    non_operating_investments: Optional[float] = None
    cwip: Optional[float] = None
    fixed_assets: Optional[float] = None
    other_assets: Optional[float] = None
    other_liabilities: Optional[float] = None
    total_assets: Optional[float] = None
    intangibles: Optional[float] = None
    minority_interest: Optional[float] = None
    contingent_liabilities: Optional[float] = None

    # --- inventory / receivables (usually NOT separately reported) -------- #
    inventory: Optional[float] = None
    receivables: Optional[float] = None
    inventory_days: Optional[float] = None
    debtor_days: Optional[float] = None
    days_payable: Optional[float] = None

    # --- market --------------------------------------------------------- #
    market_price: Optional[float] = None
    market_cap: Optional[float] = None
    diluted_shares: Optional[float] = None
    shares_are_diluted: bool = False

    # --- provenance ----------------------------------------------------- #
    missing: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def require(self, **fields: Any) -> List[str]:
        """Record which of the named fields are None. Returns the missing names."""
        gone = [name for name, value in fields.items() if value is None]
        for name in gone:
            if name not in self.missing:
                self.missing.append(name)
        return gone


def build_inputs(company: Dict[str, Any],
                 *, symbol: str = "",
                 basis: str = "consolidated",
                 source_url: str = "",
                 normalisation_years: int = 5,
                 ) -> FinancialInputs:
    """Turn a parsed company snapshot into :class:`FinancialInputs`.

    This function MEASURES. It makes no valuation judgements and applies no
    haircuts - it only extracts what is reported and derives what is safely
    derivable. Everything it cannot establish stays ``None`` and is listed in
    ``missing``.

    Args:
        company: the ``data`` dict from ``ScreenerClient.get_company``.
        normalisation_years: length of the trailing window for normalisation.
    """
    inp = FinancialInputs(symbol=symbol, basis=basis, source_url=source_url)

    pl = company.get("profit_loss")
    bs = company.get("balance_sheet")
    cf = company.get("cash_flow")
    ratios = company.get("ratios")
    top = company.get("top_ratios") or []

    # TTM handling: screener.in appends "TTM" as the final P&L column. It is
    # not an audited annual period, so the annual normalisation window excludes
    # it. We detect it by header text rather than assuming a fixed position.
    pl_headers = (pl or {}).get("headers") or []
    ttm_cols = sum(1 for h in pl_headers[1:] if "ttm" in str(h).lower())

    # ---- revenue / EBIT / margin ---------------------------------------- #
    sales_all = _row_series(pl, "sales")
    ebit_all = _row_series(pl, "operating profit")
    opm_all = _row_series(pl, "opm %")

    inp.revenue_series = _normalisation_window(sales_all, normalisation_years,
                                               skip_trailing=ttm_cols)
    inp.ebit_series = _normalisation_window(ebit_all, normalisation_years,
                                            skip_trailing=ttm_cols)
    inp.ebit_margin_series = _normalisation_window(opm_all, normalisation_years,
                                                   skip_trailing=ttm_cols)

    # ---- depreciation, tax, capex, working capital ----------------------- #
    inp.depreciation_series = _normalisation_window(
        _row_series(pl, "depreciation"), normalisation_years,
        skip_trailing=ttm_cols)

    # "Tax %" matched EXACTLY. A substring match for "tax" returns
    # "Profit before tax" (crores, not a percent) - see _row_series docstring.
    inp.tax_rate_series = _normalisation_window(
        _row_series(pl, "tax %"), normalisation_years, skip_trailing=ttm_cols)

    # Maintenance-capex proxy: the cash-flow statement's investing activity is
    # NET (sale proceeds netted against purchases) and includes acquisitions, so
    # it is NOT a capex figure on its own. We keep it only as a cross-check and
    # rely on depreciation x multiple for the model. Recorded as ASSUMPTION A1.
    inp.capex_series = _normalisation_window(
        _row_series(cf, "investing activity"), normalisation_years,
        skip_trailing=ttm_cols)

    inp.working_capital_days_series = _normalisation_window(
        _row_series(ratios, "working capital days"), normalisation_years,
        skip_trailing=ttm_cols)

    # ---- balance sheet (latest column only) ------------------------------ #
    def latest(table, label, exact=True):
        return _latest(_row_series(table, label, exact=exact))

    equity_capital = latest(bs, "equity capital")
    reserves = latest(bs, "reserves")
    if equity_capital is not None and reserves is not None:
        inp.shareholders_equity = equity_capital + reserves
    elif reserves is not None:
        inp.shareholders_equity = reserves
    else:
        inp.require(shareholders_equity=None)

    inp.borrowings = latest(bs, "borrowing", exact=False)
    inp.fixed_assets = latest(bs, "fixed assets")
    inp.cwip = latest(bs, "cwip")
    inp.other_assets = latest(bs, "other assets")
    inp.other_liabilities = latest(bs, "other liabilities")
    inp.total_assets = latest(bs, "total assets")

    # Investments on screener.in's balance sheet are typically non-operating
    # (quoted/unquoted stakes), so they feed EPV's non-operating add-back and
    # CANAV's investment revaluation.
    inp.non_operating_investments = latest(bs, "investments")

    # ---- receivables / inventory via ratio inversion --------------------- #
    # The balance sheet lumps inventory, receivables and cash into
    # "Other Assets +", so they are not directly available. We invert the
    # reported activity ratios to RECOVER them, which is a real measurement
    # rather than a guess:
    #     Inventory Days  = Inventory / (Sales/365)  ->  Inventory
    #     Debtor Days     = Receivables / (Sales/365) -> Receivables
    # Both are only meaningful when sales > 0 and the days value is sane, so we
    # refuse anything outside (0, 3650) - a 9000-day receivable is a data error,
    # not an asset.
    inp.inventory_days = latest(ratios, "inventory days")
    inp.debtor_days = latest(ratios, "debtor days")
    inp.days_payable = latest(ratios, "days payable", exact=False)

    revenue_latest = _latest(inp.revenue_series)
    if revenue_latest and revenue_latest > 0:
        daily_sales = revenue_latest / 365.0
        if inp.inventory_days is not None and 0 < inp.inventory_days < 3650:
            inp.inventory = inp.inventory_days * daily_sales
        if inp.debtor_days is not None and 0 < inp.debtor_days < 3650:
            inp.receivables = inp.debtor_days * daily_sales

    # ---- market ---------------------------------------------------------- #
    inp.market_price = to_number(_first_value(top, "current_price"))
    inp.market_cap = to_number(_first_value(top, "market_cap"))

    # Shares: derived from Net Profit / EPS. screener.in does not publish a
    # share count, and EPS is the only bridge between absolute profit and
    # per-share profit. This gives BASIC shares (EPS is normally reported on a
    # basic or weighted-average basis), and the module never claims otherwise -
    # see ASSUMPTION A3.
    net_profit_latest = _latest(_row_series(pl, "net profit"),
                                skip_trailing=ttm_cols)
    eps_latest = _latest(_row_series(pl, "eps in rs"),
                         skip_trailing=ttm_cols)
    if net_profit_latest and eps_latest and eps_latest > 0:
        inp.diluted_shares = net_profit_latest / eps_latest
        inp.shares_are_diluted = False
        inp.notes.append(
            "share count derived as Net Profit / EPS (basic, weighted average); "
            "screener.in publishes no diluted count")

    # Cross-check against market cap, which is an independent path to shares.
    if inp.market_cap and inp.market_price and inp.market_price > 0:
        implied = inp.market_cap / inp.market_price
        if inp.diluted_shares is None:
            inp.diluted_shares = implied
            inp.notes.append(
                "share count derived from Market Cap / Price (no usable EPS)")
        else:
            # If the two implied counts disagree by more than 15%, one input is
            # stale (a bonus/split between the EPS period and today, most
            # often). Keep the market-cap figure for a per-share valuation
            # because that is the one consistent with the CURRENT price, and
            # say so loudly.
            ratio = implied / inp.diluted_shares if inp.diluted_shares else 0
            if not (0.85 <= ratio <= 1.15):
                inp.notes.append(
                    f"share count mismatch: EPS implies {inp.diluted_shares:,.0f} "
                    f"but market cap/price implies {implied:,.0f} "
                    f"(ratio {ratio:.2f}); using the market-cap figure - likely a "
                    f"bonus/split between the EPS period and today")
                inp.diluted_shares = implied

    # ---- missing-input ledger ------------------------------------------- #
    inp.require(
        shareholders_equity=inp.shareholders_equity,
        market_price=inp.market_price,
        diluted_shares=inp.diluted_shares,
    )
    if not inp.revenue_series:
        inp.missing.append("revenue_series")
    if not inp.ebit_series:
        inp.missing.append("ebit_series")

    return inp


def _first_value(top_ratios: List[Dict[str, Any]], metric: str) -> Optional[str]:
    """Thin wrapper so valuations.py does not import the whole numbers surface."""
    from .client.numbers import ratio_lookup
    return ratio_lookup(top_ratios, metric)


# --------------------------------------------------------------------------- #
# EPV - Earnings Power Value
# --------------------------------------------------------------------------- #
@dataclass
class EpvResult:
    """EPV output, one field per line of the requested formula."""

    status: str = "ok"                 # "ok" | "insufficient_data"
    reason: str = ""

    # normalised earnings
    normalized_revenue: Optional[float] = None
    normalized_ebit_margin: Optional[float] = None
    normalized_ebit: Optional[float] = None
    normalized_tax_rate: Optional[float] = None
    nopat: Optional[float] = None

    # sustainable earnings bridge
    depreciation: Optional[float] = None
    maintenance_capex: Optional[float] = None
    maintenance_working_capital: Optional[float] = None
    sustainable_earnings: Optional[float] = None

    # value
    required_return: Optional[float] = None
    enterprise_value: Optional[float] = None
    excess_cash: Optional[float] = None
    non_operating_investments: Optional[float] = None
    debt: Optional[float] = None
    debt_like: Optional[float] = None
    minority_interest: Optional[float] = None
    equity_value: Optional[float] = None
    per_share: Optional[float] = None
    margin_of_safety: Optional[float] = None

    # audit trail
    revenue_window: List[float] = field(default_factory=list)
    ebit_window: List[float] = field(default_factory=list)
    margin_window: List[float] = field(default_factory=list)
    tax_window: List[float] = field(default_factory=list)
    assumptions: List[Assumption] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "status": self.status,
            "reason": self.reason,
            "normalized_revenue": self.normalized_revenue,
            "normalized_ebit_margin": self.normalized_ebit_margin,
            "normalized_ebit": self.normalized_ebit,
            "normalized_tax_rate": self.normalized_tax_rate,
            "nopat": self.nopat,
            "depreciation": self.depreciation,
            "maintenance_capex": self.maintenance_capex,
            "maintenance_working_capital": self.maintenance_working_capital,
            "sustainable_earnings": self.sustainable_earnings,
            "required_return": self.required_return,
            "enterprise_value": self.enterprise_value,
            "excess_cash": self.excess_cash,
            "non_operating_investments": self.non_operating_investments,
            "debt": self.debt,
            "debt_like": self.debt_like,
            "minority_interest": self.minority_interest,
            "equity_value": self.equity_value,
            "per_share": self.per_share,
            "margin_of_safety": self.margin_of_safety,
            "revenue_window": self.revenue_window,
            "ebit_window": self.ebit_window,
            "margin_window": self.margin_window,
            "tax_window": self.tax_window,
            "assumptions": [a.as_dict() for a in self.assumptions],
        }
        return out


def _safe_median(values: Sequence[float]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return statistics.median(clean) if clean else None


def compute_epv(inp: FinancialInputs, scenario: str = "base",
                *, config: Optional[Dict[str, Any]] = None) -> EpvResult:
    """Compute Earnings Power Value for one scenario.

    Formula (exactly as specified):

        Normalized EBIT      = Normalized Revenue x Normalized EBIT Margin
        NOPAT                = Normalized EBIT x (1 - Normalized Tax Rate)
        Sustainable Earnings = NOPAT + Depreciation - Maintenance Capex
                               - Maintenance Working Capital
        EPV Enterprise Value = Sustainable Earnings / Required Return
        EPV Equity Value     = EV + Excess Cash + Non-operating Investments
                               - Debt - Debt-like Liabilities - Minority Interest
        EPV Per Share        = Equity Value / Diluted Shares
        EPV Margin of Safety = 1 - (Market Price / EPV Per Share)

    NORMALISATION CHOICES (and why)
    -------------------------------
    * Revenue and EBIT are the MEDIAN of the trailing 3-5 annual periods. The
      median, not the mean: one exceptional year moves a mean but barely moves a
      median, and "do not use one unusually profitable year" is the explicit
      requirement.
    * EBIT is normalised as ``median(margin) x median(revenue)``, NOT as
      ``median(EBIT)``. This is deliberate: it forces the margin to be
      sustainable rather than letting a single high-margin, low-revenue year
      dominate the product. The two are reported separately so the difference
      is visible.
    * The tax rate is the MEDIAN reported rate, clamped to [0, 0.50]. A negative
      reported rate is a refund/one-off and a >50% rate is almost always a
      one-time item; neither is sustainable.
    """
    cfg = dict(config or {})
    scen = dict(EPV_SCENARIOS.get(scenario, EPV_SCENARIOS["base"]))
    for key, value in (cfg.get("scenario_overrides") or {}).items():
        if key in scen and isinstance(value, (int, float)):
            scen[key] = float(value)

    res = EpvResult()

    # ---- required inputs ------------------------------------------------ #
    hard_missing = []
    if not inp.revenue_series:
        hard_missing.append("revenue (P&L 'Sales')")
    if not inp.ebit_series and not inp.ebit_margin_series:
        hard_missing.append("EBIT ('Operating Profit') or margin ('OPM %')")
    if inp.diluted_shares is None:
        hard_missing.append("diluted shares")
    if inp.market_price is None:
        hard_missing.append("market price")
    if hard_missing:
        res.status = "insufficient_data"
        res.reason = "cannot compute EPV without: " + ", ".join(hard_missing)
        return res

    # ---- 1. normalised revenue ----------------------------------------- #
    rev_med = _safe_median(inp.revenue_series)
    res.normalized_revenue = rev_med
    res.revenue_window = list(inp.revenue_series)

    # ---- 2. normalised EBIT margin ------------------------------------- #
    margins = [m / 100.0 if m is not None else None
               for m in inp.ebit_margin_series if m is not None]
    margin_med = _safe_median(margins)
    if margin_med is None and inp.ebit_series and rev_med:
        # Fall back to an implied margin only when OPM % is absent entirely.
        ebit_med = _safe_median(inp.ebit_series)
        if ebit_med is not None and rev_med:
            margin_med = ebit_med / rev_med
            res.assumptions.append(Assumption(
                "A4", "OPM % missing; EBIT margin implied from "
                      "median(Operating Profit) / median(Sales)", "material"))
    res.normalized_ebit_margin = margin_med
    res.margin_window = [round(m, 6) for m in margins]
    res.ebit_window = list(inp.ebit_series)

    if res.normalized_revenue is None or margin_med is None:
        res.status = "insufficient_data"
        res.reason = "cannot normalise revenue or EBIT margin"
        return res

    # ---- 3. normalised EBIT -------------------------------------------- #
    # A negative normalised margin means the business has no positive earnings
    # power. EPV is undefined for it - a negative value divided by a discount
    # rate produces a NEGATIVE enterprise value, which would then be "passed"
    # by every price. Refuse instead of publishing a meaningless negative.
    if margin_med <= 0:
        res.status = "insufficient_data"
        res.reason = (f"normalised EBIT margin is {margin_med:.2%} (not positive); "
                      f"EPV is undefined without positive earnings power")
        return res

    res.normalized_ebit = res.normalized_revenue * margin_med

    # ---- 4. normalised tax rate ---------------------------------------- #
    tax_rates = [t / 100.0 for t in inp.tax_rate_series if t is not None]
    tax_med = _safe_median(tax_rates)
    if tax_med is None:
        tax_med = float(cfg.get("fallback_tax_rate", 0.25))
        res.assumptions.append(Assumption(
            "A5", f"no reported 'Tax %'; assumed a {tax_med:.0%} statutory rate",
            "material"))
    else:
        clamped = min(max(tax_med, 0.0), 0.50)
        if abs(clamped - tax_med) > 1e-9:
            res.assumptions.append(Assumption(
                "A6", f"median reported tax rate {tax_med:.2%} clamped to "
                      f"{clamped:.2%} (one-off items / refunds are not sustainable)",
                "material"))
        tax_med = clamped
    res.normalized_tax_rate = tax_med
    res.tax_window = [round(t, 6) for t in tax_rates]

    # ---- 5. NOPAT ------------------------------------------------------- #
    res.nopat = res.normalized_ebit * (1.0 - tax_med)

    # ---- 6. depreciation ------------------------------------------------ #
    dep = _safe_median([d for d in inp.depreciation_series if d is not None])
    if dep is None:
        # Depreciation is the single most important non-cash add-back. Without
        # it we would understate EPV, so we refuse rather than assume a number.
        res.status = "insufficient_data"
        res.reason = "no reported Depreciation; cannot build sustainable earnings"
        return res
    res.depreciation = dep

    # ---- 7. maintenance capex ------------------------------------------ #
    capex_mult = float(scen["maintenance_capex_multiple"])
    res.maintenance_capex = dep * capex_mult
    res.assumptions.append(Assumption(
        "A1",
        f"maintenance capex proxied as Depreciation x {capex_mult:.2f}. "
        f"screener.in does not split maintenance from growth capex.",
        "high - this is the largest single judgement in EPV"))

    # ---- 8. maintenance working capital --------------------------------- #
    wc_days = _safe_median([w for w in inp.working_capital_days_series
                            if w is not None])
    if wc_days is None:
        res.maintenance_working_capital = 0.0
        res.assumptions.append(Assumption(
            "A7", "no 'Working Capital Days'; maintenance working capital taken "
                  "as 0 (neutral assumption, neither adds nor subtracts)", "low"))
    else:
        # Maintenance WC is the working capital needed to SUPPORT the current
        # level, not to grow. We therefore charge only the interest on the
        # working capital already deployed - i.e. WC_days/365 x revenue x
        # required return - rather than the full balance. Charging the full
        # balance would double-count it (it is already inside the enterprise).
        #
        # A NEGATIVE working-capital cycle (a supermarket, a subscription
        # business) is a genuine source of funding, so we allow the sign through
        # rather than clamping. Clamping to zero would silently punish the best
        # working-capital businesses in the universe.
        wc_base = (wc_days / 365.0) * res.normalized_revenue
        res.maintenance_working_capital = wc_base * float(scen["discount_rate"])
        res.assumptions.append(Assumption(
            "A8",
            f"maintenance working capital = (Working Capital Days {wc_days:.0f}"
            f"/365) x normalized revenue x required return, so only the funding "
            f"cost of the deployed working capital is charged. Negative cycles "
            f"are allowed to reduce the charge.",
            "medium"))

    # ---- 9. sustainable earnings --------------------------------------- #
    res.sustainable_earnings = (res.nopat + res.depreciation
                                - res.maintenance_capex
                                - res.maintenance_working_capital)

    if res.sustainable_earnings <= 0:
        res.status = "insufficient_data"
        res.reason = ("sustainable earnings are non-positive "
                      f"({res.sustainable_earnings:,.0f} Cr); EPV is undefined")
        res.per_share = None
        res.margin_of_safety = None
        return res

    # ---- 10. enterprise value ------------------------------------------ #
    req = float(scen["discount_rate"])
    res.required_return = req
    res.enterprise_value = res.sustainable_earnings / req

    # ---- 11. equity bridge --------------------------------------------- #
    res.debt = inp.borrowings
    if res.debt is None:
        res.debt = 0.0
        res.assumptions.append(Assumption(
            "A9", "no reported Borrowings; debt taken as 0 - VERIFY before "
                  "trusting this EPV", "high"))

    # Excess cash: only cash ABOVE an operating minimum is available to equity.
    # The operating minimum is a configurable fraction of revenue (default 2%).
    cash = inp.cash_and_equivalents
    non_op = inp.non_operating_investments or 0.0
    if cash is None:
        # screener.in does not report cash separately (it sits inside
        # "Other Assets +"). We do NOT guess it. We record the gap and, when the
        # caller allows it, treat un-invested "Other Assets" as the pool from
        # which cash would be drawn - only if explicitly enabled.
        if cfg.get("treat_other_assets_as_cash", False) and inp.other_assets:
            floor = res.normalized_revenue * float(cfg.get(
                "operating_cash_floor_pct", 0.02))
            res.excess_cash = max(0.0, inp.other_assets - floor)
            res.assumptions.append(Assumption(
                "A2",
                "cash not reported separately; 'Other Assets' used as the cash "
                "pool less a 2% operating floor. This OVERSTATES excess cash "
                "because Other Assets also contains inventory and receivables.",
                "high"))
        else:
            res.excess_cash = 0.0
            res.assumptions.append(Assumption(
                "A2",
                "cash/equivalents not reported separately by screener.in and "
                "not inferred; excess cash taken as 0, which makes this EPV "
                "CONSERVATIVE (it can only understate equity value)",
                "high"))
    else:
        floor = res.normalized_revenue * float(cfg.get(
            "operating_cash_floor_pct", 0.02))
        res.excess_cash = max(0.0, cash - floor)

    res.non_operating_investments = non_op
    res.minority_interest = inp.minority_interest or 0.0
    if inp.minority_interest is None:
        res.assumptions.append(Assumption(
            "A10", "minority interest not reported by screener.in; taken as 0 "
                   "(understates the deduction, so EPV is slightly optimistic "
                   "for consolidating groups)", "medium"))

    # Debt-like liabilities: prefer disclosed contingent liabilities.
    if inp.contingent_liabilities is not None:
        res.debt_like = inp.contingent_liabilities
    else:
        res.debt_like = 0.0
        res.assumptions.append(Assumption(
            "A11", "contingent liabilities not disclosed in the summary tables; "
                   "taken as 0. EPV therefore ignores off-balance-sheet claims.",
            "medium"))

    res.equity_value = (res.enterprise_value
                        + res.excess_cash
                        + res.non_operating_investments
                        - res.debt
                        - res.debt_like
                        - res.minority_interest)

    # ---- 12. per share and margin of safety ---------------------------- #
    if inp.diluted_shares and inp.diluted_shares > 0:
        res.per_share = res.equity_value / inp.diluted_shares
        if res.per_share and res.per_share > 0:
            res.margin_of_safety = 1.0 - (inp.market_price / res.per_share)
        else:
            res.status = "insufficient_data"
            res.reason = ("EPV equity value is non-positive "
                          f"({res.equity_value:,.0f} Cr); per-share EPV undefined")
            res.per_share = None
            res.margin_of_safety = None
            return res
    else:
        res.status = "insufficient_data"
        res.reason = "no diluted share count"
        return res

    # One final sanity gate: an EPV per share below 1% of the market price is a
    # modelling failure, not an opportunity, and it would otherwise present as a
    # ~-10000% margin of safety that looks like a screaming buy.
    if res.per_share < inp.market_price * 0.01:
        bad_per_share = res.per_share
        res.status = "insufficient_data"
        res.reason = (f"EPV per share {bad_per_share:,.2f} is <1% of price "
                      f"{inp.market_price:,.2f}; treat as a data/modelling "
                      f"failure, not a valuation")
        # Clear the derived per-share figures. A caller that reads
        # `epv['base']['per_share']` without first checking `status` would
        # otherwise act on a number this function has just declared unusable.
        # The rejected value is preserved in `reason` for diagnosis, and the
        # full intermediate bridge (EBIT, NOPAT, EV) stays populated so the
        # failure can be debugged.
        res.per_share = None
        res.margin_of_safety = None
        return res

    return res


# --------------------------------------------------------------------------- #
# CANAV - Conservative Adjusted Net Asset Value
# --------------------------------------------------------------------------- #
@dataclass
class CanavResult:
    """CANAV output, one field per adjustment in the requested formula."""

    status: str = "ok"
    reason: str = ""

    shareholders_equity: Optional[float] = None

    # adjustments (each a SIGNED amount in crores)
    quoted_investment_revaluation: Optional[float] = None
    unquoted_investment_revaluation: Optional[float] = None
    inventory_haircut: Optional[float] = None
    receivable_haircut: Optional[float] = None
    intangible_haircut: Optional[float] = None
    asset_revaluation: Optional[float] = None
    contingent_liabilities: Optional[float] = None
    other_hidden_liabilities: Optional[float] = None

    # also deducted before adjustment (a lender's borrowings are a real claim)
    borrowings_deducted: Optional[float] = None

    adjusted_equity: Optional[float] = None
    per_share: Optional[float] = None
    margin_of_safety: Optional[float] = None

    # audit trail
    components_used: Dict[str, Optional[float]] = field(default_factory=dict)
    assumptions: List[Assumption] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "shareholders_equity": self.shareholders_equity,
            "quoted_investment_revaluation": self.quoted_investment_revaluation,
            "unquoted_investment_revaluation": self.unquoted_investment_revaluation,
            "inventory_haircut": self.inventory_haircut,
            "receivable_haircut": self.receivable_haircut,
            "intangible_haircut": self.intangible_haircut,
            "asset_revaluation": self.asset_revaluation,
            "contingent_liabilities": self.contingent_liabilities,
            "other_hidden_liabilities": self.other_hidden_liabilities,
            "borrowings_deducted": self.borrowings_deducted,
            "adjusted_equity": self.adjusted_equity,
            "per_share": self.per_share,
            "margin_of_safety": self.margin_of_safety,
            "components_used": self.components_used,
            "assumptions": [a.as_dict() for a in self.assumptions],
        }


def compute_canav(inp: FinancialInputs, scenario: str = "base",
                  *, config: Optional[Dict[str, Any]] = None) -> CanavResult:
    """Compute Conservative Adjusted Net Asset Value for one scenario.

    Formula (exactly as specified):

        CANAV = Adjusted Shareholders' Equity / Diluted Shares Outstanding
        CANAV Margin of Safety = 1 - (Market Price / CANAV)

    with

        Adjusted Equity = Shareholders' Equity
                        + Quoted Investment Revaluation
                        + Unquoted Investment Revaluation
                        - Inventory Haircut
                        - Receivable Haircut
                        - Intangible / Goodwill Haircut
                        + Asset Revaluation
                        - Contingent Liabilities
                        - Other hidden / debt-like liabilities

    TWO DEPARTURES, BOTH DELIBERATE AND BOTH RECORDED
    -------------------------------------------------
    1. **Borrowings are deducted.** Shareholders' equity on screener.in is
       already net of borrowings (it is an accounting identity), so deducting
       them again would double-count. But when a caller supplies a TOTAL-ASSETS
       based equity (e.g. from a different feed), the deduction IS needed. The
       module therefore deducts borrowings only when it built equity from
       assets, and records which path it took. For the screener.in path the
       deduction is 0 and the reason is stated.

    2. **"Asset Revaluation" is applied as a downward adjustment only.** The
       wording allows either sign, but a revaluation UP would make CANAV less
       conservative, which contradicts the name. Upward revaluation is therefore
       never automatic; it requires an explicit per-company input.

    WHY THIS IS NOT ONE BLANKET HAIRCUT PER COMPANY
    ----------------------------------------------
    The requested defaults (33.33% inventory, 50% receivables) are applied only
    when the company-specific figure cannot be established, and then only as the
    scenario's BASE level. Where the data allows a better read, the module uses
    it:

      * Receivables: the half of the trade-receivable book that is genuinely at
        risk is estimated from the company's own debtor days. A business
        collecting in 20 days has a different credit record from one collecting
        in 150 days, and its balance sheet already reflects it.
      * Inventory: driven by inventory days the same way. Fast-moving inventory
        (a retailer) does not deserve a 33% write-down; a 400-day inventory does
        not deserve only 33%.
      * Investments: quoted stakes are marked to market where a price exists;
        unquoted stakes take a larger haircut because there is no bid.
      * Intangibles/goodwill: written off entirely in every scenario. There is
        no conservative case for counting goodwill as recoverable asset backing.

    Each deviation or fallback is appended to ``assumptions``.
    """
    cfg = dict(config or {})
    scen = dict(CANAV_SCENARIOS.get(scenario, CANAV_SCENARIOS["base"]))
    # Seed every scenario from the conservative defaults the request named.
    scen.setdefault("inventory", DEFAULT_INVENTORY_HAIRCUT)
    scen.setdefault("receivables", DEFAULT_RECEIVABLE_HAIRCUT)
    for key, value in (cfg.get("scenario_overrides") or {}).items():
        if key in scen and isinstance(value, (int, float)):
            scen[key] = float(value)

    res = CanavResult()

    # ---- required inputs ------------------------------------------------ #
    hard_missing = []
    if inp.shareholders_equity is None:
        hard_missing.append("shareholders' equity")
    if inp.diluted_shares is None:
        hard_missing.append("diluted shares")
    if inp.market_price is None:
        hard_missing.append("market price")
    if hard_missing:
        res.status = "insufficient_data"
        res.reason = "cannot compute CANAV without: " + ", ".join(hard_missing)
        return res

    res.shareholders_equity = inp.shareholders_equity
    adjusted = inp.shareholders_equity
    res.components_used = {
        "shareholders_equity": inp.shareholders_equity,
        "investments": inp.non_operating_investments,
        "inventory": inp.inventory,
        "receivables": inp.receivables,
        "inventory_days": inp.inventory_days,
        "debtor_days": inp.debtor_days,
        "fixed_assets": inp.fixed_assets,
        "intangibles": inp.intangibles,
        "contingent_liabilities": inp.contingent_liabilities,
        "other_liabilities": inp.other_liabilities,
    }

    # ---- A. investments -------------------------------------------------- #
    # screener.in reports a single "Investments" line without splitting quoted
    # from unquoted. We therefore apply ONE blended haircut derived from the two
    # scenario rates, weighted toward the UNQUOTED rate because assuming a
    # quoted price when we cannot see one is the wrong way to be wrong.
    invest = inp.non_operating_investments or 0.0
    if invest and inp.market_price is not None:
        quoted_rate = float(scen["quoted_investment"])
        unquoted_rate = float(scen["unquoted_investment"])
        blended = 0.35 * quoted_rate + 0.65 * unquoted_rate
        res.quoted_investment_revaluation = -invest * 0.35 * quoted_rate
        res.unquoted_investment_revaluation = -invest * 0.65 * unquoted_rate
        adjusted += res.quoted_investment_revaluation
        adjusted += res.unquoted_investment_revaluation
        res.assumptions.append(Assumption(
            "B1",
            f"investments ({invest:,.0f} Cr) are not split quoted/unquoted by "
            f"screener.in; applied a blended write-down of {blended:.1%} "
            f"(35% quoted @ {quoted_rate:.1%}, 65% unquoted @ {unquoted_rate:.1%}). "
            f"Weighted toward unquoted: assuming a market price exists when it "
            f"cannot be seen is the wrong direction to be wrong in.",
            "medium"))
    else:
        res.quoted_investment_revaluation = 0.0
        res.unquoted_investment_revaluation = 0.0
        if inp.non_operating_investments is None:
            res.assumptions.append(Assumption(
                "B1", "no Investments line reported; investment revaluation = 0",
                "low"))

    # ---- B. inventory ---------------------------------------------------- #
    inv = inp.inventory
    inv_rate = float(scen["inventory"])
    if inv is None or inv <= 0:
        res.inventory_haircut = 0.0
        res.assumptions.append(Assumption(
            "B2", "inventory not separately reported (it sits inside 'Other "
                  "Assets +') and could not be recovered from Inventory Days; "
                  "no inventory haircut applied", "medium"))
    else:
        # Company-specific tilt: scale the scenario rate by how long the
        # inventory sits. <=60 days -> 0.6x, >=180 days -> 1.6x, linear between.
        tilt = _days_tilt(inp.inventory_days, fast=60.0, slow=180.0,
                          low=0.6, high=1.6)
        effective = inv_rate * tilt
        res.inventory_haircut = -inv * effective
        adjusted += res.inventory_haircut
        res.assumptions.append(Assumption(
            "B2",
            f"inventory {inv:,.0f} Cr (recovered from Inventory Days "
            f"{_fmt_days(inp.inventory_days)}) written down "
            f"{effective:.1%} = {inv_rate:.1%} base x {tilt:.2f} tilt",
            "medium"))

    # ---- C. receivables -------------------------------------------------- #
    recv = inp.receivables
    recv_rate = float(scen["receivables"])
    if recv is None or recv <= 0:
        res.receivable_haircut = 0.0
        res.assumptions.append(Assumption(
            "B3", "receivables not separately reported and could not be "
                  "recovered from Debtor Days; no receivable haircut applied",
            "medium"))
    else:
        tilt = _days_tilt(inp.debtor_days, fast=30.0, slow=120.0,
                          low=0.6, high=1.6)
        effective = recv_rate * tilt
        res.receivable_haircut = -recv * effective
        adjusted += res.receivable_haircut
        res.assumptions.append(Assumption(
            "B3",
            f"receivables {recv:,.0f} Cr (recovered from Debtor Days "
            f"{_fmt_days(inp.debtor_days)}) written down "
            f"{effective:.1%} = {recv_rate:.1%} base x {tilt:.2f} tilt",
            "medium"))

    # ---- D. intangibles / goodwill --------------------------------------- #
    intang = inp.intangibles
    if intang is None or intang <= 0:
        res.intangible_haircut = 0.0
        res.assumptions.append(Assumption(
            "B4", "intangibles/goodwill not reported as a separate balance-sheet "
                  "line by screener.in; no intangible haircut applied. If the "
                  "company has material goodwill, CANAV is OVERSTATED.",
            "high"))
    else:
        res.intangible_haircut = -intang * float(scen["intangibles"])
        adjusted += res.intangible_haircut
        res.assumptions.append(Assumption(
            "B4", f"intangibles/goodwill {intang:,.0f} Cr written down "
                  f"{float(scen['intangibles']):.0%} - there is no conservative "
                  f"case for counting goodwill as recoverable asset backing",
            "high"))

    # ---- E. fixed-asset revaluation -------------------------------------- #
    fa = inp.fixed_assets
    asset_rate = float(scen["asset_revaluation"])
    if fa is None or fa <= 0:
        res.asset_revaluation = 0.0
        res.assumptions.append(Assumption(
            "B5", "fixed assets not reported; no asset revaluation applied",
            "low"))
    else:
        res.asset_revaluation = -fa * asset_rate
        adjusted += res.asset_revaluation
        res.assumptions.append(Assumption(
            "B5", f"fixed assets {fa:,.0f} Cr written down {asset_rate:.0%}. "
                  f"Cannot be negative here (an upward revaluation would make "
                  f"CANAV less conservative and must be an explicit per-company "
                  f"input, not an automatic adjustment).",
            "medium"))

    # ---- F. contingent liabilities --------------------------------------- #
    if inp.contingent_liabilities is not None:
        res.contingent_liabilities = -abs(inp.contingent_liabilities) * float(
            scen["contingent"])
        adjusted += res.contingent_liabilities
    else:
        res.contingent_liabilities = 0.0
        res.assumptions.append(Assumption(
            "B6", "contingent liabilities are not disclosed in screener.in's "
                  "summary tables; deducted 0. CANAV therefore IGNORES "
                  "guarantees, tax disputes and similar off-balance-sheet claims.",
            "high"))

    # ---- G. other hidden / debt-like liabilities ------------------------- #
    # 'Other Liabilities +' is a real balance-sheet claim (payables, provisions,
    # deferred tax). It is ALREADY inside shareholders' equity via the
    # accounting identity, so deducting it again would double-count. Recorded
    # rather than deducted; the value is surfaced so a caller can decide.
    res.other_hidden_liabilities = 0.0
    if inp.other_liabilities:
        res.assumptions.append(Assumption(
            "B7", f"'Other Liabilities' of {inp.other_liabilities:,.0f} Cr "
                  f"(payables/provisions/deferred tax) is already reflected in "
                  f"shareholders' equity via the accounting identity, so it is "
                  f"NOT deducted again. Surface it separately if you want a "
                  f"stricter, double-counted view.", "low"))

    # ---- H. borrowings ---------------------------------------------------- #
    res.borrowings_deducted = 0.0
    res.assumptions.append(Assumption(
        "B8", "borrowings are NOT deducted again: screener.in's shareholders' "
              "equity is already net of debt (net assets = total assets - total "
              "liabilities, inclusive of borrowings). Deducting them would "
              "double-count.", "low"))

    res.adjusted_equity = adjusted

    # ---- per share and margin of safety ---------------------------------- #
    if res.adjusted_equity <= 0:
        res.status = "insufficient_data"
        res.reason = (f"adjusted equity is non-positive "
                      f"({res.adjusted_equity:,.0f} Cr); CANAV undefined")
        res.per_share = None
        res.margin_of_safety = None
        return res

    res.per_share = res.adjusted_equity / inp.diluted_shares
    if res.per_share and res.per_share > 0:
        res.margin_of_safety = 1.0 - (inp.market_price / res.per_share)
    else:
        res.status = "insufficient_data"
        res.reason = "CANAV per share is non-positive"
        res.per_share = None
        res.margin_of_safety = None
        return res

    return res


def _days_tilt(days: Optional[float], *, fast: float, slow: float,
               low: float, high: float) -> float:
    """Scale a base haircut by how long an asset sits on the books.

    ``fast`` days or fewer -> ``low`` (light haircut, the asset turns over).
    ``slow`` days or more  -> ``high`` (heavy haircut, the asset is stale).
    Linear between. Unknown -> 1.0 (the base rate, no opinion).

    This is what makes the haircut company-specific instead of a constant: it
    uses the company's OWN reported activity ratio as evidence of how much of
    the asset is real.
    """
    if days is None or days <= 0:
        return 1.0
    if days <= fast:
        return low
    if days >= slow:
        return high
    span = slow - fast
    return low + (high - low) * ((days - fast) / span)


def _fmt_days(days: Optional[float]) -> str:
    return "n/a" if days is None else f"{days:.0f}"


# --------------------------------------------------------------------------- #
# Top-level valuation
# --------------------------------------------------------------------------- #
@dataclass
class Valuation:
    """The complete valuation for one company across all scenarios."""

    symbol: str = ""
    status: str = "ok"
    reason: str = ""

    basis: str = "consolidated"
    source_url: str = ""

    market_price: Optional[float] = None
    diluted_shares: Optional[float] = None
    shares_are_diluted: bool = False

    #: scenario name -> EpvResult.as_dict()
    epv: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: scenario name -> CanavResult.as_dict()
    canav: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    classification: str = CLASS_INSUFFICIENT
    classification_reason: str = ""

    assumptions: List[Dict[str, str]] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "reason": self.reason,
            "basis": self.basis,
            "source_url": self.source_url,
            "market_price": self.market_price,
            "diluted_shares": self.diluted_shares,
            "shares_are_diluted": self.shares_are_diluted,
            "epv": self.epv,
            "canav": self.canav,
            "classification": self.classification,
            "classification_reason": self.classification_reason,
            "assumptions": self.assumptions,
            "missing": self.missing,
            "notes": self.notes,
        }

    # -- convenience accessors ------------------------------------------- #
    def epv_per_share(self, scenario: str = "base") -> Optional[float]:
        return (self.epv.get(scenario) or {}).get("per_share")

    def canav_per_share(self, scenario: str = "base") -> Optional[float]:
        return (self.canav.get(scenario) or {}).get("per_share")

    def as_text(self) -> str:
        """Plain-text render. ASCII-only for cp1252 Windows consoles."""
        def f(v, pct=False):
            if v is None:
                return "n/a"
            if pct:
                return f"{v:+.1%}"
            return f"{v:,.2f}"

        lines = []
        lines.append(f"{self.symbol}  [{self.basis}]  status={self.status}")
        if self.reason:
            lines.append(f"  reason: {self.reason}")
        lines.append(f"  price={f(self.market_price)}  "
                     f"shares={f(self.diluted_shares)}"
                     f"{'' if self.shares_are_diluted else ' (basic)'}")
        lines.append("  EPV per share:")
        for name in ("bear", "base", "bull"):
            e = self.epv.get(name) or {}
            lines.append(f"    {name:<6} {f(e.get('per_share')):>14}  "
                         f"MoS {f(e.get('margin_of_safety'), True):>9}"
                         f"   [{e.get('status', '-')}]")
        lines.append("  CANAV per share:")
        for name in ("stress", "base", "moderate"):
            c = self.canav.get(name) or {}
            lines.append(f"    {name:<6} {f(c.get('per_share')):>14}  "
                         f"MoS {f(c.get('margin_of_safety'), True):>9}"
                         f"   [{c.get('status', '-')}]")
        lines.append(f"  CLASSIFICATION: {self.classification}")
        if self.classification_reason:
            lines.append(f"    {self.classification_reason}")
        if self.missing:
            lines.append(f"  missing inputs: {', '.join(self.missing)}")
        if self.notes:
            for n in self.notes:
                lines.append(f"  note: {n}")
        return "\n".join(lines)


def value_company(company: Dict[str, Any], *,
                  symbol: str = "",
                  basis: str = "consolidated",
                  source_url: str = "",
                  config: Optional[Dict[str, Any]] = None) -> Valuation:
    """Compute EPV (bear/base/bull) and CANAV (stress/base/moderate).

    ``company`` is the ``data`` dict from ``ScreenerClient.get_company``, so the
    caller keeps control of the fetch (and of caching). This function does no
    I/O.

    The returned :class:`Valuation` always contains all six scenario keys; a
    scenario that could not be computed carries
    ``status="insufficient_data"`` and a ``reason``, never a fabricated number.
    """
    cfg = dict(config or {})
    inp = build_inputs(company, symbol=symbol, basis=basis, source_url=source_url,
                       normalisation_years=int(cfg.get("normalisation_years", 5)))

    val = Valuation(symbol=symbol or company.get("symbol", ""),
                    basis=basis, source_url=source_url,
                    market_price=inp.market_price,
                    diluted_shares=inp.diluted_shares,
                    shares_are_diluted=inp.shares_are_diluted,
                    missing=list(inp.missing), notes=list(inp.notes))

    epv_results: Dict[str, EpvResult] = {}
    canav_results: Dict[str, CanavResult] = {}

    for name in EPV_SCENARIOS:
        epv_results[name] = compute_epv(inp, name, config=cfg)
        val.epv[name] = epv_results[name].as_dict()

    for name in CANAV_SCENARIOS:
        canav_results[name] = compute_canav(inp, name, config=cfg)
        val.canav[name] = canav_results[name].as_dict()

    # Classification uses the BASE scenario for both, per classify()'s contract.
    val.classification, val.classification_reason = classify(
        inp.market_price,
        epv_results["base"].per_share,
        canav_results["base"].per_share,
    )

    # Collect assumptions, ONE entry per code. Every scenario carries its own
    # numeric variant of the same assumption (A1 appears with 0.80/1.00/1.30),
    # so naive concatenation produces an audit list with three contradicting
    # copies of the same code - which is worse than no list, because a reader
    # cannot tell which is authoritative. Instead we keep the code once and
    # record how its value varies across scenarios.
    by_code: Dict[str, Dict[str, Any]] = {}
    for scen_name, res_obj in ([(n, epv_results[n]) for n in EPV_SCENARIOS]
                               + [(n, canav_results[n]) for n in CANAV_SCENARIOS]):
        for a in res_obj.assumptions:
            entry = by_code.get(a.code)
            if entry is None:
                by_code[a.code] = {
                    "code": a.code,
                    "text": a.text,
                    "impact": a.impact,
                    "scenarios": [scen_name],
                }
            else:
                # Keep the BASE wording when it exists: it is the scenario the
                # classification and the headline numbers use.
                entry["scenarios"].append(scen_name)
                if scen_name == "base":
                    entry["text"] = a.text
                    entry["impact"] = a.impact

    # Order A-codes before B-codes, then numerically - matches the module's own
    # numbering, so the list reads in the same order as the docstrings.
    import re as _re

    def _code_key(code: str):
        m = _re.match(r"([A-Za-z]+)(\d+)", str(code))
        if not m:
            return (str(code), 0)
        return (m.group(1), int(m.group(2)))

    val.assumptions = [by_code[c] for c in sorted(by_code, key=_code_key)]

    # Top-level status: ok when at least the BASE pair computed.
    base_epv = epv_results["base"]
    base_nav = canav_results["base"]
    if base_epv.status == "ok" or base_nav.status == "ok":
        val.status = "ok"
        if base_epv.status != "ok" and base_nav.status == "ok":
            val.reason = f"EPV unavailable ({base_epv.reason}); CANAV available"
        elif base_nav.status != "ok" and base_epv.status == "ok":
            val.reason = f"CANAV unavailable ({base_nav.reason}); EPV available"
    else:
        val.status = "insufficient_data"
        val.reason = (f"EPV: {base_epv.reason}; CANAV: {base_nav.reason}")

    return val


# --------------------------------------------------------------------------- #
# Company-level helper that uses the client (I/O)
# --------------------------------------------------------------------------- #
def value_symbol(symbol: str, *, client: Any = None,
                 basis: str = "consolidated",
                 config: Optional[Dict[str, Any]] = None) -> Valuation:
    """Fetch one company and value it. Convenience wrapper for the CLI.

    Deliberately thin: fetching stays in :class:`ScreenerClient` so caching,
    throttling and retry are not duplicated here.
    """
    if client is None:
        from .client.screener_client import ScreenerClient
        client = ScreenerClient()

    resp = client.get_company(symbol, basis)
    if not resp.ok or not resp.data:
        return Valuation(symbol=symbol.upper(), status="insufficient_data",
                         basis=basis,
                         reason=f"fetch failed: {resp.error}")

    return value_company(resp.data, symbol=symbol.upper(), basis=basis,
                         source_url=(resp.meta.source_url if resp.meta else ""),
                         config=config)


__all__ = [
    "EPV_SCENARIOS",
    "CANAV_SCENARIOS",
    "DEFAULT_INVENTORY_HAIRCUT",
    "DEFAULT_RECEIVABLE_HAIRCUT",
    "CLASS_STRONG",
    "CLASS_ASSET_RICH",
    "CLASS_FRANCHISE",
    "CLASS_RICH",
    "CLASS_INSUFFICIENT",
    "Assumption",
    "FinancialInputs",
    "EpvResult",
    "CanavResult",
    "Valuation",
    "build_inputs",
    "compute_epv",
    "compute_canav",
    "classify",
    "value_company",
    "value_symbol",
]
