# Portfolio Engine — Build Plan & Quant Playbook

**Package:** `blue_engine/portfolio/`
**Stage:** `EngineStage.PORTFOLIO` (value 30 — the *third* stage of the pipeline)
**Role in the flow:** `Economic -> Market -> Portfolio -> Alpha -> POV -> Infrastructure`
**Audience:** quant analyst / trader / PM / coder building the desk.
**Status today:** a working *pure* allocator (`portfolio_engine.py`) that turns a
universe + vols + correlation + the Economic regime multiplier into target
weights, a drift/rebalance trade list, a risk snapshot (vol, HHI, beta) and a
naive beta hedge. `dashboard/pages/portfolio.py` renders **live positions only**
(a flat OMS/EMS view). This document plans the upgrade from a one-shot
inverse-vol allocator into a **data-fed, ML/statistically-driven, "
super-algo" portfolio-management engine** — risk models, optimisers, meta-
allocation, live attribution and GPT-assisted commentary — with the whole thing
operable from one Dash console.

> Companion document to `Blue_engine/economics/ECONOMIC_ENGINE_PLAN.md`. Same
> structure, same golden rules, same "degrade, never halt" contract.

---

## 0. What the engine is (and is not)

| It IS | It is NOT |
|---|---|
| The **capital-allocation** stage: "given the macro regime and the market picture, *how much* do we hold in *which sleeves*?" | A stock picker (that is the Alpha engine — it decides *which names* inside a sleeve meet min-score). |
| A consumer of **Economic** (`gross_exposure_multiplier`) and **Market** (`leaders`, breadth, vol). | A live execution path. It emits **target weights and rebalance intents**, never orders. |
| **Pure by contract** (`Blue_engine/core/base.py`): no network, no SDK, no DB in `_run()`. All data arrives via an injected `provider` callable. | Allowed to import `openalgo` internals. It reads account data only through the injected SDK client in `context['client']`. |
| The **owner of the risk budget**: vol target, gross/net caps, concentration caps, beta neutrality. | A place to store positions. Positions live in `core/ledger.py` + the broker `positionbook`; this engine *reads and reasons about* them. |

**Current output contract** (`EngineResult.data`, from `portfolio_engine.py`):

```python
{
  "target_weights": {"NIFTY": 0.31, "BANKNIFTY": 0.22, ...},   # sleeve -> weight
  "rebalance_trades": [
      {"sleeve": "IT", "action": "BUY", "target_weight": ..,
       "current_weight": .., "delta_weight": ..}, ...],
  "risk": {
      "portfolio_vol": 0.1326, "hhi": 0.3121, "beta": 1.04,
      "dominant_contributor": "BANKNIFTY",
      "contributions": {"NIFTY": .., ...}, "vol_target_scale": 0.94},
  "hedge": {"needed": True, "instrument": "NIFTY", "side": "SELL",
            "hedge_ratio": 0.06, "beta": 1.06, "reason": "..."},
  "capital": 1_000_000.0,
  "cash_weight": 0.12,
}
```

`target_weights`, `rebalance_trades` and `risk.vol_target_scale` are the numbers
the rest of the desk acts on: Alpha sizes within the sleeve weights, the risk
gate enforces caps on the resulting book, and the dashboard displays the target
vs actual drift.

---

## 1. The current allocator (baseline) and its weaknesses

Today `_construct()` does, in order:

1. `risk_parity_weights(vols)` → inverse-vol weights (naive risk parity).
2. tilt ×`(1 + leader_tilt)` toward `market['leaders']`.
3. `clamp` per sleeve to `[min_weight, max_weight]`, renormalise.
4. scale gross by `econ['gross_exposure_multiplier']`; remainder is cash.

Then `_risk()` computes portfolio vol from a **hand-supplied** corr matrix, HHI,
beta vs benchmark, and a proxy for the dominant risk contributor. `_hedge()`
short-sells the benchmark by `|beta − 1|`, capped at `max_hedge_ratio`.

**Weaknesses a quant must flag today:**

