# Deploying my-trading-system on an Ubuntu VPS

**Architecture (locked):** `Python (pov_engine) → OpenAlgo (server) → Broker → Exchange`

Everything here follows that line. OpenAlgo is a **pristine upstream clone** — nothing
of ours is patched into it, and no POV wiring is injected. The engine is a pure
*consumer* of the platform through the public SDK.

> **Note on the old scripts.** A previous generation of deploy scripts
> (`add_pov_engine.sh`, `first_boot.sh`, `install_openalgo.sh`) lived in this folder.
> They copied `patches/pov_terminal.py` into `blueprints/` and `sed`-edited `app.py`
> to embed the dashboard in the OpenAlgo UI. That wiring is retired — the dashboard is
> opened directly — so those scripts would fail their own `die` checks against a
> pristine clone. They are archived under `archive/ubuntu_pov_wiring/` for reference.
> **Do not run them.**

---

## 1. What gets installed

```
/opt/my-trading-system/
├── openalgo/          pristine upstream clone (git pull to upgrade)
│   └── .venv/         the one venv everything uses
├── _restored/         YOUR STATE — the source of truth
│   ├── .env           OpenAlgo config (FERNET_SALT, API_KEY_PEPPER, DB urls)
│   ├── db/            SQLite DBs — broker session tokens, encrypted
│   ├── keys/          Fernet / oauth key material
│   └── download/      master-contract symbol cache
├── pov_engine/        the engine, installed editable with [dashboard] extras
├── scripts/           run_pov.py, smoke_test.py, probe_sdk.py
├── data/              engine state
├── backups/           dated copies of .env + db
└── log/               service logs
```

### Why `_restored/` is a sibling, not a subfolder

OpenAlgo resolves `.env` and `db/` **relative to its own root**. State kept in a
subfolder would be invisible to it. As a sibling it is both visible to OpenAlgo
(the installer bridges it into the clone) and immune to a re-clone.

### The one thing that will bite you

**`.env` and `db/` are cryptographically married.** `FERNET_SALT` and
`API_KEY_PEPPER` in `.env` are the KDF inputs that encrypt the broker session
tokens in `db/openalgo.db`. Copy one without the other and OpenAlgo boots happily
and then **cannot decrypt your broker login** — presenting as an inexplicable
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
The API key belongs in `.env` only — never in a source file. (This shipped incorrectly
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

The installer will stop and warn if `_restored/` is empty — that is intentional.
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
#   expect 404 — its absence is what proves no old-architecture POV is patched in

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

1. Open `http://trade.example.com` and log in to the OpenAlgo UI.
2. Complete broker authentication (mstock or whichever broker).
3. **Confirm the platform is in ANALYZER (paper) mode.** Go live deliberately.
4. Start the engine only when ready:

```bash
sudo systemctl start pov-runner pov-dash
systemctl status pov-runner pov-dash --no-pager
```

The engine is deliberately **not** started by the installer — installing is not
the same as intending to trade.

---

## 7. Services

| Unit | Role | Started by installer |
|---|---|---|
| `openalgo` | Flask + SocketIO via gunicorn (eventlet, `-w 1`) | ✅ yes |
| `openalgo-ws` | broker tick WebSocket proxy (port 8765) | ✅ yes |
| `pov-runner` | the engine (`scripts/run_pov.py`) | ❌ manual |
| `pov-dash` | Plotly dashboard (port 8050) | ❌ manual |

`--workers 1` is **required** on `openalgo`. Multiple gunicorn workers break
SocketIO room state, which shows up as orders silently never receiving updates.

---

## 8. Upgrading

```bash
# stop the engine first — never upgrade mid-execution
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

- **TLS is not optional.** This UI is internet-facing and can place live orders:
  `sudo certbot --nginx -d trade.example.com`
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
| `pov-dash` exits immediately | bad config JSON | check `pov_engine/input/*.json` |
| `pov-runner` exits immediately | platform not up yet | it has `Restart=on-failure`; check `journalctl -u pov-runner` |
| Dashboard unreachable at `/pov-dash/` | dash unit stopped | `systemctl start pov-dash` |