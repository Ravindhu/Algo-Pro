# Run the algo (quick start for agents)

Read this file first. You do not need to read the whole repo to start the system.

## 0. Safety gates (check before any start)

1. **Mode.** Open `config/bot.yaml`. Check the `mode:` line. The start is paper unless the owner has said live.
   - On master today, `mode: live` and `live_enabled: true`. Do not change it yourself. Ask the owner first.
2. **Secrets.** The OpenAlgo `.env` lives in `_restored\.env` at the repo root. Never generate a new one. Never commit it. If OpenAlgo refuses to boot with a secret or database error, stop and ask the owner. Do not rotate keys.
3. **Engine.** The platform starts without any trading. The trading engine starts only with `--engine`, and only after the owner confirms.

## 1. Start (Windows, from the repo root)

Before the first start on any PC, run the setup once (safe to run again):
```
.venv\Scripts\python.exe scripts\ops\backup_state.py --setup
```
If it reports `MISSING: _restored\.env`, stop and ask the owner for the original `.env`. Never create one.

```
.venv\Scripts\python.exe scripts\ops\start_all.py            # platform only (OpenAlgo, WebSocket, Dash)
.venv\Scripts\python.exe scripts\ops\start_all.py --check    # ports and prerequisites only
.venv\Scripts\python.exe scripts\ops\start_all.py --engine   # also starts the trading engine (owner decision)
```

URLs after start:
- OpenAlgo: http://127.0.0.1:5000
- Dash terminal: http://127.0.0.1:8050
- WebSocket: port 8765

## 1b. Screener symbol inputs (M and A switches)

The bot trades only the names in `data\screened\latest.json`. Two sources feed it:

| Source | Where it is set | Rule |
|---|---|---|
| A - Screener.in automation | Screener stocks panel, **A** button | The saved Screener.in screen, scraped once an hour |
| M - manual symbols | Screener stocks panel, **M** button and input box | Symbols in `data\screened\manual_symbols.json` |

- **M on, A off:** the bot uses manual symbols only.
- **A on, M off:** the bot uses Screener.in only.
- **Both on, or both off:** both paths work.
- Owner only. A change applies at the next bot refresh, within 5 minutes.
- A manual symbol still needs a live quote, a valid circuit band and no ASM/GSM flag. It cannot skip those checks.
- Manual rows are listed first in the panel and tagged `manual`.

Sandbox (analyze) mode: when OpenAlgo confirms the analyzer is ON, a quote with no circuit
limits uses the NSE maximum band (20%), labelled `sandbox_default`. This never applies when the
analyzer is OFF or its state is unknown. ASM/GSM is never relaxed.

Screener.in login: `SCREENER_USERNAME`, `SCREENER_PASSWORD` and `URL_SCREENER_DOT_IN` in the repo `.env`
(not `_restored\.env`). The session is reused for 24 hours. Never print or commit these values.

## 2. Stop

```
.venv\Scripts\python.exe scripts\ops\start_all.py --stop
```

## 3. Problems

| Symptom | Action |
|---|---|
| "address already in use" on 5000, 8050 or 8765 | Run `--stop`, then start again. Use `--force` only if the owner agrees. |
| OpenAlgo refuses to boot, secret or database error | Stop. Report the exact message to the owner. |
| Broker session invalid | Do not log in for the owner. The owner signs in at `/broker`. |
| Screener panel empty, or every name dropped | Check the analyzer switch in OpenAlgo. Read `data\screened\latest.json`: each dropped row records its reason. Do not change the ASM rule. Ask the owner. |
| Screener panel shows "screener returned no rows" | The saved screen could not be read. Check `SCREENER_USERNAME`, `SCREENER_PASSWORD` and `URL_SCREENER_DOT_IN`. The last good selection is kept. |

## 4. Do not read or run to start the system

- `Temp/`, `archive/`, `Temp/Stag/` (retired code, staging only)
- `docs/` plan files (read only when a task asks for them)
- Scripts that place orders or close positions, such as `sample_order.py` or `kill_close.py`. Only the owner runs these.

## 5. Where the code is

- OpenAlgo: `src/quant/openalgo_platform/` (vendored, pinned in `openalgo.UPSTREAM_PIN`)
- Launchers: `scripts/ops/` (`start_all.py`, `start_openalgo.py`)
- Trading engine: `src/quant/` (the bot entry points are in `scripts/run/`)
- Rules for agents: `AGENTS.md` and `CLAUDE.md`

## 6. Auto-start the platform on Windows (run once)

This starts OpenAlgo, the WebSocket proxy and the dashboard without opening Python by hand. It never starts the trading engine.

Setup (once, no admin needed, from the repo root):
```
powershell -ExecutionPolicy Bypass -File scripts\ops\register_start_jobs.ps1
```
This creates two Task Scheduler jobs for the current user:
- `MTS - start at logon`: runs at Windows sign-in.
- `MTS - pre-open start`: Mon-Fri at 08:40, wakes the PC if asleep.

Stop the scheduler (remove both jobs):
```
powershell -ExecutionPolicy Bypass -File scripts\ops\register_start_jobs.ps1 -Remove
```
Change the pre-open time: re-run setup with `-PreOpenTime HH:MM`.
Log of each scheduled run: `Temp\run\scheduled_start.log`.

Agents: do not register or remove these jobs unless the owner asks.

## 7. Backup and first setup

Run once on a new PC, after `git clone` and `install.bat`:
```
.venv\Scripts\python.exe scripts\ops\backup_state.py --setup        # folders and templates (never creates .env)
```
Then copy your ORIGINAL `.env` into `_restored\.env`.

Setup also creates `data\screened\` from its templates (2026-11-27): `README.md`,
`manual_symbols.json` (empty) and `source_mode.json` (both switches off). Existing files are never
overwritten. The Ubuntu launcher does not run setup, so on Ubuntu run the command above by hand.

Daily backup (once per PC): `scripts\ops\register_backup_job.ps1` schedules `MTS - daily backup` at 15:45. Backups go to `E:\Algopro-restore\<YYYYMMDD-HHMMSS>\`, and the last 30 are kept. Log: `Temp\run\backup.log`.

Backup now: `.venv\Scripts\python.exe scripts\ops\backup_state.py`
Restore: stop the stack (`--stop`), then `.venv\Scripts\python.exe scripts\ops\backup_state.py --restore E:\Algopro-restore\<folder>`. The current state is first saved to `pre-restore-<stamp>`.
Stop the daily backup: `scripts\ops\register_backup_job.ps1 -Remove`

Agents: do not restore, and do not remove the backup job, unless the owner asks.

## 8. Backup rules for agents

- Run a backup before you change anything in `_restored\`, `data\` or the OpenAlgo database:
  `.venv\Scripts\python.exe scripts\ops\backup_state.py`
- The daily backup job (15:45) is set up by the owner. Check it exists before you assume backups are running.
- Never run `--restore` without the owner's approval. Stop the stack first.
- Never copy, print or commit the `.env` or any backup folder.