1. **Naive inverse-vol, not true risk parity.** Inverse-vol balances *standalone*
   vols; it ignores correlation. Two 0.8-correlated sleeves both get 1/σ weight
   and end up holding the *same* risk twice. True ERC (equal risk contribution)
   solves `w_i (Σw)_i` equal across `i`.
2. **Hand-fed correlation.** `corr` must be supplied in the provider dict; there
   is no estimation from returns, no shrinkage, no decay, no eigenvalue cleaning.
   Stale/hand-set correlations are the #1 source of "diversification that isn't".
3. **One-shot, not path-aware.** It rebalances on a fixed band from *target*,
   with no transaction-cost model, no tax awareness, no turnover penalty, no
   no-trade region around the optimum.
4. **No optimiser.** No mean–variance, min-variance, max-Sharpe, Black–Litterman
   or CVaR objective. Weights are a heuristic, not an optimum.
5. **Static risk budget.** `target_vol` scales gross but there is no risk
   targeting through drawdown, no vol-of-vol regime, no Kelly/EV sizing from the
   regime *probability*.
6. **No attribution or analytics.** The dashboard shows live positions but not
   *contribution to return*, *factor exposure*, *tracking error*, *realised vs
   target drift over time*, or *drawdown*.
7. **Hedge is beta-only.** No variance-minimising hedge ratio, no basis risk
   from the futures vs index, no cointegration check on the hedge pair.
8. **No tax / India-specific reality.** STCG vs LTCG, STT, expiry roll, MIS/CNC
   product differences, F&O vs cash margins — all invisible today.

Everything in §4–§9 replaces a numbered weakness above with a model.

---

## 2. What a quant PM actually analyses (the "full stack" checklist)

This is the menu. Sections 4–9 each implement a slice.

### 2.1 Risk model
- **Covariance estimation:** sample → EWMA (RiskMetrics) → Ledoit–Wolf shrinkage →
  factor model (PCA) → DCC-GARCH for time-varying correlation
- **Volatility:** realised (Garman–Klass / Parkinson), EWMA, GARCH(1,1),
  HAR-RV multi-horizon forecast
- **Tail risk:** historical & Cornish–Fisher VaR, Expected Shortfall (CVaR),
  drawdown-at-risk, stress scenarios (2008, Mar-2020, Mar-2025-style spikes)
- **Beta / factor:** OLS beta, downside beta, Fama-French/MSCI-style factor
  loadings (market, size, value, momentum, quality, low-vol)

### 2.2 Construction / optimisation
- **Classical:** mean–variance, min-variance, max-Sharpe (tangency)
- **Robust:** inverse-vol, **ERC (true risk parity)**, hierarchical risk parity
  (HRP), Nested Clustered Optimisation (NCO)
- **Bayesian:** **Black–Litterman** (market prior + alpha views), resampling
- **Downside:** CVaR / mean-CVaR, mean-absolute-deviation
- **Constraints:** box (per-sleeve), turnover, sector/issuer caps, beta band,
  gross/net, integer lots (India F&O), no-trade region
- **Sizing:** fractional Kelly from `P(regime)`, vol targeting, drawdown governor

### 2.3 Meta-allocation / regime
- **Regime-conditional weights:** different target vols/objects per Economic
  regime (`RISK_ON` .. `RISK_OFF`)
- **Tactical overlay:** momentum/trend filter on sleeves, breadth gate
- **Rebalancing policy:** calendar vs threshold vs optimal (cost-aware)

### 2.4 Performance / attribution
- **Return decomposition:** Brinson (allocation vs selection), factor-attribution
- **Risk decomposition:** marginal & component risk contribution, VaR contribution
- **Efficiency:** Sharpe, Sortino, Calmar, information ratio vs benchmark,
  tracking error, up/down capture, hit-rate, payoff ratio
- **Time-series:** rolling Sharpe, drawdown curve, underwater plot, turnover vs cost

