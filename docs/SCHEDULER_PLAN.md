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

## Change of decision (owner, 2026-11-10, later)

The owner removed the schedule on/off switch and the auto broker login switch.
Only these controls remain:

- Scheduler details: OS, broker, start time. Changes need one install run on the machine.
- Auto engine start (ON or OFF). Read at run time by `scripts/ops/read_engine_flag.py`,
  so a change takes effect at the next run without reinstalling. Default is OFF.
- START ENGINE button. It refuses while the kill switch is on or an engine is running.

Effects to know about:

- Once the scheduler is installed, broker login runs every weekday at the start time.
  To stop that, an operator runs `scripts/ops/install_schedule.py --remove --apply`.
  The dashboard does not remove the schedule.
- The `enabled` and `broker_login` fields were removed from `schedule_settings.json`.
  Old files that still contain them load without error. The keys are ignored.
- Popup layout: two headers, Infrastructure (checks, scheduler, engine, START ENGINE)
  and Account (dashboard login only).

## Missed runs (owner, 2026-11-10)

- If the machine is off at the start time, the run must not be skipped. It runs when
  the machine is on: `scripts/ops/run_preopen_once.py` is the one entry point.
- Triggers: Windows uses a weekday trigger at the start time, a logon trigger, and
  "start as soon as possible". Ubuntu uses a daily cron line plus `@reboot`.
- One marker per trading day (`data/run/preopen_done_<date>.json`, created exclusively)
  stops a second login when both triggers fire.
- Before the start time: wait, then run. After the start time: run now (catch-up).
- After 15:30: skip that day and log it. The owner must confirm this (see below).
- Engine: starts only when auto engine start is ON and the run is before 09:15.
  Later runs log in only, and the engine waits for the button.
- Holidays: `config/calendar/holidays.csv`, read with the standard library.

Open limits:
- Windows: the logon trigger needs the user to sign in. A machine that is powered on
  but has nobody logged in will not run the job until someone logs in. Storing a
  password for an always-run task would need the owner to enter it on the machine.
- Windows: a machine fully powered off (not sleep) cannot be woken. WakeToRun only
  helps from sleep.
- Ubuntu: `@reboot` runs as soon as the server boots, and the job waits for OpenAlgo
  for up to 300 seconds. If the login fails, the marker stays and there is no retry
  that day. The broker login cooldown also limits retries.

Account name for the dashboard: Ravichandiran (owner, 2026-11-10). The password is
still owner input and must be 12 or more characters.
