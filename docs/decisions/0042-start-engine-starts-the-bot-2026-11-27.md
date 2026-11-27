# 0042 — START ENGINE starts the bot; login happens at sign-in (2026-11-27)

## Context

The dashboard START ENGINE button reached `scripts/ops/start_engine_now.py` (login, then
engine), which called `engine_control.start_engine()` → `scripts/run/run_supervised.py`.
That file is **not in this repo** — it is parked in `Temp/Stag/old_runner/` — and even it
ran `scripts/run/engine_entry.py`, also absent. `b3b067c` moved the button one level deeper
into the same dead end.

Effect: pressing START ENGINE logged into the broker (real side effect) and then stopped
silently. `POST /api/engine/start` returned success, because `request_manual_start` only
checked that the *runner* existed, never the supervisor.

Separately, the login was in the wrong place. `quant.core.broker_signin.login_after_signin`
already logs in OpenAlgo + broker on every dashboard sign-in (built 2026-11-24, 3 tests).
Logging in again from the button hits the broker login cooldown for a session the operator
has just created.

## Decision

| Concern | Owner | Where |
|---|---|---|
| OpenAlgo + broker login | Dashboard **sign-in** | `broker_signin.login_after_signin` (unchanged) |
| Screener.in session | **Every START ENGINE**, when a login is needed | `start_engine_now.ensure_screener_session` |
| Starting the bot | **START ENGINE** | `engine_control` → `start_engine_now.py` → `supervise_bot.py` |
| Paper accounting loop | Unchanged | SANDBOX popup → `POST /api/loop/start` → `scripts/run/paper_loop.py` |
| Paper vs real money | **OpenAlgo analyzer (sandbox) switch**, owner's UI | `OpenAlgoClient._require_analyzer_off` |

- `engine_control.SUPERVISOR` = `scripts/ops/supervise_bot.py` (new). It runs the strategy
  bot under `quant.engines.core.supervisor` with the gates kill switch, OpenAlgo reachable,
  broker session, positionbook, working orders, quote. **Market-hours gate deliberately
  excluded**: the bot is needed for exits, and it already refuses entries after square-off.
- `--mode paper|live` picks the runner (`run_strategy_bot.py` / `run_strategy_bot_live.py`).
  Mode does not relax any gate; live still needs `bot.yaml` mode+live_enabled, and the
  analyzer switch still decides real vs simulated.
- `SCHEDULED_PREOPEN.cmd` and `start_all.py --engine` re-pointed at `supervise_bot.py`.

## Screener session timing (owner request, same day)

"A session on every start when login is needed." `ensure_screener_session` runs on every
START ENGINE and delegates to `screen_refresh.ensure_session`, which is free when a session
is already valid. The reuse rule now has a **09:00 IST morning boundary**
(`screener_session.SESSION_REFRESH_HOUR`): a marker written before 09:00 does not cover today,
because a portal cookie is a session cookie and dies with the trading day. A refused login is
reported and **never blocks the bot** — the bot must still start to manage exits.

## What this does NOT change

- The paper trading accounting loop (`paper_loop.py`, pre-recorded signals) stays as it is.
  It is not the strategy bot; the OpenAlgo sandbox analyzer is the fake-money path for the bot.
- No new order path. Only `execution/openalgo_client.py` may place an order; this work does
  not touch it.

## Consequences

- One login per day, at sign-in. No cooldown collision from the button.
- Every path the button reaches is asserted to exist in `tests/unit/test_start_engine_now.py`,
  so a commit like `b3b067c` now fails CI instead of shipping.
- `config/bot.yaml` currently has `mode: live` / `live_enabled: true`: with `--mode live` and
  the analyzer OFF, START ENGINE trades real money. That is the owner's existing setting and
  is unchanged here.