### 2.5 Statistical / probability
- **Estimation:** shrinkage (James–Stein / Ledoit–Wolf), robust covariance (MCD)
- **Stationarity & cointegration** (Engle–Granger / Johansen) for hedge pairs
- **Resampling:** block bootstrap for CI on Sharpe/vol; Monte-Carlo for paths
- **Bayesian:** posterior on expected returns (Black–Litterman), regime posterior

### 2.6 ML / DL / AI / NLP
- **Supervised:** GBM/XGBoost to forecast *sleeve* next-20d return sign &
  vol (used as **views** into the optimiser)
- **Unsupervised:** clustering sleeves into risk clusters (HRP/NCO tree)
- **DL:** LSTM/TFT covariance/vol forecaster; autoencoder tail detector
- **RL:** allocation policy over regimes (frontier)
- **NLP:** news/filing tone per sleeve; **GPT** to write the daily PM note and to
  convert text into structured **views** for Black–Litterman

---

## 3. Free / open data sources (India-first) — credentials & safety

> **Security rule (from `AGENTS.md`):** *never* hardcode keys. Resolve from the
> environment first, then `_restored/.env`. Same rule as `core/client.py` and the
> Economic engine. The OpenAlgo API key is **never** logged (see `core/ledger.py`
> `api_key_hash`), and portfolio docs contain **no account numbers, no PII**.

### 3.1 No-key sources (start here — zero credential risk)

| Source | What | How | Cost |
|---|---|---|---|
| **OpenAlgo SDK** (`client.history`, `quotes`, `multiquotes`, `depth`) | OHLCV for every sleeve/index/equity — the raw material for covariance, beta, vol | Already wired via `core/client.py` → the platform owns the broker session | Free (your platform) |
| **OpenAlgo SDK** (`client.positionbook`, `client.holdings`, `client.funds`, `client.orderbook`, `client.tradebook`) | **Live book**: current weights, avg price, margin, realised/unrealised P&L | Injected client in `context['client']`; used read-only | Free |
| **NSE India** | index/sector historical levels, PCR, VIX, FII/DII flows | `nseindia.com` JSON/session-cookie | Free |
| **RBI DBIE / FBIL** | 10Y G-Sec (risk-free), USD/INR, liquidity | CSV/web | Free |

### 3.2 Free-key APIs (register once; store in `_restored/.env`)

| API | Env var (suggested) | Free tier | Use |
|---|---|---|---|
| **data.gov.in** | `DATAGOVIN_API_KEY` | Free | sector macro context (reuse Economic providers) |
| **FRED (St. Louis)** | `FRED_API_KEY` | Free | US10Y, DXY — global risk factor for cross-asset sleeve |
| **Alpha Vantage** | `ALPHAVANTAGE_API_KEY` | 25 req/day | cross-asset (gold, crude) for the commodity sleeve |
| **NewsAPI / GNews** | `NEWSAPI_KEY` | 100 req/day | headlines for sleeve sentiment |
| **OpenAI / local Ollama** | `OPENAI_API_KEY` | paid / local free | GPT PM note + view extraction (§8) |

**Storage pattern** (mirror `core/client.py`):
```python
def _key(name: str) -> str | None:
    return os.getenv(name)          # env wins
    # else: read from my-trading-system/_restored/.env (single source of truth)
```
`.env` is already `.gitignore`-d. Only env-var *names* appear in code and docs.

---

## 4. Target architecture (why it stays upgrade-proof)

The `openalgo/` clone stays **pristine**. This engine talks *only* through the
public SDK (`client.history`, `client.positionbook`, ...) plus the standard
library (+ optional `numpy`/`scipy`/`scikit-learn`), so the upgrade path is
unchanged: `git pull` the clone and the engine keeps working because it never
imported OpenAlgo internals. This matches the architecture locked in the master
README (`Python → OpenAlgo → Broker → Exchange`).

