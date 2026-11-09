# Live trading implementation plan

Status: PLANNING ONLY (created 2026-11-06). No code is changed by this document.
NOT YET IN THE GITHUB REPO: the GitHub connector returned 403 on both branch
creation and file write. Owner must add it (see end of file).

Read first:
- docs/IMPLEMENTATION_PLAN.md: item list, status and exit checks for Phases 1-3.
  This plan does not repeat them; it orders the work needed to reach live trading
  and adds the gates that must pass.
- docs/STATUS_2026-11-06.md: latest state.
- README.md: "Nothing is live" rule and the non-negotiables.
- docs/decisions/0007, 0008, 0016, 0017: owner decisions that affect this plan.

## 1. Goal and definition of "live"

Goal: one strategy (OCL pattern, POV executor) places real orders through OpenAlgo
to NSE, under the same risk gate that paper uses, with small fixed capital and a
human able to stop it at any time.

"Live works" means all of these are true:
1. The same code path runs in paper and live. Only the broker adapter differs.
2. Every order intent passes the risk gate, the live guard and a human approval record.
3. Live fills match the replayed backtest within a tolerance the owner sets (P3-7).
4. Kill switch, safe mode and restart were tested with real OpenAlgo in analyze mode,
   then with a tiny live order.
5. Capital is capped in config. Any increase needs a new owner sign-off.

Out of scope for this plan: new strategies, optimisation, models (X-11, X-18).
Live is proven first on the one simple strategy.

## 2. Non-negotiable gates (from the repo rules)

- G1. Only src/quant/execution/openalgo_client.py calls the order API
  (tests/unit/test_layout.py).
- G2. Live orders need a LiveApproval tied to the current risk_limits.yaml hash,
  plus an approved GateDecision for the same intent, plus a non-empty API key
  (already in code; tested in test_live_guard.py).
- G3. No AI/LLM stage on the trade path (test_no_llm_in_trade_path.py).
- G4. Simulated or backtested data is never shown as live.
- G5. Risk limits stay null or approved by the owner. Paper approval (2026-11-06)
  does not approve live.
- G6. No phase starts before the previous phase's exit checks pass.

## 3. Current blockers

| #   | Blocker                                                              | Owner or code | Ref                              |
|-----|----------------------------------------------------------------------|---------------|----------------------------------|
| B1  | No runner for src/quant/ (scripts/run_live.py deliberately not made) | code          | P3, run_paper.py pending         |
| B2  | Broker live feed not built (data/ingest.py is CSV only)              | code + broker | P3-1                             |
| B3  | Order router and stop-at-fill protection not built                   | code          | P3-3                             |
| B4  | Live path not tested against a real OpenAlgo                         | owner machine | P1-5, X-6                        |
| B5  | openalgo / httpx not installed in sandbox; full test counts unknown  | owner         | P0-2                             |
| B6  | Blue_engine lazy-import gap (LLM code loaded in same process)        | code          | X-9b                             |
| B7  | Live risk limit values not set                                       | owner         | P0-3                             |
| B8  | SEBI algo checklist not verified                                     | owner         | P3-8, X-24                       |
| B9  | Live data source not chosen; INFY is test-only                       | owner         | X-10, config/data_sources.yaml   |
| B10 | Scope conflict on SEBI checks (status 11-04 vs plan 11-05)           | owner         | Open owner decisions             |
| B11 | Folder Blue_engine vs package blue_engine (import may fail on Linux) | code          | L0-1                             |
| B12 | Duplicate-order block after restart at gate level                    | code          | X-2, X-7                         |

## 4. Phases

### L0 - Reproducible baseline (owner machine first)
Tasks:
- L0-1. Fix package name (B11): confirm on Linux that `import blue_engine` works from
  repo root; if not, document a path rule or fix packaging. Test: import smoke test.
- L0-2. Owner installs requirements.txt and openalgo==2.0.3; runs
  `pytest tests/unit tests/parity Blue_engine/tests`; sends counts (P0-2).
- L0-3. Classify every Blue_engine failure as environment or logic (X-28).
- L0-4. Fix X-9b: make Blue core/__init__.py and portfolio/__init__.py imports lazy so
  the trade path does not load LLM client code.

Exit checks:
1. Owner-run counts recorded in docs/STATUS_*.md, zero unexplained failures.
2. Import smoke test passes on Windows and on Ubuntu.
3. X-9b test passes (remove its expected-failure marker).

Owner gate: sign off baseline counts.

### L1 - Paper run end to end (no broker orders)
Tasks:
- L1-1. Write scripts/run_paper.py (P1-5, X-6): data -> signal -> conviction -> size ->
  portfolio risk -> session guards -> risk gate -> queue -> paper router -> ledger -> audit.
- L1-2. Feed event blackout dates into RiskState (X-8 open part).
- L1-3. Gate-level duplicate-order block (X-2) and kill-and-restart test (X-7).
- L1-4. Read-only OpenAlgo quotes and order book only (readonly_client.py) in this path.

Exit checks:
1. One full paper session on a real trading day: every intent has an audit record,
   a gate decision and a ledger row.
2. Kill and restart mid-session: zero duplicate paper orders.
3. Planted bad intents (over limit, blackout day, unapproved) are refused and logged.
4. Paper P&L reconciles with the ledger to the rupee.

Owner gate: review the paper session log.

### L2 - Replay parity (evidence)
Tasks:
- L2-1. Real-data OCL backtest with costs, walk-forward, DSR and PBO (P1-6, X-10).
  Owner downloads data on own machine.
