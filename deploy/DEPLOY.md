# Ubuntu VPS Deployment Guide

Deploy the full stack (OpenAlgo platform + POV engine) on a fresh
**Ubuntu 22.04 / 24.04 LTS** server.

> **Compatibility verdict: YES — Ubuntu is the *better* home for this stack.**
> Python, OpenAlgo and the POV engine all run natively; the engine needs
> **zero code changes**. Details and caveats in section 8.

---

## 1. What you get

| Service | Port | Bound to | Public via | Autostart |
|---|---|---|---|---|
| `openalgo` | 5000 | 127.0.0.1 | nginx (443) | ✅ enabled |
| `pov-dash` | 8050 | 127.0.0.1 | nginx (/pov-dash/) | ✅ enabled |
| `pov-runner` | — | — | ❌ **manual only** |
| WebSocket proxy | 8765 | 127.0.0.1 | internal | with platform |

Only **nginx (80/443)** and **SSH** are exposed. App ports never leave loopback.

---

## 2. Prerequisites

- Fresh **Ubuntu 22.04 or 24.04 LTS** VPS (1 vCPU / 2 GB RAM minimum, 2 vCPU / 4 GB recommended)
- A **non-root user** with `sudo`
- A **domain name** pointing at the VPS IP (for TLS)
- Your `my-trading-system/` repo copied to the server, e.g.:
  ```bash
  scp -r my-trading-system user@YOUR_SERVER_IP:~/my-trading-system
  ```

---

## 3. One-shot setup

```bash
cd ~/my-trading-system
chmod +x deploy/setup_ubuntu.sh
./deploy/setup_ubuntu.sh
```

This installs packages, creates `/opt/my-trading-system`, builds the venv,
installs both apps, hardens `.env`, installs the systemd units and
configures `ufw`. It is **idempotent** — safe to re-run after an update.

---

## 4. Add your secrets

```bash
sudo nano /opt/my-trading-system/openalgo/.env
```