```
┌─────────────────────────────────────────────────────────────┐
│ blue_engine/portfolio/                                        │
│                                                               │
│  providers/            ← NEW. Adapters (the ONLY I/O here).   │
│    base.py               PortfolioProvider ABC + caching +    │
│                          staleness + fuse (fail-soft).        │
│    openalgo_provider.py  returns/vols/corr/betas/current      │
│                          weights from client.history +        │
│                          client.positionbook (no extra keys). │
│    universe.py           sleeve -> constituent map (NIFTY,    │
│                          BANKNIFTY, IT, PHARMA, AUTO, ...)    │
│    account_provider.py   funds/margin/holdings -> capital,    │
│                          buying power, product type.          │
│    composite.py          Merge providers -> one portfolio     │
│                          input dict (the §0 provider shape).  │
│                                                               │
│  risk/                 ← NEW. Pure risk model (no I/O).       │
│    covariance.py         sample / EWMA / Ledoit-Wolf shrinkage│
│    factor_model.py       PCA/factor decomposition, cleaning   │
│    volatility.py         EWMA, GARCH(1,1), HAR-RV forecast    │
│    tail.py               VaR, CVaR, drawdown-at-risk, stress  │
│    beta.py               OLS/downside beta, factor loadings   │
│    contribution.py       marginal & component risk            │
│                                                               │
│  optim/                ← NEW. Pure optimisers (no I/O).       │
│    base.py               BaseOptimiser ABC (constraints in,   │
│                          weights out; projects to simplex).   │
│    mean_variance.py      min-var / max-Sharpe (closed form)   │
│    risk_parity.py        true ERC via cyclical coord descent  │
│    hrp.py                hierarchical risk parity (Lopez de   │
│                          Prado) + NCO                         │
│    black_litterman.py    BL posterior from views (§9/§8)      │
│    cvar.py               mean-CVaR (LP / subgradient)         │
│    constraints.py        box/turnover/beta/gross + no-trade   │
│    cost_aware.py         cost-penalised rebalance + band      │
│                                                               │
│  sizing/               ← NEW. Pure sizing (no I/O).           │
│    vol_target.py         gross scale toward target vol        │
│    kelly.py              fractional Kelly from P(regime)      │
│    drawdown_governor.py  de-risk on drawdown/vol-of-vol       │
│                                                               │
│  attribution/          ← NEW. Pure analytics (no I/O).        │
│    brinson.py            allocation vs selection effect       │
│    factor_attr.py        factor return decomposition          │
│    perf.py               Sharpe/Sortino/Calmar/IR/TE/capture  │
│    drift.py              target-vs-actual drift time series   │
│                                                               │
│  views/                ← NEW. Alpha/ML/NLP -> BL views.       │
│    ml_views.py           GBM forecast -> expected sleeve ret   │
│    nlp_views.py          news tone -> BL view + confidence     │
│    llm_note.py           GPT PM note + structured views       │
│                                                               │
│  portfolio_engine.py   ← thin orchestrator: calls the above.  │
│  portfolio_state.py    ← NEW. persists target/drift history.  │
└─────────────────────────────────────────────────────────────┘
```

**Golden rules**
1. `portfolio_engine.py` stays **pure**; only `providers/` touches the network /
   SDK, and only via the injected client.
2. Every model is **dependency-optional** — if `numpy`/`scipy`/`sklearn` is
   absent, the engine degrades to today's inverse-vol + hand-corr path. This is
   the same "degrade, never halt" rule the Economic and Infrastructure engines use.
3. Engines communicate only via `EngineResult.data` (dicts). No shared globals.
4. **India lot reality:** any weight that must become an order is converted to
   *integer lots* by the existing `core/lot.py` at the POV boundary — the
   portfolio layer stays in continuous weights.

---

## 5. Methods & models — concrete catalogue

### 5.1 Covariance & risk (implement first — highest ROI, no keys)

