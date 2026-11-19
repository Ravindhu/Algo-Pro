# Implementation plan: risk ladder for the cycle strategies (2026-11-19)

Status: PLAN ONLY. No code changed yet. Live stays OFF. Paper mode only.
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

## 6. Owner decisions needed before P2

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

- 2026-11-19: P0 started. Repo cloned to /home/claude/algo-pro at 19aeb5f. Code gaps confirmed.
  Section 14 arithmetic error found. Waiting for owner decisions (section 6).
