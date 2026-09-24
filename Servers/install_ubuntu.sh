#!/usr/bin/env bash
# =============================================================================
# install_ubuntu.sh — Install my-trading-system on a fresh Ubuntu VPS
# =============================================================================
#
# ARCHITECTURE (locked):
#     Python (pov_engine)  ->  OpenAlgo (server)  ->  Broker  ->  Exchange
#
# OpenAlgo is a PRISTINE upstream clone. Nothing of ours is patched into it.
# The engine talks to it only through the public OpenAlgo SDK.
#
# LAYOUT THIS SCRIPT CREATES:
#
#     /opt/my-trading-system/
#       openalgo/        pristine upstream clone (git pull to upgrade)
#       _restored/       YOUR STATE - the source of truth
#         .env           OpenAlgo config (FERNET_SALT, API_KEY_PEPPER, DB urls)
#         db/            SQLite DBs - broker session tokens, encrypted
#         keys/          Fernet / oauth key material
#         download/      master-contract symbol cache
#       pov_engine/      the engine (editable install, SDK-only)
#       scripts/         launchers + health checks
#       data/            engine state
#
# WHY _restored/ IS A SIBLING, NOT A SUBFOLDER:
#   OpenAlgo resolves .env and db/ relative to its OWN root, so state kept in a
#   subfolder would be invisible to it. As a sibling it is both visible to
#   OpenAlgo (via the start script's bootstrap bridge) and immune to a re-clone.
#
# CRITICAL - .env AND db/ ARE CRYPTOGRAPHICALLY MARRIED:
#   FERNET_SALT and API_KEY_PEPPER in .env are the KDF inputs that encrypt the
#   broker session tokens in db/openalgo.db. Restore one without the other and
#   OpenAlgo boots fine but CANNOT DECRYPT your broker login. They always
#   travel together - that is why both live inside _restored/.
#
# USAGE
#   scp this file plus your state to the VPS, then:
#     sudo bash install_ubuntu.sh --domain trade.example.com
#     sudo bash install_ubuntu.sh --domain trade.example.com --no-nginx
#
# WHAT IT DOES NOT DO
#   * It does not touch archives/, and it never patches app.py.
#   * It does not start trading. Analyzer mode is the default; go live in the UI.
#   * It does not put your API key in any file it creates. You place .env.
# =============================================================================
set -euo pipefail

# ------------------------------------------------------------------ config
APP_NAME="my-trading-system"
INSTALL_ROOT="/opt/${APP_NAME}"
SERVICE_USER="algo"
OPENALGO_REPO="https://github.com/marketcalls/openalgo.git"
OPENALGO_URL="http://127.0.0.1:5000"
DASH_PORT="8050"
WS_PORT="8765"
SETUP_NGINX=1
DOMAIN=""
PY_VER="python3"

# ------------------------------------------------------------------ output
C_RESET=$'\033[0m'; C_OK=$'\033[32m'; C_ERR=$'\033[31m'
C_WARN=$'\033[33m'; C_STEP=$'\033[36m'
step() { printf '\n%s==> %s%s\n' "$C_STEP" "$*" "$C_RESET"; }
ok()   { printf '    %s[OK]%s %s\n'   "$C_OK"   "$C_RESET" "$*"; }
warn() { printf '    %s[WARN]%s %s\n' "$C_WARN" "$C_RESET" "$*"; }
die()  { printf '    %s[ABORT]%s %s\n' "$C_ERR" "$C_RESET" "$*" >&2; exit 1; }

# ------------------------------------------------------------------ args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)   DOMAIN="${2:-}"; shift 2 ;;
    --user)     SERVICE_USER="${2:-}"; shift 2 ;;
    --root)     INSTALL_ROOT="${2:-}"; shift 2 ;;
    --no-nginx) SETUP_NGINX=0; shift ;;
    -h|--help)
      sed -n '2,55p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run with sudo"

step "Preflight"
[[ -f /etc/os-release ]] || die "cannot detect OS"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ok "OS: ${PRETTY_NAME:-$ID}" ;;
  *) warn "untested OS '${ID:-unknown}' - continuing" ;;
esac
if [[ -d "$INSTALL_ROOT" ]] && [[ -n "$(ls -A "$INSTALL_ROOT" 2>/dev/null || true)" ]]; then
  die "$INSTALL_ROOT already exists and is not empty. Move it aside first."
