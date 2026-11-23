# Economic Engine — Build Plan & Quant Playbook

**Package:** `blue_engine/economics/`
**Stage:** `EngineStage.ECONOMIC` (value 10 — the *first* stage of the pipeline)
**Role in the flow:** `Economic -> Market -> Portfolio -> Alpha -> POV -> Infrastructure`
**Audience:** quant analyst / trader / PM / coder building the desk.
**Status today:** a working *pure scorer* (`economic_engine.py`) that turns 9 macro
signals into one `risk_on` score in `[-1, +1]` + a regime label. This document
plans the upgrade from a hand-fed scorer into a **data-fed, ML-augmented,
statistically-validated macro regime engine**.

---

## 0. What the engine is (and is not)

| It IS | It is NOT |
|---|---|
| A **top-down risk-tilt** stage: "what macro regime are we in, and how much gross risk should the desk carry?" | A signal that picks a stock. That is the Alpha engine's job. |
| The **first** stage — everything downstream (Market breadth, Portfolio weights, Alpha min-score) reads its output. | A live execution path. It never places orders. |
| **Pure by contract** (`Blue_engine/core/base.py`): no network, no SDK, no DB in `_run()`. Data arrives via an injected `provider` callable. | Allowed to import `openalgo` internals. It consumes data through a provider, never the platform. |

**Current output contract** (`EngineResult.data`):

```python
{
  "regime": "RISK_ON" | "MILD_RISK_ON" | "NEUTRAL" | "MILD_RISK_OFF" | "RISK_OFF" | "UNKNOWN",
  "guidance": "<one-line desk instruction>",
  "risk_on_score": -1.0 .. +1.0,
  "gross_exposure_multiplier": 0.25 .. 1.5,   # consumed by PortfolioEngine
  "signals": { "<name>": {"label","value","previous","direction","weight","score"} },
  "signals_available": int,
}
```

The `gross_exposure_multiplier` is the single most important number this engine
produces — `portfolio_engine.py` multiplies book gross by it.

---

## 1. The nine current signals (baseline)

From `DEFAULT_SIGNALS` in `economic_engine.py`. `direction=+1` means a *higher*
reading is risk-**ON**.

| # | Signal | Direction | Default weight | Unit / scale | India source |
|---|--------|-----------|----------------|--------------|--------------|
| 1 | `gdp_growth` | +1 | 1.5 | % YoY (scale 5) | MoSPI advance estimates / RBI |
| 2 | `bond_return` | −1 | 1.0 | 10Y G-Sec % (scale 7) | FBIL / RBI (also tradable via NSE) |
| 3 | `debt` | −1 | 1.0 | Debt/GDP % (scale 90) | RBI / MoF |
| 4 | `employment` | −1 | 1.2 | Unemployment % (scale 8) | CMIE / PLFS |
| 5 | `inflation` | −1 | 1.5 | CPI % YoY (scale 6) | MoSPI CPI |
| 6 | `climate` | −1 | 0.5 | Climate-stress index (scale 100) | IMD monsoon, GSOD |
| 7 | `tax` | −1 | 0.8 | Effective tax % (scale 30) | Budget docs |
| 8 | `currency` | −1 | 0.5 | USD/INR (scale 85) | RBI reference / FX |
| 9 | `forex_reserves` | +1 | 1.0 | Reserves $bn (scale 700) | RBI weekly |

Normalisation: `score = tanh(value / scale)`, then `signed = direction * score`,
then a **weighted mean** over available signals → composite.

**Weaknesses a quant should flag today:**

1. **Level-only.** Uses the *level*, not the *surprise*. Markets price the
   **delta vs. expectation**, not the level. A 6% CPI that beats a 6.5% forecast
   is risk-**on**; the engine scores it risk-off.
2. **Static weights.** Weights are hand-set, equal-ish, and never fitted.
3. **No volatility regime.** Geometric breakdowns (2020, Mar-2025-style VIX
   spikes) are *not* captured by macro levels alone.
4. **No regime persistence / Markov.** A single print flips the label
   immediately; real regimes are autocorrelated (sticky).
5. **No confidence.** `signals_available` is the only quality hint; there is no
   posterior probability per regime.

---

## 2. What a quant analyst actually analyses (the "full stack" checklist)

This is the menu. Everything in sections 3–8 implements a slice of it.

