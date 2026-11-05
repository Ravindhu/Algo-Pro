# `frontline.screener` — Exclusion skill

**Stage:** `screener` · **Engine:** `screener/screener_engine.py` ·
**Publishes:** `data/agent_stage/screener.json`

## Purpose

Exclude symbols that must not be traded today — governance events, results
leaks, fraud flags, halted names — with a stated reason for every exclusion.
It can only *remove* names from the universe, never add one.

## Output contract (`_SCHEMAS["screener"]`)

| Key | Type | Rules |
|---|---|---|
| `asset_blacklist` | array of tickers | uppercase, `A-Z0-9&._-`, ≤ 20 chars, ≤ 250 items |
| `blacklist_reasons` | array of objects | each `{ticker, reason}`; `reason` ≤ 10 words; every ticker **must** appear in `asset_blacklist` |
| `data_requests` *(optional)* | object | `{"needs": [...], "reason": "..."}` |

## Evidence it wants

None mandatory — an empty blacklist is a valid, honest answer.

## What it may do

- Blacklist **any** symbol for **any** reason it can state. There is no cap on
  how many names it may remove (up to the 250 schema bound).
- Request more data (announcements, corporate-action calendar, surveillance
  lists).

## What it may **not** do

- Add a symbol to the universe.
- Emit a reason for a ticker it did not blacklist (validator rejects).
- Write anything but `screener.json`.
- Place, size or cancel an order.

## Failure behaviour

Non-blocking: with no valid answer the engine proceeds with an unfiltered
universe. That is deliberate — a broken screener skill must not silently empty
the universe.

## Tests

`tests/test_frontline_agent.py`, `tests/test_screener_engine.py`
