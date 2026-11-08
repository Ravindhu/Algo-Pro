# Implementation plan: paper-only BUY scanner (2026-11-08)

Status: IN PROGRESS. Branch: `feat/paper-buy-scanner`. Owner: Ravindhu.
Scope set by owner (2026-11-08): buy side only, paper trading only, commit to branch.

## Goal

Every 1 minute, check all screened NSE symbols against the BUY strategies. When a
symbol matches, send the signal through the risk gate and into the POV slicer
(`quant.execution.pov_executor.PovExecutor`). In this phase the sender is a PAPER
sender: it records the child orders and never calls a broker.

## What is ported from Algo-Lab (source: Ravindhu/Algo-Lab, read-only)

| Algo-Lab file | Algo-Pro file | Notes |
|---|---|---|
| `framework/strategies/indicators/hm.py`, `ocl.py` (atr, hm) | `src/quant/strategies/indicators.py` | Pure math, unchanged. |
| `framework/protect.py` | `src/quant/strategies/protect.py` | Forex pip cost removed. Cost is a percent of price. |
| `framework/strategies/multi.py`, `base.py` | `src/quant/strategies/base.py` | No ForexSymbol. Symbol is a plain NSE name. |
| `framework/strategies/ocl.py` | `src/quant/strategies/ocl.py` | BUY only. Forex pip buffer removed. |
| `framework/strategies/ocl_sell_fail.py` | `src/quant/strategies/ocl_sell_fail.py` | TOP and BOTTOM. Handoff store is passed in, not read from a file. |
| `framework/strategies/engulfing_strategy.py` (functions only) | `src/quant/strategies/engulfing.py` | `engulfing()`, `fib_entry_level()`, `body_ratio()`. |
| `framework/strategies/engulfing_ocl.py` | `src/quant/strategies/engulfing_ocl.py` | BUY side only. |

Not ported (out of scope for this phase):
- SELL side (`ocl_inverse.py`). Owner: buy side is enough for equity.
- MT5 adapter, forex symbol wrapper, `ops/state.py` (replaced by the Algo-Pro state layer later).
- DV-ATR trail. Algo-Lab does not port it either. The paper config sets `dvs_enabled: false`.
- The legacy `Engulfing` class (`engulfing_strategy.py`). Only the engulfing HTF setup is used.

## Strategies in this phase (BUY only)

1. `ocl` (normal OCL keylevel)
2. `ocl_sell_fail_top` (failed-short core, tier 1 and tier 2 levels)
3. `ocl_sell_fail_bottom` (single level at lowest swing; runs only after TOP exhausts the same core)
4. `engulfing_ocl` (bullish engulfing with fib entry, OCL LTF entry model)

Open question: the owner asked for "5 strategies". Only these 4 BUY blueprints were found
in Algo-Lab. The fifth one is not identified yet. Waiting for owner answer.

## Build order (each step: tests pass, doc updated, commit, push)

- S1 (this commit): plan doc, ported helpers, the 4 BUY strategies, parity tests against Algo-Lab
  on synthetic data, paper strategy config.
- S2: scanner loop. 1-minute cycle, NSE hours only (09:15 to 15:30 IST, holiday calendar), reads
  closed bars, runs the registry for each screened symbol, persists signal keys so a restart
  cannot trade the same bar twice.
- S3: wire signal to risk gate and `PovExecutor` with a paper sender. Audit log for every signal,
  gate result and child order.
- S4: tests for the whole path with fake bars; a run in paper mode on recorded data.

## Safety rules kept

- Only `openalgo_client.py` may place orders. The paper sender never calls it.
- Strategy code never sets size or changes risk limits.
- signal_frames() is pure: no orders, no file writes, only closed bars (last row dropped).
- Paper only. `live_enabled: false` in the config. Risk limits stay as they are.
- NSE holidays and the Muhurat session (2026-11-08, today) are not yet modelled. The scanner will
  skip cycles on closed days. Owner decision on today's Muhurat session still open.

## Decisions taken without a reply (owner can change)

