# Install My Trading System

One-click installers for Windows and Ubuntu. Both are safe to re-run.
Neither places orders. The trading engine stays stopped until you start it.

## Windows (one click)

1. Install **Python 3.11+** (tick "Add python.exe to PATH") and **Git for Windows**.
2. Clone or download this repo.
3. Double-click **`install.bat`**.

It creates `.venv`, installs the dependencies and `Blue_engine`, clones
`openalgo\`, creates `.env` from `.env.example`, then starts the stack.

- Dashboard: http://127.0.0.1:8050
- OpenAlgo: http://127.0.0.1:5000
- Stop: `.venv\Scripts\python.exe scripts\ops\start_all.py --stop`

Fill in `.env` with your keys before you connect a broker.

## Ubuntu server

Target: a fresh Ubuntu or Debian server with systemd. This is a terminal
install, not a double-click. Two entry points exist:

| Path | Command | What it does |
|---|---|---|
| Platform only | `sudo bash install.sh ...` | Installs OpenAlgo, engine, services and nginx |
| Whole server (recommended) | `sudo bash Servers/launch_ubuntu_server.sh` | Platform, plus desktop/remote access, pre-open schedule and daily backup, then verifies |

`install.sh` clones this repo to `/opt/my-trading-system` (set `MTS_ROOT` to
change it) and then runs `Servers/install_ubuntu.sh`. The launcher runs the
same platform step as its first real stage.

### Step 1 - Prepare the config

```bash
cp Servers/deploy.env.example Servers/deploy.env
chmod 600 Servers/deploy.env
nano Servers/deploy.env     # set MTS_DOMAIN, MTS_EMAIL, MTS_API_KEY
```

`deploy.env` is gitignored. It holds your API key, so keep it out of git.

### Step 2 - Copy your broker state (required)

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

Without this state the installer only prints a warning and the platform cannot
log in to the broker.

### Step 3 - Run the installer

Platform only:

```bash
sudo bash install.sh --domain trade.example.com --email you@example.com
```

Whole server in one command:

```bash
sudo bash Servers/launch_ubuntu_server.sh
sudo bash Servers/launch_ubuntu_server.sh --rdp-allow 203.0.113.7/32   # also allow RDP from your IP
```

The launcher runs six stages: preflight, platform install, desktop/remote
(XFCE, xrdp, Chrome, Node, PM2, UFW), the 08:45 IST pre-open timer, the 16:00
backup job, and a final verify. Each stage reports OK, WARN or FAIL, and one
failing stage does not stop the others.

Useful flags: `--skip-platform`, `--skip-desktop`, `--skip-schedule`,
`--skip-backup`, `--dry-run` (print stages only), `--start-engine`, `--no-chrome`,
`--no-rdp`.

No domain yet on a private LAN? Use `--allow-plain-http`. Never expose that
host to the internet.

### Step 4 - Verify and log in

```bash
sudo -u algo /opt/my-trading-system/openalgo/.venv/bin/python \
     /opt/my-trading-system/scripts/smoke_test.py
```

The smoke test must pass before you trade. Then open the UI (`https://<your
domain>`), log in to your broker, and confirm the platform is in analyzer
(paper) mode.

## Engine start and boot behaviour

- The engine (`pov-runner`) is **not** enabled at boot. A reboot does not start
  order placement.
- It starts only by the pre-open timer (weekdays 08:45 IST, behind a smoke gate
  and a broker login check), or by hand: `sudo systemctl start pov-runner pov-dash`.
- Both timers and cron are available. Use the timer. Do not run both.

## Where to read more

- [`Servers/DEPLOY_UBUNTU.md`](Servers/DEPLOY_UBUNTU.md) covers the full server
  guide: layout, upgrades, backups, security, troubleshooting, desktop/RDP and
  the pre-open schedule.
- `Servers/launch_ubuntu_server.sh` has the full stage list in its header.
