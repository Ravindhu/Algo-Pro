# Implementation plan: risk ladder for the cycle strategies (2026-11-19)

Status: P0 to P5 done (2026-11-19). Remaining: owner order caps (section 11.9 item 26) and the replay full-fill limit. Live stays OFF. Paper mode only.
Rules source: `docs/STRATEGY_RULES_2026-11-19.md` section 11 (rules) and section 14 (worked example).
Gap list source: the same file, section 13.

## 1. Goal (simple words)

Make the bot place the same order sizes that the owner's rules describe, and make the replay
(backtest) use the same sizes, so a backtest number means the same thing as a live run.

Three things must become true:
1. Each entry is sized at its own price against the HTF OCL low (HTF L).
2. The probe, ladder and conviction stages follow section 11.3 to 11.5.
3. A position with no exit rule is never opened (fail closed).

## 2. Current state (checked in code, commit 19aeb5f)

| Item | Where | Today |
|---|---|---|
| One size per cycle | `strategy_bot._scan_cycle` | One parent, one `_gate_entry` call with `risk_per_trade`. Later triggers keep the same quantity. |
| Ladder stages | none | Not built. |
| Conservative and new-OCL entries sent as orders | `_scan_cycle` uses `fresh[0]` only | Not sent. Replay still averages them in (`cycle._ltf_loop`). |
| No exit rule | `cycle._finish` | `mode="target_pending"`, `target=None`. Parent gets `target=None` and no trail, so only the HTF stop closes it. Fail open. |
| Per-cycle loss ceiling | none | Not built. |

## 3. Arithmetic check of section 14 (done 2026-11-19)

Formula (section 11.3): `shares = floor(equity x risk / (entry - HTF L))`.
Equity 1,00,000. HTF L = 93.

| Step | Owner doc says | Recomputed | Note |
|---|---|---|---|
| OCL #1 aggressive, 0.10 % at 100 | 14 | 14 | OK |
| OCL #1 conservative, 0.15 % at 95 | 30 | **75** | Doc is WRONG. 150 / (95-93) = 75. |
| Total sized for OCL #1 | 44 | **89** | Follows from the line above. |
| OCL #2, 0.25 % at 94 (after carry) | 274 (24 + 250) | 250 + carry | Carry depends on the fill assumption. |

The doc's "20 filled, 24 remain" assumes 44 sized. With 89 sized and 20 filled, 69 remain, and
OCL #2 = 69 + 250 = 319. **The owner must confirm the corrected numbers before the tests are written.**
This plan does not edit the owner's doc.

## 4. Design decisions

- **Sizing function (one place).** `ladder_qty(equity, risk_fraction, entry, htf_low, carry)` returns
  `floor(equity * risk / (entry - htf_low)) + carry`, and returns 0 when `entry <= htf_low`.
  Used by both the bot and the replay so the two cannot drift.
- **Stage state machine (one place).** A small pure class holds: stage (probe, ladder, conviction),
  carried quantity, last invalidated OCL low, whether a conservative level was touched after the
  last entry (the "escalation pair" flag). The bot and the replay both call it. Pure = easy to test.
- **Each entry is a child order.** The gate runs per entry at that entry's price and size
  (`_gate_entry` with `ref_price = entry`). Per-order caps (`max_order_quantity`,
  `max_order_notional`) still clamp each child (section 11.9 item 26).
- **Fail closed on exits.** If the cycle is aggressive-only and N is not set, the bot refuses the
  parent. Replay marks it `refused`, not `target_pending`.
- **Keep the existing parent key.** One parent per HTF cycle stays. Extra entries add quantity to
  that parent, so the restart guard (`_cycle_done`) still works.

## 5. Phases

| Phase | Work | Files (expected) | Tests | Exit check |
|---|---|---|---|---|
| P0 | This plan doc. Report the section 14 error to the owner. | `docs/IMPLEMENTATION_PLAN_2026-11-19_LADDER.md` | none (doc only) | Owner confirms numbers |
| P1 | Fail closed: refuse aggressive-only cycles while N is unset. Replay returns `refused`. | `strategies/cycle.py`, `execution/strategy_bot.py` | new: refuse when target unset; allowed when N set | No parent without an exit rule |
| P2 | Ladder sizing function and stage state machine (pure code, no broker). | new `strategies/risk_ladder.py` | new: section 14 numbers (after owner fix); carry; 0.25 re-add; escalation pair; 0.50 one-shot | Unit tests green |
| P3 | Bot: send each ladder entry as a child order, sized per entry against HTF L. | `execution/strategy_bot.py` | new: conservative touch sends an order; new-OCL entry sends an order; invalidation pauses remainder | Paper run shows the section 14 sequence |
| P4 | Replay: quantity-weighted average from the same sizes as the bot. | `strategies/cycle.py` | new: replay fills equal bot fills on a fixed bar sequence | Replay and bot agree |
| P5 | Per-cycle loss ceiling (only after owner sets the number). | `strategy_bot.py`, `risk` | new: ceiling stops new entries | Owner number in config |

Each phase: validate, run relevant tests, update this doc, commit, push. Stop and report if a
test fails. Never push a failing phase.

## 6. Owner decisions (answered 2026-11-19)

1. Section 14 numbers: the typed example values are illustrative only. The FORMULA is the rule.
2. N = 2.1 (settings.yaml `cycle.target_r`).
3. Per-cycle loss ceiling = 1 % of equity (settings.yaml `cycle.loss_ceiling`). Owner changed it from 3.1 % to 1 % on 2026-11-19.
4. Stop stays at HTF L. Conservative touches do not move it (see 6b).

