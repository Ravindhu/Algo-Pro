# Install My Trading System

One script per OS. Both are safe to re-run. Each run installs what is missing,
updates what is safe to update, and starts the stack. Neither places orders.
The trading engine stays stopped until you start it.

## Windows (one click): `install.bat`

1. Install **Python 3.11+** (tick "Add python.exe to PATH") and **Git for Windows**.
2. Clone or download this repo.
3. Double-click **`install.bat`**.

Each run:
1. Updates the repo (fast-forward only; local changes are kept).
2. Finds Python 3.11+.
3. Creates `.venv` and installs the dependencies and `Blue_engine`.
4. Clones OpenAlgo if missing. If it exists, runs the safe upgrade
   (`scripts\ops\upgrade_openalgo.ps1`). That upgrade refuses during market hours
   or while there are open positions, and then the old version is kept.
5. Creates `.env` from `.env.example` if missing.
6. Starts the stack.

- Dashboard: http://127.0.0.1:8050
- OpenAlgo: http://127.0.0.1:5000
- Stop: `.venv\Scripts\python.exe scripts\ops\start_all.py --stop`

Fill in `.env` with your keys before you connect a broker.

## Ubuntu server (one command): `Servers/launch_ubuntu_server.sh`

Target: a fresh Ubuntu or Debian server with systemd. This is a terminal
command, not a double-click.

### Step 1 - Clone once

```bash
git clone https://github.com/Ravindhu/Algo-Pro.git /opt/my-trading-system
```

### Step 2 - Prepare the config

```bash
cp /opt/my-trading-system/Servers/deploy.env.example /opt/my-trading-system/Servers/deploy.env
chmod 600 /opt/my-trading-system/Servers/deploy.env
nano /opt/my-trading-system/Servers/deploy.env     # set MTS_DOMAIN, MTS_EMAIL, MTS_API_KEY
```

`deploy.env` is gitignored. It holds your API key, so keep it out of git.

### Step 3 - Copy your broker state (required)

Your `.env` and `db/` are secrets and are not in git. They must come from your
dev machine and travel **together**. If only one is copied, the broker login
cannot be decrypted.

```bash
# on the dev machine
scp -r _restored/.env _restored/db user@<server>:/tmp/mts-state/

# on the server
sudo mkdir -p /opt/my-trading-system/_restored
sudo cp -r /tmp/mts-state/.env /tmp/mts-state/db /opt/my-trading-system/_restored/
```

### Step 4 - Run the one script

```bash
sudo bash /opt/my-trading-system/Servers/launch_ubuntu_server.sh --domain trade.example.com --email you@example.com
```

Useful flags: `--allow-plain-http` (LAN lab only, never on the internet),
`--rdp-allow 203.0.113.7/32` (open RDP for your IP), `--skip-platform`,
`--skip-desktop`, `--skip-schedule`, `--skip-backup`, `--dry-run` (print stages
only), `--start-engine`, `--no-chrome`, `--no-rdp`.

The run has six stages: preflight, platform, desktop/remote, the 08:50 IST
pre-open timer, the 16:00 backup job, and a final verify. Each stage reports
OK, WARN or FAIL, and one failing stage does not stop the others.

### Updating later

Run the same command again. If the platform is already installed, it:
- fast-forwards this repo and `openalgo/` (never a forced merge),
- refreshes the Python packages,
- restarts `openalgo`, `openalgo-ws` and `pov-dash`.

It **refuses** to update during NSE hours (09:15-15:30 IST) and while `pov-runner`
is running. Exchange holidays are not checked, so update outside hours.
`pov-runner` is never restarted by the update.

### Verify and log in

```bash
sudo -u algo /opt/my-trading-system/openalgo/.venv/bin/python \
     /opt/my-trading-system/scripts/smoke_test.py
```

The smoke test must pass before you trade. Then open the UI (`https://<your
domain>`), log in to your broker, and confirm the platform is in analyzer
(paper) mode.

## Start automatically (scheduled)

| Machine | When | What runs | Set up by |
|---|---|---|---|
| Windows | When you sign in | `install.bat --scheduled` (start only) | `scripts\ops\register_start_jobs.ps1` (run once) |
| Windows | Mon-Fri 08:40 IST, wakes the PC | `install.bat --scheduled` (start only) | same script |
| Ubuntu | At every boot | `launch_ubuntu_server.sh --start-only` (start only) | the launcher (stage 4) |
| Ubuntu | Mon-Fri 08:50 IST | `preopen_launch.sh`: clock, login, smoke test, then engine | the launcher (stage 4, existing timer) |

Scheduled runs **never update code** and **never start the trading engine**
at boot or on Windows. The Ubuntu 08:50 timer is the only scheduled path that
starts the engine, and it runs behind the smoke gate.

Windows setup, once, in the repo folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\register_start_jobs.ps1
powershell -ExecutionPolicy Bypass -File scripts\ops\register_start_jobs.ps1 -Remove   # undo
```

Scheduled-run output: `Temp\run\scheduled_start.log` on Windows, and
`journalctl -u mts-boot-start` on Ubuntu.

Updates stay manual: run `install.bat` on Windows, or the launcher command on
Ubuntu, when you want new code.

## Engine start and boot behaviour

- The engine (`pov-runner`) is **not** enabled at boot. A reboot does not start
  order placement.
- It starts only by the pre-open timer (weekdays 08:50 IST, behind a smoke gate
  and a broker login check), or by hand: `sudo systemctl start pov-runner pov-dash`.
- Both timers and cron are available. Use the timer. Do not run both.

## Retired files

`install.sh`, `scripts/ops/START_ALL.cmd` and `scripts/ops/START_OPENALGO.bat`
were moved to `Temp/archive/2026-11-07/installers/` (see its `index.txt`).
Git history keeps every copy.

## Where to read more

- [`Servers/DEPLOY_UBUNTU.md`](Servers/DEPLOY_UBUNTU.md): full server guide, upgrades, backups, security, troubleshooting, desktop/RDP and the pre-open schedule.
- `Servers/launch_ubuntu_server.sh`: stage list in its header.
- `docs/IMPLEMENTATION_PLAN_2026-11-07_INSTALLERS.md`: this change's plan and status.
