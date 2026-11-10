# Pre-open scheduler and single switch — plan

Status: PHASES A-C BUILT (2026-11-10); the account password is still owner input. Updated before each commit.

## What the owner asked for

1. One settings popup in the NSE dashboard header. It lists: auto broker login,
   auto engine start, Windows or Ubuntu target, and Task Scheduler or cron.
2. One switch turns the whole pre-open schedule on or off.
3. Only the machine's own scheduler is written. Windows writes Task Scheduler
   only. Ubuntu writes cron only. Never both.
4. Both engine start and broker login run. One switch controls both.
5. Pre-open time is 08:50 IST on both OSes (25 min before the 09:15 open).
6. A separate dashboard account, `Ravindhu`, with a default password. The
   password is NOT stored in git (see "Accounts" below).
7. Broker: mstock.

## Decisions taken in this plan

| # | Decision | Why |
|---|---|---|
| D1 | The switch is the opt-in. It replaces the per-machine `BROKER_AUTOLOGIN` flag for scheduled runs. | One control, one place. `.env` flag stays as the default-off safety for manual runs. |
| D2 | Settings live in `data/schedule_settings.json` (git-ignored). Default: `enabled=false`. | Runtime state must not be committed. Default off means nothing runs until the owner switches it on. |
| D3 | A corrupt or missing settings file means OFF. | Fail closed. |
| D4 | `os_target=auto` detects the OS. A machine that does not match an explicit target refuses to install anything. | Stops a Windows setting from touching a Linux box and the reverse. |
| D5 | Ubuntu: cron file `/etc/cron.d/mts-preopen` with `CRON_TZ=Asia/Kolkata`. The systemd timer `pov-preopen.timer` is disabled while the switch is on. | Cron and the timer would both log in. One trigger only. |
| D6 | Windows: Task Scheduler job `BlueEngine\PreOpen` runs `scripts\ops\SCHEDULED_PREOPEN.cmd` at 08:50 Mon-Fri. The old `BlueEngine\BrokerLogin` job is deleted when this one is created. | Same single-trigger rule. |
| D7 | Before installing, the machine's clock must be IST (UTC+05:30). Otherwise refuse. | 08:50 means nothing on a machine in another timezone. |
| D8 | Pre-open time is `08:50`, both OSes. The shell script window stays 08:00-09:30. | Owner instruction. |
| D9 | The dashboard write route uses the existing token check (`DASHBOARD_API_TOKEN`). | Same protection as the other POST routes. |
| D10 | mstock is the first broker. Its credentials are checked before any login attempt. | Owner instruction. |

## Bugs found while reading (fixed in this change)

- `scripts/ops/preopen_launch.sh`: `record_only` is called in the clock
  section before it is defined. Outside the window the result file was never
  written and the failure was silent. Fix: move the helper above its first use.
- `Servers/launch_ubuntu_server.sh`: pre-open time was 08:45. Changed to 08:50.

## Accounts (dashboard login)

The dashboard has only one token today. This plan adds named accounts:

- Account `Ravindhu`. Password is never written to git.
- The first password comes from the environment variable
  `DASHBOARD_DEFAULT_PASSWORD` when the account is created. It is hashed with
  PBKDF2-SHA256 and stored in `data/dashboard_users.json` (git-ignored).
- Owner must choose the value. Nothing is hardcoded.

## Phases and status

| Phase | Work | Status |
|---|---|---|
| A | Plan doc, settings store (D2, D3), installer with OS detection (D4-D7), cron and schtasks builders, `record_only` fix, 08:50 change, mstock preflight check (D10), unit tests | this change |
| B | Dashboard: `GET/POST /api/schedule`, account login, engine start, infrastructure list | built (`dashboard/app.py`) |
| C | SETTINGS popup: infrastructure, account, schedule switch, engine start button | built (`dashboard/main_dashboard.html`) |
| D | SOP section 20 rule change ("read-only, no POST routes" is no longer true) | with phase B |

## Limits

- The sandbox cannot run Task Scheduler, cron, systemd or the mstock API.
  The first real test is on the owner's machines, with someone watching.
- mstock's single-step TOTP login is verified against OpenAlgo's source, not
  against a live account.
- Tests here cover the pure logic only. The OpenAlgo and FastAPI suites need
  the owner's machine.

## Engine start rule (owner, 2026-11-10)

- Auto engine start ON: the schedule starts the engine after a good broker login.
- Auto engine start OFF: login only. The engine waits for the START ENGINE button.
- The button refuses while the kill switch is on or an engine is already running.
  The supervisor's own gates still decide whether anything trades.

## Known gaps

- `/api/schedule` saves settings but does not change the machine's scheduler.
  Root is needed, so run `scripts/ops/install_schedule.py --apply` once after a change.
- Account name: the owner wrote both `Ravindhu` and `Ravi`. Not created yet.
- Default password: owner input. Set `DASHBOARD_DEFAULT_PASSWORD` and run
  `scripts/ops/create_dashboard_user.py --name <name>`.
- `DASHBOARD_SESSION_SECRET` must be set on the server, or account login is off.
- FastAPI routes were NOT run here (FastAPI cannot be installed in the sandbox).
