# Deploying my-trading-system on an Ubuntu VPS

**Architecture (locked):** `Python (blue_engine) â†’ OpenAlgo (server) â†’ Broker â†’ Exchange`

Everything here follows that line. OpenAlgo is a **pristine upstream clone** â€” nothing
of ours is patched into it, and no POV wiring is injected. The engine is a pure
*consumer* of the platform through the public SDK.

> **Note on the old scripts.** A previous generation of deploy scripts
> (`add_blue_engine.sh`, `first_boot.sh`, `install_openalgo.sh`) lived in this folder.
> They copied `patches/pov_terminal.py` into `blueprints/` and `sed`-edited `app.py`
> to embed the dashboard in the OpenAlgo UI. That wiring is retired â€” the dashboard is
> opened directly â€” so those scripts would fail their own `die` checks against a
> pristine clone. They are archived under `archive/ubuntu_pov_wiring/` for reference.
> **Do not run them.**

---

## 0. Quick start â€” the whole thing in 2 commands

The repository contains **your code only**. The installer clones OpenAlgo itself, so
you never track or upload the 3,175 upstream files.

### One-time setup

```bash
# 1. Clone your repo onto the VPS and enter it
git clone https://github.com/Ravindhu/my-trading-system.git
cd my-trading-system

# 2. Create the deploy config from the template
cp Servers/deploy.env.example Servers/deploy.env
chmod 600 Servers/deploy.env
nano Servers/deploy.env         # set MTS_DOMAIN, MTS_EMAIL and MTS_API_KEY
```

`Servers/deploy.env` is gitignored. It holds your API key, so it must never be
committed. Putting the key here rather than on the command line keeps it out of
`~/.bash_history` and out of `ps` output.

### Copy your state (required once â€” secrets cannot ride in git)

```bash
# on the dev laptop
scp -r .\_restored\.env .\_restored\db user@<vps>:/tmp/mts-state/

# on the VPS
sudo mkdir -p /opt/my-trading-system/_restored
sudo cp -r /tmp/mts-state/.env /tmp/mts-state/db /opt/my-trading-system/_restored/
```

### The 2 commands

```bash
# 1) install everything and start the platform
sudo bash Servers/install_ubuntu.sh

# 2) verify (command 1 also runs this as its acceptance gate)
sudo -u algo /opt/my-trading-system/openalgo/.venv/bin/python \
     /opt/my-trading-system/scripts/smoke_test.py
```

### Or ONE command (this section's recommended path)

```bash
sudo bash Servers/launch_ubuntu_server.sh            # platform + desktop/remote + schedules
sudo bash Servers/launch_ubuntu_server.sh --rdp-allow 203.0.113.7/32   # + RDP for your IP
```

`launch_ubuntu_server.sh` runs six stages: preflight, `install_ubuntu.sh`,
`provision_desktop_remote.sh` (XFCE + xrdp + Chrome + Node/PM2 + UFW), the
08:45 pre-open schedule, the 16:00 backup job, and verification. Stage flags:
`--skip-platform --skip-desktop --skip-schedule --skip-backup --dry-run
--start-engine --no-chrome --no-rdp`. See sections 12-14.

Command 1 reads `deploy.env`, clones OpenAlgo, builds the venv, installs
`blue_engine`, writes the systemd units, configures nginx, bridges `_restored/`,
writes the API key into `_restored/.env`, verifies the `.env`/`db` pairing still
decrypts, and runs the smoke gate.

### Optional flags