Fill in:
- broker API keys (your broker's section)
- `APP_KEY` / security keys (generate, never reuse dev values)
- `OPENALGO_API_KEY` — generate one in the UI *after* first start (section 6)

The script already set the server-critical values:

```
FLASK_HOST_IP=127.0.0.1     # loopback only; nginx is the public face
FLASK_DEBUG=False           # NEVER debug on a public box
FLASK_ENV=production
WEBSOCKET_HOST=127.0.0.1
WEBSOCKET_PORT=8765
```

---

## 5. Start the platform + dashboard

```bash
sudo systemctl start openalgo
sudo systemctl status openalgo          # expect: active (running)

# health check (loopback)
curl -s http://127.0.0.1:5000/auth/check-setup
# → {"needs_setup": false, "status": "success"}

sudo systemctl start pov-dash
sudo systemctl status pov-dash
```

Logs (live):
```bash
journalctl -u openalgo -f
journalctl -u pov-dash -f
```

---

## 6. TLS + nginx

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/my-trading-system
sudo sed -i 's/YOUR_DOMAIN/your-real-domain.com/g' /etc/nginx/sites-available/my-trading-system

# DNS must already resolve to this server:
sudo certbot --nginx -d your-real-domain.com

sudo ln -sf /etc/nginx/sites-available/my-trading-system /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Now open **https://your-real-domain.com** → generate your API key in
*Keys & Reports → API Keys* → put it in `.env` → `sudo systemctl restart openalgo`.

---

## 7. Running the POV engine

**The runner is deliberately NOT enabled at boot.** A trading process that
starts sending orders every time the server reboots is unacceptable.

```bash
# dry check first: put the platform in ANALYZE mode in the UI,
sudo systemctl start pov-runner
journalctl -u pov-runner -f

# stop it
sudo systemctl stop pov-runner
```

Config lives in `pov_engine/input/*.json` — edit and restart the unit.

---

## 8. Compatibility & known constraints (read this)

### ✅ Works cleanly
| Component | Status |
|---|---|
| Python 3.10–3.12 | native |
| OpenAlgo (Flask + SQLite + WebSocket) | native |
| POV engine (`pip install -e`, SDK client) | native, **no changes** |
| `pov_engine` ↔ platform | loopback HTTP/WS |
| Docker (optional alternative) | works natively on Linux |

### ⚠️ Constraint 1 — SocketIO is pinned to `threading` mode
OpenAlgo's `extensions.py` **hardcodes** `async_mode="threading"`:

```python
# Disable eventlet to prevent greenlet threading errors
socketio = SocketIO(cors_allowed_origins="*", async_mode="threading", ...)
```

**Consequences:**
- `eventlet` / `gevent` **cannot be used as gunicorn workers** — installing
  them changes nothing (they are ignored) and eventlet workers can deadlock.
- The units therefore use **`--worker-class gthread`**.
- In `threading` mode flask-socketio runs its own background poll threads;
  keep `--workers 1 --threads 8`. **Do not scale workers** — SocketIO state
  is per-process and order-update events would be split across workers.

### ⚠️ Constraint 2 — do not use `python app.py` in production
`app.py` ends with `socketio.run(app, ...)` without
`allow_unsafe_werkzeug=True`, so a bare run only works with
`FLASK_DEBUG=True` — and debug mode ships an **interactive debugger
(remote code execution)**. That is fine on a laptop loopback, never on a VPS.
Use the gunicorn unit above.

### ⚠️ Constraint 3 — SQLite
OpenAlgo uses SQLite. Keep it on local disk (do **not** put `db/` on NFS or
a network mount), and include `openalgo/db` in your backups.

### ⚠️ Constraint 4 — WebSocket proxy
If your build starts the WS proxy as a **separate process**, add a dedicated
unit (`websocket_proxy.server`, port 8765) and keep it loopback-bound. If
your build starts it inside the app (as ours does), nothing to do.

### ⚠️ Constraint 5 — market hours
Deploy, restart and upgrade **outside 09:15–15:30 IST**. The upgrade SOP
enforces this automatically on Windows; on Linux run upgrades after the close.

---

## 9. Upgrading on the VPS

```bash
cd ~/my-trading-system

# 1. back up first
sudo cp -r /opt/my-trading-system/openalgo/.env  ~/backup_env_$(date +%F)
sudo cp -r /opt/my-trading-system/openalgo/db     ~/backup_db_$(date +%F)

# 2. refresh OpenAlgo only (POV engine is untouched by design)
sudo rsync -a --delete --exclude '.venv' --exclude '.env' --exclude 'db' \
     ~/my-trading-system/openalgo/ /opt/my-trading-system/openalgo/

# 3. reinstall requirements + re-apply the POV wiring patch
cd /opt/my-trading-system/openalgo
sudo -u $USER .venv/bin/python -m pip install -r requirements.txt
# (the blueprint patch: run patches/apply_patches.ps1 equivalent,
#  or simply copy patches/pov_terminal.py into blueprints/ and ensure the
#  two lines exist in app.py)

# 4. restart + verify
sudo systemctl restart openalgo
curl -s http://127.0.0.1:5000/auth/check-setup
```

The POV engine is a **separate tree** (`/opt/my-trading-system/pov_engine`)
and is never touched by platform upgrades — that is the whole point of the
architecture.

---

## 10. Backups & monitoring checklist

```bash
# daily DB + env backup (add to crontab)
0 16 * * 1-5  cp -r /opt/my-trading-system/openalgo/db /opt/my-trading-system/backups/db_$(date +\%F)
0 16 * * 1-5  cp /opt/my-trading-system/openalgo/.env /opt/my-trading-system/backups/env_$(date +\%F)
```

Monitor:
```bash
systemctl is-active openalgo pov-dash
df -h /opt                    # SQLite + logs grow
journalctl -u openalgo --since "1 hour ago" -p err
```

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `RuntimeError: Werkzeug ... pass allow_unsafe_werkzeug` | running `app.py` directly in production mode | use the gunicorn unit (section 5) |
| Port 5000 not listening | app crashed at import | `journalctl -u openalgo -n 80` |
| Order updates stop arriving | websocket cut | check nginx `Upgrade`/`Connection` headers (section 6) |
| SocketIO 400s after scaling | multiple gunicorn workers | keep `--workers 1` |
| Login brute-force attempts | public exposure | rate limit already in nginx; add fail2ban |
| `pov-runner` exits immediately | bad config JSON | check `pov_engine/input/*.json` |

---

## 12. Security notes (non-negotiable for a live trading box)

1. `FLASK_DEBUG=False` always — debug mode is RCE.
2. App ports loopback-only; only nginx is public.
3. TLS with HSTS; TLS 1.2+ only.
4. SSH: keys only (`PasswordAuthentication no`), fail2ban recommended.
5. Add basic auth or IP-allowlisting on `/pov-dash/` if it is internet-facing.
6. Secrets live in `/opt/.../openalgo/.env`, chmod `600`, never in git.
7. Keep the `pov-runner` manual — never `systemctl enable` it.
---

## 13. OPTION B — self-contained server install (fresh upstream clone + your patch)

Your `openalgo/` tree is **not vanilla upstream**: it carries 66 custom changes
(31 POV/OCL platform files — POV order REST API, `database/pov_db.py` which the
engine's ledger depends on, OCL screener, broker/mstock auction support,
frontend pages, tests). A bare clone would silently destroy all of it.

So Option B = **clone upstream at a pinned baseline, then re-apply your code**:

```bash
# on the VPS
cd ~/my-trading-system
./deploy/setup_ubuntu.sh          # 1. packages + venv + systemd units + .env
./deploy/install_openalgo.sh      # 2. clone upstream + apply your patch + deps
sudo nano /opt/my-trading-system/openalgo/.env   # 3. secrets
sudo systemctl start openalgo pov-dash           # 4. run
```

### The patch bundle

| Artefact | Purpose |
|---|---|
| `patches/openalgo-custom.patch` | 65 files / 45 new — your entire custom platform layer, binary-safe |
| `deploy/.baseline` | upstream commit the patch was cut against |
| `deploy/rebuild_patch.sh` | re-cut the patch after you change platform code (dev box) |
| `deploy/install_openalgo.sh` | checkout baseline → apply → 3-way fallback → verify |

**Baseline pinning is deliberate:** the installer checks out the exact commit the
patch was generated against, so it always applies cleanly. It does *not* silently
track upstream HEAD — a moving target would corrupt a trading platform.

### Updating to a NEWER OpenAlgo

```bash
# 1. on your DEV box, sync your custom code onto the new upstream
git -C openalgo fetch origin && git -C openalgo rebase origin/main
# 2. re-cut the patch
./deploy/rebuild_patch.sh
# 3. commit the new patch + .baseline, then deploy
```

### Verification performed

```
[PASS] patch applies CLEANLY to the pinned pristine baseline
[PASS] 11/11 critical files land (pov_db, volume_participation API, ocl_screener, PovTerminal.tsx)
[PASS] app.py wiring at lines 87 + 343
[PASS] database.pov_db imports -> durable ledger enabled
[PASS] ZERO hardcoded secrets in the patch
```

### 🔒 Secrets incident (already fixed)

The original scan found a **live API key hardcoded in two custom files**
(`test_order.py`, `strategies/examples/POV/volume_participation_order.py`).
Both were fixed to read `os.getenv("OPENALGO_API_KEY")`, the patch was
regenerated, and a repo-wide rescan confirmed **zero** occurrences.

**Action required from you:** rotate that key in the OpenAlgo UI anyway — it was
committed to an unencrypted file on disk, so treat it as compromised.