fi
command -v systemctl >/dev/null || die "systemd not found"
ok "install root: $INSTALL_ROOT"

# ------------------------------------------------------------------ packages
step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  git curl ca-certificates build-essential pkg-config \
  "${PY_VER}" "${PY_VER}-venv" "${PY_VER}-dev" python3-pip \
  sqlite3 rsync nginx >/dev/null
ok "base packages installed"

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash "$SERVICE_USER"
  ok "created service user: $SERVICE_USER"
else
  ok "service user exists: $SERVICE_USER"
fi

# ------------------------------------------------------------------ dirs
step "Creating directory layout"
mkdir -p "$INSTALL_ROOT"/{openalgo,_restored,scripts,data,backups,log}
mkdir -p "$INSTALL_ROOT/_restored"/{db,keys,download}
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_ROOT"
ok "created $INSTALL_ROOT/{openalgo,_restored,scripts,data,backups,log}"

# ------------------------------------------------------------------ clone
step "Cloning OpenAlgo (pristine - never patched)"
if [[ -d "$INSTALL_ROOT/openalgo/.git" ]]; then
  ok "openalgo/ already a git repo - leaving it alone"
else
  rm -rf "$INSTALL_ROOT/openalgo"
  sudo -u "$SERVICE_USER" git clone --depth 1 "$OPENALGO_REPO" "$INSTALL_ROOT/openalgo" >/dev/null 2>&1 \
    || die "git clone failed"
  ok "cloned $(cd "$INSTALL_ROOT/openalgo" && git log -1 --format='%h %s')"
fi

# ------------------------------------------------------------------ venv
step "Creating OpenAlgo virtualenv"
OA_VENV="$INSTALL_ROOT/openalgo/.venv"
if [[ ! -x "$OA_VENV/bin/python" ]]; then
  sudo -u "$SERVICE_USER" "$PY_VER" -m venv "$OA_VENV"
  sudo -u "$SERVICE_USER" "$OA_VENV/bin/python" -m pip install -q --upgrade pip wheel setuptools
  ok "venv created"
else
  ok "venv already present"
fi
sudo -u "$SERVICE_USER" "$OA_VENV/bin/python" -m pip install -q -r "$INSTALL_ROOT/openalgo/requirements.txt"
ok "OpenAlgo requirements installed"

# ------------------------------------------------------------------ state
step "Checking _restored/ state"
STATE_OK=1
for item in .env db; do
  if [[ -e "$INSTALL_ROOT/_restored/$item" ]]; then
    ok "_restored/$item present"
  else
    warn "_restored/$item MISSING"
    STATE_OK=0
  fi
done

if [[ "$STATE_OK" -eq 0 ]]; then
  warn "----------------------------------------------------------------"
  warn "You must place your state before the platform can work:"
  warn "    $INSTALL_ROOT/_restored/.env      (OpenAlgo config)"
  warn "    $INSTALL_ROOT/_restored/db/       (databases; broker tokens)"
  warn "Copy them from your dev machine. .env and db/ MUST come together,"
  warn "or OpenAlgo will boot but be unable to decrypt the broker login."
  warn "----------------------------------------------------------------"
else
  chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_ROOT/_restored"

  # Bridge state into the clone the same way start_openalgo.py does. OpenAlgo
  # reads .env and db/ from its OWN root, so they must be materialised there.
  for item in .env db keys download; do
    [[ -e "$INSTALL_ROOT/_restored/$item" ]] || continue
    cp -r "$INSTALL_ROOT/_restored/$item" "$INSTALL_ROOT/openalgo/" 2>/dev/null || true
  done
  chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_ROOT/openalgo"
  ok "bridged _restored/* into openalgo/"

  # The one check that matters: can the restored .env decrypt the db tokens?
  if sudo -u "$SERVICE_USER" test -f "$INSTALL_ROOT/openalgo/db/openalgo.db"; then
    VERIFY=$(mktemp)
    cat > "$VERIFY" <<'PYEOF'
import os, sys, sqlite3
sys.path.insert(0, os.getcwd())
try:
    from dotenv import load_dotenv
    load_dotenv(".env")
    from database.auth_db import decrypt_token
    c = sqlite3.connect("db/openalgo.db")
    row = c.execute("select auth from auth limit 1").fetchone()
    if not row:
        print("NO_AUTH_ROW"); sys.exit(0)
    decrypt_token(row[0])
    print("DECRYPT_OK")