| Flag | Effect |
|---|---|
| `--start-engine` | Also start `pov-runner` + `pov-dash` (default: left stopped) |
| `--api-key KEY` | Override `MTS_API_KEY` (prefer the file) |
| `--domain NAME` | Override `MTS_DOMAIN` |
| `--email ADDR` | Override `MTS_EMAIL` (Let's Encrypt contact) |
| `--allow-plain-http` | Serve the UI over HTTP. LAN-only labs; never on the internet |
| `--no-smoke` | Skip the acceptance gate (not recommended) |
| `--no-nginx` | Skip the reverse proxy |

---

## 1. What gets installed

```
/opt/my-trading-system/
â”œâ”€â”€ openalgo/          pristine upstream clone (git pull to upgrade)
â”‚   â””â”€â”€ .venv/         the one venv everything uses
â”œâ”€â”€ _restored/         YOUR STATE â€” the source of truth
â”‚   â”œâ”€â”€ .env           OpenAlgo config (FERNET_SALT, API_KEY_PEPPER, DB urls)
â”‚   â”œâ”€â”€ db/            SQLite DBs â€” broker session tokens, encrypted
â”‚   â”œâ”€â”€ keys/          Fernet / oauth key material
â”‚   â””â”€â”€ download/      master-contract symbol cache
â”œâ”€â”€ blue_engine/        the engine, installed editable with [dashboard] extras
â”œâ”€â”€ scripts/           grouped by job (see scripts/README.md)
â”‚   â”œâ”€â”€ run/           run_pov, run_screener_bridge, run_economic_engine
â”‚   â”œâ”€â”€ ops/           start_openalgo, upgrade_openalgo, scheduled tasks, QA
â”‚   â”œâ”€â”€ verify/        smoke_test, probe_sdk, verify_*, check_*
â”‚   â”œâ”€â”€ train/         ml_train, optimize_strategy, backtests
â”‚   â”œâ”€â”€ research/      e2e_replay, make_real_levels (offline analysis)
â”‚   â”œâ”€â”€ data/          fetch_infy*, audits
â”‚   â”œâ”€â”€ diagnostics/   deep_dive + scans
â”‚   â””â”€â”€ *.py           compatibility shims for documented entry points
â”œâ”€â”€ data/              engine state
â”œâ”€â”€ backups/           dated copies of .env + db
â””â”€â”€ log/               service logs
```

### Why `_restored/` is a sibling, not a subfolder

OpenAlgo resolves `.env` and `db/` **relative to its own root**. State kept in a
subfolder would be invisible to it. As a sibling it is both visible to OpenAlgo
(the installer bridges it into the clone) and immune to a re-clone.

### The one thing that will bite you

**`.env` and `db/` are cryptographically married.** `FERNET_SALT` and
`API_KEY_PEPPER` in `.env` are the KDF inputs that encrypt the broker session
tokens in `db/openalgo.db`. Copy one without the other and OpenAlgo boots happily
and then **cannot decrypt your broker login** â€” presenting as an inexplicable
"session invalid" at order time. The installer runs a decrypt check for exactly
this reason; do not skip past a failure there.

---

## 2. Prepare your state (on the dev machine)

You need three things. **`.env` and `db/` must travel together.**

```powershell
# from C:\Users\KanagalakshmiM\my-trading-system
$stamp = Get-Date -Format yyyyMMdd
New-Item -ItemType Directory -Force -Path ".\deploy_$stamp" | Out-Null
Copy-Item ".\_restored\.env"          ".\deploy_$stamp\.env"
Copy-Item ".\_restored\db"            ".\deploy_$stamp\db"    -Recurse
Copy-Item ".\_restored\keys"          ".\deploy_$stamp\keys"  -Recurse
Copy-Item ".\_restored\download"      ".\deploy_$stamp\download" -Recurse
```

Never commit `deploy_*/`, `_restored/`, or anything containing `.env` or a database.
The API key belongs in `.env` only â€” never in a source file. (This shipped incorrectly
three times in this project's history; `install_ubuntu.sh` asserts the retired endpoint
is absent, and `smoke_test.py` will catch a regression.)

---

## 3. Copy to the VPS

```bash
# from the dev machine
scp -r .\deploy_20260923\*  user@your-server:/tmp/mts-state/
scp .\Servers\install_ubuntu.sh user@your-server:/tmp/
```

---

## 4. Run the installer

```bash
ssh user@your-server
sudo bash /tmp/install_ubuntu.sh --domain trade.example.com
```

Optional flags:

| Flag | Effect |
|---|---|
| `--domain example.com` | nginx `server_name` |
| `--user algo` | service account (default `algo`) |
| `--root /opt/...` | install root (default `/opt/my-trading-system`) |
| `--no-nginx` | skip the reverse proxy |

The installer will stop and warn if `_restored/` is empty â€” that is intentional.
Place your state, then re-run:

```bash
sudo mkdir -p /opt/my-trading-system/_restored
sudo cp -r /tmp/mts-state/.env     /opt/my-trading-system/_restored/
sudo cp -r /tmp/mts-state/db       /opt/my-trading-system/_restored/
sudo cp -r /tmp/mts-state/keys     /opt/my-trading-system/_restored/ 2>/dev/null || true
sudo cp -r /tmp/mts-state/download /opt/my-trading-system/_restored/ 2>/dev/null || true
sudo chown -R algo:algo /opt/my-trading-system/_restored
sudo bash /tmp/install_ubuntu.sh --domain trade.example.com
```

---

## 5. Verify

```bash
# platform + retired endpoint
curl -s http://127.0.0.1:5000/auth/check-setup
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:5000/api/v1/povorder
#   expect 404 â€” its absence is what proves no old-architecture POV is patched in

# service health
systemctl status openalgo openalgo-ws --no-pager
journalctl -u openalgo -n 50 --no-pager

# the acceptance gate
sudo -u algo /opt/my-trading-system/openalgo/.venv/bin/python \
    /opt/my-trading-system/scripts/smoke_test.py
```

`smoke_test.py` must print `SMOKE TEST: PASS` before you trade. It checks that
`_restored/` state is present, the clone is clean of POV, and the SDK answers.

---

## 6. Broker login & first run

1. Open `https://trade.example.com` and log in to the OpenAlgo UI.
2. Complete broker authentication (mstock or whichever broker).
3. **Confirm the platform is in ANALYZER (paper) mode.** Go live deliberately.
4. Start the engine only when ready:

```bash
sudo systemctl start pov-runner pov-dash
systemctl status pov-runner pov-dash --no-pager
```

The engine is deliberately **not** started by the installer â€” installing is not
the same as intending to trade.

---

## 7. Services

| Unit | Role | Started by installer |
|---|---|---|
| `openalgo` | Flask + SocketIO via gunicorn (eventlet, `-w 1`) | âœ… yes |
| `openalgo-ws` | broker tick WebSocket proxy (port 8765) | âœ… yes |
| `pov-runner` | the engine (`scripts/run_pov.py`) | âŒ manual |
| `pov-dash` | Plotly dashboard (port 8050) | âŒ manual |

`--workers 1` is **required** on `openalgo`. Multiple gunicorn workers break
SocketIO room state, which shows up as orders silently never receiving updates.

---

## 8. Upgrading

```bash
# stop the engine first â€” never upgrade mid-execution
sudo systemctl stop pov-runner

cd /opt/my-trading-system
sudo -u algo git -C openalgo fetch --all --tags
sudo -u algo git -C openalgo pull --ff-only
sudo -u algo openalgo/.venv/bin/python -m pip install -r openalgo/requirements.txt

# state is untouched by the pull; re-bridge it into the clone
for i in .env db keys download; do
  [ -e "_restored/$i" ] && sudo cp -r "_restored/$i" openalgo/ 2>/dev/null || true
done
sudo chown -R algo:algo openalgo

sudo systemctl restart openalgo openalgo-ws
sudo -u algo openalgo/.venv/bin/python scripts/smoke_test.py   # must PASS
sudo systemctl start pov-runner
```

**No patch re-application step.** Nothing POV-related is patched into OpenAlgo, so
`git pull` can never conflict with our code. That is the whole point of the layout.

A re-clone is equally safe, because state lives in `_restored/`:

```bash
sudo mv openalgo openalgo.old
sudo -u algo git clone --depth 1 https://github.com/marketcalls/openalgo.git openalgo
# recreate .venv, re-bridge _restored/, reinstall requirements
```

---

## 9. Backups

```bash
# /etc/cron.d/mts-backup
0 16 * * 1-5 algo cp -r /opt/my-trading-system/_restored/db /opt/my-trading-system/backups/db_$(date +\%F)
5 16 * * 1-5 algo cp    /opt/my-trading-system/_restored/.env /opt/my-trading-system/backups/env_$(date +\%F)
```

Back up `.env` and `db/` **as a pair**, and keep the API key out of anything you sync
to a git remote.

---

## 10. Security

- **HTTPS is the default.** The installer obtains a Let's Encrypt certificate
  for `MTS_DOMAIN` and redirects HTTP to HTTPS. It refuses to serve the UI over
  plain HTTP unless you pass `--allow-plain-http`. If issuance fails, the site
  is removed from nginx so nothing is exposed over HTTP. Logs:
  `/var/log/my-trading-system-certbot.log`.
- **The API key lives only in `_restored/.env`.** Never in a source file, never in
  a container image, never in this repo.
- Consider `fail2ban` on nginx; rate limiting is already configured.
- Restrict SSH to keys only, and firewall everything except 80/443.
- `.env`, `db/`, `keys/`, `deploy_*/` should be in `.gitignore`.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `openalgo` won't start, import error | state not bridged into clone | check `openalgo/.env` exists; re-run installer |
| Boots but "broker session invalid" | `.env` and `db/` restored separately | restore both from the same snapshot |
| Order updates never arrive | nginx missing `Upgrade` headers, or `--workers > 1` | check section 5/7 |
| `povorder` returns 200 | old-architecture POV present | investigate the clone; it should be 404 |
| `pov-dash` exits immediately | bad config JSON | check `blue_engine/input/*.json` |
| `pov-runner` exits immediately | platform not up yet | it has `Restart=on-failure`; check `journalctl -u pov-runner` |
| Dashboard unreachable at `/pov-dash/` | dash unit stopped | `systemctl start pov-dash` |
| RDP connects, screen is black | `startwm.sh` starts a bare X session | `provision_desktop_remote.sh` rewrites it to `startxfce4`; verify `/etc/xrdp/startwm.sh` |
| RDP login loops back to the prompt | user not in `ssl-cert`, or XFCE missing | `usermod -aG ssl-cert xrdp`; `dpkg -l xfce4` |
| `ufw` blocks you out | reset ran before port 22 was allowed | the script allows 22 **before** `ufw enable`; console access: `ufw disable` |
| Pre-open timer did nothing | outside 08:00-09:30 IST, weekend, or lock held | `journalctl -u pov-preopen -n 80`; `cat /opt/my-trading-system/log/preopen/last_preopen.json` |

---

## 12. Desktop + remote access (`provision_desktop_remote.sh`)

Adds the human-access layer to a box built by `install_ubuntu.sh`.

| Item | What is installed | Why |
|---|---|---|
| XFCE | `xfce4 xfce4-goodies xfce4-terminal dbus-x11` | lightweight GUI; ~700 MB |
| xrdp | `xrdp xorgxrdp x11-xserver-utils` | Remote Desktop server on 3389 |
| Chrome | official `google-chrome-stable_current_amd64.deb` | real browser inside the desktop |
| Node.js LTS | NodeSource `setup_lts.x`, fallback to distro | required by PM2 |
| PM2 | `npm i -g pm2` + `pm2 startup` | for **non-trading** helpers only |
| UFW | deny-incoming default, 22 allowed, 3389 only per `--rdp-allow` | lock the VPS down |

**XFCE over RDP needs two fixes** that the packages do not apply themselves:
`/etc/xrdp/startwm.sh` must `exec startxfce4` (otherwise black screen), and
`/etc/polkit-1/localauthority/50-local.d/45-allow-colord.pkla` stops the
"authentication required" colour-profile prompt from blocking the session.

**PM2 is not the supervisor for trading processes.** systemd already
supervises OpenAlgo, the websocket proxy, the dashboard and the engine, with
journald and cgroup accounting. Two supervisors on one port is how an order
gets placed twice. PM2 is installed and its boot hook is set, but it is used
only for non-trading helpers. Supervisor-of-record = **systemd**.

## 13. Firewall + WinSCP

```bash
# RDP only from your laptop's public IP (recommended)
sudo ufw allow from 203.0.113.7/32 to any port 3389 proto tcp

# or tunnel RDP over SSH and keep 3389 closed entirely
ssh -L 3389:127.0.0.1:3389 algo@<server>     # then mstsc 127.0.0.1:3389
```

**WinSCP is Windows-only.** There is no WinSCP package for Ubuntu - installing
one does not exist, and the "apt install winscp" recipe you will find online is
wrong. Use WinSCP **on the laptop** with an SFTP session:

```
WinSCP -> New Session -> File protocol: SFTP -> Host: <server-ip> -> Port: 22
File protocol: SFTP  (NOT SCP)
Username: algo       (or your sudo user)
```

For a Linux-side SFTP GUI: `sudo apt-get install -y filezilla`. For scripts,
the shipped `rsync`/`scp` are already enough.

nginx needs 80/tcp (certificate challenge and redirect) and 443/tcp (HTTPS).
The provisioning script opens these for you when it sets the firewall. If you
configure ufw yourself, open both:

```bash
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp
```

## 14. Pre-open automation (`scripts/ops/preopen_launch.sh`)

Scheduled at **08:45 IST, Mon-Fri** (`pov-preopen.timer`). Each step is a gate;
any failure stops the chain and the engine does **not** start.

```
clock window (08:00-09:30 IST, weekdays, or --force)
  -> flock (one engine, ever)
  -> start openalgo + openalgo-ws
  -> wait for :5000 (300 s) and :8765 (120 s)
  -> scheduled_login.py   (BROKER_AUTOLOGIN=1 opt-in; exit 2 = not opted in, continue)
  -> smoke_test.py        (must PASS)
  -> start pov-dash + pov-runner
  -> wait for :8050, assert pov-runner is-active
  -> write log/preopen/last_preopen.json
```

```bash
bash scripts/ops/preopen_launch.sh --check     # prerequisites only
bash scripts/ops/preopen_launch.sh --dry-run   # decide, start nothing
bash scripts/ops/preopen_launch.sh --force     # ignore the clock window
systemctl list-timers pov-preopen.timer
journalctl -u pov-preopen -n 80
```

Exit codes: `10` outside window · `11` lock held · `12` platform down ·
`13` broker session failed · `14` smoke gate failed · `15` engine not up.

A commented crontab line is written to `/etc/cron.d/mts-preopen`. Use the
timer **or** cron, never both. The timer is the default because it has
`Persistent=true` (a missed 08:45 runs after boot, still inside the launcher's
own gate) and its output goes to journald instead of a mailbox nobody reads.

**`pov-runner` is still not enabled at boot.** A reboot does not start the
order-placing engine. Only the clock trigger, behind the smoke gate, or an
explicit `systemctl start pov-runner`. That is AGENTS.md, not an oversight.