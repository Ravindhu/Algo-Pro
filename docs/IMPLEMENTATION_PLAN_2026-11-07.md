# Implementation plan, 2026-11-07

Plain-English plan for this batch. Paper and research only. Nothing here sends an order.

## Done in this batch
| Item | What | Where | Test |
|---|---|---|---|
| 4 | Feed reconnect with exponential backoff (pure schedule; connect and sleep are injected) | `src/quant/data/reconnect.py` | `tests/unit/test_reconnect.py` |
| 11 | Reconciliation job: ledger vs broker positions, logged to the audit log | `src/quant/monitor/recon_job.py` | `tests/unit/test_recon_job.py` |
| 17 | Startup clock-skew check against NTP, logged; skew blocks trading (fail closed) | `src/quant/monitor/clock.py` | `tests/unit/test_clock.py` |
| 18 | Backup of ledger, audit log and limits with SHA-256 manifest; restore refuses to overwrite and checks hashes | `src/quant/monitor/backup.py` | `tests/unit/test_backup_restore.py` |
| 19 | CI: GitHub Actions runs `pytest tests/unit tests/parity` on push and pull request | `.github/workflows/tests.yml` | runs in GitHub |
| 22 | Strategy-deviation alert: CUSUM drift alarm emits an ERROR alert | `src/quant/monitor/strategy_health.py` | `tests/unit/test_deviation_alert.py` |

## Not done in this batch (needs an answer first)
| Item | Why not yet |
|---|---|
| 1 Demerger | Needs the demerger ratio convention for back-adjustment. Confirm the rule. |
| 3 Delivery calendar | Delivery has no expiry and uses the same holiday list. Unclear what to add. Please say what "for delivery trading" should change. |
| 6 Square-off retry | Goes through the order path. Needs the gate design approved. |
| 7 Circuit freeze wired to feed | Needs the halt event source from the broker feed. |
| 9 Pre-open state | Needs the exact NSE pre-open times from the circular. |
| 10 T+1 funds in gate | Changes the risk gate. Needs approval. |
| 13 SEBI algo tag | I cannot confirm the SEBI rule. Owner must confirm the rule. |
| 15 Audit off-machine copy | Owner decision on where copies go. |
| 23 Hedge overlay | Deferred by owner instruction. |
| 24 Order-rate caps | Exchange cap value comes from the broker. Needs the broker value. |

## Scheduling not done here
- Nightly backup, reconciliation and clock check need an OS task (Windows Task Scheduler or cron) on the owner machine. This batch provides the functions only.
- The reconciliation job needs a read-only broker positions reader wired in. This batch takes it as an argument.

## Rules followed
- Each new file has a `Flow stage:` line and a `Status:` line (checked by `tests/unit/test_layout.py`).
- No new code calls the order API.
- Tests run in the sandbox without network. Unit tests use fakes.

## Update: second batch
| Item | What | Where | Test |
|---|---|---|---|
| 3 | Delivery (CNC) settlement is T+1 on trading days; holidays and weekends skipped; refuses if the calendar is unverified | `src/quant/core/calendar.py` (`settlement_date`) | `tests/unit/test_settlement_t1.py` |
| 9 | Cash pre-open phases from NSE circular 61/2019 (NSE/CMTR/41180). Entries until 09:07, uncertain 09:07-09:08, market orders refused in pre-open | `src/quant/core/pre_open.py` | `tests/unit/test_pre_open.py` |
| 13 | Optional exchange-approved algo ID on each intent. Refused only when `require_algo_id=True` | `core/types.py`, `risk/pretrade.py` | `tests/unit/test_algo_tag.py` |

Notes:
- Item 9: the 7 Sep 2026 pre-open change is for equity derivatives (F&O) only, per secondary sources. Cash timings above come from the 2019 NSE circular. Check against the current cash circular before live use.
- Item 13: the owner runs a personal algo through OpenAlgo, hosted on a whitelisted IP. The owner has decided no compliance block for now. The gate does not pass `require_algo_id` yet. Wire it when you want the check on.
- Item 1 (demerger): dropped by owner instruction.
- Still deferred: 6, 7, 10, 15, 23, 24. See above.

