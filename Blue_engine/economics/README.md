# Economic Engine — Operator & Developer Guide

**Package:** `blue_engine/economics/`  ·  **Stage:** `ECONOMIC` (first in the pipeline)
**Flow:** `Economic → Market → Portfolio → Alpha → POV → Infrastructure`

The economic engine answers one question for the whole desk:

> *What macro regime are we in, and how much gross risk should the book carry?*

It emits a `gross_exposure_multiplier` that the Portfolio engine multiplies the
whole book by. Everything downstream reads its output.

This guide is the *how-to*; the strategy rationale and research catalogue live in
[`ECONOMIC_ENGINE_PLAN.md`](ECONOMIC_ENGINE_PLAN.md).

---

## 1. File map (one concern per file)

```
economics/
├─ economic_engine.py      Orchestrator. PURE (no I/O). Thin: wires the pieces.
├─ scoring.py              Three-view signal blend (level / delevel / surprise).
├─ regime.py               Banding + gross-exposure multiplier (score & posterior).
├─ ECONOMIC_ENGINE_PLAN.md Research playbook (the "why").
├─ README.md               This file (the "how").
│
├─ providers/              THE ONLY I/O. Data adapters.
│   ├─ base.py             MacroProvider ABC + CachedProvider (TTL + disk cache).
│   ├─ secrets.py          Env-first credential resolver (never hardcoded).
│   ├─ openalgo_provider.py SDK-based FX/commodities/indices (no extra key).
│   ├─ india_provider.py   RBI / data.gov.in official prints (free key opt.).
│   ├─ fii_provider.py     FII/DII flows, PCR, India VIX (NSE public).
│   └─ composite.py        Merge many providers → one macro dict (priority).
│
├─ features/               PURE transforms.
│   ├─ surprise.py         Value vs expectation (consensus or AR(1)).
│   ├─ delevel.py          z-score of change vs rolling baseline.
│   ├─ pca.py              De-collinearise the macro block (numpy optional).
│   └─ stationarity.py     ADF / KPSS / Granger gates (statsmodels optional).
│
├─ models/                 PURE models.
│   ├─ hmm_regime.py       Gaussian HMM → P(RISK_ON) (numpy-only implementation).
│   ├─ cluster.py          k-means macro states (numpy-only implementation).
│   ├─ classifier.py       Macro→market classifier + walk-forward CV (sklearn opt.).
│   └─ dl_nowcast.py       Deep-learning nowcast interface (torch opt., stub).
│
└─ nlp/                    Text → scores.
    ├─ sentiment.py        Lexical tone (always on, no key).
    ├─ llm_scorer.py       GPT / local-LLM structured scoring (opt-in, cached).
    ├─ rbi_minutes.py      RBI text → risk-on tone.
    └─ event_calendar.py   Scheduled event risk (MPC / budget / expiry).
```

**Golden rules**

1. `economic_engine.py` is pure. Only `providers/` touches the network.
2. Every model/feature is optional — missing a library degrades to the baseline
   tanh scorer, never a crash.
3. Engines communicate only via `EngineResult.data`. No shared globals.
4. The `openalgo/` clone stays pristine; the SDK is consumed read-only.

---

## 2. Output contract (`EngineResult.data`)

```python
{
  "regime": "RISK_ON" | "MILD_RISK_ON" | "NEUTRAL" | "MILD_RISK_OFF" | "RISK_OFF" | "UNKNOWN",
  "guidance": "<one-line desk instruction>",
  "risk_on_score": -1.0 .. +1.0,
  "gross_exposure_multiplier": 0.25 .. 1.5,        # consumed by Portfolio
  "multiplier_detail": {"multiplier", "path", "score_multiplier",
                        "posterior_multiplier", "model_weight"},
  "regime_model": {"method": "hmm"|"none"|..., "p_risk_on", "probabilities", ...},
  "signals": { "<name>": {"score", "views", "views_used", "value", ...} },
  "signals_available": int,
  "nlp": {...} | None,
  "event": {...} | None,
  "source_meta": {...} | None,      # provider provenance / staleness / warnings
}
```

---

## 3. Config knobs (all opt-in; defaults reproduce the baseline)

Set inside `input/macro_master.json` → `engines.economic`:

| Key | Default | Effect |
|---|---|---|
| `use_surprise` | `true` | Blend the value-vs-expectation view. |
| `use_delevel` | `true` | Blend the change-vs-trend view. |
| `blend` | `{level:0.35, delevel:0.25, surprise:0.40}` | View weights. |
| `regime_model` | `"none"` | `"none"` / `"hmm"` / `"cluster"` — sticky posterior. |
| `model_weight` | `0.0` | 0 = pure score path; 1 = pure model posterior. |
| `use_nlp` | `true` | Read `context['headlines']` / `context['rbi_text']`. |
| `event_trim` | `0.0` | 0..1 — how hard to trim gross near events. |
| `event_window_days` | `10` | Look-ahead window for the event calendar. |
| `signals.<name>` | — | Per-signal `direction` / `weight` / `scale` / `blend`. |

To use the sticky-HMM posterior at half weight:

```json
"economic": {
  "macro": { "...": "..." },
  "regime_model": "hmm",
  "model_weight": 0.5
}
```

Then pass a composite-score history in the run context:

```python
engine.run(context={"economic_history": [scores...], "headlines": [...]})
```

