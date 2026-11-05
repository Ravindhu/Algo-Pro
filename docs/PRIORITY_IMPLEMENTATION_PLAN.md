# Priority Implementation Plan: Conviction Sizing and Dashboard P&L

Status: items 1-7 and the P&L change implemented 2026-11-05. Plan was written before implementation (project rule). Owner items remain (section 6).
Last updated: 2026-11-05.

Companion to `docs/IMPLEMENTATION_PLAN.md`. Item IDs (P0-*, P1-*, P2-*, PB-*, D-*) map to that file.

Owner decisions taken on 2026-11-05:
- **P&L is SHOWN by default** on all dashboards (overrides the earlier "hidden by default" rule).
  A toggle can still hide it on screen. Record: `docs/decisions/0007-pnl-shown-by-default.md`.
- The NSE event-calendar feed (`https://www.nseindia.com/api/event-calendar`) is the source for
  board-meeting events used by the event-blackout check. It is NOT a holiday calendar.

---

## 1. What exists after merging `feat/conviction-score-v2`

The branch held earlier work that was never on `master`. It was merged in this update.

| Piece | Where | State |
|---|---|---|
| Conviction score (six conditions, weighted geometric mean) | `src/quant/models/conviction.py` | Present. Tests: `tests/unit/test_conviction.py` (pass). |
| Size tiers (disabled by default) | `src/quant/portfolio/tiers.py`, `config/conviction_tiers.yaml` | Present. `enabled: false`. Tests pass. |
| Cap wiring (tier multiplier, then min-of-caps) | `src/quant/portfolio/tiers.py` | Present. Tests in `test_conviction_tiers.py` (pass). |
| Trading calendar loader | `src/quant/core/calendar.py`, `config/calendar/holidays.csv` | Present. 2026 NSE holidays filled. Tests pass. |
| P&L hidden by default | `render.py`, `reporting/dashboard.py`, `dashboard/algo_pro/index.html` | Present. **Reversed by this update** (see section 3). |
| Accounting password lockout | `src/quant/reporting/accounting.py`, `scripts/set_accounting_password.py` | Present. Kept as the authorised accounting view. Tests pass. |

Known test state: 79 passed, 5 skipped on the new branch test files. 70 unit tests fail only because `openalgo` is not installed in this sandbox (PyPI blocked). These are the same pre-existing failures, not regressions.

---

## 2. Items to implement now (numbered, in order)

| # | Item | Plan ID | Status before this update | Action |
|---|---|---|---|---|
| 1 | Check `feat/conviction-score-v2` | (new) | Not checked | DONE: merged into `master`. |
| 2 | Conviction score | P2-1 | Branch only | DONE by merge. Verify tests pass. |
| 3 | Size tiers (disabled by default) | P2-2 | Branch only | DONE by merge. Verify disabled by default. |
| 4 | Wire tiers into sizing with min-of-caps and audit record | P2-3 | Branch only | DONE by merge. Verify caps still bind. |
| 5 | Calendar loader | P1-1 | Present on `master` | Fix stale docstring on `is_trading_day`. Add test for holiday and weekend cases if missing. |
| 6 | Verify safe mode, audit log, override log, queue, order FSM, ledger | P1-2, P1-3, P1-4 | Partly tested | Add tests for `override_log` (none existed) and ledger restart duplicate detection (`seen_intent_ids` after reload). |
| 7 | Review flow doc and `ALGO_FLOW.md` | D-2 | Not reviewed | Fix stale P&L line and event-blackout source. Record what was not reviewed. |
| A | P&L shown by default in all three dashboards | PB-1 (reversed) | Hidden on branch | Flip defaults; keep toggle; update tests. |
| B | NSE event-calendar parser (offline) | P2-8 (partial) | Not present | New parser in `src/quant/data/market_inputs.py` for the NSE JSON format, with a small fixture from the sample records. No network call in code. |
| C | Event blackout uses NSE board-meeting dates | P1 (controls) | `in_event_blackout(day, days)` exists | Add helper that turns parsed events into blackout dates for a symbol. |
| D | Docs: plan, status, flow doc, decision 0007 | D-2 | Stale | Update in the same commit as the code. |

Not in this round (need owner action or data the sandbox cannot reach):
- P0-2 test baseline with `openalgo` installed.
- P0-3 approve risk limits.
- P0-4 real holiday verification on owner machine (2026 list is on the branch, verify it once).
- P1-6 real-data OCL backtest (NSE bars not reachable from sandbox).
- P3 live path (blocked by Phase 2 exit checks).

---

## 3. P&L visibility change (decision 0007)

Before: P&L hidden unless `reveal_pnl=True`. Web page started hidden.
After:
- `render.py`, `reporting/dashboard.py`: `reveal_pnl` defaults to `True`.
- `index.html`: starts shown. The button label starts as "Hide ₹".
- Accounting view (password-gated) stays unchanged.
- Tests in `test_pnl_hidden.py` and `test_phase3.py` are updated to assert "shown by default" and "toggle hides".

Guardrails unchanged: EV, MAE/MFE, profit factor, and rule compliance stay visible. No P&L value can set size or create an order.

---

## 4. Guardrails

- Conviction never overrides a hard cap. Final size = min(Kelly, vol-target, drawdown, tier, exposure caps).
- Tiers stay disabled until owner approves values from out-of-sample evidence.
- No module in `risk/`, `execution/`, `portfolio/` imports `core/llm`.
- NSE data is used for event blackout only. Parsed events are advice inputs, not orders.
- Every change: tests pass for the new files; the doc set is updated in the same commit.

---

## 5. Order of work

1. Merge branch (done).
2. Verify conviction, tiers, calendar tests (done: pass).
3. Write this plan (done).
4. P&L shown by default + tests.
5. Override log and ledger restart tests.
6. NSE event parser + blackout helper + tests (fixture from sample records).
7. Calendar docstring fix.
8. Flow doc and ALGO_FLOW review fixes.
9. Update IMPLEMENTATION_PLAN, STATUS, decision 0007.
10. Commit (merge commit already prepared) and push fast-forward to `master`.

---

## 6. Open decisions

- Tier values and weights: placeholders until out-of-sample evidence.
- "Authorised" accounting view: current design is a password (set via `scripts/set_accounting_password.py`). Owner to confirm.
- AGENTS.md still says "P&L hidden by default" (rule 8) and "Phase 0 pending". Full rewrite needed. Not done in this round.
- Remote `main` (`76f21af`) and `docs/status-2026-11-05` still exist. Owner to delete on GitHub.