| Method | What it answers | Output | Library |
|---|---|---|---|
| **EWMA covariance** (λ≈0.94 daily) | recent vol/corr, reacts to regime shifts | `Σ` | stdlib/numpy |
| **Ledoit–Wolf shrinkage** | stabilise a noisy sample `Σ` | `Σ_shrunk` + shrinkage `δ` | `sklearn.covariance` |
| **PCA / factor cleaning** | separate signal from noise eigenvalues | `loadings`, `residual σ` | numpy |
| **GARCH(1,1)/HAR-RV** | next-day/next-week vol forecast | `σ_hat` | `arch` (opt) |
| **Historical/Cornish-Fisher VaR** | loss at 95/99% | `VaR`, `CVaR` | stdlib/numpy |
| **Component risk contribution** | *which sleeve* drives risk | `RC_i` | stdlib |

### 5.2 Construction / optimisation

| Method | Objective | Why it matters | Library |
|---|---|---|---|
| **Inverse-vol** (today) | balance standalone vol | baseline | stdlib |
| **ERC / true risk parity** | equal `w_i (Σw)_i` | corrects weakness #1 | numpy/scipy |
| **Hierarchical Risk Parity** | cluster then allocate | robust to bad `Σ` | scipy |
| **Min-variance / Max-Sharpe** | lowest var / highest SR | classical optima | numpy (closed form) |
| **Black–Litterman** | market prior + views | merges ML/NLP views sanely | numpy |
| **Mean-CVaR** | minimise tail loss | drawdown control | scipy (opt) |
| **NCO** | HRP + per-cluster opt | best of both | scipy |
| **Cost-aware rebalance** | penalty on turnover/tax | India STT/STCG reality | stdlib |

### 5.3 Sizing / meta-allocation

| Method | Input | Output |
|---|---|---|
| **Vol targeting** | forecast `σ_p` | gross multiplier (today: warn-only → make it act) |
| **Fractional Kelly** | `P(regime)`, edge | optimal gross |
| **Drawdown governor** | peak-to-trough | de-risk schedule |
| **Regime-conditional object** | Economic regime | which optimiser + target vol |

### 5.4 ML / DL

| Model | Target | Why |
|---|---|---|
| **GBM / XGBoost** | next-20d *sleeve* return sign & vol | **views** for Black–Litterman |
| **k-means / hierarchical** | risk clusters | HRP/NCO tree |
| **LSTM / TFT** | vol & covariance forecast | better `Σ` than EWMA |
| **Autoencoder** | reconstruction error | tail/anomaly detector |
| **RL (PPO)** | allocation policy over regimes | frontier research |

### 5.5 NLP / GPT

| Method | Input | Output |
|---|---|---|
| **Lexical / FinBERT sentiment** | headlines per sleeve | `tone ∈ [-1,1]` |
| **LLM structured extract** | broker notes, filings, news | `{view_return, confidence, horizon}` → BL view `Q`, `Ω` |
| **GPT PM note** | the run's numbers | a written daily portfolio commentary |

---

## 6. Case study: HRP vs inverse-vol — a real diversification lesson

**Scenario.** Three sleeves with vols `[0.14, 0.16, 0.50]` (say NIFTY, IT,
`SMALLCAP`) and the small-cap sleeve ~0.9 correlated to IT.

| Sleeve | σ | inv-vol weight | note |
|---|---|---|---|
| NIFTY | 0.14 | 0.42 | — |
| IT | 0.16 | 0.37 | — |
| SMALLCAP | 0.50 | 0.12 | but 0.9 corr to IT |

Inverse-vol *believes* it has diversified (SMALLCAP only 12%) but shares 0.9
correlation with IT — the **combined IT+SMALLCAP risk** dominates the book.
Component risk contribution (RC) tells the truth:

```
RC_IT+SMALLCAP ≈ 0.78 of total risk    (inverse-vol  → blind)
```