6b. Plain-words note on item 4: each entry has the same stop, the HTF OCL low. A conservative touch
only adds shares; it never raises the stop. This matches section 11.7 item 17.

### Old question list (kept for history)

1. Correct the section 14 numbers (OCL #1 conservative = 75 sh, not 30; total 89, not 44).
2. Value of N (backtested target multiple). Until set, aggressive-only cycles are refused.
3. Per-cycle loss ceiling (percent of equity), or confirm "none for now".
4. Whether the conservative level sets the stop (section 10 item 11). Plan assumes no: the stop is HTF L.

## 7. Risks and limits

- Clamps by per-order caps mean the real share count can be smaller than the formula. Replay and
  bot must both apply the same clamp, or the backtest will look better than live.
- A probe entry at 0.25 % of equity over a small stop gives a very large share count. Caps must be
  set in config before any paper run (`max_order_quantity`, `max_order_notional`).
- The 5-minute LTF must stay at or above the bot interval (section 5), or touches can be missed.
- Nothing here turns live on. Live gate stays refused until the owner sets the live caps and runs
  the live checklist.

## 8. Progress log

- 2026-11-19: P1 done: N = 2.1 and loss ceiling 1 % in config/settings.yaml (3.1 % at the time, changed to 1 %) `cycle`; runner passes N to
  the sequence strategy; bot refuses cycles with no N (target_pending) with a log line; replay unchanged.
- 2026-11-19: P2 done: src/quant/strategies/risk_ladder.py (formula, probe/ladder/pair stages, carry,
  per-cycle ceiling, fail closed when the ceiling is unset). Tests: tests/unit/test_risk_ladder.py (12 pass).
  Carried shares DO join the 0.50 % conviction entry (owner 2026-11-19: join). Carry is cleared on that entry.
  Pre-existing failures (before this change, same 16): src/quant/tests/test_strategy_bot*.py. Cause: the
  pre-entry gate refuses the test entries (pre_gate_refused). Not touched yet.
- 2026-11-19: P0 started. Repo cloned to /home/claude/algo-pro at 19aeb5f. Code gaps confirmed.
  Section 14 arithmetic error found. Waiting for owner decisions (section 6).
- 2026-11-19: Red bot tests fixed (prerequisite for P3). Causes: (a) httpx and websocket-client were
  missing in the sandbox, so the feed guard failed closed; installed. (b) The owner caps in
  config/risk_limits.yaml (10 shares, 10,000 INR) now apply at the gate, so test entries were refused;
  the bot tests now use a test-only caps file (real file unchanged). (c) The cycle tests used
  target_pending, which P1 now refuses; they use the owner N = 2.1, and a new test checks the refusal.
  Result: 98 bot and cycle tests pass. Full suite: no failure that is not in the pre-work baseline
  (27 unrelated failures remain there: exit_bot, pov_executor, audit_fixes, bot_recon and others).
- 2026-11-19: P3 done (bot). Each LTF event from the replay goes through risk_ladder.py. An order is sized
  at its own price; the gate sees the whole order (fresh plus carried) as the risk fraction, so it sizes
  exactly that many shares. A stop-out (invalidated event) pauses the parent and closes the old order at
  its fills; its unfilled shares move to the next order (carry). Children are attributed FIFO to the oldest
  open order and booked at that order's price. Ladder state is saved with the parent and restored after a
  restart. Only events on the newest closed bar act; older events are skipped (freshness).
  Known limits, to fix in P4 or later:
  (a) The replay still averages entries by price, so the target (avg + N x R) uses that average, not the
      ladder's quantity-weighted average. P4.
  (b) Peg mode (_submit_peg) still uses the parent intent, not the ladder order's price. Peg is off by default.
  (c) An order that the gate clamps below its planned size keeps the clamped size; any carry that does not
      fit is dropped (logged as a clamp, not yet logged separately).
  (d) Tests: 98 bot and cycle tests pass; cycle tests rewritten to the ladder model (a stop-out now adds
      carry + 0.25 %, not the same quantity as before). Full suite: no new failures vs baseline; 27 unrelated
      failures remain.
- 2026-11-19: P4 done (replay). cycle.ladder_plan runs the same RiskLadder on the replay events. The cycle
  average is the quantity-weighted mean of the planned orders, and the target is avg + N x (avg - HTF L).
  run_cycles/run_cycle/SequenceStrategy take equity (paper capital, the same as the bot's read_funds) and the
  owner loss ceiling; ceiling None returns no orders (fail closed). Runner passes both.
  Known limit: the replay assumes every order fills in full, so no carry happens in the replay. Live POV
  fills can be partial, so the live average can differ from the replay when fills are partial. The replay
  average is the expected value under full fills; the bot's average is the real fill-weighted one.
  Replay tests: tests/unit/test_cycle*.py updated (avg 9.3125 for the probe + conservative case; new OCL case
  computed with the ladder formula). Full suite: no new failures vs baseline; 27 unrelated remain.
- 2026-11-19: P5 done. Owner loss ceiling 1 % of equity (settings.yaml cycle.loss_ceiling = 0.01, loaded as
  0.01 in the check). The ladder enforces it on the planned risk at the HTF stop for each HTF cycle, in the
  bot and in the replay. A refused order is now audited: ladder_refused with reason loss_ceiling, planned
  risk and the ceiling in INR. Fail closed when the ceiling is unset. Tests: ladder refusal recorded; bot
  refusal sends nothing and is audited. Full suite: no new failures vs baseline (27 unrelated remain).
  Note: the ceiling is a cap on planned loss at the HTF stop, not a daily loss limit. The daily loss kill
  switch (max_daily_loss) is separate and still applies.
