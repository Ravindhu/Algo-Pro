# Mobile Dashboard Plan

Status: PLANNED. No code changed yet.
Flow stage: Dashboard, alerts (`src/quant/reporting/`, `dashboard/`)

## Decisions (owner, 2026-11-12)

1. Mobile is the existing main page `dashboard/main_dashboard.html` (`/ui`), made phone-ready and installable. No second dashboard page (decision 0027).
2. The engine runs on a separate server. The phone reaches it over HTTPS through a reverse proxy (Caddy or Nginx), with login.
3. v1 controls: monitoring, plus the kill-switch TRIP button only. Kill-switch RESET and engine START stay on the desktop.

## Facts from the repo

- `dashboard/app.py` already serves `/ui` and read APIs (`/api/overview`, `/api/positions`, `/api/events`, `/api/health`, `/api/kill`, `/api/portfolio`).
- Write routes already exist: `POST /api/kill/trip` and `POST /api/kill/reset`. Both need a reason and the write token or a signed-in account.
- The app binds to `127.0.0.1:8050`. Remote access needs a proxy.
- Live feed is not connected. Current data is paper only and must be labelled as paper.

## Phases

| Phase | Work | Exit check |
|---|---|---|
| M1 Responsive layout | Single-column phone layout. Top bar: mode, kill-switch state, P&L. Bottom tabs: Overview, Positions, Events, Health. | Page usable at 390 px width, no horizontal scroll. |
| M2 Install to home screen | Web app manifest, icons, minimal service worker that caches the page shell only. | Install works. No `/api/*` response is ever cached. |
| M3 Remote access | Proxy config in `Servers/` (Caddy/Nginx). App reads `DASHBOARD_HOST` (default `127.0.0.1`). Secure cookie flag when served over HTTPS. Update `Servers/DEPLOY_UBUNTU.md`. | Login works over HTTPS. Plain HTTP refused for writes. |
| M4 Kill-switch trip button | Big button, confirm dialog, reason field, calls existing `POST /api/kill/trip`. No reset button on mobile. | Trip writes one audit row. Reset is not reachable from the mobile view. |
| M5 Tests and audit | Tests: `/ui` serves, mobile view has no reset/start/order controls, kill trip needs auth, service worker never caches `/api`. Security review of token handling (no token in localStorage). | `pytest` full run passes. `test_layout.py` guards unchanged. |
| M6 Docs | Update `docs/SOP.md`, `Temp/Daily work log/`, `docs/quant_system_flow_v5.md` if stage text changes, `docs/decisions/` note for the mobile approach. | Docs match the code. |

## Safety rules for this work

- The mobile page never sends an order and never adds a new order path. Only `execution/openalgo_client.py` may place orders.
- Kill-switch RESET and engine START/STOP are not on the mobile view.
- Live mode status is shown as read-only.
- Data shown is labelled PAPER or LIVE, and delayed or stale data is marked stale.
- Errors and CRITICAL events show at the top of the mobile view.

## Open items

- Live market-data feed: not connected (AGENTS.md: owner approval needed).
- Alerts to phone (push notifications): PENDING. Not in v1.
- Owner must choose the domain name and TLS method for the proxy.