## Update: third batch (approved items 6, 7, 10, 24; item 15 dropped; item 23 deferred)
| Item | What | Where | Test |
|---|---|---|---|
| 24 | Order-rate throttle: 8 per second (80% of OpenAlgo's documented 10/s for placeorder). Waits up to 2 s, then refuses (not sent) | `monitor/throttle.py` (`acquire`), `execution/openalgo_client.py` (`AnalyzeBroker`) | `tests/unit/test_order_rate.py` |
| 6 | Exit retry. **Design change from the recommendation:** a retry happens only if the broker shows no open order for that intent AND the position still needs closing. Without both readers, no retry: a CRITICAL alert is raised. An order that exists is never resent | `execution/retry.py`, `AnalyzeBroker` | `tests/unit/test_exit_retry.py` |
| 10 | Funds check in the gate: entry refused if notional > available funds. Not checked when funds are None (live runner must pass funds) | `risk/gate.py` (`available_funds`) | `tests/unit/test_gate_halt_funds.py` |
| 7 | Circuit freeze in the gate: entries refused while an index is frozen. Freeze also on an index move of 10% or more from previous close (fail closed) | `risk/gate.py` (`halt`), `risk/controls.py` (`index_move_freezes`) | `tests/unit/test_gate_halt_funds.py` |

Notes:
- Item 24: the mStock-specific limit is not listed in the OpenAlgo docs. OpenAlgo's page says a 429 can also come from the broker. Confirm mStock's own per-second limit with the broker.
- Item 6: the position reader and open-order reader are not built yet. Until they are passed in, exits never retry and always raise CRITICAL.
- Item 7: no live halt-message field is wired. Only the index-move fallback and the gate check exist.
- Item 10: `run_paper.py` does not pass funds yet. A live runner must pass funds, or entries are not checked.
- Item 23: deferred, per owner.

## Update: live read-side wiring
What is wired now (read-only; no order is sent by these changes):
- `execution/readonly_client.py`: adds `funds` and `positionbook` read paths (five read paths total).
- `execution/live_inputs.py`: `LiveReaders` gives funds (fail closed to 0), position per symbol, and open-order check for exits (fail closed to "no retry").
- `scripts/run_paper.py`: with `--broker analyze`, the gate gets live funds for entries, and exit retries use the live position and open-order readers.

Not done, and why:
- Live order placement: NOT enabled. It still needs the owner's `LiveApproval` and an approved risk limits file (decision 0017 and the live guard).
- Halt message source (item 7): OpenAlgo's documented endpoints have no halt field. Only the index-move fallback and manual freeze exist.
- Orderbook intent tag: not documented. Open-order match is by symbol and side (conservative).
- The analyze-mode sandbox may return sandbox funds and positions, not the real account. Confirm on the owner machine.

## Update: main dashboard (2026-11-07)
| Item | What | Where | Test |
|---|---|---|---|
| Main dashboard | NSE/BSE main dashboard with header and FLOW popup (the v5 SVG diagram). Static placeholder values, not wired to the API yet | `dashboard/main_dashboard.html`, decision `docs/decisions/0027-main-dashboard.md`, AGENTS rule 10 | none (static page; open in a browser) |

Notes:
- `/ui` in `dashboard/app.py` still serves the older page until the owner approves the route switch.
- The flow popup is generated from the SVG (`dashboard/assets/flow_svg.js`).

## Update: /ui switched to the main dashboard (2026-11-07)
| Item | What | Where | Test |
|---|---|---|---|
| /ui route | `/ui` serves `dashboard/main_dashboard.html`. Trade setup copied from the old page (reads and saves `/api/setup`) | `dashboard/app.py`, `dashboard/main_dashboard.html` | `tests/unit/test_main_dashboard.py` |

Notes:
- Kill switch, START/STOP and limit inputs are not wired on the main page yet. The kill switch button does nothing. Do not rely on it.
- `src/quant/index.html` is no longer served. Kept until its tests move.

## Update: main dashboard wired, old page removed (2026-11-07)
| Item | What | Where | Test |
|---|---|---|---|
| Wire main page | Reads `/api/overview`; limits form, kill trip/reset (token, reason, confirm), trade setup. P&L hidden by default from settings | `dashboard/main_dashboard.html` | `tests/unit/test_pnl_visible.py`, `tests/unit/test_main_dashboard.py` |
| Remove old page | Deleted `src/quant/index.html`; its checks moved to the main page | `tests/unit/test_pnl_visible.py` | `test_old_pages_are_gone` |
| Remove Forex mock | Deleted the published preview artifact (chat only; not in repo) | none | none |

Open:
- START and STOP disabled. Equity curve and live neural shell not built.

## Update: equity curve (2026-11-07)
| Item | What | Where | Test |
|---|---|---|---|
| Equity curve | Realized P&L after each closed trade, from 0, with max drawdown. Shown on the main page | `src/quant/reporting/dashboard_data.py` (`equity_curve`), `dashboard/main_dashboard.html` | `tests/unit/test_dashboard_data.py` |

Still open: START and STOP. They need a design decision first (see the chat).

## Update: paper and live mode on the dashboard (2026-11-07)
| Item | What | Where | Test |
|---|---|---|---|
| Mode buttons | PAPER and LIVE in the header. Backend: GET and POST `/api/trading-mode`. LIVE is always refused (409) | `src/quant/core/trading_mode.py`, `dashboard/app.py`, `dashboard/main_dashboard.html`, decision `0028` | `tests/unit/test_trading_mode.py`, `tests/unit/test_pnl_visible.py` |

## Current status: dashboard (consolidated, 2026-11-07)

Living status. Older sections above are the batch log.

### What is live (served at `/ui`)
- The page is `dashboard/main_dashboard.html` (decision 0027). The old page `src/quant/index.html` is deleted.
- Reads `/api/overview` every 2 s: mode, kill switch, feed, realized and unrealized P&L (hidden by default from settings), win rate, profit factor, risk used, open positions, safety events, AI insight (rules only), current limits, realized equity curve with max drawdown.
- Writes, each with the token in the header and an audit record: limits (`/api/limits`), kill switch trip and reset (`/api/kill/*`, reason required, confirm), trade setup (`/api/setup`), and paper or live mode (`/api/trading-mode`, decision 0028).
- LIVE is always refused (409). Live needs LiveApproval (decision 0017), which the dashboard never creates.
- Flow popup: the v5 SVG via `dashboard/assets/flow_svg.js` (decision 0027).

### Not built or not live (open)
| Item | Why | Needs |
|---|---|---|
| START / STOP (background paper loop) | No live signal source. Loop design needs two answers (signal input, paper-only) | Owner decision (option B) |
| Neural shell values | Layout only | Backend feed for each node |
| Screener table | Removed from the page; no backend data | Screener output wired to an endpoint |
| Market hours | Hours only (09:15-15:30 IST, Mon-Fri). Exchange holidays not checked | Holiday list from `config/` |
| Equity curve, unrealized | Realized only. Past prices are not stored | Price history store |
| Live trading | Refused by design | Decision 0017 flow, owner approval |

### Test state (this commit)
- Passing in the sandbox: `test_dashboard_data`, `test_trading_mode`, `test_pnl_visible`, `test_main_dashboard`, `test_layout`.
- Skipped here: FastAPI route tests (`test_dashboard_app.py`). FastAPI is not installed in the sandbox. Run them on the owner machine after `pip install -r requirements.txt`.
- Full `tests/unit`: 27 failures in the HRP set, same as before this dashboard work.

### Next decisions for the owner
1. START / STOP: choose option A, B or C (see chat).
2. Holiday list source for market hours.
3. Screener and neural-shell data: which endpoint to add.

## Update: paper loop and holidays (2026-11-07, decision 0029)
| Item | What | Where | Test |
|---|---|---|---|
| Paper loop | START / STOP run `scripts/run/paper_loop.py` in the background. Paper only. NSE cash session only. Refused with the kill switch on or if already running. STOP does not close positions | `src/quant/monitor/paper_loop.py`, `scripts/run/paper_loop.py`, `dashboard/app.py` (`/api/loop/*`), `dashboard/main_dashboard.html` | `tests/unit/test_paper_loop.py`, `tests/unit/test_pnl_visible.py` |
| Holidays | Equity list from NSE Capital Market circular 12 Dec 2025 + amendment 12 Jan 2026. Dashboard shows market status and holiday count | `config/calendar/holidays.csv`, `src/quant/core/calendar.py` | `tests/unit/test_paper_loop.py`, `tests/unit/test_calendar_holidays.py` |

Open:
- No live signal source. The loop only records "no signals file" until `data/paper/signals.csv` exists.
- Owner to confirm the holiday list on nseindia.com, and run `scripts/research/fetch_nse_holidays.py` on the owner machine.
- FastAPI route tests are skipped in the sandbox (FastAPI is not installed). Run them on the owner machine.
- The unit suite shows 27 HRP-related failures, the same as before this work.
