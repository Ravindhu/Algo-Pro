# Implementation plan: manual symbols in the Screener stocks panel (2026-11-27)

Flow stage: Liquidity screener -> screen file (owner input)
Status: DONE (tests green: 2718 passed)

## Goal (plain English)
The owner can add NSE symbols by hand from the Screener stocks panel header. A manual symbol
is merged into every screen refresh. It is NOT a bypass: it still needs a live quote, a valid
circuit band and no ASM/GSM flag, exactly like a Screener.in name.

## Phases
1. [ ] Module `src/quant/data/manual_symbols.py`: validate, read, add, remove (atomic file
       `data/screened/manual_symbols.json`, max 50 symbols, never raises on read).
2. [ ] `screen_refresh.make_screen_refresher(manual_path=None)`: append manual symbols to the
       candidates (de-duplicated). Default None = no change for existing callers and tests.
3. [ ] Bot wiring `scripts/run/run_strategy_bot_live.py` passes the manual path.
4. [ ] Dashboard: owner-only `POST /api/screener/manual` (add/remove, audited); `manual_symbols`
       added to `/api/overview`.
5. [ ] UI: small input + Add button and removable chips in the Screener stocks header
       (viewers see the chips; the write returns "sign in as the owner").
6. [ ] Tests: validation, merge, gate still applies (a manual symbol with ASM/no quote is
       dropped), route auth, audit line, route-set test updated.
7. [ ] Full test run, secret scan, commit, push branch, fast-forward master.

## Rules kept
- Minimal diff; no order path touched.
- Audit every add/remove with actor.
- Bot picks up a change at its next 5-minute refresh (no immediate rebuild).

## Phase 2 (2026-11-27, owner request): M and A source switches, manual rows first

Owner words: "Create manual M and automation A button at header. If both are not selected then
both paths should work. Sort manual stock first, screener stocks next."

Rule (decided here, owner to confirm if different):
- M on, A off  -> bot uses manual symbols only.
- A on, M off  -> bot uses Screener.in selection only (no manual symbols).
- Both on, or both off -> both paths work.

Steps:
1. [x] `src/quant/data/source_mode.py`: read/set/effective, file `data/screened/source_mode.json`.
2. [x] `make_screen_refresher(mode_path=None)`: applies the rule each cycle; the Screener.in scrape
       runs only when the automation path is on. A manual symbol that is also screened is tagged
       manual (not duplicated).
3. [x] `refresh_once` tags each written row `source: manual | screener`.
4. [x] Bot wiring passes `mode_path`.
5. [x] Owner-only `POST /api/screener/source` {key, on} (audited); `screener_source` in overview.
6. [x] Panel: M and A toggle buttons in the header; manual rows sorted first, tagged "· manual".
7. [x] Tests + full suite + secret scan, commit, push, fast-forward master.