- Paper only: yes (owner, 2026-11-08).
- Buy side only: yes (owner, 2026-11-08).
- Commit to branch `feat/paper-buy-scanner`, not `main`.
- Fixed quantity per signal for paper: 1 share (until the sizing engine is wired).

## S1 result (2026-11-08)

Tests (run locally, `PYTHONPATH=src pytest tests/unit tests/parity`):
- New: `tests/unit/test_buy_strategies_paper.py`, 27 hand-checked tests, all pass.
- Whole suite: 27 failed, 1921 passed. The 27 failures are the same HRP tests that already fail on
  `main` (baseline 27 failed, 1894 passed). No new failures.

Parity with the Algo-Lab originals (random walks, scratchpad script, not in the repo):
- OCL (C3 only), engulfing OCL: exact match on every step tested.
- Sell-fail TOP: exact match on short history (<= 30 HTF bars). 4800 of 4800 steps match for each
  variant tested. Sample is small: few signals fire on random data, so this is weak evidence.
- Sell-fail TOP with long history: 5 mismatches in 4800 steps. Cause: a bug in Algo-Lab. The
  pattern key uses `htf.index[core_start]` on the full history, but `core_start` is relative to the
  last `max_window` bars. The wrong bar's key misses the "used" list, so a finished core can be
  re-armed. The port uses the correct offset. Regression test: `test_sell_fail_key_points_at_core_start_even_with_long_history`.

Deviations from Algo-Lab (all deliberate):
- Forex pip buffer and pip cost removed. Prices are rupees.
- Sell-side not ported. Only BUY is registered.
- Sell-fail handoff is an in-memory store passed in by the caller. File persistence comes in S2.
- Signal target is a distance (`tp_dist`), same as Algo-Lab. The scanner turns it into a price.

Open items found in this step (owner decision needed):
1. `config/strategies/ocl_keylevel.yaml` says `aggressive_level_source: "C3_CLOSE"` and
   `conservative_level_source: "C3_OPEN"`. The Algo-Lab port (and this port) uses C2 open as the
   aggressive level after a C3 break, and C3 close only after a C4 confirmation. The YAML needs
   to be checked against this before any YAML-to-params loader is written. The loader is not in S1.
2. `dvs_enabled: true` in that YAML. DV-ATR is not ported. The paper config must set it to false
   (same as Algo-Lab, which also does not port it).
3. The owner asked for "5 strategies". Only 4 BUY blueprints exist in Algo-Lab
   (OCL, sell-fail TOP, sell-fail BOTTOM, engulfing). The fifth is not identified yet.

Status of the layout test: all `src/quant/strategies/*.py` files carry `Status: PHASE 2 IMPLEMENTED`
(paper only). This is required by `tests/unit/test_layout.py`.

Next (S2): scanner loop, NSE session and holiday checks, persisted signal keys, and a paper run
over recorded bars.

## S2 result (2026-11-08): scanner, quantity, owner rules

Owner answers applied:
1. Scalp: not imported. Left out on purpose.
2. Aggressive level: C3 break -> aggressive = C2 open. C4 confirmation -> aggressive = C3 close
   (as in Algo-Lab; a first draft wrongly used C3 open and was reverted after owner correction).
   Conservative = C3 open. Parity with Algo-Lab unchanged. Test:
   `test_ocl_c4_confirmation_uses_c3_close_as_aggressive`.
3. Skip today's Muhurat session (2026-11-08): `config/settings.yaml` -> `paper.skip_dates`.
4. Quantity: from portfolio construction. `src/quant/portfolio/paper_qty.py` takes the lowest of
   - the risk budget: capital x risk_per_trade (0.25%, settings.yaml) / stop distance, and
   - the caps: max_order_quantity, max_order_notional / entry, remaining open exposure / entry.
   Any null limit fails closed (qty 0).

New code:
- `src/quant/portfolio/paper_qty.py` (quantity rule, uses `decide_size` and `risk_budget_qty`).
- `src/quant/strategies/scanner.py`: session check, dedupe ledger saved before action, quantity,
  parent intent, POV slicing into child intents through `PovExecutor`, paper fill recording.
