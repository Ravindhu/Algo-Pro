# Implementation plan — Notification tab in the Execution log (2026-11-27)

## Goal
The main dashboard's "Execution log" block gets two tabs, styled like the Range / Sorting tabs
of a grid: **Fills** (the existing broker fills, unchanged) and **Notification**. The Notification
tab lists every alert, warning and critical event the quant bot raised, newest first.

## Source of truth
The live and paper bots already write every alert to their audit trail
(`data/bot_live/audit.jsonl`, `data/bot/audit.jsonl`): kind `alert` with `level` and `message`.
The notification list is read from those files. Nothing new is written by the bot.

## Phases
1. **Backend reader** — `src/quant/reporting/notifications.py`. Reads the tail of each bot audit
   file, keeps `alert` rows and these warning kinds: `exit_halted`, `exit_held_stale_bar`,
   `positions_unmanaged` (critical); `exit_not_sent`, `cycle_skipped`, `entries_blocked`,
   `limits_refresh_failed` (warning); `screen_refresh` with a non-zero exit code; `startup_recon`
   with a status other than match or skipped. Newest first, capped at 50 items. Read-only; a
   missing or unreadable file gives an empty list, never an error.
2. **Overview** — `/api/overview` gains a `notifications` key from phase 1.
3. **Dashboard** — the Execution log block gets the tab strip; the Notification table shows
   time (IST), level, source and message; the tab shows the count.
4. **Tests** — reader rules (kinds, levels, order, cap, bad lines); the page contract (tab ids,
   renderer present). Full suite must stay green.

## Not in scope
- No change to the fills data or the sandbox "Execution log (paper fills)" block.
- No acknowledge/clear buttons (a later decision).

## Status
- [x] Phase 1 plan written
- [x] Phase 1 reader and tests
- [x] Phase 2 overview key
- [x] Phase 3 dashboard tabs
- [ ] Phase 4 full suite, commit, push
