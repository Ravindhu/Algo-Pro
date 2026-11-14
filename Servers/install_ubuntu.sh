#!/usr/bin/env bash
# =============================================================================
# install_ubuntu.sh â€” Install my-trading-system on a fresh Ubuntu VPS
# =============================================================================
#
# ARCHITECTURE (locked):
#     Python (blue_engine)  ->  OpenAlgo (server)  ->  Broker  ->  Exchange
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
#       src/quant/blue_engine/   the engine (editable install, SDK-only)
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
#     sudo bash install_ubuntu.sh --domain trade.example.com --email you@example.com
#     sudo bash install_ubuntu.sh --domain trade.example.com --no-nginx
#     sudo bash install_ubuntu.sh --allow-plain-http        # LAN-only lab, no TLS
#
# TLS IS THE DEFAULT. The UI and API carry broker logins and order controls,
# so nginx serves them over HTTPS (Let's Encrypt) and redirects HTTP to it.
# The installer refuses to serve over plain HTTP unless --allow-plain-http is
# given explicitly. If the certificate cannot be issued, the site is removed
# from nginx rather than left reachable over plain HTTP.
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
OPENALGO_PORT="5000"
OPENALGO_URL="http://127.0.0.1:${OPENALGO_PORT}"
DASH_PORT="8050"
WS_PORT="8765"
SETUP_NGINX=1
DOMAIN=""
PY_VER="python3"
DEPLOY_ENV=""
MTS_API_KEY=""
RUN_SMOKE=1
START_ENGINE=0
EMAIL=""                 # Let's Encrypt contact, required for TLS
ALLOW_PLAIN_HTTP=0       # explicit opt-out: serve the UI over plain HTTP

# ------------------------------------------------------------------ output
C_RESET=$'\033[0m'; C_OK=$'\033[32m'; C_ERR=$'\033[31m'
C_WARN=$'\033[33m'; C_STEP=$'\033[36m'
step() { printf '\n%s==> %s%s\n' "$C_STEP" "$*" "$C_RESET"; }
ok()   { printf '    %s[OK]%s %s\n'   "$C_OK"   "$C_RESET" "$*"; }
warn() { printf '    %s[WARN]%s %s\n' "$C_WARN" "$C_RESET" "$*"; }
die()  { printf '    %s[ABORT]%s %s\n' "$C_ERR" "$C_RESET" "$*" >&2; exit 1; }

# ------------------------------------------------------------------ config file
# Load Servers/deploy.env if present, so the common case is a bare
# `sudo bash install_ubuntu.sh` with no arguments.
#
# Deploy.env holds the API key. A key on the command line would land in
# ~/.bash_history and in `ps` output; a file does not. Values here are
# defaults: any CLI flag still overrides them.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ENV="${SCRIPT_DIR}/deploy.env"
if [[ -f "$DEPLOY_ENV" ]]; then
  # Parse only simple KEY=VALUE lines. Deliberately not `source`d: sourcing
  # would execute arbitrary shell from the file, and this file is writable by
  # whoever prepares the deploy.
  while IFS='=' read -r _k _v; do
    [[ "$_k" =~ ^[A-Z_]+$ ]] || continue
    _v="${_v%\"}"; _v="${_v#\"}"      # strip surrounding double quotes
    _v="${_v%\'}"; _v="${_v#\'}"        # strip surrounding single quotes
    case "$_k" in
      MTS_DOMAIN)           [[ -n "$_v" ]] && DOMAIN="$_v" ;;
      MTS_API_KEY)          [[ -n "$_v" ]] && MTS_API_KEY="$_v" ;;
      MTS_ROOT)             [[ -n "$_v" ]] && INSTALL_ROOT="$_v" ;;
      MTS_USER)             [[ -n "$_v" ]] && SERVICE_USER="$_v" ;;
      MTS_OPENALGO_URL)     [[ -n "$_v" ]] && OPENALGO_URL="$_v" ;;
      MTS_OPENALGO_PORT)    [[ -n "$_v" ]] && OPENALGO_PORT="$_v" ;;
      MTS_DASH_PORT)        [[ -n "$_v" ]] && DASH_PORT="$_v" ;;
      MTS_WS_PORT)          [[ -n "$_v" ]] && WS_PORT="$_v" ;;
      MTS_OPENALGO_REPO)    [[ -n "$_v" ]] && OPENALGO_REPO="$_v" ;;
      MTS_EMAIL)            [[ -n "$_v" ]] && EMAIL="$_v" ;;
      MTS_ALLOW_PLAIN_HTTP) [[ "$_v" == "1" ]] && ALLOW_PLAIN_HTTP=1 ;;
      MTS_START_ENGINE)     [[ "$_v" == "1" ]] && START_ENGINE=1 ;;
      MTS_SETUP_NGINX)      [[ "$_v" == "0" ]] && SETUP_NGINX=0 ;;
      MTS_RUN_SMOKE)        [[ "$_v" == "0" ]] && RUN_SMOKE=0 ;;
    esac
  done < <(grep -E '^[A-Z_]+=' "$DEPLOY_ENV" 2>/dev/null || true)
