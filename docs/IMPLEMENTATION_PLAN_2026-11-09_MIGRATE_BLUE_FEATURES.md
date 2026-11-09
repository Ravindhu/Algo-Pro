# Implementation plan: move Blue-only features into src/quant (flow v5)

Date: 2026-11-09
Status: PLAN ONLY. No code has been moved in this step.
Parent plan: docs/DECOUPLING_PLAN.md (stage 6). Flow reference: docs/quant_system_flow_v5.md and quant_system_flow_v5.svg.
Branch: master (see docs/BRANCHES.md).

## 1. Goal

Move the five Blue-only features into `src/quant/`, wrapped to the flow v5 blocks. When done, `src/quant/` does not import `Blue_engine/`, and `Blue_engine/` can move to `Temp/archive/` (stage 5).

## 2. Scope

| # | Feature | Blue location | Rough size | Flow v5 block | Target package | Status |
|---|---|---|---|---|---|---|
| 1 | Screener | `Blue_engine/screener/` | about 300 KB Python (`valuations.py` 92 KB, `screener_engine.py` 46 KB, `filters.py` 17 KB, `yf_fallback.py` 17 KB, `annual_reports.py` 11 KB, plus client and vendor code) | 2.2 Market inputs (filings), 2.4 Liquidity screener | `src/quant/data/screener/` (rules already in `data/screen_rules.py`) | Rules DONE (6.1). Client, parsing, valuations, yfinance fallback PENDING |
| 2 | Economics | `Blue_engine/economics/` | about 60 KB (`economic_engine.py` 24 KB, `scoring.py`, `regime.py`, `run_history.py`, `features/`, `models/`, `nlp/`, `providers/`) | No exact block. Nearest: 2.12 Global cues, 2.3 Regime detector | `src/quant/data/economics/` | PENDING. Needs owner mapping (O1) |
| 3 | Agent pipeline | `Blue_engine/core/` (agent_team, pipeline, frontline_agent, agent_memory, llm/, stage_skills, self_correction, data_tools, paper_shift, risk_engine) plus `ml/models/rl.py`, `portfolio/views/llm_note.py`, `economics/nlp/llm_scorer.py` | Not yet sized. Spread over many files | 2.4 Event scoring (advice only), 2.11 Improvement loop | `src/quant/feedback/` (advice only), `src/quant/data/` (NLP) | PENDING. Highest risk (see section 5) |
| 4 | POV engine loop | `Blue_engine/pov/pov_engine.py` (18 KB) and `slicers/` | about 20 KB | 2.9 Algo router (POV) | `src/quant/execution/pov/` | Phases B and C DONE (band, guards, sizing, PEG, liquidity). Phase D (engine loop, paper only) PENDING |
| 5 | Installers | `install.bat`, `requirements.txt`, root `pyproject.toml`, `Blue_engine/pyproject.toml`, `Blue_engine/_sdkpath.py`. The decoupling plan also names `install_ubuntu.sh`, which is not in the root listing read on 2026-11-09 | small | Checklist item (section 5 of the flow doc), not a drawn block | Root installers only | PENDING. Last step, after 1 to 4 |

## 3. Flow v5 wrapping rules

- Each new module has a `Flow stage:` line naming its v5 block(s), as the flow doc requires.
- `tests/unit/test_layout.py` must still pass. It checks that every SVG block title is mapped.
- If a feature has no matching block (economics, agent pipeline), we do NOT invent one silently. Owner decides (O1). Options: add a block to the SVG, or map to an existing block. Any SVG change updates the flow doc, AGENTS.md and README.md in the same commit.

## 4. Rules for every sub-stage

1. Wrap first, then port. Read the Blue code, write local code, write tests.
2. Test type per item:
   - Hand-checked tests run everywhere.
   - Parity tests run where Blue loads (the sandbox loads only some modules, using the stub loader).
   - Golden tests (JSON from Blue, via `scripts/ops/export_blue_goldens.py`) for modules the sandbox cannot load.
3. Keep behaviour the same unless the owner approves a change. Write down any known difference (as done for `optimiser_registry` and `screen_rules`).
4. After a sub-stage: no `blue_engine` or `blue_bridge` import in the moved code. Update the `Flow stage:` line. Update the decoupling plan work log.
5. Fail closed on missing data, unset limits and optimiser errors.
6. No LLM or AI call in any trade decision path. `src/quant/tests/test_no_llm_in_trade_path.py` must stay green.
7. Audit trail: signals, orders and risk decisions keep logging to the audit log.
8. No look-ahead or survivorship bias in data code. Keep point-in-time rules.
9. Commit only after tests pass in CI scope: `tests/unit src/quant/tests tests/parity`. Note: the four-path run hangs at about 3% (open item, 2026-11-08). CI is not affected.

## 5. Sub-stage order and reasons

Proposed order (follows the decoupling plan):

1. **6.2 Screener** (split into sub-steps):
   - 6.2a parsing (`annual_reports.py`, `symbol_map.py`), tested on saved sample files
   - 6.2b screener.in client (`client/`), with mocked network tests
   - 6.2c valuations (`valuations.py`), golden tests
   - 6.2d yfinance fallback (`yf_fallback.py`)
   - 6.2e `screener_engine.py` and `filters.py`, wired to the rules already in `screen_rules.py`
2. **6.3 Economics**: start with `regime.py` and `scoring.py` (pure logic), then providers and NLP last (NLP is advice only).
3. **6.5 POV Phase D**: engine loop, paper only. Live stays blocked.
4. **6.6 Agent pipeline**: advice only. Moves last because it is the largest and touches the most code.
5. **Stage 5 cleanup and installers**: remove Blue from the installers, move `Blue_engine/` to `Temp/archive/<date>/`, update AGENTS.md, README and SOP.

Difference from the requested list: the request lists agent pipeline third and POV fourth. This plan puts POV before the agent pipeline because the agent is the riskiest. Change the order if preferred.

## 6. Owner decisions needed before code moves

- **O1.** Economics and agent pipeline have no exact flow v5 block. Choose: add blocks to the SVG (and update the flow doc, AGENTS.md, README.md together), or map them to existing blocks.
- **O2.** Agent pipeline: confirm the split. `core/risk_engine.py` and `core/paper_shift.py` touch trade decisions. They must not be moved as-is. They need a separate review before any move.
- **O3.** Screener: confirm screener.in data access and where parity or golden fixtures come from (owner machine).
- **O4.** Set `max_symbols_per_portfolio` in `config/risk_limits.yaml`. Until then the paper gate blocks all orders.
- **O5.** Run `scripts/ops/export_blue_goldens.py` on the owner machine (D4). Needed for items 2c and 3.
- **O6.** Confirm `install_ubuntu.sh` exists or is obsolete, so the installer step is correct.

## 7. Verification status

Code moves need the full test suite to pass. This planning step changes docs only, so no tests are affected. The implementation sessions must run tests in an environment where the dependencies install.

## 8. Work log

- 2026-11-09: plan written from the repo state at commit 0b3a467. Sizes are from the GitHub listing. Agent pipeline size not yet measured. No code moved.
