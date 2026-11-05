# Quant structure design

Status: design doc. Docs only. No code changed.
Date: 2026-11-05.
Source checked: `master` at commit `89000639`.
Related: `AGENTS.md` (project tree), `docs/QUANT_FOLDER_PLAN.md`, `docs/REPO_LAYOUT_PLAN.md`, `docs/decisions/`.

---

## 1. Purpose

This doc lists every folder under `src/quant/` and says what it is for. It also covers the two vendored folders that the earlier layout did not describe.

---

## 2. Owner decisions closed here

| ID | Question | Answer | Where recorded |
|---|---|---|---|
| D8 | Show or hide P&L on the dashboard by default? | **Show** (owner, 2026-11-05) | `docs/decisions/0007-pnl-shown-by-default.md` |
| D6 (part) | Keep the `openalgo_platform/` and `vendor/` folders? | **Keep, document in layout** | `docs/decisions/0013-vendor-openalgo-platform.md`, `docs/decisions/0014-vendor-openalgo-sdk.md` |

Decision 0007 already covers P&L. Decision 0008 (EV, MAE/MFE and rule compliance removed from dashboard) still stands. Anything else in the owner's brief that conflicts with `AGENTS.md` rule 8 stays open.

---

## 3. Vendored folders (new documentation)

### `src/quant/openalgo_platform/`
- What: full copy of the OpenAlgo platform (Flask app, broker adapters, websocket proxy, Docker files, and so on).
- Upstream: `https://github.com/marketcalls/openalgo`.
- Licence: AGPL-3.0 (keeps its own `LICENSE`).
- Rules: no local edits. Excluded from our layout tests and package discovery (decision 0013).
- Our code must never call its order path directly. Orders go through the risk gate and `execution/openalgo_client.py`.

### `src/quant/vendor/openalgo/`
- What: the OpenAlgo Python SDK package (`from openalgo import api`), pinned to tag v2.0.3.
- Licence: MIT (keeps its own `LICENSE`).
- Import order: `blue_bridge.py` adds `src/quant/vendor` at the END of `sys.path`. A pip-installed SDK wins.
- Extra runtime packages go in `requirements.txt`: `httpx`, `websocket-client`.

Both folders are third-party code. They are not part of the trade path and are not changed by our stages.

---

## 4. Full folder map (`src/quant/`)

| Folder | Lane | Purpose | Detail |
|---|---|---|---|
| `core/` | shared | Types, config, clock, logging, Blue bridge | `blue_bridge.py` loads the SDK and Blue_engine |
| `data/` | market inputs | Data QA, feature store, universe, live feed | Live feed `ingest.py` not built yet |
| `models/` | signal and conviction | Conviction, calibration, factors, signal | `events/` is advice only, never on trade path |
| `strategies/` | strategy | Strategy classes, registered by name | Empty. No strategy built yet |
| `portfolio/` | sizing | Kelly sizing, tiers, covariance, rebalance, allocator | Tiers disabled until owner approves |
| `risk/` | safety gate | Gate, pre-trade, liquidity, loss breaker, kill switch, exits | Gate is the only path to orders |
| `execution/` | orders | Order queue, FSM, OpenAlgo client, router | Phase 3 only. Nothing live |
| `research/` | validation | Backtest, walk-forward, trial log, costs | Needs real NSE data |
| `monitor/` | post-trade | Ledger, TCA, health, parity, session | Partly built |
| `audit/` | audit trail | Override log, append-only log | Immutable store check pending |
| `feedback/` | improvement | Re-optimisation, champion/challenger | Needs owner sign-off |
| `reporting/` | dashboard | Dashboard data, accounting view | P&L shown by default (0007) |
| `notify/` | alerts | Alerts by severity | Not started |
| `openalgo_platform/` | vendored | Full OpenAlgo platform copy | Section 3 |
| `vendor/` | vendored | OpenAlgo SDK copy | Section 3 |

Rule: a folder that is not in this table or in `AGENTS.md` needs a plan entry before it gets code.

---

## 5. Open items (not decided here)

- D1: `Servers/` move. Needs owner yes or no.
- D2: flow SVG location (root or `docs/`).
- D3: SOP path (`docs/SOP.md` or `docs/My_trading_system_SOP/SOP.md`).
- D4: move `scripts/diagnostics/` and `scripts/verify/` to `scripts/ops/`.
- D5: two `e2e_replay.py` files. Which one is real?
- D7: whether `Temp/Daily work log/` stays local only.
- Dashboard conflict: AGENTS rule 8 (hide EV, MAE/MFE, rule compliance) vs owner brief (show them). Decision 0008 already removed them. Owner to confirm.
- `AGENTS.md` project tree: does not yet list `openalgo_platform/` or `vendor/`. This doc is the record until AGENTS is updated in its own commit.

---

## 6. Sandbox limit

Unit tests cannot run in this sandbox. The 70 `openalgo` failures are a known sandbox limit (see `docs/STATUS_2026-11-05.md`). Owner machine: `pytest tests/unit Blue_engine/tests` after `pip install -r requirements.txt`.