fi

# ------------------------------------------------------------------ args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)       DOMAIN="${2:-}"; shift 2 ;;
    --email)        EMAIL="${2:-}"; shift 2 ;;
    --allow-plain-http) ALLOW_PLAIN_HTTP=1; shift ;;
    --user)         SERVICE_USER="${2:-}"; shift 2 ;;
    --root)         INSTALL_ROOT="${2:-}"; shift 2 ;;
    --api-key)      MTS_API_KEY="${2:-}"; shift 2 ;;
    --no-nginx)     SETUP_NGINX=0; shift ;;
    --start-engine) START_ENGINE=1; shift ;;
    --no-smoke)     RUN_SMOKE=0; shift ;;
    -h|--help)
      sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run with sudo"

# Preflight: refuse to expose the trading UI over plain HTTP by accident.
if [[ "$SETUP_NGINX" -eq 1 && "$ALLOW_PLAIN_HTTP" -eq 0 ]]; then
  [[ -n "$DOMAIN" && -n "$EMAIL" ]] || die "refusing to serve the trading UI over plain HTTP. Pass --domain <name> --email <you@example.com> for TLS, --allow-plain-http for a LAN-only lab, or --no-nginx if you terminate TLS yourself."
fi
if [[ "$ALLOW_PLAIN_HTTP" -eq 1 ]]; then
  warn "plain HTTP explicitly allowed - do NOT expose this host to the internet"
fi

step "Preflight"
[[ -f /etc/os-release ]] || die "cannot detect OS"
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ok "OS: ${PRETTY_NAME:-$ID}" ;;
  *) warn "untested OS '${ID:-unknown}' - continuing" ;;
esac
if [[ -d "$INSTALL_ROOT" ]] && [[ ! -d "$INSTALL_ROOT/.git" ]] && [[ -n "$(ls -A "$INSTALL_ROOT" 2>/dev/null || true)" ]]; then
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

if [[ "$SETUP_NGINX" -eq 1 && -n "$DOMAIN" && -n "$EMAIL" ]]; then
  apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
  ok "certbot installed (TLS)"
