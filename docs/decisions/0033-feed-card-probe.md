# Decision 0033 — Feed card wired to a real probe (2026-11-25)

## Context

The dashboard feed card showed `not connected` from a hard-coded line in
`dashboard/app.py` (`load_state`), even after the live feed was approved,
OpenAlgo was up, the broker session was valid and the API key was set. The
owner asked why the feed was not connected and then asked for the card to be
wired.

## Decision

1. New module `src/quant/reporting/feed_status.py`:
   - reads `config/data_sources.yaml` (`live_feed.live_allowed`) — policy gate;
   - requires `OPENALGO_API_KEY` in the environment;
   - makes ONE read-only quote call (`NSE:INFY`) through OpenAlgo; a usable
     `ltp` proves key + broker session + feed at once;
   - caches the result 30 s (`FeedProbeCache`) so the 2 s `/api/overview`
     poll never hammers OpenAlgo;
   - never raises, never writes, never places an order. Every failure maps to
     a status string: `connected` / `stale` / `api key missing` /
     `openalgo down` / `auth failed` / `blocked by policy`.

2. `dashboard/app.py`: the hard-coded line
   `overview["feed"] = {"status": "not connected", ...}` is replaced by
   `overview["feed"] = FEED_PROBE.get()`.

3. `tests/unit/test_dashboard_app.py`: `test_overview_and_health` asserts the
   new contract (status in the known set, never `not connected`); a direct
   `feed_status` contract test is added.

4. Scope note: `monitor/feed_health.py` (per-symbol PAUSED latch) stays
   bot-side and remains unwired; this decision wires the dashboard read-side
   only.

## Alternatives rejected

- Wiring `feed_health.py` into the dashboard: it is a per-symbol latch with no
  production caller; the dashboard has no symbol loop to feed it. Wrong owner.
- Making the card read `ws_proxy_stats.json`: it reflects OpenAlgo-internal
  proxy clients, not whether the engine can read a quote.

## Consequences

- The card is honest: it shows what the engine would actually get.
- One extra OpenAlgo quote call per 30 s from the dashboard process.