---

## 4. Wiring live data (production)

The engine is pure; a **provider** brings the data. Build the default stack and
inject it (the pipeline supports this via `provider_ref`):

```python
from blue_engine.core.client import init_client
from blue_engine.economics.providers import build_default_stack

client = init_client()                     # SDK (env / _restored/.env key)
provider = build_default_stack(client)     # FII → SDK proxies → India prints
macro = provider.fetch()                   # one dict, warnings merged
```

`{"engines": {"economic": {"provider_ref": "my_pkg.adapters:econ_provider"}}}`
in the JSON wires it without editing engine code.

---

## 5. Credentials (safe by construction)

Resolution order is fixed and centralised in `providers/secrets.py`:

1. process environment (`os.environ`)
2. `<repo>/_restored/.env`  *(single source of truth)*
3. `<repo>/.env`

- **Never** hardcode a key. Only the env-var *name* appears in code.
- `redact("OPENAI_API_KEY", key)` shows only the last 2 chars — safe to log.
- `.env` is git-ignored; secrets never enter a report.

| Env var | Needed for | Free? |
|---|---|---|
| `OPENALGO_API_KEY` | SDK provider (already required) | yes |
| `DATAGOVIN_API_KEY` | official CPI/IIP/GDP | free key |
| `OPENAI_API_KEY` *or* `OLLAMA_HOST` | LLM text scoring (M7) | paid / local free |

Verify what's loaded without leaking it:

```python
from blue_engine.economics.providers import has_secret, redact, get_secret
print(redact("OPENAI_API_KEY", get_secret("OPENAI_API_KEY")))
```

---

## 6. Examples

### 6a. The headline case study — surprise flips the sign

```python
from blue_engine.economics import EconomicEngine

# CPI prints 5.1% vs a 5.6% consensus -> the print BEAT expectations.
macro = {
  "inflation": {"value": 5.1, "previous": 4.8, "forecast": 5.6},
  "gdp_growth": 6.8, "bond_return": {"value": 6.9, "previous": 7.1},
  "debt": 82.0, "employment": 7.6, "climate": 42.0, "tax": 27.0,
  "currency": 84.5, "forex_reserves": 690.0,
}
r = EconomicEngine({"macro": macro}).run()
print(r.data["signals"]["inflation"]["views"])
# {'level': -0.69, 'delevel': -0.29, 'surprise': +0.46}
#  ^ the surprise view is POSITIVE -> risk-ON, the whole point.
```

*Before* this engine, the level alone (`tanh(5.1/6)`) scored CPI risk-OFF.

### 6b. Sticky regime with a posterior

```python
e = EconomicEngine({"macro": macro, "regime_model": "hmm", "model_weight": 0.5})
hist = [-0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7] * 2
r = e.run(context={"economic_history": hist})
print(r.data["regime_model"]["p_risk_on"], r.data["gross_exposure_multiplier"])
```

### 6c. News tone

```python
r = EconomicEngine({"macro": macro}).run(
    context={"headlines": ["Sensex rallies on FII inflow", "RBI dovish"]})
print(r.data["signals"]["news_tone"])       # positive tone
```

---

## 7. Testing

```powershell
# via pytest (preferred)
& ..\openalgo\.venv\Scripts\python.exe -m pytest tests -q

# or standalone (no pytest needed)
& ..\openalgo\.venv\Scripts\python.exe tests\test_economic_engine.py
```

28 tests cover: engine contract (never raises), empty/missing degradation, the
CPI-surprise case study, regime cut-points, posterior sizing, HMM/cluster,
walk-forward labels, NLP polarity & negation, provider fuse/cache/priority, the
SDK provider with a **fake client**, secret redaction, and the full pipeline
offline. **No test touches the network or a broker.**

---

## 8. Debugging & observability

- Every run logs one line: `regime=... risk_on_score=... gross_mult=... p_risk_on=... model=...`
- Per-signal `views` show exactly which of level/delevel/surprise moved the score.
- `source_meta` carries provider provenance, cache hit/miss and staleness.
- Because the engine is pure, a bug reproduces from a saved macro dict alone —
  no network required.

---

## 9. Performance notes

- 9-signal blend is O(9), sub-millisecond.
- HMM/cluster are the cost centres → pass a *short* history; refit off the hot
  path (per day/week), not per tick.
- Provider calls are TTL-cached (flows 5 min, SDK 15 min, official prints 6 h)
  with a filesystem fallback under `data/macro_cache/`.
- LLM scores are cached by SHA-256 of the input under `data/llm_cache/`.

---

## 10. Status vs the milestone plan

| M | Deliverable | Status |
|---|---|---|
| M1 | providers/ + composite (SDK, no extra key) | ✅ |
| M2 | features/ surprise + delevel | ✅ |
| M3 | models/ HMM posterior + cluster | ✅ |
| M4 | India public + FII providers | ✅ |
| M5 | walk-forward classifier + labels | ✅ |
| M6 | nlp/ lexical sentiment + event calendar | ✅ |
| M7 | nlp/ llm_scorer (opt-in GPT / Ollama) | ✅ |
| M8 | dl_nowcast interface (torch opt.) | ✅ (stub by design) |

Everything optional degrades to M1–M2 behaviour, so the desk is never blocked by
a missing key or a heavy dependency.