- Tests: `tests/unit/test_paper_scanner.py` (16 tests, real `check_intent` gate and the real
  `risk_limits.yaml`, with one test copy that sets the symbol limit).

Test state: 27 failed (all HRP, same as `main`), 1938 passed.

Findings from running the real gate on the shipped limits (these are blocks, not bugs):
- `max_symbols_per_portfolio` is null in `config/risk_limits.yaml`, so every entry is rejected.
  Owner must set it. Not changed by Claude.
- ADV is not supplied. The gate rejects a symbol with no ADV. The runner must supply ADV from daily
  data. Not built yet.

Open items before any paper run:
1. Owner: set `max_symbols_per_portfolio` in `config/risk_limits.yaml`.
2. Owner: confirm `paper.capital` (placeholder 100000 INR) in `config/settings.yaml`.
3. Build: OpenAlgo history provider (1-minute bars, and 5-minute and 15-minute built from them),
   plus daily ADV. Not verified against the OpenAlgo API yet.
4. Build: runner loop (every 1 minute), file-based audit log, state persistence for the sell-fail
   handoff and strategy state (restart safety beyond signal keys).
5. Gap: no simulated exits. A traded strategy stays in trade for that symbol (conservative).
6. Owner: the fifth strategy is still unidentified.

## S3 result (2026-11-08): owner answers, 1-minute feed, exits, runtime

Owner answers applied:
1. Max symbols per portfolio = 8 (`config/risk_limits.yaml`).
2. Paper capital = 1,000,000 INR (`config/settings.yaml`, `paper.capital`).
3. ADV: NOT used in Algo-Lab (checked: no ADV code there). ADV comes from Algo-Pro's own risk gate
   (`max_adv_pct`). It is now supplied per symbol from daily history (20 completed days).
4. 1-minute data: `src/quant/data/minute_feed.py`. Bars are built from OpenAlgo 1-minute history into
   5-minute and 15-minute frames aligned to 09:15. Only closed minutes are used; the last row is the
   forming bucket. A missing previous-minute bar raises `StaleData` and that symbol is skipped.
5. Exits (investment, no simulated exit scenarios): `src/quant/strategies/positions.py` manages each
   filled entry with the same rules as Algo-Lab (`protect.Trade`): trailing ATR stop, breakeven,
   fixed target, broker disaster stop, HTF floor exit. Processed once per new closed 5-minute bar.
   No squareoff: positions can stay overnight (investment).

New code:
- `src/quant/data/minute_feed.py`: bucket alignment, aggregation, forming row, stale check, ADV, and
  `make_fetcher(history_fn, symbols)` where `history_fn` is injected (OpenAlgo SDK `history` in production).
- `src/quant/strategies/positions.py`: PaperPositionManager (open, update, close, open notional, symbols).
- `src/quant/strategies/paper_runtime.py`: `build_paper_runtime`, `PaperRuntime.cycle`, `run_loop`
  (1-minute loop; a failed fetch is audited and the loop continues).
- Scanner now registers each filled entry with the position manager.

Risk gate with live state: open notional, open symbols (for the 8-symbol limit) and available funds
(capital minus open notional) are read at each check.

Tests: `tests/unit/test_paper_runtime.py` (feed, exits, runtime, loop). Full suite: 27 failed (HRP,
same as `main`), 1956 passed.

Known gaps (must close before any real run):
1. Positions and the handoff store are in memory. A restart loses open positions. Persist them.
2. Paper exits are recorded directly. They do not pass through the risk gate. Live exits must pass
   the gate (closing exits are allowed by the gate; this needs a test before live).
3. The bar timestamp convention (start of minute) is assumed. Verify against OpenAlgo history output.
4. The runner process (entry script, file audit log, the OpenAlgo SDK call that builds `history_fn`)
   is not built yet. Nothing scans live symbols until it is.
5. Fifth strategy still unidentified.