- L2-2. Live paper fills vs replayed fills over N sessions, tolerance set by owner (P3-7).
- L2-3. Trial log on for every run (X-12); owner decides default.
- L2-4. Cost model check against actual broker contract notes via TCA (P3-4 partial).

Exit checks:
1. Out-of-sample net result reported with DSR and PBO. If the decision rule
   (decision 0001) finds no edge, stop here. Do not go to L3.
2. Parity tolerance met over the agreed paper period (length not set yet).
3. Cost differences explained, not ignored.

Owner gate: written approval to move to L3.

### L3 - Live guard drill (no orders)
Tasks:
- L3-1. Build execution/router.py (P3-3) with the single order client. Paper and live
  both go through it.
- L3-2. Broker session manager and order-rate throttle (P3-4).
- L3-3. Live feed adapter in data/ingest.py (P3-1), read-only first.
- L3-4. Kill switch and safe mode drill with live feed running, zero orders sent (P3-6).
- L3-5. Alerts at INFO / WARNING / ERROR / CRITICAL (P3-5).

Exit checks:
1. Kill switch stops new intents within one loop cycle (measured in the drill).
2. Safe mode blocks entries and allows exits (tested with live feed).
3. Broker disconnect: system goes to safe mode, alerts, and does not guess positions.
4. Restart after disconnect reconciles with the broker book before any new intent.

Owner gate: sign off drill log.

### L4 - First real orders, minimal capital
Tasks:
- L4-1. Set live risk limits (P0-3) and live cap in config (decision 0016 says live =
  paper values unless owner changes them).
- L4-2. Owner creates LiveApproval for the exact risk_limits.yaml hash.
- L4-3. One supervised session, one minimum-size order (SME lot rule applies, decision
  0016). Human watches the whole session.
- L4-4. Protection (stop) placed at fill, verified at the broker (P3-3).
- L4-5. Compare each live fill with the paper model for the same intent (TCA feed).

Exit checks:
1. Each live order has: gate decision, approval record, audit line, ledger row,
   protection order.
2. No order outside the approval window or above the cap (test + log check).
3. Broker book reconciles with ledger at end of day.
4. Owner reviews the first five sessions before any change.

Owner gate: written approval for each cap step-up.

### L5 - Monitoring and operations
Tasks:
- L5-1. Live monitor and strategy health (P3-4): fill quality, slippage, rejects,
  drawdown vs limit.
- L5-2. Dashboard live view: P&L visible by default (decision 0007); risk utilisation
  and execution quality (decision 0008).
- L5-3. Daily SOP entry (D-3): pre-open checklist, smoke test, OpenAlgo login check,
  end-of-day reconciliation.
- L5-4. Pre-open timer and start rules (Servers/). The engine must not start on reboot
  without a human.
- L5-5. Backup of ledger and audit store; restore tested once.

Exit checks:
1. Two weeks of unattended sessions with no unexplained stops.
2. Every stop has a reason in the audit log.
3. Restore from backup tested.

Owner gate: approve any move to a larger cap.

### L6 - Improvement loop under human control (later)
Only after L5. Model promotion needs registry sign-off (X-19). Optimiser search over
MAE/MFE weights stays off until owner approves bounds (PB-6). Impact model (X-30) and
HRP choice (X-25) need their own decisions first.

### L7 - Regulatory and data (runs in parallel from L0)
- L7-1. SEBI algo-trading checklist from official sources: registration, order tagging,
  immutable audit store, clock sync (P3-8, X-24).
- L7-2. Resolve scope conflict (B10) in writing.
- L7-3. Owner confirms the data licence allows live use (B9). Current INFY data is
  personal, non-commercial research only; commercial automated trading needs written
  permission (STATUS_2026-11-06, section 4).

Exit check: written checklist with official links, owner-signed, before L4.

## 5. Order of work

L7-1 (start now) -> L0 -> L1 -> L2 -> L3 -> L4 -> L5 -> L6.
L7 must finish before L4. L2 must pass before live-feed code (L3) is trusted,
because a feed without evidence gives no edge to protect.

## 6. What is needed from the owner

1. Baseline test counts (L0-2).
2. Scope of SEBI checks (B10).
3. Live data source and licence (B9).
4. Live risk limits and first cap (B7).
5. Parity tolerance and paper period length (L2-2).
6. Trial-log default (X-12), impact model (X-30), HRP choice (X-25).
7. Muhurat 2026-11-08 and BSE holiday handling (X-23).

## 7. Stop conditions (any one stops the plan)

- A test that protects live is skipped or removed without owner sign-off.
- Any order reaches the broker outside openalgo_client.py.
- Parity fails and the cause is not explained.
- SEBI checklist incomplete at L4.
- Broker book and ledger disagree at end of day and cannot be reconciled.
- Owner says stop.

## 8. Risks

- Edge may not exist after costs. L2 is the test; this plan does not assume profit.
- Cost model may understate impact (flat 5 bps today, X-30).
- Live feed quality may differ from backtest data.
- Two hosts (Windows dev, Ubuntu server): keep one interpreter per host.

## 9. Document rules

- Update this file before each commit that changes live code.
- Status uses DONE / PARTIAL / PENDING, as in IMPLEMENTATION_PLAN.md.
- Do not mark a phase DONE until its owner gate is recorded.

## 10. To add this file to the repo

The GitHub connector cannot write to Ravindhu/Algo-Pro (403 on branch creation and
file write). Options: grant write access to the connector, or copy this file to
docs/LIVE_TRADING_IMPLEMENTATION_PLAN.md and commit it yourself.