### 2.1 Macro / top-down
- **Growth:** GDP nowcast, IIP, PMI (Mfg & Services), GST e-way bills, credit growth
- **Inflation:** CPI (food vs core), WPI, imported inflation via USD/INR
- **Rates:** repo, 10Y G-Sec, term spread (10Y−91d), credit spread (AAA−G-Sec)
- **Liquidity:** RBI net LAF, systemic liquidity, banking credit-deposit
- **External:** FX reserves, current-account, FPIs (FII) cash + F&O, DXY
- **Fiscal:** deficit/GDP, borrowing calendar, effective tax

### 2.2 Market / cross-asset
- **Breadth:** advance/decline, % above 50-DMA, new highs−lows
- **Volatility:** India VIX level + term-structure slope, realised vs implied
- **Flows:** FII/DII cash, FII index-futures net, put-call ratio, OI buildup
- **Cross-asset:** Gold, Crude (India is a big importer), USD/INR, US10Y, DXY
- **Seasonality:** expiry-week effects, budget/monsoon seasonality

### 2.3 Statistical / probability
- **Stationarity:** ADF/KPSS before any regression (macro levels are non-stationary)
- **Leading vs coincident:** Granger causality, cross-correlation at lags
- **Regime models:** 2–3 state Hidden Markov Model (HMM), Markov-switching AR
- **Change-point / CUSUM** for structural breaks (regime shifts)
- **Factor analysis / PCA** to de-collinearise the macro block
- **Bayesian updating** of regime probabilities as new prints arrive
- **EV / Kelly** position sizing from the regime probability, not the level

### 2.4 ML / AI / NLP
- **Supervised:** logistic regression → gradient boosting → to forecast
  *next-20d NIFTY return sign* from macro features
- **Time-series DL:** LSTM / Temporal Fusion Transformer for macro nowcasting
- **Unsupervised:** clustering macro states (k-means/HMM) → map to regimes
- **NLP:** RBI policy-statement sentiment, budget speech tone, news-flow tone
- **LLM (GPT):** summarise RBI minutes into a structured hawkish/dovish score,
  extract event-risk calendar (budget, MPC, Fed, expiry)
- **Reinforcement learning:** regime-conditional exposure policy (advanced)

---

## 3. Free / open data sources (India-first) — credentials & safety

> **Security rule (from AGENTS.md):** *never* hardcode keys. Resolve from the
> environment first, then `_restored/.env`. This engine follows the same rule
> as `core/client.py`.

### 3.1 No-key sources (start here — zero credential risk)

| Source | What | How | Cost |
|---|---|---|---|
| **OpenAlgo SDK** (`client.history`, `quotes`, `depth`, `multiquotes`) | OHLCV for indices/equities/commodities/FX, LTP, depth | Already wired via `core/client.py` → the platform handles the broker session | Free (your platform) |
| **RBI DBIE** | Repo, G-Sec, FX reserves, CPI, credit | CSV/web scrape (`rbi.org.in`) | Free, no key |
| **MoSPI / data.gov.in** | CPI, IIP, GDP | `api.data.gov.in` (has a free key) | Free key |
| **FBIL** | G-Sec yield curve, USD/INR reference | `fbil.org.in` daily | Free |
| **NSE India** | FII/DII flows, PCR, VIX, breadth | `nseindia.com` JSON endpoints (session-cookie) | Free, no key |
| **MoSPI, IMD, CMIE (PLFS)** | Unemployment, monsoon | Web/PDF | Free |

### 3.2 Free-key APIs (register once; store in `_restored/.env`)

| API | Env var name (suggested) | Free tier | Use |
|---|---|---|---|
| **data.gov.in** | `DATAGOVIN_API_KEY` | Free, generous | CPI, IIP, official macro series |
| **Alpha Vantage** | `ALPHAVANTAGE_API_KEY` | 25 req/day free | US10Y, DXY, cross-asset |
| **FRED (St.)** | `FRED_API_KEY` | Free, unlimited-ish | US10Y, DXY, global risk |
| **NewsAPI / GNews** | `NEWSAPI_KEY` | 100 req/day free | headlines for NLP sentiment |
| **OpenAI / local LLM** | `OPENAI_API_KEY` | paid, or Ollama = local free | GPT-based text scoring (§8) |

