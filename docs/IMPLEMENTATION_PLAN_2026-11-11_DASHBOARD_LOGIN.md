# Dashboard owner login for all edits (plan)

Date: 2026-11-11. Owner decision: owner login (option 3). The write token stays for scripts.

## Facts found (discovery)

- `src/quant/core/dashboard_accounts.py` is built: PBKDF2-SHA256 hashes, 5 failures then 15 min lock, signed cookie `name|expiry` with 8 h TTL, fail closed without `DASHBOARD_SESSION_SECRET`.
- `dashboard/app.py` has `POST /api/account/login`, `POST /api/account/logout`, `GET /api/account`, and `write_denied()` (login OR token).
- `write_denied()` is used only by `POST /api/schedule`, `POST /api/engine/start`, `POST /api/engine/stop`.
- These routes still accept the token ONLY: `POST /api/limits`, `POST /api/kill/trip`, `POST /api/kill/reset`, `POST /api/setup`, `POST /api/trading-mode`, `POST /api/loop/start`, `POST /api/loop/stop`.

## Rules

1. Every browser write accepts a signed-in owner account OR the write token (scripts).
2. The token is never sent to or read by the page.
3. Live trading stays gated by the approval file and the risk gate, never by the login alone.
4. Every write is audited with the actor name (account name, or `dashboard-api` for the token).

## Phases

- **Phase 1 (routes):** switch the 7 token-only routes to `write_denied()`.
- **Phase 2 (sessions):** per-account `session_version`. Password change bumps it; old sessions are refused.
- **Phase 3 (lockout):** keep per-account lock; add a per-IP limit so one attacker cannot lock the owner out.
- **Phase 4 (cookie):** set `Secure` when the request is HTTPS.
- **Phase 5 (setup and tests):** add `scripts/ops/create_dashboard_user.py` (asks name and hidden password, saves hash only). Unit tests for each route with login, with token, and with neither (401).

## Exit checks

1. Each of the 7 routes returns 401 with no login and no token.
2. Each route accepts a valid login cookie and a valid token.
3. A password change refuses the old session.
4. Lockout and per-IP limit tested.
5. Full unit suite run on the owner machine (FastAPI and pytest installed). Commit code only after it passes. This commit is the plan only.

## Owner setup (not in code)

- Set `DASHBOARD_SESSION_SECRET` in `.env` (not committed).
- Run `scripts/ops/create_dashboard_user.py` once to create the `algopro` account. The password is never stored in the repo.

## Work log

- 2026-11-11: plan written. Phases 1 to 5 not started.
