# File: docs/IMPLEMENTATION_PLAN_2026-11-12_MULTI_BROKER.md

> Plan only. No code changed in this commit. Owner asked for different brokers running at the same time.
> Live trading stays refused in code until the owner approves it (see Phase 9).

## 0. Goal in simple English

Run several broker accounts at once, at different brokers (for example, one Zerodha account and one Angel account). Each account trades through its own session. The bot sees all of them as one portfolio. Orders, positions, P&L and risk are tracked per account and in total.

Success means:
- Two or more accounts at different brokers can be connected at the same time.
- Every order says which account it goes to. No order goes to an account by guess.
- The risk gate checks each account, and also checks the whole portfolio.
- One broker going down does not stop the other accounts, and does not send orders to the wrong one.
- The dashboard shows each account and the combined view.

## 1. What the code does today (facts from the repo, 2026-11-12)

| Area | Current behaviour | File |
|---|---|---|
| Order client | One `OpenAlgoClient` per process. Reads one `OPENALGO_API_KEY` from env. Sends to one `base_url`. | `src/quant/execution/openalgo_client.py` |
| Risk gate | One `RiskState` for the whole bot (`open_notional`, `daily_pnl`, `strategy_pnl`). No account field. | `src/quant/risk/gate.py` |
| Intent | `TradeIntent` has no `account_id`. | `src/quant/core/types.py` |
| Broker keys | One row per broker in `broker_keys` table, encrypted. Many brokers can be stored. | `src/quant/openalgo_platform/database/broker_keys_db.py` |
| Broker session | Code comment says one broker is logged in at a time. A login to a second broker ends the first session. Switch is refused while the first runs live automation. | `src/quant/openalgo_platform/utils/broker_switch.py` |
| Reconciliation | Compares bot positions with one broker's position book. | `src/quant/monitor/broker_recon.py` |
| Live guard | `LiveApproval` object (human-made) + limits file hash + compliance gate. Fails closed. | `openalgo_client.py` |
| Bot | Single bot cycle (`strategy_bot.py`), single `risk_limits.yaml`, single `bot.yaml`. | `src/quant/execution/strategy_bot.py`, `config/` |

**Key finding:** the broker session is one-at-a-time. Adding accounts at different brokers at the same time therefore cannot be done by just adding a config list. It needs one session per account. The likely route is one OpenAlgo instance per broker account (each with its own API key and port). This must be **verified in Phase 0** before any code is written.

## 2. Rules this plan must obey (from AGENTS.md)

- Each stage goes in v5 flow order. Name the owning stage: Broker layer, Execution (OpenAlgo), Risk gate, Post-trade monitor, Dashboard.
- Only `execution/openalgo_client.py` may contain the order call (`.placeorder(`).
- Only the risk gate approves order intents. No other module imports the order client.
- `Blue_engine/` is wrapped, never copied. Reuse `broker_keys_db.py`, `broker_switch.py`, `broker_recon.py` through wrappers.
- Secrets stay in `.env` (git-ignored). Config holds only env-var names, never key values.
- `live_enabled` stays `false`. Real money needs a human `LiveApproval` per account.
- Auto-tune must not change any risk-bounding key.
- Dashboard UI goes only in `dashboard/main_dashboard.html`. No new dashboard page.
- A new or changed stage updates the SVG, `docs/quant_system_flow_v5.md`, and `AGENTS.md` in the same commit.
- Never delete files. Move unwanted files to `Temp/`.
- Test suite: `pytest` (no args), paths from `pyproject.toml`.

## 3. Design (target state)

```
config/accounts.yaml          (new)  account list: id, broker, base_url, api-key env name, mode, enabled
                                      no secret values in this file
AccountRegistry               (new)  loads and validates accounts; refuses duplicates and unknown brokers
AccountSession (per account)  (new)  one OpenAlgoClient + its own mode guard + its own LiveApproval
OrderRouter                   (new)  picks the session from intent.account_id; refuses if none
TradeIntent.account_id        (edit) new required field
RiskState per account         (edit) open_notional, daily_pnl, strategy_pnl keyed by account
PortfolioRisk                 (new)  total exposure and total daily loss across accounts
Recon per account             (edit) broker_recon reads each account's position book
Portfolio ledger              (new)  positions and P&L per account and total
Dashboard panel               (edit) account switcher, combined view, per-account broker status
```

