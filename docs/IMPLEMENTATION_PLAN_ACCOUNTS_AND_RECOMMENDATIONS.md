# Implementation plan: owner accounts and saved recommendations

Date: 2026-11-12
Status: PLANNED. No code changed.
Flow stage: Dashboard, Risk gate (limits only). No order path touched.
Target path in repo: docs/IMPLEMENTATION_PLAN_ACCOUNTS_AND_RECOMMENDATIONS.md

## Facts from the code

- src/quant/core/dashboard_accounts.py has create_user(), verify_login() and signed session cookies. It has no role field, so every signed-in account is treated the same.
- src/quant/core/limits_store.py apply_limit_updates() validates bounds and writes atomically. It is the only safe writer for config/risk_limits.yaml.
- dashboard/app.py write_denied() accepts any signed-in account OR the write token.
- Accounts live in data/dashboard_users.json (git-ignored). Passwords are PBKDF2 hashes. Lockout: 5 failures, 15 minutes.

## Part A: owner account creation

Goal: one owner account that can save recommendations. Nobody can create an owner from the web page.

1. Add a role to each account: "owner" or "viewer". Accounts without a role are viewer, so the check fails closed.
2. create_user(name, password, role): role must be owner or viewer.
3. New CLI scripts/ops/create_dashboard_user.py:
   - Password read with getpass, never a command-line argument.
   - Run on the owner machine only, for the bootstrap owner and later accounts.
   - Writes one audit row (account_created, role, actor = local shell user).
4. The dashboard has NO route to create accounts or change roles. Privilege can only be raised from the machine.
5. Owner check helper: is_owner(session_name), read from the users file on each request, so a demoted account loses access at once.

Exit checks:
- A viewer session gets 403 on the save route.
- A demoted owner gets 403 on the next request, with no re-login.
- A password under 12 characters is refused.
- The CLI never prints or logs the password.

## Part B: save agent recommendations

Goal: the agent's calculated value is stored, shown on the node, and applied by one click.

1. Store recommendations in data/paper/recommendations.json. The pipeline writes it atomically. Each entry has: id, parameter, current value, recommended value, reason, created time, expiry (default 1 hour), and the station that produced it. The dashboard reads it and never writes it.
2. Node status (reporting/flow_status.py) adds, per issue: parameter, current value, recommended value, and whether it can be applied.
3. Apply route: POST /api/flow/apply with body {"id": "..."}. Only the id is accepted. The browser never sends a number.
4. Server checks, in order:
   - Owner session only. The write token is not accepted on this route.
   - Recommendation exists and has not expired.
   - Current value still equals the value the recommendation was made against. If not: 409, and the popup asks to refresh.
   - The value passes validate_limit_updates.
   - A risk veto is never turned off.
   - LOOSENING (raising a limit, or setting an unset limit so entries open) needs the typed phrase in the body. Live mode: "APPLY LIVE LIMIT". Paper mode: "APPLY LIMIT".
   - Tightening never needs a phrase.
5. Write through apply_limit_updates (or apply_setup_updates for setup fields).
6. Audit row "recommendation_applied": id, parameter, old, new, mode, actor, phrase used.
7. Remove the used recommendation, so a double-click cannot apply twice.
8. Popup: each row shows parameter, current, recommended, reason, and a "Take recommended value" button, shown only to the owner. Non-owners see the text only.

Live limits: saving a live limit changes only the stored limit. It does not start trading. The trading-mode switch still needs LiveApproval and the Phase 3 gates.

Exit checks:
- A viewer, or the token alone, gets 403.
- An expired or stale recommendation gets 409, and nothing is written.
- A loosening without the phrase gets 422.
- A tightening applies with no phrase.
- A veto cannot be turned off.
- Every apply writes exactly one audit row, and a second apply of the same id is refused.
- test_pnl_visible.py route list is updated on purpose.
- A tampered "value" in the request body is ignored.

## Order of work

1. Part A: role field, owner check, CLI, tests.
2. Part B step 1: recommendation store and pipeline writer, tests.
3. Part B steps 2-3: node data and apply route, tests.
4. Part B steps 4-8: rules, popup, audit, tests.
5. Docs: decision note (next free number), flow doc, SVG labels, AGENTS.md, in the same commit.

## Open items

- Phrase text ("APPLY LIVE LIMIT" / "APPLY LIMIT") is a proposal. Confirm or change.
- Recommendation expiry (1 hour) is a proposal.
- Repo write access is needed before the file can be committed. The session's GitHub token returned 403 on branch creation.