fi

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

  # ---- API key ---------------------------------------------------------------
  # If MTS_API_KEY was supplied (via deploy.env or --api-key), reconcile it with
  # _restored/.env. The key is written to the FILE, never passed on a command
  # line, so it does not end up in shell history or in `ps` output.
  #
  # If _restored/.env already carries an OPENALGO_API_KEY, that value wins	unless
  # MTS_API_KEY differs - in which case we update it and say so, because a
  # mismatch between the .env key and the engine's key is a silent auth failure.
  if [[ -n "$MTS_API_KEY" ]]; then
    ENVF="$INSTALL_ROOT/_restored/.env"
    if grep -q '^OPENALGO_API_KEY=' "$ENVF" 2>/dev/null; then
      CURRENT=$(grep '^OPENALGO_API_KEY=' "$ENVF" | head -1 | cut -d= -f2- | tr -d "\"'\r")
      if [[ "$CURRENT" == "$MTS_API_KEY" ]]; then
        ok "API key already matches _restored/.env (unchanged)"
      else
        sed -i "s|^OPENALGO_API_KEY=.*|OPENALGO_API_KEY='${MTS_API_KEY}'|" "$ENVF"
        ok "API key updated in _restored/.env (replace_in_file)"
      fi
    else
      printf "\n# Set by install_ubuntu.sh from deploy.env\nOPENALGO_API_KEY='%s'\n" \
        "$MTS_API_KEY" >> "$ENVF"
      ok "API key written to _restored/.env"
    fi
    # The engine reads this; also export it for the smoke test below.
    if grep -q '^HOST_SERVER=' "$ENVF" 2>/dev/null; then
      sed -i "s|^HOST_SERVER=.*|HOST_SERVER='${OPENALGO_URL}'|" "$ENVF"
    else
      printf "HOST_SERVER='%s'\n" "$OPENALGO_URL" >> "$ENVF"
    fi
    ok "HOST_SERVER set to ${OPENALGO_URL} in _restored/.env"
    chmod 600 "$ENVF"
    chown "$SERVICE_USER:$SERVICE_USER" "$ENVF"
    ok "_restored/.env locked down (chmod 600)"
  else
    warn "no MTS_API_KEY supplied - relying on the key already in _restored/.env"
    warn "set MTS_API_KEY in Servers/deploy.env to have the installer manage it"
  fi

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
step "Installing blue_engine (editable, SDK-only)"
if [[ -f "$INSTALL_ROOT/src/quant/blue_engine/pyproject.toml" ]]; then
  sudo -u "$SERVICE_USER" "$OA_VENV/bin/python" -m pip install -q -e "$INSTALL_ROOT/src/quant/blue_engine[charts]"
  sudo -u "$SERVICE_USER" "$OA_VENV/bin/python" -m pip install -q fastapi uvicorn
  ok "blue_engine installed editable with [charts] extras; fastapi + uvicorn installed for the dashboard"

  # The engine must not import from the OpenAlgo tree. Assert that here so a
  # regression is caught at install time rather than at 09:15 on a trading day.
  ENGINE_CHECK=$(cd "$INSTALL_ROOT" && sudo -u "$SERVICE_USER" "$OA_VENV/bin/python" -c "
import sys
sys.path.insert(0, '$INSTALL_ROOT')
import blue_engine.core.ledger as L
print('LEDGER_AVAILABLE=%s' % L._LEDGER_AVAILABLE)
" 2>&1 | tail -1)
  ok "engine import probe: $ENGINE_CHECK  (LEDGER_AVAILABLE=False is correct: in-memory authoritative)"
else
  warn "src/quant/blue_engine/pyproject.toml not found - skipping engine install"
  warn "clone the repo into $INSTALL_ROOT (launch_ubuntu_server.sh does this), then re-run"
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
Description=POV FastAPI dashboard (port ${DASH_PORT})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_ROOT}
Environment=OPENALGO_URL=${OPENALGO_URL}
ExecStart=${OA_VENV}/bin/python -m uvicorn --app-dir ${INSTALL_ROOT}/dashboard app:create_app --factory --host 127.0.0.1 --port ${DASH_PORT}
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
    if [[ -n "$DOMAIN" && -n "$EMAIL" ]]; then
      step "Obtaining TLS certificate (Let's Encrypt)"
      CERT_LOG="/var/log/${APP_NAME}-certbot.log"
      if certbot --nginx --non-interactive --agree-tos -m "$EMAIL" \
           -d "$DOMAIN" --redirect >"$CERT_LOG" 2>&1; then
        ok "HTTPS enabled for ${DOMAIN} (HTTP redirects to HTTPS)"
        TLS_OK=1
      else
        # Fail closed: do not leave the trading UI reachable over plain HTTP.
        rm -f "/etc/nginx/sites-enabled/${APP_NAME}"
        systemctl reload nginx || true
        warn "certificate issuance FAILED - site removed from nginx, nothing served over plain HTTP"
        warn "check DNS points ${DOMAIN} at this server; log: ${CERT_LOG}"
        warn "after fixing DNS, re-run this installer or: sudo certbot --nginx -d ${DOMAIN}"
        TLS_OK=0
      fi
    fi
  else
    warn "nginx -t failed - config left in place but NOT reloaded"
    nginx -t || true
  fi