HRP first clusters (IT & SMALLCAP merge, they're ~2 correlated), *then* allocates
across clusters → weights ≈ `[0.46, 0.24, 0.30]`, RC ≈ `[0.34, 0.32, 0.34]`.
**Same inputs, materially different risk profile.** True risk parity / HRP is the
fix for weakness #1 and #2. Validate on your own universe via walk-forward (§9):
compare realised vol, max-DD and Sharpe of inv-vol vs ERC vs HRP.

---

## 7. Case study: how the regime multiplier + views flow to P&L

Trace one run end-to-end, showing where the **new** pieces slot in:

```
Economic.risk_on_score = +0.61  ->  gross_exposure_multiplier = 1.45
        │
        ▼
portfolio.providers.openalgo_provider  ->  Σ from EWMA(client.history)
        │                                     betas from OLS vs NIFTY
        ▼
portfolio.views.ml_views   ->  GBM says BANKNIFTY next-20d +1.2% (conf 0.6)
portfolio.views.nlp_views  ->  news tone on PHARMA -0.3 (conf 0.4)
        │
        ▼
optim.black_litterman  ->  posterior expected returns (prior = market caps,
                            tilted by the views, scaled by confidence Ω)
optim.risk_parity/hrp  ->  base weights ;  optim.mean_variance -> tilt to views
optim.constraints      ->  box 40%, beta band [0.9,1.1], turnover cap
        │
        ▼
sizing.vol_target      ->  σ_p forecast 0.138 vs target 0.12 -> gross ×0.94
sizing.kelly           ->  P(regime)=0.72 -> frac-Kelly caps gross
sizing.drawdown_governor -> book 4% off peak -> ×0.85 de-risk
        │
        ▼
target_weights (post-clamp) -> rebalance_trades (cost-aware band)
        │
        ▼
Alpha selects names inside each sleeve; core/lot.py -> integer lots
        │
        ▼
POV slices; core/risk_gate enforces max_open_exposure on the enlarged book
        │
        ▼
attribution.brinson + dashboard: allocation vs selection, drift, drawdown
```

Backtest intuition for the desk: a **risk-parity/HRP base + regime gross overlay
+ cost-aware rebalancing** historically improves Sharpe and max-DD versus naive
inverse-vol with monthly equal-weight rebalancing on Indian index/sector sleeves.
**Validate with walk-forward and India cost assumptions — not a single fit.**

---

## 8. NLP + GPT integration — design (opt-in, safe)

**Principle:** text/LLM is *additive and optional*. No key → lexical fallback +
warning, pipeline still runs. LLM output is a **structured view**, never free
text the code must parse.

```
headlines ─► views/nlp_views.py (lexical, always-on, no key)
                 │
filings/notes ─► views/ml_views.py (GBM, no key, local)
                 │
broker note/ ─► views/llm_note.py ─► if OPENAI_API_KEY or local Ollama:
RBI text          │                     extract structured JSON (cached)
                  ▼
          BL views {Q: expected return, Ω: view covariance} ─► optim.black_litterman
                 │
                 ▼
          views/llm_note.py ─► GPT PM note (drafted from the run's numbers)
```

**LLM prompt contract (JSON only):**
```json
{"sleeve": "PHARMA", "view_return": -0.006, "confidence": 0.4,
 "horizon_days": 20, "rationale": "input-cost pressure", "source_ids": ["h1","h2"]}
```
Cache every response by **hash of the input text** under `data/portfolio_llm/` so
we never pay twice and results are reproducible.

**Security:**
- Key read from env / `_restored/.env`; never logged.
- Prompts contain **no account numbers, no positions, no PII** — only public text
  and generic sleeve names.
- The GPT PM *note* is drafted **from numbers, not from the live book**: it is
  given aggregate stats (weights, vol, drift), not raw orders or account IDs.

---

## 9. Testing, validation & anti-look-ahead (non-negotiable)

| Layer | Test | Tool |
|---|---|---|
| Unit | each optimiser hits its constraint set; weights sum to ≤1 (≤ gross) | `pytest` |
| Unit | ERC yields equal component risk (`RC_i` equal to tolerance) | `pytest` |
| Unit | `_regime`-conditional object selection at exact regime labels | `pytest` |
| Contract | `PortfolioEngine.run()` never raises; returns `EngineResult` | `pytest` |
| Degradation | no provider/`numpy` → falls back to inverse-vol + hand-corr | `pytest` |
| Provider | fuse: N failed fetches → falls back to config inputs | `pytest` + mock |
| Covariance | shrinkage `δ∈[0,1]`, `Σ_shrunk` PSD (positive-definite) | `pytest` |
| Model | **walk-forward CV only** (fit `t0..t1`, test `t1..t2`, roll) | custom |
| Model | no target leakage: features at `t`, label from `t+20` (monthly rebal) | review |
| Statistical | stationarity/cointegration gate before pair-hedge estimation | `statsmodels`(opt) |
| Backtest | inv-vol vs ERC vs HRP vs BL: Sharpe, max-DD, hit-rate, **after costs** | `core/benchmark.py` |
| Rebalance | cost-aware policy beats naive band on net-of-cost turnover test | custom |

**Look-ahead traps to avoid explicitly:**
- **Covariance window leakage** — estimate `Σ` from returns *up to and including*
  `t`, never the full sample; re-estimate on a schedule, not per tick.
- **Survivorship / point-in-time universe** — sleeve constituents as they were,
  not today's index members.
- **Beta estimated on the full sample** then applied historically → always
  rolling/expanding-window beta.
- **Slippage ignore** — Indian sleeves have STT + impact; a rebalancer that
  ignores cost will over-trade in backtest and bleed live.
- **Rebalance-date look-ahead** — rebalance on the *close* you actually trade,
  using weights computed from that same close (or the prior close), consistently.

---

## 10. Observability: logging, debugging, performance

- **Logging:** extend today's `self.log(...)` line to a structured JSON line per
  run → `data/portfolio_runs/YYYY-MM-DD.jsonl`: regime, chosen optimiser,
  target weights, forecast vol, gross scale, Kelly cap, turnover, cost estimate,
  hedge. Mirrors `economics/run_history.py`.
- **Debugging:** `--explain` mode dumps *why* each weight changed — the binding
  constraint (box/turnover/beta), the view that moved it, the shrink `δ`. Because
  the engine is pure, a bug reproduces from a saved input dict alone.
- **Performance:** covariance/optimiser are the expensive parts → cache `Σ` and
  fitted models under `data/portfolio_models/` and refit on a schedule (daily
  after close / weekly), not per tick. Provider calls TTL-cached (history is
  end-of-day; **never** poll `positionbook` per tick — poll on the dashboard's
  existing 5s interval, cache the rest). The current O(n²) `portfolio_vol` is
  fine for ~20 sleeves; move to numpy only when n grows.
- **File organisation:** one concern per file (§4 tree). `providers/` = I/O;
  `risk/ optim/ sizing/ attribution/ views/` = pure; `portfolio_engine.py` =
  orchestration; `portfolio_state.py` = persistence.

---

## 11. Dashboard (Dash/Plotly) — "super terminal" portfolio tabs

The dashboard is the *operation console*. Today `pages/portfolio.py` is a flat
live-positions view. Keep that page for the live book, and add:

| Tab | Contents | Data source |
|---|---|---|
| **Allocation** | target vs actual weights (grouped bar), drift %, cash | `target_weights` + `positionbook` |
| **Risk** | vol vs target gauge, HHI, component risk bars, beta, VaR/CVaR | `risk` block |
| **Optimiser** | ERC vs HRP vs BL weights (comparison), binding constraints | run log |
| **Attribution** | Brinson allocation/selection, factor exposure, perf stats | `attribution/` |
| **Efficiency** | rolling Sharpe, drawdown (underwater), turnover vs cost | `portfolio_state` |
| **Views** | ML/NLP/GPT views + confidences feeding BL | `views/` |
| **PM Note** | the GPT daily commentary (read-only) | `views/llm_note.py` |
| **Connection** | OpenAlgo/broker/dashboard status chips (already in infra page) | infra engine |

All charts are Plotly; keep the existing polling `Interval` pattern and add
`portfolio-refresh-out` targets per tab. **No secrets, no account numbers** are
ever rendered — sleeve names and weights only.

---

## 12. Recommended build order (milestones)

| M | Deliverable | Keys needed | Deps | Value | Fixes weakness |
|---|---|---|---|---|---|
| **M1** | `providers/openalgo_provider.py` + `composite.py`: build returns/vols/**corr from history**, plus live book from `positionbook` | none | stdlib | real, live inputs instead of hand-fed | #2, #4-ish |
| **M2** | `risk/covariance.py` (EWMA + Ledoit-Wolf) + `risk/contribution.py` | none | numpy, sklearn (opt) | honest diversification | #2, #6 |
| **M3** | `optim/risk_parity.py` (true ERC) + `optim/hrp.py` + `constraints.py` | none | numpy/scipy | correct risk balancing | #1, #4 |
| **M4** | `sizing/vol_target.py` (act, not warn) + `kelly.py` + `drawdown_governor.py` | none | stdlib | dynamic risk budget | #5 |
| **M5** | `optim/cost_aware.py` + turnover/tax aware band | none | stdlib | India-realistic rebalancing | #3, #8 |
| **M6** | `attribution/` + dashboard tabs + `portfolio_state.py` | none | stdlib | transparency, drift history | #6 |
| **M7** | `optim/black_litterman.py` + `views/ml_views.py` (GBM) | none | sklearn | views merged sanely | #4 |
| **M8** | `views/nlp_views.py` (lexical) + `views/llm_note.py` (GPT/Ollama) | `OPENAI_API_KEY` (opt) | `openai` (opt) | text as views + PM note | — |
| **M9** | DL (`risk/volatility.py` GARCH/HAR, LSTM cov forecast) + RL policy | none | `arch`/`torch` (opt) | frontier research | #2, #5 |
| **M10** | Pair-hedge (cointegration) + variance-min hedge + basis-risk | none | statsmodels (opt) | better than beta-only hedge | #7 |

Everything optional degrades to M1–M2 behaviour, so the desk is never blocked by
a missing key or a heavy dependency.

---

## 13. Immediate next step

Build **M1 + M2**:
1. `providers/openalgo_provider.py` — assemble the existing provider dict
   (`universe`, `vols`, `corr`, `betas`, `current_weights`, `capital`) from
   `client.history(...)` (weekly returns → EWMA vol & corr) and
   `client.positionbook()` / `client.funds()` for the live book and capital,
   wired via the existing `provider_ref` injection point
   (`pipeline._provider("portfolio")`).
2. `risk/covariance.py` — EWMA + Ledoit–Wolf shrinkage, replacing the hand-fed
   `corr` when available, with a **fallback to today's path** if numpy is absent.

No change to the `openalgo/` clone, no new credentials, and the allocator
immediately moves from *hand-fed heuristics* to *live-estimated risk*. Then M3
(true ERC/HRP) delivers the biggest single Sharpe/max-DD improvement — validate
it with `core/benchmark.py` walk-forward **after India costs** before trusting it.

---

*Cross-references:* `Blue_engine/core/base.py` (engine contract),
`Blue_engine/core/pipeline.py` (stage order + `provider_ref` injection),
`Blue_engine/core/results.py` (`risk_parity_weights`, `portfolio_vol`,
`correlation` — the pure helpers this builds on),
`Blue_engine/core/lot.py` (weights → integer lots at the POV boundary),
`Blue_engine/core/risk_gate.py` (`max_open_exposure` enforced on the final book),
`Blue_engine/core/ledger.py` (execution/P&L ledger, `api_key_hash`),
`Blue_engine/market/market_engine.py` (`leaders`, breadth → portfolio tilt),
`Blue_engine/economics/ECONOMIC_ENGINE_PLAN.md` (sibling playbook),
`Blue_engine/dashboard/pages/portfolio.py` (live OMS/EMS page to extend),
`Blue_engine/economics/run_history.py` (JSONL run-log pattern to mirror),
OpenAlgo [Python SDK](https://pypi.org/project/openalgo/) and
[API docs](https://docs.openalgo.in/) (consumed read-only via the SDK).