**Storage pattern** (mirror `core/client.py`):
```python
def _key(name: str) -> str | None:
    return os.getenv(name)          # env wins
    # else: read from my-trading-system/_restored/.env (single source of truth)
```
Never commit `.env`. `.gitignore` already excludes it. Only *names* of env vars
appear in docs and code.

---

## 4. Target architecture (why it stays upgrade-proof)

The `openalgo/` clone stays **pristine**. The engine talks *only* through the
public SDK (`client.history`, `client.quotes`, ...) which is version-stable. So
the upgrade path is unchanged: `git pull` the clone, and the engine keeps working
because it never imported OpenAlgo internals.

```
┌─────────────────────────────────────────────────────────────┐
│ blue_engine/economics/                                        │
│                                                               │
│  providers/            ← NEW. Adapters (the ONLY I/O here).   │
│    base.py               MacroProvider ABC + caching +        │
│                          staleness + fuse (fail-soft).        │
│    openalgo_provider.py  FX, commodities, index-level macro   │
│                          from the SDK (no extra keys).        │
│    india_provider.py     RBI/FBIL/MoSPI/NSE public JSON.      │
│    fii_provider.py       FII/DII flows, PCR, breadth.         │
│    composite.py          Merge many providers → one macro dict│
│                                                               │
│  features/             ← NEW. Pure transforms (no I/O).       │
│    delevel.py            z-score of delta vs rolling mean      │
│    surprise.py           value vs consensus/AR(1) forecast     │
│    pca.py                de-collinearise the macro block       │
│    stationarity.py       ADF / KPSS gates                      │
│                                                               │
│  models/               ← NEW. Pure models (no I/O).           │
│    hmm_regime.py         2–3 state GaussHMM → P(regime)       │
│    markov_switch.py      Markov-switching AR                   │
│    classifier.py         LogReg / GBM: sign of 20d NIFTY ret   │
│    cluster.py            k-means macro states                  │
│                                                               │
│  nlp/                  ← NEW. Text → scores (no I/O).          │
│    sentiment.py          lexical (VADER-style) first           │
│    llm_scorer.py         GPT / local LLM (opt-in, cached)      │
│    rbi_minutes.py        hawkish/dovish score from RBI text    │
│    event_calendar.py     budget / MPC / Fed / expiry dates      │
│                                                               │
│  scoring.py            ← refactor of the tanh scorer (pure).   │
│  regime.py             ← banding + probability → multiplier.   │
│  economic_engine.py    ← thin orchestrator: calls the above.   │
└─────────────────────────────────────────────────────────────┘
```

**Golden rules**
1. `economic_engine.py` stays **pure**; only `providers/` touches the network.
2. Every model is **dependency-optional** — if `numpy`/`hmmlearn`/`openai` is
   absent, the engine degrades to the current tanh scorer (never crashes). This
   is the same "degrade, never halt" rule the Infrastructure engine uses.
3. Engines communicate only via `EngineResult.data` (dicts). No shared globals.

---

## 5. Methods & models — concrete catalogue

### 5.1 Statistical / probability (implement first — highest ROI, no keys)

| Method | What it answers | Output | Library |
|---|---|---|---|
| **z-score of change** | Is this print unusual *for this series*? | `z` per signal | stdlib (§ `results.zscore`) |
| **AR(1) surprise** | Level vs its own trend forecast | `surprise` | stdlib/numpy |
| **HMM (GaussianHMM)** | Probability of being in risk-on vs risk-off state | `P(state)` vector | `hmmlearn` |
| **Markov-switching AR** | Regime-dependent mean/vol of returns | `regime_params` | `statsmodels` |
| **ADF / KPSS** | Is the series stationary (gate for regression)? | `p_value` | `statsmodels` |
| **Granger causality** | Does macro X lead NIFTY? | `p_value` | `statsmodels` |
| **PCA / factor** | Collapse 9 collinear signals → 2–3 factors | `loadings` | `numpy` |
| **Kelly / EV sizing** | Optimal gross from `P(regime)` | `exposure` | stdlib |

### 5.2 Machine learning

| Model | Target | Why |
|---|---|---|
| **Logistic regression** | sign of next 20d NIFTY return | baseline, interpretable coefficients |
| **Gradient boosting (XGBoost/LightGBM)** | same | non-linear, handles mixed scales |
| **Random Forest** | regime label (supervised) | feature importances for the desk |
| **k-means / GMM** | unsupervised macro states | label-free regime discovery |
| **Walk-forward CV** | validate *any* of the above | avoids look-ahead bias (§9) |

