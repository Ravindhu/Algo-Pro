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
  filters.py                       Rule / Screener / the two requested rules
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

## Tests

```powershell
python -m pytest Blue_engine\tests\test_screener_engine.py -q
```

**76 tests, all passing.** They are fully offline: pure-function tests plus a
`ScreenerClient` driven against saved HTML via a monkeypatched session.

The ones that matter most are in `TestThreeValuedRules` — a missing metric must
FAIL, not pass.

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
