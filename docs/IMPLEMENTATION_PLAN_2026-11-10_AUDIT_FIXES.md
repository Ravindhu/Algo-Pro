# Audit fixes plan and status - 2026-11-10

Branch: `fix/audit-2026-11-10` (from `master` 05454b5). Not merged to `master`.
Source: the audit of `master` in the same day. Flow stage owners are listed per item.

## Status of the 14 audit findings

| # | Finding | Status | Where |
|---|---------|--------|-------|
| 1 | Daily and strategy loss never fed real P&L | DONE | `strategy_bot.py` `_record_pnl`, `_roll_day`. Daily loss at limit trips kill switch |
| 2 | No runner | DONE | `scripts/run/run_strategy_bot.py` (paper only, stop flag, refuses analyze and live) |
| 3 | Exits off when data source not approved | PARTIAL | Cycle still stops before exits (owner rule: no cycle until source approved). Now raises CRITICAL "positions unmanaged". Owner must approve a source to get exits |
| 4 | Test suite red | PARTIAL | 32 failures are missing `httpx` (sandbox cannot install). 1 pin failure: vendored OpenAlgo hash differs from `openalgo.UPSTREAM_PIN`. Owner must check the diff. Not re-pinned |
| 5 | Stale bars trigger stop exits | DONE | `max_outage_cycles` (default 3). Older bar: exit held, CRITICAL alert |
| 6 | Exposure bookkeeping drift | DONE | Exposure booked at send, released by the same booked amount on exit |
| 7 | Sizing vs caps conflict | DONE | `risk_adapter._clamp_to_caps` lowers qty to order and exposure caps before the gate |
| 8 | SELL exit could open a short | DONE | Gate refuses SELL exit larger than `held_qty` (bot passes it) |
| 9 | No fill confirmation | PARTIAL | Analyze: fill needs order status `complete`. Live reconciliation is still startup only |
| 10 | Halted positions had no alert | DONE | `Alerts` CRITICAL/WARNING on halt, held, unmanaged |
| 11 | Only engulfing in bot | OPEN | OCL needs a bar adapter. Not started |
| 12 | Conviction sizing not built | OPEN | Needs out-of-sample evidence (Priority A4). Tiers stay disabled |
| 13 | Lot size not passed | PARTIAL | Bot reads `lot_size` from daily stats (default 1). `live_bars` does not yet supply the SME broker lot |
| 14 | Repo clutter and drift | PARTIAL | Docs and AGENTS updated. No files deleted (AGENTS rule). Flow SVG not regenerated (signature decision 0027 owner) |

## Decisions needed from the owner

1. Approve a licensed live data source (`config/data_sources.yaml`). Without it no exits run.
2. Check the vendored OpenAlgo diff against upstream and decide the re-pin.
3. Confirm whether the daily loss counter should survive restarts (persist it) before live use.
4. Confirm the sizing clamp is the intended behaviour (qty is reduced, not rejected).

## Test state

- Quant, unit and parity: 2269 passed, 33 failed, 25 skipped, 1 xfailed.
- New tests: `src/quant/tests/test_audit_fixes_2026_11_10.py` (20 tests). All pass.
- Layout guard `src/quant/tests/test_layout.py`: 4 passed (two modules got their Status line).
- The 33 failures are the same set as before the change: 32 `httpx`, 1 pin.

## Not done in this change

- Flow SVG (`quant_system_flow_v5.svg`) not regenerated. It is a signed artifact.
- Work-log entry is in git-ignored `Temp/`, not committed.
- Not merged to `master`.
