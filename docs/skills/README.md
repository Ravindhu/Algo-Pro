# Blue Engine — Agentic Frontline Skills

One **small skill per pipeline stage**. Each skill is a contract, not an engine:
it says what a stage may *conclude*, what evidence it needs, and where its JSON
answer is published. The stage's own logic code — Economic, Market, Sector,
Portfolio, Risk, Screener, Backtest, Optimizer — remains deterministic.

## The three rules (non-negotiable)

1. **Agents write JSON only.** `core/skill_writer.py` refuses every extension
   except `.json`, every path outside `data/agent_stage/`, and every protected
   config file (`macro_master.json`, `strategy.active.json`). There is no code
   write path.
2. **Execution is logic-only.** `pov/`, `core/risk_gate.py`, `core/risk_engine.py`
   and `core/orchestrator.py` do not import the LLM layer, the skills, or the
   writer. A verified-never-import test enforces this.
3. **No caps on judgement, but capital clamps stay in code.** A skill may veto,
   blacklist, or demand more data for any reason. It may not widen a live limit
   — that clamp lives in `frontline_agent._CAPS` / `_escalate` and in
   `RiskEngine` / `RiskGate`, neither of which a skill can reach.

## The data gate

`core/stage_gate.py` is deterministic Python, not an LLM. It answers one
question per stage: *is the evidence here?* With
`engines.stage_gate.enabled = true` and `on_block = "halt_analysis"`, a stage
whose requirements fail stops the analytic chain; `risk`, `infrastructure` and
the POV block still run so the report is complete. Blocked stages are listed in
`report["gated"]` with the exact missing paths in `report["stage_gate"]`.

Configure per stage under `engines.stage_gate.stages`:

```json
"economic": [
  {"path": "macro.policy_rate", "check": "range", "min_value": -5, "max_value": 30},
  {"path": "macro.as_of", "check": "present", "timestamp_path": "macro.as_of",
   "max_age_seconds": 86400}
]
```

Checks: `present`, `min_rows`, `min_count`, `range`, `one_of`, `enum_keys`.
An unevaluable requirement is a **block**, never a pass.

## When a stage needs more data

A skill answer may carry `data_requests`:

```json
{"needs": ["cpi_history_12m", "repo_rate_curve"], "reason": "regime call needs a trend"}
```

The pipeline folds that into `report["data_requests"][stage]` so the data layer
(`database/`, providers) can supply it on the next cycle. It never changes an
order — it fills a gap.

## Skill index

| Skill | Stage | Publishes | See |
|---|---|---|---|
| `frontline.economic` | Economic | `economic.json` | [economic/SKILL.md](economic/SKILL.md) |
| `frontline.market` | Market | `market.json` | [market/SKILL.md](market/SKILL.md) |
| `frontline.screener` | Screener | `screener.json` | [screener/SKILL.md](screener/SKILL.md) |
| `frontline.valuation` | Valuation | `valuation.json` | [valuation/SKILL.md](valuation/SKILL.md) |
| `frontline.sector` | Sector | `sector.json` | [sector/SKILL.md](sector/SKILL.md) |
| `frontline.portfolio` | Portfolio | `portfolio.json` | [portfolio/SKILL.md](portfolio/SKILL.md) |
| `frontline.alpha` | Alpha | `alpha.json` | [alpha/SKILL.md](alpha/SKILL.md) |
| `frontline.backtest` | Backtest | `backtest.json` | [backtest/SKILL.md](backtest/SKILL.md) |
| `frontline.optimization` | Optimization | `optimization.json` | [optimization/SKILL.md](optimization/SKILL.md) |
| `frontline.risk` | Risk | `risk.json` | [risk/SKILL.md](risk/SKILL.md) |

## Code map

| Concern | Module |
|---|---|
| Skill definitions + config override | `core/stage_skills.py` |
| Data-sufficiency gate | `core/stage_gate.py` |
| JSON-only sandbox writer + audit | `core/skill_writer.py` |
| Read-only agent data toolbelt | `core/data_tools.py` |
| Automatic self-correction loop | `core/self_correction.py` |
| Team roster + shift ledger (factory line) | `core/agent_team.py` |
| Dashboard handoff (last shift) | `dashboard/state.py` |
| Agent Line dashboard page | `dashboard/pages/agent_line.py` |
| LLM transport + `_CAPS` / `_escalate` / `_validate` | `core/frontline_agent.py`, `core/llm/` |
| Chain wiring | `core/pipeline.py` |
| Tests | `tests/test_stage_gate.py`, `tests/test_skill_writer.py`, `tests/test_stage_skills.py`, `tests/test_data_tools.py`, `tests/test_self_correction.py`, `tests/test_agent_team.py`, `tests/test_agent_line.py`, `tests/test_pipeline_stage_integration.py` |

## The factory line
Each stage is a **station**. One small skill agent is posted at it; the
**machine** is the deterministic engine. The agent supplies raw material and
flags what is missing - it never operates the machine.

```
economic -> market -> screener -> valuation -> portfolio -> alpha -> risk
                     (research stations: backtest, optimization)
```

Rules the line enforces (`core/agent_team.py`):

- **One product**: a single risk-checked trading decision, built station by
  station.
- **Handoff contract**: a station passes the part on only when the machine
  reports `ok` *and* the data gate is satisfied. Missing data stops the line.
- **No blame-shifting**: stations behind a stop are `skipped`, not `blocked`.
- **Idle is not failure**: no agent opinion -> `idle`; the machine runs on its
  deterministic defaults.
- **Material requests a blocked station names the exact missing paths, so the
  next shift starts with a shopping list.

The shift ledger is published to `data/last_shift.json` and rendered on the
**Agent Line** dashboard page (`/agent-line`).

## The self-correction loop
`core/self_correction.py` closes the gap between "the model answered" and "the
answer is usable":

1. the model answers;
2. a **deterministic judge** (schema validator + data gate) decides;
3. only a real failure produces a correction prompt, built from whitelisted
   fragments (key names, allowed ranges, missing paths);
4. a repeated identical failure stops the loop - no burning calls on the same
   mistake;
5. a *clamped* answer is a **success** (the rails already bounded it).

Bounds: `max_attempts` clamped 1..3, sharing the one agentic call budget for
the run. Transport failures (`disabled`, `quota`, `llm_failed`) are never
retried.

## Operating

```powershell
cd C:\Users\Admin\CodeGPT\my-trading-system
.\.venv\Scripts\python.exe -m pytest Blue_engine\tests\test_stage_gate.py Blue_engine\tests\test_skill_writer.py Blue_engine\tests\test_stage_skills.py -q
```

Skills are **disabled by default** (`engines.agent.enabled = false`) and the
gate is **disabled by default** (`engines.stage_gate.enabled = false`), so
enabling either is an explicit operator decision — paper-run first.