## S4 result (2026-11-08): state saved, paper exits through the risk gate

Owner requests: (1) save positions across restarts, (2) paper exits must pass the risk gate, (5) the fifth strategy is not needed and is dropped.

What changed:
- `src/quant/strategies/state_store.py` (new): one JSON state file with open positions, strategy state (`state()`/`restore()`), and Handoff marks. Written to a temp file, then renamed (atomic). Version mismatch raises. The signal ledger stays in its own file.
- `src/quant/strategies/positions.py`: every exit is an order intent (`kind="exit"`, `side="SELL"`, full qty, ref price = exit price, ADV carried). It goes through the same executor as entries (gate, approved queue, PaperSender, audit). If the gate rejects it, the position keeps a `pending` exit, is retried on every cycle, and no new bars are processed until the exit is sent. The strategy's `on_exit` and the closed-trade record run only after the exit is sent.
- `src/quant/strategies/paper_runtime.py`: `build_paper_runtime` takes `state_path` and `handoff`, restores state at start, and saves at the end of every cycle (also on skipped cycles). Exit submit uses the same `PovExecutor` as entries.
- `src/quant/strategies/ocl_sell_fail.py`: `Handoff.to_list` / `restore_list` (done in S3 follow-up).
- Fifth strategy: dropped by owner decision. The registry keeps four BUY strategies: ocl, ocl_sell_fail_top, ocl_sell_fail_bottom, engulfing_ocl.

Gate facts checked for exits: money, liquidity (ADV) and per-order checks apply to entries only. Exits are still checked for duplicate ids, kill-switch closes, and safe mode. A rejected exit is not recorded as sent, so a retry with the same intent id is accepted.

Tests: `tests/unit/test_paper_persistence.py` (14 tests): exit intent shape, rejected exit stays pending then fills, real-gate approval, no bar skipped while pending, refusal when no submit is wired, restart keeps position and exit still works, no reprocessing of old bars after restart, pending exit survives restart, strategy state and Handoff marks restored, no duplicate position, unknown strategy refused, version check, atomic save, runtime cycle saves and a new runtime resumes. The existing exit tests now pass an approving submit.

Results: paper and layout tests 80 passed. Full unit suite: 892 passed before the runtime test was counted, 27 failed (HRP only). All 27 failures are the HRP tests (`httpx` missing in this environment), which were already the baseline.

Open items and flags for owner:
1. SAFE MODE HALT_ALL blocks exits too (only kill-switch closes are exempt). In HALT_ALL a stop exit stays pending and is not sent. This is the current gate behaviour and it is not changed here. Please confirm whether protective exits should pass HALT_ALL. Recommended: yes, for exits that reduce risk.
2. Bar timestamp convention is still assumed (start of minute). Verify against OpenAlgo history output.
3. Runner entry point (script, file audit log, OpenAlgo SDK history call for `history_fn`) is not built yet.
4. Exits are sent as one child (not POV-sliced). Exit qty is capped by max_order_quantity (10) on entry, so one child is enough at current limits. Revisit if limits rise.

## S5 result (2026-11-08): risk-reducing exits pass HALT_ALL

Owner decision: a risk-reducing exit passes HALT_ALL. Entries stay blocked.

Rule (src/quant/risk/gate.py): an intent with kind "exit" and side "SELL" passes HALT_ALL. The book is long-only, so a SELL exit closes a long and never opens a short. Kill closes still pass only while the kill switch is on. BUY exits, entries, `risk_limits.blocked`, and duplicate ids are all still blocked under HALT_ALL.

Known limit: the gate does not see held quantity. A SELL exit larger than the held qty would open a short. The paper position manager always sends exactly the held qty, so this holds for the paper path. A held-qty check in the gate is a follow-up.

Tests: tests/unit/test_halt_risk_reducing_exits.py (6 tests, including a paper stop-out that fills during HALT_ALL). Two older assertions changed to the new policy: test_halt_all_kill_close.py and test_phase1_risk_exec.py. Full unit suite: 898 passed, 27 failed (HRP baseline only).