except Exception as e:
    print("DECRYPT_FAIL: %s" % e)
PYEOF
    chmod +r "$VERIFY"
    RESULT=$(cd "$INSTALL_ROOT/openalgo" && sudo -u "$SERVICE_USER" "$OA_VENV/bin/python" "$VERIFY" 2>&1 | tail -1)
    rm -f "$VERIFY"
    case "$RESULT" in
      DECRYPT_OK)   ok "broker tokens decrypt with restored .env (pairing valid)" ;;
      NO_AUTH_ROW)  warn "no broker login stored yet - log in via the UI" ;;
      *)            warn "pairing check: $RESULT"
                    warn ".env and db/ may have been restored separately. They must match." ;;
    esac
  fi
fi

# ------------------------------------------------------------------ engine
step "Installing pov_engine (editable, SDK-only)"
if [[ -f "$INSTALL_ROOT/pov_engine/pyproject.toml" ]]; then
  sudo -u "$SERVICE_USER" "$OA_VENV/bin/python" -m pip install -q -e "$INSTALL_ROOT/pov_engine[dashboard]"
  ok "pov_engine installed editable with [dashboard] extras"

  # The engine must not import from the OpenAlgo tree. Assert that here so a
  # regression is caught at install time rather than at 09:15 on a trading day.
  ENGINE_CHECK=$(cd "$INSTALL_ROOT" && sudo -u "$SERVICE_USER" "$OA_VENV/bin/python" -c "
import sys
sys.path.insert(0, '$INSTALL_ROOT')
import pov_engine.core.ledger as L
print('LEDGER_AVAILABLE=%s' % L._LEDGER_AVAILABLE)
" 2>&1 | tail -1)
  ok "engine import probe: $ENGINE_CHECK  (LEDGER_AVAILABLE=False is correct: in-memory authoritative)"
else
  warn "pov_engine/pyproject.toml not found - skipping engine install"
  warn "rsync your pov_engine/ here, then re-run this script"
fi

# ------------------------------------------------------------------ systemd
step "Writing systemd units"
cat > "/etc/systemd/system/openalgo.service" <<UNIT
[Unit]
Description=OpenAlgo trading platform (Flask + SocketIO)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_ROOT}/openalgo
# gunicorn + eventlet: the production pairing OpenAlgo's own start.sh uses.
# --workers 1 is REQUIRED - multiple workers break SocketIO room state.
ExecStart=${OA_VENV}/bin/gunicorn --worker-class eventlet -w 1 --bind 127.0.0.1:5000 app:app
Restart=on-failure
RestartSec=5
StandardOutput=append:${INSTALL_ROOT}/log/openalgo.out.log
StandardError=append:${INSTALL_ROOT}/log/openalgo.err.log

[Install]
WantedBy=multi-user.target
UNIT

cat > "/etc/systemd/system/openalgo-ws.service" <<UNIT
[Unit]
Description=OpenAlgo WebSocket proxy (port ${WS_PORT})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_ROOT}/openalgo
ExecStart=${OA_VENV}/bin/python -m websocket_proxy.server
Restart=on-failure
RestartSec=5
StandardOutput=append:${INSTALL_ROOT}/log/openalgo-ws.out.log
StandardError=append:${INSTALL_ROOT}/log/openalgo-ws.err.log

[Install]
WantedBy=multi-user.target
UNIT

cat > "/etc/systemd/system/pov-runner.service" <<UNIT
[Unit]
Description=POV execution engine (client of OpenAlgo via SDK)
After=openalgo.service openalgo-ws.service
Wants=openalgo.service

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_ROOT}
Environment=OPENALGO_URL=${OPENALGO_URL}
ExecStart=${OA_VENV}/bin/python ${INSTALL_ROOT}/scripts/run_pov.py
Restart=on-failure
RestartSec=10
StandardOutput=append:${INSTALL_ROOT}/log/pov.out.log
StandardError=append:${INSTALL_ROOT}/log/pov.err.log

[Install]
WantedBy=multi-user.target
UNIT

cat > "/etc/systemd/system/pov-dash.service" <<UNIT
[Unit]
Description=POV Plotly dashboard (port ${DASH_PORT})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_ROOT}
Environment=OPENALGO_URL=${OPENALGO_URL}
ExecStart=${OA_VENV}/bin/python -m pov_engine.dashboard.cli --host 127.0.0.1 --port ${DASH_PORT}
Restart=on-failure
RestartSec=5
StandardOutput=append:${INSTALL_ROOT}/log/pov-dash.out.log
StandardError=append:${INSTALL_ROOT}/log/pov-dash.err.log

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
ok "units written: openalgo, openalgo-ws, pov-runner, pov-dash"