### 5.3 Deep learning (advanced, optional)

| Model | Use | Library |
|---|---|---|
| **LSTM / GRU** | macro nowcast / return forecaster | `torch` / `tensorflow` |
| **Temporal Fusion Transformer** | multi-horizon macro forecasting | `pytorch-forecasting` |
| **Autoencoder** | anomaly detection on the macro block | `torch` |
| **RL (PPO/DQN)** | regime-conditional exposure policy | `stable-baselines3` |

### 5.4 NLP / GPT

| Method | Input | Output |
|---|---|---|
| **Lexical sentiment** | headlines | `tone ∈ [-1,1]` |
| **FinBERT / VADER** | news + filings | signed sentiment |
| **LLM structured extract** | RBI minutes, budget speech | `{hawkish: -1..1, inflation_focus: .., guidance: "..."}` |
| **Event extraction** | news | trading-calendar of known events |

---

## 6. Example: regime as a probability, not a label (worked case study)

**Scenario.** CPI prints 5.1% vs a consensus of 5.6%; RBI holds; 10Y G-Sec
eases 8 bps; FII net-buy ₹1,450 cr.

| Engine version | CPI handling | Result |
|---|---|---|
| **Today (level)** | 5.1% → `tanh(5.1/6)` → risk-off contribution | drags composite *down* even though the print **beat** consensus |
| **Upgraded (surprise)** | `surprise = 5.1 − 5.6 = −0.5%` → *below* forecast → **risk-on** | flips the sign correctly; HMM assigns `P(RISK_ON)=0.72` |

**Concrete maths (upgraded):**
```
surprise_cpi   = actual - forecast            = -0.50
z_cpi          = surprise / rolling_sd        = -0.50 / 0.25 = -2.0
score_cpi      = tanh(-2.0 * direction=-1)    = tanh(+2.0)  = +0.964   # risk-ON
10Y delta      = -8 bps -> z = -1.1 -> score  = +0.80
fii_score      = 1450/1000 -> clamp           = +1.0
composite      = weighted mean(...)           = +0.61  -> RISK_ON
P(RISK_ON)     = HMM posterior                = 0.72
gross_mult     = 1 + 0.61                     = 1.45 (capped 1.5)
```

Desk instruction: raise gross toward 1.45×, favour high-beta (Banks, Realty),
keep a hedge on the index-beating fast-money signal. **This is the jump from a
scorer to a quant engine.**

---

## 7. Case study: how the regime multiplier flows to P&L

The `gross_exposure_multiplier` is not cosmetic — it scales every downstream
decision. Trace:

```
Economic.gross_exposure_multiplier = 1.45
      ↓
Portfolio._run():  gross = clamp(econ_mult, ...) ; weights scaled
      ↓
Alpha.min_score  can be relaxed (more names qualify in a risk-on regime)
      ↓
POV children per poll = delta_volume * participation_pct/100  (unchanged)
      ↓
Ledger / risk_gate: max_open_exposure enforced on the enlarged book
```

Backtest intuition for the desk: a regime overlay that only toggles gross
between 0.5× and 1.5× on an NIFTY strategy historically captures most of the
drawdown-avoidance benefit of "going to cash" while staying ~fully invested in
uptrends. **Validate with walk-forward, not a single fit (§9).**

---

## 8. NLP + GPT integration — design (opt-in, safe)

**Principle:** text scoring is *additive and optional*. If no key is present, the
engine uses the lexical fallback and logs a warning — the pipeline still runs.

```
Headlines ──► nlp/sentiment.py (lexical, always-on, no key)
                 │
RBI minutes ─► nlp/llm_scorer.py ──► if OPENAI_API_KEY or local Ollama:
                 │                       extract structured JSON
                 │                    else: skip, warn
                 ▼
         nlp_signal ∈ [-1, +1]  ──► added as signal #10 "news_tone"
```

**LLM prompt contract (return JSON only — never free text we must parse):**
```json
{"hawkish": -0.4, "inflation_focus": 0.8, "growth_focus": 0.3,
 "guidance": "neutral-to-dovish", "confidence": 0.7}
```
Cache every LLM response by hash of the input text (`data/` folder), so we never
pay twice for the same document and results are reproducible.

**Security:** the key is read from env/`_restored/.env`; prompts contain **no
account numbers, no positions, no PII** — only public central-bank text.

