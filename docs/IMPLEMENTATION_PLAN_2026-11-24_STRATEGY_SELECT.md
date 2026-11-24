# Plan: dashboard strategy choice drives the bot (2026-11-24)

Owner request: the Trade setup panel should list all five strategies and the
choice should control what the bot runs. Live stays under the owner's manual control.

## Scope
1. `src/quant/core/setup_store.py`: the five names come from `SEQUENCE_KINDS`.
   A new `strategies` list is validated (at least one, no duplicates, known names only)
   and written to the `strategies:` list in `config/bot.yaml`, keeping comments.
2. `dashboard/main_dashboard.html`: the single dropdown becomes five tick boxes.
   Saving sends the ticked list.
3. Tests: `tests/unit/test_strategy_select.py`.

## Not in scope (unchanged)
- `live_enabled`, `mode`, live caps, egress IP and live approval stay as they are.
  The owner turns live on by hand.
- The legacy single `strategy` key still saves to settings and is not used by the bot.

## Behaviour
- The bot reads `bot.yaml` when it starts. A new selection applies at the next start.
- Refused input leaves `bot.yaml` unchanged.

## Verification
- New unit tests, plus the existing setup tests and the full quant suite.
- Pull request for owner review before merge (changes which strategies trade).