Flow placement:
- Broker layer: `AccountRegistry`, `AccountSession`.
- Execution (OpenAlgo): `OrderRouter`, per-account `OpenAlgoClient`.
- Trade intent / Risk gate: `account_id` on intent, per-account and portfolio checks.
- Post-trade monitor: per-account recon and ledger.
- Dashboard: panel in `main_dashboard.html`.

## 4. Phases

Each phase is done only when its **exit check** passes and the Definition of Done in AGENTS.md holds.

### Phase 0 — Discovery and verification (no code)

Steps:
1. Read the official OpenAlgo docs (https://openalgo.in). Confirm:
   - Can one OpenAlgo instance hold more than one broker login at the same time? (Code says no.)
   - If not, confirm how to run one instance per broker (port, data folder, API key).
   - Confirm the `placeorder`, `cancelorder`, `orderstatus`, `positionbook` payloads for each instance.
2. Read `Blue_engine/core/client.py` and `src/quant/execution/strategy_bot.py` end to end.
3. Read `src/quant/monitor/broker_recon.py` and `src/quant/risk/money_checks.py`.
4. List every place that uses one global `api_key`, one `base_url`, or one `RiskState`. Save the list.
5. Write `docs/decisions/0030_multi_broker_sessions.md`: the chosen route (one instance per account, or other), and why.
6. Write the owner questions (section 6) into the decision note.

**Exit check:** decision note committed. OpenAlgo behaviour is written down with the doc link. Owner has answered Q1, Q2 and Q3 (they block design).

### Phase 1 — Account registry (config and loader)

Steps:
1. Create `config/accounts.yaml` with an example of 2 accounts, each marked `enabled: false`.
   Fields: `account_id`, `broker`, `openalgo_base_url`, `api_key_env` (name only), `mode` (`analyze` or `paper`), `enabled`, `max_open_exposure` (per account, `null` until owner sets it).
2. Create `src/quant/core/accounts.py`:
   - `Account` dataclass (frozen).
   - `load_accounts(path)` returns a list and validates it.
   - Refuses: duplicate `account_id`, unknown broker, a `base_url` shared by two enabled accounts, a missing env var for an enabled account, mode `live` (not allowed in this phase).
3. Add `account_id` to `TradeIntent` in `src/quant/core/types.py`, with a default of `"default"` so old code and tests still pass.
4. Add unit tests in `src/quant/tests/test_accounts.py`:
   - valid config loads
   - duplicate id refused
   - unknown broker refused
   - shared URL refused
   - missing env var refused
   - live mode refused
5. Update `config/settings.yaml` only if a new key is needed.

**Exit check:** all new tests pass. Old tests still pass (`pytest`). No secret value appears in the file or in logs.

### Phase 2 — Per-account session and guards

Steps:
1. Create `src/quant/execution/account_session.py`:
   - `AccountSession` holds one `OpenAlgoClient`, its API key (read from env at start), its mode, and its own `LiveApproval` slot (empty in this phase).
   - Each session checks its own analyzer state before every send (existing `_require_analyzer_on` logic, unchanged).
2. Create `src/quant/execution/order_router.py`:
   - `OrderRouter.register(session)` and `OrderRouter.session_for(account_id)`.
   - Unknown or disabled `account_id` returns a refusal, never a default account.
3. Wrap, do not copy: reuse `OpenAlgoClient` as is. Do not change its guards.
4. Tests (`src/quant/tests/test_account_session.py`, `test_order_router.py`):
   - order to account A goes only to A's URL (fake transport records calls)
   - order with unknown account is refused, transport not called
   - analyzer off on account B does not block account A
   - one account's transport error does not change another's state

**Exit check:** placing one order per account in analyze mode reaches the right fake URL. Live mode still refused.

### Phase 3 — Risk gate with accounts

Steps:
1. Change `RiskState` in `src/quant/risk/gate.py` to keep per-account values:
   - `open_notional[account_id]`, `daily_pnl[account_id]`, `strategy_pnl[(account_id, strategy_id)]`. `seen_ids` stays global, because intent ids are unique.
   - Keep the existing single-account behaviour when only `"default"` exists (backward compatible).
2. Add `src/quant/risk/portfolio_risk.py`:
   - Total open exposure across accounts vs `max_open_exposure` (portfolio cap).
   - Total daily loss across accounts vs `max_daily_loss` (portfolio cap).
   - Max open symbols across accounts (portfolio cap).
   - Per-account caps come from `config/accounts.yaml`.
3. `check_intent` runs: account checks, then portfolio checks. Any breach = refusal.
4. Symbol rule (owner decision Q2): default plan is "same symbol may be held in two accounts, but total symbol exposure is capped". Implement as a config switch, default off.
5. Kill switch: add scope `global` and `account:<id>`. A global kill blocks all. An account kill blocks only that account.
6. Tests (`src/quant/tests/test_gate_multi_account.py`):
   - account A at its cap refuses A, allows B
   - portfolio cap refuses both accounts together
   - daily loss on A does not change B's daily counter
   - kill on account A blocks A only
   - global kill blocks all
   - single `"default"` account behaves exactly as before (regression)
7. Check `config/risk_limits.yaml`: new per-account keys must match the risk-bounding list in AGENTS.md, so auto-tune cannot change them.

**Exit check:** all gate tests pass. Existing gate tests pass with no change in expected results.

### Phase 4 — Order management (OMS)

Steps:
1. Store `account_id` on every order record and on the audit row.
2. Order ids: prefix each order tag with the account id, so a fill can be matched back. Keep the intent id as the unique key.
3. Cancel and status calls use the account of the original order (look it up from the order record). Never guess.
4. Rate limit per account: reuse `TokenBucket` in `monitor/throttle.py`, one bucket per account (current limit 8 orders/second per account).
5. Exit retry (`execution/retry.py`) runs inside the same account. An exit on account A never retries through account B.
6. Keep the "unclear reply halts the position" rule per account: `exit_halted` marks the position on that account only.
7. Tests:
   - fill on A is recorded under A
   - cancel for an order on B uses B's session
   - rate limit on A does not slow B
   - exit retry stays in the same account

**Exit check:** order records and audit rows show `account_id`. All OMS tests pass.

### Phase 5 — Reconciliation and portfolio ledger

Steps:
1. Run the startup reconciliation gate per account (`monitor/broker_recon.py`):
   - Entries for account A are blocked if A's broker book does not match. B is not blocked.
   - Exits are never blocked by reconciliation (existing rule).
2. Position ledger: positions keyed by `(account_id, symbol)`. Add a total view.
3. P&L: realized and unrealized per account, and total. Persist per day (reuse `execution/pnl_store.py`, keyed by account).
4. Recon result per account is written to the audit log.
5. Tests:
   - mismatch on A blocks A entries only
   - total position = sum of account positions
   - P&L counters persist and reload per account after restart

**Exit check:** recon and ledger tests pass. Restart test shows per-account counters restored.

### Phase 6 — Failure isolation and recovery

Steps:
1. A broker or OpenAlgo failure on account A puts only A into a safe state. Other accounts keep running.
2. Each account has its own health status: `connected`, `degraded`, `down`, `unknown`.
3. Recovery order (AGENTS.md, Automatic Recovery) runs per account. A restarted account resumes only after its own checks pass.
4. Critical error on one account raises a CRITICAL alert with `account_id`.
5. Tests (fault injection with fake transports):
   - A times out, B still places orders
   - A returns bad JSON, A goes to `UNKNOWN`, B unaffected
   - process restart: A waits for recon, B resumes only if its recon passes

**Exit check:** fault-injection tests pass. No account resumes without its own checks.

### Phase 7 — Dashboard (one file only)

Steps:
1. Edit only `dashboard/main_dashboard.html`. Do not add another page.
2. Add to the page:
   - account switcher (All, or one account)
   - per-account card: broker, connection status, mode, open orders, positions, P&L, risk utilisation
   - combined card: total exposure, total P&L, portfolio cap use
   - error panel rows show `account_id`
3. Data comes from the existing reporting layer (`src/quant/reporting/`). No new data path.
4. Batch updates: refresh no faster than 500 ms (existing dashboard rule).
5. Tests: a smoke test that the page loads and reads the account list.

**Exit check:** page shows two accounts (fake data in test) with separate and combined values.

### Phase 8 — Testing and regression

Steps:
1. Unit tests for every new module (phases 1–7).
2. Parity: old single-account results equal new results when only `"default"` is configured.
3. Integration: 2 fake brokers (different URLs) run a full bot cycle in analyze mode.
4. Replay: one recorded day of bars, two accounts, result matches expected positions.
5. Full regression: `pytest` (no args) using the test paths in `pyproject.toml`.
6. Security check: grep code, config and logs for key values. Expect none.

**Exit check:** full `pytest` green. No new warnings about secrets.

### Phase 9 — Rollout (no live until owner approves)

Steps:
1. Analyze mode only: every account in analyze mode, sandbox confirmed by OpenAlgo.
2. Run for at least 2 weeks of market days. Record: order counts, rejects, recon mismatches, P&L by account.
3. Owner review of the logs (Phase 9 report in `docs/audits/`).
4. Live step (only with owner decision):
   - owner creates a `LiveApproval` per account (approver, date, limits hash)
   - compliance gate passes
   - owner sets `live_allowed` in `config/data_sources.yaml` if still false
   - first live run with one account, then add the second after review
5. Until step 4 is signed, `live_enabled` stays `false` and no live order is possible.

**Exit check:** owner signs the rollout report. This plan alone does not enable live trading.

### Phase 10 — Documentation (same commit as each stage)

Steps:
1. Update `docs/quant_system_flow_v5.md` and `quant_system_flow_v5.svg` with the new blocks (router, account sessions, portfolio risk).
2. Update the stage ownership table in `AGENTS.md` for changed packages.
3. Keep the `Flow stage:` line accurate in every changed file.
4. Update `docs/SOP.md` with: config file for accounts, how to add an OpenAlgo instance, pre-session checks per account.
5. After each commit, append a work-log entry in `Temp/Daily work log/`. Note: `Temp/` is git-ignored, so this log stays local. Owner to confirm whether it should be tracked.

## 5. Risks and how the plan handles them

| Risk | Control |
|---|---|
| Order goes to wrong broker | `account_id` required on every intent. Router refuses unknown ids. |
| Risk limit counted per account only (portfolio over-exposed) | Portfolio risk checks in phase 3. |
| One broker failure stops all trading | Phase 6 isolation. |
| Duplicate order across accounts | Unique intent id, account-scoped rate limit, retry stays in one account. |
| Secrets leak | Env names only in config. Grep check in phase 8. |
| Auto-tune changes account limits | New keys added to the risk-bounding list. |
| Restart resumes live trades unsafely | Per-account recon before entries (phases 5 and 6). |
| Live trading enabled by mistake | `live_enabled` false, `LiveApproval` per account, compliance gate. |

## 6. Owner questions (answer before Phase 1 code)

- **Q1 (blocks Phase 0):** Is one OpenAlgo instance per broker account acceptable? Or do you want a different route?
- **Q2 (blocks Phase 3):** May the same symbol be held in two accounts at once? Default in plan: yes, with a total symbol cap.
- **Q3 (blocks Phase 3):** Portfolio caps: should total exposure and total daily loss be set as a percent of combined capital, or as fixed rupee amounts? Values stay `null` until you set them.
- **Q4:** How many accounts at first, and at which brokers?
- **Q5 (Phase 9):** Who is the approver named in `LiveApproval`?

## 7. Next priority

1. Phase 0: confirm the OpenAlgo one-instance-per-broker route from the official docs, then answer Q1–Q3.
2. Phase 1: account registry and `account_id` on intents (no behaviour change for one account).
