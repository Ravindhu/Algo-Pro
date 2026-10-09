# blue_engine/screener/ — the Screener engine

Fundamental universe selection for the Indian market, built on a Python port of
the [screener-india](https://github.com/sahiljani/screener-india) npm package.

```
Screener engine  ->  WHICH names exist and pass the fundamental gates
Alpha engine     ->  WHICH of those to buy/sell, and at what level
POV engine       ->  HOW to slice the resulting order
```

---

## The requested screen

```
1. market cap  >  200 Cr
2. debt/equity <  1
```

Declared in `blue_engine/input/screener_engine.json`. The code defaults in
`filters.default_rules()` are identical, and a test asserts the two never drift.

Both are **strict** inequalities: a company at exactly ₹200 Cr or exactly
D/E 1.0 is excluded.

### Sector gate (on by default)

`bank`, `finance`, `insurance`, `nbfc` are excluded **before** the debt rule.

This is not a nicety. For a lender, borrowings *are* the inventory — a bank with
a low debt/equity is not a safe bank, it is a bank that is not lending. Applying
"D/E < 1" to a financial company selects the opposite of what the screen means
to select. The gate is on by default in both the shipped config and the code,
because a default that has to be remembered is a default that will be forgotten.

Override with `"exclude_sectors": []` (an explicit empty list is honoured — the
difference between "no gate" and "I did not think about it" matters).

---

## Layout

```
screener/
  vendor/screener-india/           PRISTINE upstream clone (never edited)
  vendor/SCREENER_INDIA_UPSTREAM_PIN   the pinned commit
  _vendor_pin.py                   pin check / upstream upgrade helper
  client/base.py                   transport: TTL cache, throttle, retry, envelope
  client/parser.py                 HTML -> Python (port of src/utils/parser.ts)
  client/numbers.py                display strings -> comparable floats
  client/screener_client.py        ScreenerClient (port of src/client/*.ts)
  client/extractors.py             label-aware helpers for valuation inputs
  filters.py                       Rule / Screener / the two requested rules
  valuations.py                    EPV + Conservative Adjusted NAV
  yf_fallback.py                   optional yfinance gap-fill
  screener_engine.py               ScreenerEngine(BaseEngine) + CLI
```

---

## Vendoring model (same as OpenAlgo)

`vendor/screener-india/` is a **pristine upstream clone**, exactly like
`my-trading-system/openalgo/`:

* Never hand-edited. Every change we make lives in `client/`, `filters.py` or
  `screener_engine.py`.
* Pinned by `vendor/SCREENER_INDIA_UPSTREAM_PIN` to the commit the port was
  written against.
* Checked with `_vendor_pin.py`:

```powershell
python Blue_engine\screener\_vendor_pin.py --check       # verify no drift
python Blue_engine\screener\_vendor_pin.py --diff        # show commit + watched files
python Blue_engine\screener\_vendor_pin.py --update-pin  # pull --ff-only, re-pin
```

An upstream upgrade can therefore never conflict with a local edit, and the
vendored TypeScript is **provenance + a diff target, not a dependency** — Node.js
is not installed, not imported, and not required.

### Why a port instead of a Node subprocess

1. No Node.js runtime next to the execution path. A subprocess is a new failure
   mode in the trading loop; a module that returns a dict is not.
2. The port is unit-testable with the same runner as the engine.
3. Fidelity is checkable: `parser.py` mirrors `parser.ts` selector-for-selector,
   so an upstream markup fix ports straight across.

---

## Quick start

```powershell
# Offline (no network): feed candidates straight in and screen them
python -c "from blue_engine.screener import ScreenerEngine; \
  e=ScreenerEngine(); print(e.screen_now())"
```

```powershell
# Live: screen an explicit symbol list
python -m blue_engine.screener.screener_engine --symbols RELIANCE,TCS,INFY

# Live: sweep a sector, save the result for the dashboard
python -m blue_engine.screener.screener_engine --source sector --sectors IN02 --save

# Raw JSON (for piping into another tool)
python -m blue_engine.screener.screener_engine --symbols ITC --json
```

Dashboard: **Screener** page (sidebar, `/screener`). Read-only — it renders
`screener/last_screen.json` if the CLI wrote one.

### Verified live output

Real run, 2026-10-09, `--symbols RELIANCE,TCS,INFY,ITC,HDFCBANK`:

```
candidates  : 5
selected    : 4
rejected    : 1
missing data: 0
SELECTED:
  INFY      mcap=4,12,219 Cr.  D/E=0.0988  PE=13.2
  ITC       mcap=3,33,061 Cr.  D/E=0.0331  PE=16.8
  RELIANCE  mcap=15,87,028 Cr. D/E=0.4457  PE=21.2
  TCS       mcap=7,80,060 Cr.  D/E=0.0992  PE=14.3
REJECTED:
  HDFCBANK  Debt/Equity < 1: 1.00414 does not satisfy < 1
```

HDFCBANK is rejected on the number, not on missing data — it is genuinely
levered at 1.004 (`588,485 / (1,539 + 584,520)`), which is the correct outcome.

---

## The design decision that matters: three-valued rules

Every rule returns **PASS / FAIL / UNKNOWN**, never a bare boolean.

| outcome | meaning | default policy |
|---|---|---|
| `pass` | metric present and satisfies the threshold | — |
| `fail` | metric present and breaches the threshold | excluded |
| `unknown` | metric absent, blank, or `-` | **excluded** |

`UNKNOWN` is not a pass. On the debt rule this is the difference between a
screen and a liability: screener.in shows `-` for companies that have not
reported borrowings, and a naive `float("-" or 0)` would give them a
debt/equity of **0.0** — passing `< 1` precisely on the names whose debt you
cannot see.

So `on_missing` defaults to `fail`: **if the risk cannot be measured, the
position is not taken.** Capital protection outranks universe size.

Set `on_missing` per rule in the config:

```json
{ "metric": "debt_equity", "op": "<", "threshold": 1, "on_missing": "unknown" }
```

`unknown` still excludes, but tags the reason as a *data* problem rather than a
*violation* — which is what you want when triaging "why is this name missing".
`pass` ignores the rule for that name; use it only with a reason.

---

## Numbers: where a fund screener usually breaks

screener.in returns formatted strings: `"₹1,23,456 Cr."`, `"18.5"`, `"-"`,
`"12.3%"`, `"50 Lakh"`. `client/numbers.py` converts them, under one rule:

> **A missing value is `None`. It is never `0.0`.**

`None` fails every threshold comparison, loudly. Getting this wrong is invisible
in a backtest (it just reshuffles the ranking) and very visible in production.

Notable cases handled: Indian lakh/crore grouping, `Cr.`/`Lakh`/`Mn`/`Bn`
suffixes, the `-` sentinel, a bare number with an `absolute` unit request
(refused rather than guessed), and `%` signs.

### `debt_equity` is derived, not scraped

**screener.in does not publish a debt/equity cell** on the public company page.
Verified against live RELIANCE and HDFCBANK pages: the ratios table carries
Debtor Days, Inventory Days and ROCE, but no debt/equity.

So it is derived from the **balance sheet**, latest period:

```
debt/equity = Borrowings / (Equity Capital + Reserves)
```

Returns `None` — never 0 — if any input is missing, or if equity ≤ 0 (a
negative-equity company has no meaningful ratio, and a small positive number
would wrongly satisfy `< 1`).

The engine labels this in its output (`metrics_derived`), because a derived
figure and a reported one answer differently when your broker disagrees with
your screen.

> **Vendor caveat:** this is the standard total-debt-over-shareholders'-equity
> definition. Vendors differ on lease liabilities and on consolidated vs
> standalone. If a number must reconcile to a specific vendor, add that vendor's
> rule rather than loosening this one.

---

## Configuration

`blue_engine/input/screener_engine.json`. Every key is optional; code defaults
match the file, and a test enforces that.

| key | default | meaning |
|---|---|---|
| `enabled` | `true` | `false` makes the engine a no-op |
| `source` | `symbols` | `sector` \| `symbols` \| `screen` |
| `symbols` | 5 blue chips | used when `source=symbols` |
| `sectors` | `["IN02"]` | slugs from `client.list_sectors()`, when `source=sector` |
| `screen_id` / `screen_slug` | `null` | a saved public screen, when `source=screen` |
| `max_symbols` | `250` | hard cap on the sweep |
| `max_pages` | `40` | hard cap on the pagination walk |
| `enrich` | `false` | fetch each company page to fill gaps (multiplies requests by N) |
| `min_interval_ms` | `400` | min gap between requests (2× the npm default) |
| `cache_ttl_ms` | `21600000` | 6 h, memory + disk |
| `max_retries` | `2` | 429 / 5xx / network only |
| `cookies` | `""` | `sessionid=...`, only for **private** screens |
| `proxy_url` | `""` | optional |

### Sources, and the trade-off

| source | requests | accuracy | notes |
|---|---|---|---|
| `sector` | ~1 per 50 names | table columns only | fastest; `debt_equity` usually absent |
| `symbols` | 1 per symbol | full company page | **only source that can derive debt/equity** |
| `screen` | 1 per page | whatever the screen shows | needs a public screen id + slug |

Because `debt_equity` lives on the balance sheet, the `sector` source cannot
evaluate the debt rule unless `enrich: true` is set. The engine reports
`missing data` prominently rather than silently passing those names.

---

## Politeness and bounds

This is an **unofficial scraper of a site we do not own**. The engine is bounded
by design, and the limits are printed in `polite_limits` on every run:

* throttle with ±20 % jitter (a perfectly periodic request train is a bot signature)
* disk cache so a re-run does not refetch
* `max_symbols` and `max_pages` caps, warned about when hit
* sequential fetch by default; `compare_companies(concurrency=N)` is opt-in
* login-wall detection: a redirect to `/login` or `/register` becomes
  `LOGIN_REQUIRED` and is **never retried**

Raise `min_interval_ms` if you see HTTP 429.

---

## Safety posture

* **Read-only against screener.in.** No order path. The module does not import
  OpenAlgo and never touches the broker.
* **Never raises.** Transport and parse failures return
  `ApiResponse(ok=False, error=...)`; a dead scraper degrades a panel, it does
  not halt the pipeline.
* **Not a price feed.** Output is labelled `data_kind: fundamental_snapshot`
  with an `as_of` date and an explicit note. Live valuation comes from OpenAlgo
  `quotes()` via the Market engine. The two are never mixed.
* **Not in `MacroPipeline` by default.** The pipeline is pure and
  deterministic; injecting an HTTP scraper would break that contract for every
  existing test. Run the screener standalone, or pass its output to Alpha as a
  provider.

---

## Feeding Alpha

The engine emits metric keys Alpha already reads:

| engine output | Alpha key | note |
|---|---|---|
| `pe` | `pe` | |
| `debt_equity` | `de` | **key renamed** — Alpha's rule is `de` |
| `roe` | `roe` | **unit changed**: `32.6%` → `0.326` |
| `dividend_yield` | `dividend_yield` | **unit changed**: `2.1%` → `0.021` |
| `market_cap`, `roce`, `current_price`, ... | same names | screener extras |

The percent→fraction conversion happens **once, at the boundary**. screener.in
reports ROE as a percent; Alpha's `DEFAULT_FUNDAMENTAL_RULES` scales it as a
fraction. Getting this wrong is a silent 100× error that never shows up as a
crash — only as a slightly wrong ranking.

```python
from blue_engine.screener import ScreenerEngine
res = ScreenerEngine(config={"source": "symbols",
                             "symbols": ["INFY", "TCS"]}).run({})
alpha_inputs = {"symbols": res.data["symbols"],
                "fundamentals": res.data["fundamentals"]}
```

---

## Valuations: EPV and CANAV (`valuations.py`)

Two independent, deliberately conservative per-share value estimates, for the
names the screen selects.

```python
from blue_engine.screener import value_symbol, valuations as V

val = value_symbol("RELIANCE")
print(val.as_text())
print(val.classification)          # e.g. "EARNINGS FRANCHISE"
for a in val.assumptions:          # every assumption, with a stable code
    print(a["code"], a["text"])
```

### EPV (bear / base / bull)

```
Normalized EBIT      = Normalized Revenue x Normalized EBIT Margin
NOPAT                = Normalized EBIT x (1 - Normalized Tax Rate)
Sustainable Earnings = NOPAT + Depreciation - Maintenance Capex
                       - Maintenance Working Capital
EPV Enterprise Value = Sustainable Earnings / Required Return
EPV Equity Value     = EV + Excess Cash + Non-operating Investments
                       - Debt - Debt-like Liabilities - Minority Interest
EPV Per Share        = Equity Value / Diluted Shares
EPV Margin of Safety = 1 - (Market Price / EPV Per Share)
```

**Normalisation uses the MEDIAN of the trailing 3-5 annual years**, and TTM is
excluded (it is not an audited annual period). Median, not mean, because "do not
use one unusually profitable year" is the requirement — a mean absorbs a spike,
a median ignores it.

EBIT is normalised as `median(margin) x median(revenue)`, **not** `median(EBIT)`.
That forces the margin to be sustainable instead of letting one high-margin,
low-revenue year dominate the product. Both series are reported separately.

The three scenarios vary **only** the required return and the maintenance-capex
multiple. They never touch revenue or margin: EPV's entire premise is that those
are normalised, and flexing them would turn it into a DCF with growth.

### CANAV (stress / base / moderate)

```
CANAV = Adjusted Shareholders' Equity / Diluted Shares Outstanding
CANAV Margin of Safety = 1 - (Market Price / CANAV)

Adjusted Equity = Shareholders' Equity
                + Quoted Investment Revaluation
                + Unquoted Investment Revaluation
                - Inventory Haircut
                - Receivable Haircut
                - Intangible / Goodwill Haircut
                + Asset Revaluation
                - Contingent Liabilities
                - Other hidden / debt-like liabilities
```

**Haircuts are per-company, not one constant set.** The requested conservative
defaults are the BASE level (33.33% inventory, 50% receivables), but they are
scaled by the company's **own** reported activity ratios:

| Inventory / Debtor Days | Tilt on the base haircut |
|---|---|
| fast (<=60 / <=30 days) | 0.6x — the asset turns over |
| between | linear |
| slow (>=180 / >=120 days) | 1.6x — the asset is stale |

A fast-moving retailer and a company with 400-day inventory do not deserve the
same write-down, and the balance sheet already contains the evidence.

Intangibles and goodwill are written off **100% in every scenario** — there is no
conservative case for counting goodwill as recoverable asset backing.

### Classification

| condition | class |
|---|---|
| Price < NAV **and** Price < EPV | `STRONG VALUATION ALIGNMENT` |
| Price < NAV **but** Price > EPV | `ASSET RICH / EARNINGS WEAK` |
| Price > NAV **but** Price < EPV | `EARNINGS FRANCHISE` |
| Price > NAV **and** Price > EPV | `RICH / PREMIUM TO BOTH` |
| either base estimate unavailable | `INSUFFICIENT DATA` |

Classification always uses the **base** scenario for both. If a base estimate is
missing it returns `INSUFFICIENT DATA` rather than falling back to bear or bull,
because a classification produced by a different scenario than the one the
reader is looking at is unreproducible.

### Never invent missing data

This is the governing rule, and it is what most of the tests check.

* Every input is either measured from a reported figure or `None`.
* A valuation that cannot be computed returns `status="insufficient_data"`,
  `per_share=None`, and a `reason` naming the exact missing input.
* **INVARIANT (test-enforced):** a non-`ok` scenario never exposes `per_share`
  or `margin_of_safety`. A caller that forgets to check `status` cannot act on
  a number the function itself declared unusable.

Gates that refuse rather than publish:

| condition | why it is refused |
|---|---|
| normalised EBIT margin <= 0 | a negative EV / discount rate is negative, and every price would "pass" it |
| sustainable earnings <= 0 | EPV undefined without positive earnings power |
| no reported Depreciation | the largest non-cash add-back; assuming it would understate EPV |
| EPV/share < 1% of price | a modelling failure, not a -10000% margin of safety |
| adjusted equity <= 0 | CANAV is not small, it is absent |

### What screener.in does NOT provide

Every gap below is an explicit **ASSUMPTION** (with a stable code) or a recorded
gap — never a silent zero. The full list is in `Valuation.assumptions`.

| missing | handling | code |
|---|---|---|
| maintenance vs growth capex | proxied as Depreciation x 1.00 (0.80 bull / 1.30 bear) | A1 |
| cash reported separately | excess cash taken as **0** — makes EPV conservative | A2 |
| **diluted** share count | derived from Net Profit / EPS (basic); labelled `shares_are_diluted=False` | A3 |
| minority interest | taken as 0 | A10 |
| contingent liabilities | taken as 0 — EPV ignores off-balance-sheet claims | A11 |
| intangibles / goodwill | no standard line; no haircut applied — CANAV **overstated** if present | B4 |
| quoted vs unquoted investments | one blended write-down, weighted 65% toward unquoted | B1 |
| inventory / receivables | recovered by inverting Inventory Days / Debtor Days | B2, B3 |
| "Other Liabilities" | **not** deducted again — already inside equity via the accounting identity | B7 |
| borrowings | **not** deducted again — screener.in equity is already net of debt | B8 |

### yfinance fallback (opt-in)

**screener.in is the primary source. yfinance fills ONLY the gaps it leaves.**

```python
from blue_engine.screener import value_symbol
val = value_symbol("RELIANCE", use_yfinance_fallback=True)
print(val.filled_by)     # {'diluted_shares': 'yfinance', 'minority_interest': 'yfinance', ...}
```

The fallback can write into a field **only if it is still `None`** — it never
overrides a figure screener.in reported. Swapping a reported number for another
vendor's version, even a "better" one, would silently change the screen's
decisions, because the rules were calibrated on that basis.

| gap core screener leaves | yfinance row | closes assumption |
|---|---|---|
| cash & equivalents | `Cash And Cash Equivalents` | A2 |
| **diluted** share count | `Diluted Average Shares` | A3 |
| minority interest | `Minority Interest` | A10 |
| inventory / receivables | `Inventory`, `Accounts Receivable` | B2, B3 |
| goodwill / intangibles | `Goodwill`, `Other Intangible Assets` | B4 |
| capex | `Capital Expenditure` | A1 (see guard) |

Still **not** available from either source: **contingent liabilities**.

#### The unit trap (this is the dangerous one)

screener.in reports in **crores**; yfinance reports in **absolute rupees and
shares**. Verified against RELIANCE.NS:

```
yfinance Total Assets     21,781,400,000,000  = 2,178,140 Cr
yfinance Diluted Shares   13,532,736,424       = 1,353.27 crore-shares
screener.in implied shares                  1,352.97 crore-shares
```

Copying a yfinance amount in without dividing by `1e7` multiplies it by ten
million — for the share count that silently divides every per-share figure by
10 million. The first live run hit exactly this. Everything goes through
`_to_crore()`, and a regression test pins it.

#### Two guards the live run forced

**`A1`/`A1b`/`A1c` — total capex is NOT maintenance capex.** yfinance gives total
capex (Reliance: 1,22,916 Cr) which includes growth capex (5G, new energy,
retail). Charging all of it against normalised earnings cut EPV from 872 to 293
(−66%) — that is a different, growth-penalising model, not a better estimate. So
the measured figure is used **only when it is at or below the
depreciation-anchored proxy**; otherwise the proxy is kept and the headroom is
reported. `trust_total_capex_as_maintenance=true` overrides deliberately.

**`B4c` — a vendor's intangibles figure can be a classification difference.**
Yahoo reported Reliance at 4,19,107 Cr of goodwill+intangibles = **46% of
shareholders' equity**, which cannot be goodwill alone (it will include spectrum
and other long-lived rights that are not worthless). Writing 100% of it off cut
CANAV from 423 to 113 — a haircut on the wrong thing. The write-off is now
**capped at 25% of equity** (`intangible_haircut_cap_pct`), and the excess is
carried and reported.

