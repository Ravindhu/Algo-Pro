# Repo Layout Plan — match AGENTS.md

Status: PHASES A-E DONE; Phase F (commit and push) in progress (started 2026-11-05)
Owner request: "Make sure all the repo layout is as per AGENTS.md."
Rule source: `AGENTS.md` → "Project layout" tree.

## 1. Goal in plain words

Make the folders and root files match the tree in `AGENTS.md`.
Do this without deleting files and without changing trading logic.
Where `AGENTS.md` contradicts itself, do not guess. Write it down as an owner decision.

## 2. Ground rules used in this pass

- No file is deleted. Files move with `git mv`, or stay where they are.
- Only paths and path-text change. No trading logic changes.
- Every moved path is searched for across the repo and updated.
- Unit tests must not get worse. Baseline: 239 passed, 70 failed, 34 skipped, 1 xfailed.
  All 70 failures are `No module named 'openalgo'` (sandbox cannot install packages).
  The list of failing tests is kept in the work log, so new failures show up.
- Deep changes (anything that changes a production install path or a
  depth-based `parents[N]` path) are NOT done in this pass. They go to section 5.

## 3. Gap inventory (layout vs reality at commit 2b98a63)

| # | Layout says | Reality | Action this pass |
|---|---|---|---|
| G1 | `pyproject.toml` at root | missing | Create (metadata + pytest testpaths) |
| G2 | no `strategies/` at root (`config/strategies/` instead) | root `strategies/` has only `.gitkeep` | Remove empty placeholder (not a real file) |
| G3 | `dashboard/` = the ONE dashboard (`app.py` + assets) | files sit in `dashboard/algo_pro/` | Move to `dashboard/` and `dashboard/assets/` |
| G4 | `src/quant/strategies/` (strategy classes) | folder missing | Create package scaffold |
| G5 | `src/quant/models/events/` | single file `models/events.py` | Move into `models/events/__init__.py` (import path unchanged) |
| G6 | `docs/audits/` (dated audit reports) | audit reports sit in `docs/` | Move dated audit files |
| G7 | `docs/skills/` ("moved from Blue_engine") | `Blue_engine/skills/` | Move skill docs (no code loads them by path) |
| G8 | `Temp/Staging Workspace/`, `Temp/Daily work log/` | not present | Create locally (Temp is git-ignored by design) |
| G9 | `scripts/ops/` for maintenance and recon | `scripts/diagnostics/`, `scripts/verify/`, top-level shims | NOT moved (path depth in code, documented commands). See section 5 |
| G10 | `Servers/` not in layout | 5 deploy files, used by `install.sh` and live VPS | NOT moved. See section 5 |
| G11 | `quant_system_flow_v5.svg` in `docs/` (tree) | at repo root (table says root) | NOT moved. See section 5 |
| G12 | `docs/SOP.md` (tree) | `docs/My_trading_system_SOP/SOP.md` (table says this) | NOT moved. See section 5 |
| G13 | `dashboard/app.py` | missing | Not structural. Build later (Phase 3 work) |
| G14 | `scripts/run/run_paper.py` | missing | Not structural. Build later (AGENTS names it as a known gap) |

## 4. Phases

Phase A — Plan (this file). Status: DONE when committed.
Phase B — Safe moves: G1, G2, G3, G4, G5, G6, G7, G8. Status: DONE.
Phase C — Reference fixes: update every moved path in code, tests, and docs. Status: DONE (23 files).
Phase D — Verify: unit tests vs baseline (same 70 openalgo failures, zero new), `test_layout.py` 4 passed, generator output byte-identical, Blue collect 176. Status: DONE.
Phase E — Docs and logs: SOP section 19 added, work-log entry written. Status: DONE.
Phase F — Commit and push to `master`, then confirm the remote matches local.

## 5. Owner decisions needed (not done in this pass)

D1. `Servers/` → `scripts/ops/deploy/`?
   - Live VPS installs keep `deploy.env` in `/opt/<app>/Servers/`.
   - Moving the folder would make the new code miss that file on existing servers.
   - Needs a migration step and an owner yes/no.
D2. `quant_system_flow_v5.svg`: root or `docs/`?
   - AGENTS.md table says root. AGENTS.md tree says `docs/`.
   - `tests/unit/test_layout.py` pins the root path.
D3. `docs/SOP.md` or `docs/My_trading_system_SOP/SOP.md`?
   - AGENTS.md table says the second. The tree says the first.
D4. `scripts/diagnostics/` and `scripts/verify/` → `scripts/ops/`?
   - 4 files compute repo root with `parents[N]`. Moving changes depth.
   - Top-level `scripts/*.py` files are compatibility shims that keep documented commands working.
D5. Two different `e2e_replay.py` files exist (`scripts/e2e_replay.py` and `scripts/research/e2e_replay.py`). Which one is the real one?
D6. Extra root and config files not in the tree:
   `openalgo.UPSTREAM_PIN`, `config/conviction_tiers.yaml`, and extra docs in `docs/`.
   Keep or move?
D7. `Temp/Daily work log/` is git-ignored, but AGENTS.md requires an entry after each commit.
   Keep local only, or allow those files in git?
D8. P&L on the dashboard. CLOSED 2026-11-05: owner chose SHOW by default.
   Recorded in `docs/decisions/0007-pnl-shown-by-default.md`.
   Other dashboard items from the owner brief (EV, MAE/MFE, rule compliance) are still open. See `docs/QUANT_STRUCTURE_DESIGN.md` section 5.

## 6. Work log

- 2026-11-05 — Phase A: plan written. Baseline recorded (see section 2).
- 2026-11-05 — Phases B-E: moves G1-G8 done, references fixed, tests equal to baseline, SOP section 19 added.
- 2026-11-05 — D8 closed (P&L shown, decision 0007). Added `docs/QUANT_STRUCTURE_DESIGN.md`, which documents `src/quant/openalgo_platform/` and `src/quant/vendor/` (decisions 0013, 0014). Docs only.