---

## 9. Testing, validation & anti-look-ahead (non-negotiable)

| Layer | Test | Tool |
|---|---|---|
| Unit | each signal scorer maps known inputs → known scores | `pytest` |
| Unit | `_regime()` bands at exact cut-points (±0.35, ±0.10) | `pytest` |
| Contract | `EconomicEngine.run()` never raises; returns `EngineResult` | `pytest` |
| Degradation | no macro block → `regime=UNKNOWN`, `mult=1.0`, warning | `pytest` |
| Provider | fuse: N failed fetches → falls back to config macro | `pytest` + mock |
| Model | **walk-forward CV only** (train t0..t1, test t1..t2, roll) | custom |
| Model | no target leakage: features at time *t*, label from *t+20* | review |
| Statistical | ADF gate before any regression feature | `statsmodels` |
| Backtest | regime overlay vs buy-hold: Sharpe, max-DD, hit-rate | `core/benchmark.py` |

**Look-ahead traps to avoid explicitly:**
- CPI is *released* on a date later than the period it describes → stamp each
  observation with its **release date**, not its reference month.
- Survivorship in the index universe → use point-in-time constituents.
- Re-fitting weights on the full sample → always walk-forward.

---

## 10. Observability: logging, debugging, performance

- **Logging:** every signal prints `name=value score=±x`; the composite line is
  already there (`regime=... risk_on_score=... gross_mult=...`). Add a structured
  JSON log line per run to `data/economic_runs/YYYY-MM-DD.jsonl` for forensics.
- **Debugging:** `--explain` mode dumps per-signal contributions so an operator
  can see *why* the regime flipped. Because the engine is pure, a bug reproduces
  from a saved macro dict alone — no network needed.
- **Performance:** the 9-signal tanh path is O(9) and sub-millisecond. HMM/GBM
  are the expensive parts → cache fitted models to `data/models/` and refit only
  on a schedule (weekly), not per tick. Provider calls are TTL-cached (macro
  prints are daily/weekly; **never** poll RBI per tick).
- **File organisation:** one concern per file (§4 tree). Providers = I/O;
  features/models/nlp = pure; engine = orchestration.

---

## 11. Recommended build order (milestones)

| M | Deliverable | Keys needed | Deps | Value |
|---|---|---|---|---|
| **M1** | `providers/` + `composite.py`; SDK-based FX/commodity/index macro without any extra key | none | stdlib | live data into the existing scorer |
| **M2** | `features/surprise.py` + `delevel.py` (change vs level) | none | stdlib | fixes weakness #1 — biggest accuracy jump |
| **M3** | `models/hmm_regime.py` → `P(regime)` + probability-weighted multiplier | none | `hmmlearn`, `numpy` | fixes #4, adds confidence |
| **M4** | India public providers (RBI, MoSPI, FBIL, NSE flows) | `DATAGOVIN_API_KEY` (opt) | `requests` | fills all 9 signals from live sources |
| **M5** | `models/classifier.py` (walk-forward GBM) + benchmark report | none | `scikit-learn`, `pandas` | empirically fitted weights |
| **M6** | `nlp/` lexical sentiment + event calendar | none | stdlib | news as signal #10 |
| **M7** | `nlp/llm_scorer.py` (GPT / local Ollama) | `OPENAI_API_KEY` (opt) | `openai` (opt) | structured central-bank scoring |
| **M8** | Deep learning nowcaster + RL exposure policy | none | `torch` (opt) | frontier research |

Everything optional degrades to M1–M2 behaviour, so the desk is never blocked by
a missing key or a heavy dependency.

---

## 12. Immediate next step

Build **M1 + M2**: add `providers/` (OpenAlgo-SDK-backed, key-free) and the
surprise/de-level feature transforms, wired into `economic_engine.py` via the
existing `provider` injection point (`pipeline._provider("economic")`). No
change to the clone, no new credentials, and the risk-on score immediately
becomes a *change-aware* quant signal instead of a level reader.

---

*Cross-references:* `Blue_engine/core/base.py` (engine contract),
`Blue_engine/core/pipeline.py` (stage order + provider injection),
`Blue_engine/portfolio/portfolio_engine.py` (consumes the multiplier),
`openalgo/` (pristine clone — SDK consumed read-only),
OpenAlgo [History API](https://docs.openalgo.in/api-documentation/v1/data-api/history).