# Implementation plan: wire or retire the unwired safety modules (2026-11-25)

Flow stage: Risk gate, exits, monitoring (live readiness)
Status: IMPLEMENTED on branch wire/safety-modules (tests pass). NOT on master. Live still off.

## 1. Why
A static import scan of master `e94cd12` found 91 of 379 quant modules that no
running code imports (51 imported only by tests, 40 with no reference at all).
Before wiring anything, each candidate was checked against what the live bot
already does. Several flagged modules are duplicates of live code.

## 2. Findings per module

| Module | Live equivalent | Finding | Proposed action |
|---|---|---|---|
| `risk/squareoff.py` | none | MIS square-off (15:15) is not enforced by any live code. Every live order is `product: MIS` (openalgo_client.py). Only tests use it. | **Wire (priority 1)** after owner decision (section 3) |
| `engines/core/owner_limits.py` | `core/owner_limits_store.py`, used by `risk/gate.py` | Duplicate. The live gate reads the store, not this file. | **Retire** (park in Temp/Stag) after diff check |
| `risk/exits.py` | `execution/exit_manager.py` (used by strategy_bot) | Overlaps. exits.py claims a time-limit exit that exit_manager may not have. | **Check** time-limit gap; wire the gap or retire |
| `monitor/live_monitor.py` | none | Pure functions (max drawdown, excursions). No caller. | Low risk. **Wire as read-only reporting** to dashboard later, or park |
| `execution/protection.py` | `strategy_bot` uses broker SL-M stops | Not verified as duplicate. | Check before deciding |

Live (already wired, do not change): `execution/exit_manager.py`,
`risk/kill_switch.py`, `core/owner_limits_store.py` via `risk/gate.py`,
daily loss limit in `strategy_bot.py`.

## 3. Decisions for the owner
1. **Square-off behaviour.** At 15:15 IST, should the bot (a) block new entries
   only, or (b) also flatten open MIS positions with market orders through the
   same gate? Default proposed: (b), flatten through the existing exit path.
2. **Broker auto square-off.** Brokers usually square off MIS positions near
   market close. Confirm the broker's exact time and whether it charges a penalty,
   because our own flatten should run before it.
3. **Retire the duplicate owner_limits module?** Proposed yes.

## 4. Steps (after owner decisions)
1. Write failing tests for the 15:15 entry block and flatten (paper broker).
2. Wire `should_square_off` into the strategy bot cycle, through the existing
   exit path and gate (no direct broker call).
3. Diff `risk/exits.py` time-limit against `exit_manager`; port or retire.
4. Park `engines/core/owner_limits.py` in Temp/Stag after a grep shows no caller.
5. Run full suite; commit; push branch; open PR for review. Do not push master.

## 5. Not in scope
- No live run, no order placement, no change to limits or the approval file.
- Owner-run live checks (SEBI checklist, kill-switch drill, analyze soak) stay with the owner.

## 6. Risks
- Flatten at 15:15 can fail if the broker rejects orders. The bot must alert,
  not retry blindly, and must not reverse the position.
- Changing the exit path affects every open position. Test with paper broker first.

## 7. Owner decisions (2026-11-25) and what was built

1. CNC: only block new entries; never flatten. MIS: close before the broker's square-off.
   Built: `risk/squareoff.session_policy`. Entries blocked from the cutoff for all products;
   MIS positions flattened through the normal exit path (`_send_exit`, reason `square_off`).
2. Cutoff: the bot stops 15 minutes before the 15:30 close, at 15:15 IST.
   Built: `BotConfig.square_off_time = "15:15"`, validated as HH:MM.
3. Retire the duplicate owner-limits module and move it to Temp/Stag.
   Built: moved to `Temp/Stag/owner_limits_duplicate/` with its tests and a README.
   Two audit tests of the duplicate moved with it (kept in the same folder).

## 8. Limits of this change (read before live)

- **CNC is refused by config** (`product` must be MIS). Orders are still hardcoded to MIS in
  `openalgo_client.py`. Supporting CNC needs the product carried on each intent. Not done.
- **Feed outage holds the flatten.** If the bar feed is down for more than `max_outage_cycles`,
  the existing rule holds the position and raises a CRITICAL alert. A held MIS position can
  stay open past 15:15 in that case. A person must act.
- **Halted exits are skipped.** A position with `exit_halted` set is not flattened. This is the
  existing behaviour for exit errors and needs a person.
- **Repeated sends.** If the flatten order is not filled, it is re-sent each cycle (5 min), as
  the existing exit retry does.
- **Clock.** The check uses `now.time()` of the cycle timestamp. The live runner must pass IST time.
- **Null-cap coverage.** Two audit tests of the duplicate moved out. The live store and
  `test_owner_limits_at_gate.py` cover the gate. Confirm with the owner.
- **Not in this change:** `risk/exits.py` (time-limit overlap with exit_manager), `monitor/live_monitor.py`,
  `execution/protection.py`. Still open per section 2.

## 9. Test results

- New: `src/quant/tests/test_square_off_wiring.py`, 16 tests, all pass.
- Against the unchanged master bot, 7 of those fail (entry block, flatten, config checks). The
  9 pure-policy tests pass on both, as expected.
- Full suite on this branch: 2408 passed, 2 skipped, 1 xfailed.