else
  warn "--no-nginx: skipping reverse proxy setup"
fi

# Where the operator should point a browser (used in the summary below).
if [[ "$SETUP_NGINX" -eq 1 && -n "$DOMAIN" && -n "$EMAIL" && "${TLS_OK:-0}" -eq 1 ]]; then
  UI_URL="https://${DOMAIN}"
elif [[ -n "$DOMAIN" ]]; then
  UI_URL="http://${DOMAIN}"
else
  UI_URL="http://<server-ip>"
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

# ------------------------------------------------------------------ smoke gate
SMOKE_RESULT="skipped"
if [[ "$RUN_SMOKE" -eq 1 ]] && [[ -x "$OA_VENV/bin/python" ]]; then
  step "Smoke test (acceptance gate)"
  if sudo -u "$SERVICE_USER" "$OA_VENV/bin/python" "$INSTALL_ROOT/scripts/smoke_test.py"; then
    ok "smoke test PASSED"
    SMOKE_RESULT="passed"
  else
    SMOKE_RESULT="FAILED"
    warn "smoke test FAILED - do NOT trade until this passes"
    warn "hard failures are listed above; warnings are expected when the platform is idle"
  fi
elif [[ "$RUN_SMOKE" -eq 0 ]]; then
  warn "smoke test skipped (MTS_RUN_SMOKE=0 / --no-smoke)"
fi

# ------------------------------------------------------------------ engine
if [[ "$START_ENGINE" -eq 1 ]]; then
  step "Starting the engine (MTS_START_ENGINE=1)"
  if [[ "$SMOKE_RESULT" == "FAILED" ]]; then
    warn "NOT starting pov-runner: the smoke gate failed"
    warn "fix the failures, then: sudo systemctl start pov-runner pov-dash"
    ENGINE_MSG="blocked (smoke test failed)"
  else
    systemctl enable pov-dash >/dev/null 2>&1 || true
    systemctl restart pov-dash
    ok "pov-dash started (dashboard on port ${DASH_PORT})"

    # pov-runner is left DISABLED on purpose. Enabling it would restart an
    # execution automatically after a reboot or an upgrade - undesirable for an
    # order-placing service that should only run when you intend it to.
    systemctl restart pov-runner
    ok "pov-runner started (NOT enabled - it will not auto-start on reboot)"
    ENGINE_MSG="running (pov-runner + pov-dash)"
  fi
else
  ENGINE_MSG="stopped (start with: sudo systemctl start pov-runner pov-dash)"
fi

step "Installation summary"
cat <<SUMMARY

  Install root : ${INSTALL_ROOT}
  Service user : ${SERVICE_USER}
  State        : ${INSTALL_ROOT}/_restored  (source of truth)

  Data path    : Python (blue_engine) -> OpenAlgo -> Broker -> Exchange

  Units        : openalgo            running
                 openalgo-ws         running
                 pov-dash            ${ENGINE_MSG}
                 pov-runner          ${ENGINE_MSG}
  Smoke gate   : ${SMOKE_RESULT}

  Next steps
    1. Open the UI and log in to your broker:
         ${UI_URL}
    2. Confirm the platform is in ANALYZER (paper) mode.
    3. Re-verify any time:
         sudo -u ${SERVICE_USER} ${OA_VENV}/bin/python ${INSTALL_ROOT}/scripts/smoke_test.py

  SECURITY
    * TLS: $( [[ "${TLS_OK:-0}" -eq 1 ]] && echo "enabled (Let's Encrypt)" || echo "NOT enabled - do not expose this host" )
    * The API key belongs in _restored/.env ONLY - never in a source file.
    * Consider fail2ban; the UI is internet-facing.

SUMMARY
ok "done"