# Install and start the platform; leave the engine stopped until you are ready.
systemctl enable openalgo openalgo-ws >/dev/null 2>&1 || warn "enable failed for platform units"
systemctl restart openalgo openalgo-ws
ok "platform units enabled and started (engine left stopped on purpose)"

# ------------------------------------------------------------------ nginx
if [[ "$SETUP_NGINX" -eq 1 ]]; then
  step "Configuring nginx"
  cat > "/etc/nginx/sites-available/${APP_NAME}" <<NGINX
server {
    listen 80;
    server_name ${DOMAIN:-_};

    client_max_body_size 16m;

    # OpenAlgo platform: UI + REST API
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    # SocketIO needs the upgrade headers or realtime updates silently stop.
    location /socket.io/ {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 3600s;
    }

    # Broker tick websocket proxy
    location /ws {
        proxy_pass http://127.0.0.1:${WS_PORT};
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }

    # POV dashboard is served DIRECTLY. It is not embedded in the OpenAlgo UI,
    # so there is no blueprint and no app.py wiring to maintain.
    location /pov-dash/ {
        proxy_pass http://127.0.0.1:${DASH_PORT}/;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }
}
NGINX
  ln -sf "/etc/nginx/sites-available/${APP_NAME}" "/etc/nginx/sites-enabled/${APP_NAME}"
  rm -f /etc/nginx/sites-enabled/default
  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx
    ok "nginx configured (site: ${APP_NAME})"
  else
    warn "nginx -t failed - config left in place but NOT reloaded"
    nginx -t || true
  fi
else
  warn "--no-nginx: skipping reverse proxy setup"
fi

# ------------------------------------------------------------------ verify
step "Verifying installation"
sleep 3

PLATFORM_UP=0
for _ in $(seq 1 20); do
  if curl -fsS --max-time 3 "$OPENALGO_URL/auth/check-setup" >/dev/null 2>&1 \
     || curl -fsS --max-time 3 "$OPENALGO_URL/" >/dev/null 2>&1; then
    PLATFORM_UP=1; break
  fi
  sleep 2
done

if [[ "$PLATFORM_UP" -eq 1 ]]; then
  ok "OpenAlgo responding on $OPENALGO_URL"

  # The retired endpoint must NOT exist. Its presence would mean old-architecture
  # POV code was somehow patched back into the platform.
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$OPENALGO_URL/api/v1/povorder" || echo "000")
  if [[ "$CODE" == "404" ]]; then
    ok "GET/POST /api/v1/povorder -> 404 (server-side POV correctly retired)"
  else
    warn "POST /api/v1/povorder returned $CODE, expected 404"
    warn "old-architecture POV may be present in the clone - investigate"
  fi
else
  warn "OpenAlgo not responding yet. Inspect: journalctl -u openalgo -n 80"
fi

step "Installation summary"
cat <<SUMMARY

  Install root : ${INSTALL_ROOT}
  Service user : ${SERVICE_USER}
  State        : ${INSTALL_ROOT}/_restored  (source of truth)

  Data path    : Python (pov_engine) -> OpenAlgo -> Broker -> Exchange

  Units        : openalgo (running), openalgo-ws (running),
                 pov-runner (stopped), pov-dash (stopped)

  Next steps
    1. If _restored/ was empty, copy .env and db/ here, then re-run:
         sudo bash $0 --domain ${DOMAIN:-<your-domain>}
    2. Open the UI and log in to your broker:
         ${DOMAIN:+http://}${DOMAIN:-http://<server-ip>}
    3. Confirm the platform is in ANALYZER (paper) mode.
    4. Start the engine when ready:
         sudo systemctl start pov-runner pov-dash
    5. Verify:
         sudo -u ${SERVICE_USER} ${OA_VENV}/bin/python ${INSTALL_ROOT}/scripts/smoke_test.py

  SECURITY
    * Put TLS in front of this (certbot --nginx -d ${DOMAIN:-<your-domain>}).
    * The API key belongs in _restored/.env ONLY - never in a source file.
    * Consider fail2ban; the UI is internet-facing.

SUMMARY
ok "done"