#### Measured effect on RELIANCE

| | screener only | + yfinance | driver |
|---|---|---|---|
| EPV base | 872.42 | **826.04** (−5.3%) | real excess cash **+119,346** Cr, real minority interest **−181,836** Cr (both were 0) |
| CANAV base | 422.97 | **255.55** | intangible cap (226,008 Cr) still dominates |
| shares | 1,352.97 (basic) | 1,353.27 (**diluted**) | true diluted count |

The EPV change is a **genuine improvement** — two real quantities that were
previously zero. The CANAV change is **dominated by one unverifiable input**
(Yahoo's intangible classification), which is why it is capped and flagged rather
than trusted.

> **yfinance is an unofficial Yahoo scraper** — the same reliability class as the
> screener scraper, with no SLA. It is a fallback, never a dependency: if the
> import fails, the network fails, or the fields are absent, the valuation
> proceeds exactly as before and records why.

> Its `Tax Rate For Calcs` field returned **0** for RELIANCE.NS (actual 22.4%),
> so that field is never read; the rate is derived from `Tax Provision` /
> `Pretax Income` instead.

### Disagreeing with a vendor is expected

These are the standard total-debt-over-shareholders'-equity and EV-minus-debt
definitions. Vendors differ on lease liabilities, on consolidated vs standalone,
and on surplus-cash treatment. If a number must reconcile to a specific vendor,
add that vendor's rule — do not loosen this one.

Real output, RELIANCE consolidated (saved live page):

```
price=1,463.50   shares=1,352.97 (basic)
EPV per share:
  bear    620.34   MoS  -135.9%
  base    872.42   MoS   -67.8%
  bull  1,183.75   MoS   -23.6%
CANAV per share:
  stress      n/a   (haircuts consume equity - no asset backing)
  base     422.97   MoS  -246.0%
  moderate 625.39   MoS  -134.0%
CLASSIFICATION: RICH / PREMIUM TO BOTH
```

## Extractors: `client/extractors.py`

Label-aware helpers that read the screener.in page for valuation inputs. Pure
functions over an already-parsed page - no network, no I/O.

```python
from blue_engine.screener.client import extractors as EX

bundle = EX.extraction_bundle(company)   # everything, plus what is unavailable
EX.capex_from_cashflow(company)          # real capex, not the net investing line
EX.balance_sheet_balances(company)       # does the parsed page balance?
EX.market_snapshot(company)              # price, cap, implied share counts
EX.net_working_assets(company)           # inventory/receivables from the ratios
EX.statement_years(company)              # how many annual columns exist
```

### What the real page actually gives you

The tables span **12 annual columns** (Mar 2015 to Mar 2026), not 5. The exact
printed labels are:

```
cash-flow      Cash from Operating Activity +, Cash from Investing Activity +,
               Cash from Financing Activity +, Net Cash Flow, Free Cash Flow
balance-sheet  Equity Capital, Reserves, Borrowings +, Other Liabilities +,
               Total Liabilities, Fixed Assets +, CWIP, Investments,
               Other Assets +, Total Assets
```

The trailing `+` marks an expandable row. It is punctuation, not part of the
name, and it is stripped before matching.

### Two label bugs this module exists to prevent

**1. `Cash from Investing Activity +`, not `Investing Activity`.** The old short
label matched **nothing** on a real page - it only appeared to work because the
unit fixture used the short form. `capex_series` was silently empty, so the EPV
maintenance-capex step never saw a measured figure.

**2. `Borrowing` vs `Borrowings +`.** HDFCBANK writes the singular, ITC writes
the plural. A stem of `borrowing` must catch both - plain equality caught
neither, because `borrowing != borrowings`. Matching is now equality **or
equality modulo a trailing `s`**, and never a bare substring.

Why never a substring: `"tax"` is a substring of `"Profit before tax"`, which
holds **crores**, while `Tax %` holds a **percentage**. A substring lookup puts a
crore figure into a tax rate. Exact matching is the default everywhere; a
substring requires an explicit `exact=False`.

### Capex is recovered, not guessed

`Cash from Investing Activity +` is **net** - it nets asset purchases against
disposals and includes acquisitions, so it is not capex. The helper prefers:

1. `CFO - Free Cash Flow` - exact, since screener.in defines FCF that way
2. `-(Cash from Investing Activity +)` - only when that figure is **negative**

A **positive** investing figure is rejected outright: it is proof the line is
contaminated by disposals, and using it would return a negative capex and
inflate EPV. Verified live: Reliance recovers **1,22,090 Cr** where the net
investing line reads -1,01,089 Cr.

### The integrity gate

`balance_sheet_balances()` checks two identities:

```
Total Assets == Total Liabilities
Total Assets == Fixed Assets + CWIP + Investments + Other Assets
```

On the live RELIANCE page both hold exactly (21,77,546 Cr, and the components sum
to the same). If either fails, a label was mis-read and every number derived from
that page is suspect. This is the cheapest possible correctness check on a page
we do not own.

### What is genuinely NOT on the page

`extraction_bundle()` returns an `unavailable` list so a consumer sees the
boundary instead of assuming a missing key is a bug:

| field | why |
|---|---|
| cash & equivalents | folded into `Other Assets +`, no separate line |
| minority interest | no line |
| intangibles / goodwill | no line |
| contingent liabilities | annual-report PDF only |

Guessing a split of `Other Assets +` would put a fabricated number into a
valuation, which is the one thing this subsystem refuses to do.

## Tests

```powershell
python -m pytest Blue_engine\tests\test_extractors.py -q
python -m pytest Blue_engine\tests\test_valuations.py -q
```


**189 tests, all passing** (47 extractor + 57 valuation + 85 screener). They are
fully offline: pure-function tests plus a `ScreenerClient` driven against saved
HTML via a monkeypatched session.

The screener tests that matter most are in `TestThreeValuedRules` — a missing
metric must FAIL, not pass. The valuation tests that matter most are in
`TestEpv`, `TestCanav` and the `INVARIANT` check in `TestValueCompany` — a
rejected scenario must never expose a `per_share` figure.

### Running the suite on this machine

`blue_engine/core/__init__.py` eagerly imports the whole kernel
(openalgo → pandas → sklearn → scipy → statsmodels → dash). On this box that
chain takes far longer than a terminal window allows, so a plain
`pytest tests/test_screener_engine.py` produces no output at all — which looks
identical to a hang.

Prove the screener code in isolation (loads only `bs4` + `requests` + `lxml`):

```powershell
python Temp\run_screener_tests.py tests\test_screener_engine.py -q
```

> Note: this is a pre-existing environment characteristic. The existing suite
> (`test_recovery.py`) behaves the same way.

---

## License and attribution

The vendored clone is MIT © [Sahil Jani](https://github.com/sahiljani) — see
`vendor/screener-india/LICENSE`. It is an **unofficial, independently
maintained** client, not affiliated with screener.in.

Data is scraped from publicly accessible pages of
[screener.in](https://www.screener.in). Intended for personal research and
non-commercial use. This is not financial advice.
