#!/usr/bin/env bash
# ============================================================================
# setup_ubuntu.sh - One-shot provisioning for the OpenAlgo + POV engine stack
#                   on a fresh Ubuntu 22.04 / 24.04 LTS VPS.
#
# Run as a NON-root user with sudo rights:
#     chmod +x deploy/setup_ubuntu.sh && ./deploy/setup_ubuntu.sh
#
# What it does:
#   1. installs OS packages (python, nginx, build deps, ufw)
#   2. creates the deploy user layout under /opt/my-trading-system
#   3. creates the OpenAlgo venv + installs requirements
#   4. installs pov_engine (editable) into the SAME venv
#   5. installs gunicorn (threading-safe worker class)
#   6. hardens .env for a server deployment (no debug, loopback bind)
#   7. installs + enables the systemd units
#   8. prints next steps (nginx + secrets)
#
# Idempotent: safe to re-run.
# ============================================================================
set -euo pipefail

APP_USER="${APP_USER:-$USER}"
BASE_DIR="${BASE_DIR:-/opt/my-trading-system}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$BASE_DIR/openalgo/.venv"

log()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    [OK] %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    [WARN] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m    [FAIL] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "Do not run as root. Run as your normal user (it uses sudo)."

# ---------------------------------------------------------------- 1. packages
log "1. Installing OS packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3 python3-venv python3-dev python3-pip \
    build-essential libssl-dev libffi-dev \
    nginx ufw curl git sqlite3 ca-certificates
ok "packages installed"

PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
log "   python3 version: $PYV"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
    || die "Python >= 3.10 required (found $PYV)"

# ------------------------------------------------------------- 2. repo layout
log "2. Creating application layout at $BASE_DIR"
sudo mkdir -p "$BASE_DIR"
sudo chown -R "$APP_USER:$APP_USER" "$BASE_DIR"

# Copy (or refresh) the two trees. rsync keeps upgrades cheap.
sudo apt-get install -y -qq rsync
rsync -a --delete-excluded \
      --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
      "$REPO_DIR/openalgo/"   "$BASE_DIR/openalgo/"
rsync -a --delete-excluded \
      --exclude '.git' --exclude '__pycache__' \
      "$REPO_DIR/pov_engine/" "$BASE_DIR/pov_engine/"
mkdir -p "$BASE_DIR/data" "$BASE_DIR/logs"
ok "code copied to $BASE_DIR"

# --------------------------------------------------------------- 3. venv/deps
log "3. Creating venv + installing requirements"
[[ -d "$VENV_DIR" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip wheel setuptools -q
"$VENV_DIR/bin/python" -m pip install -r "$BASE_DIR/openalgo/requirements.txt" -q
ok "OpenAlgo requirements installed"

# gunicorn: gthread worker (NOT eventlet/gevent).
# Reason: OpenAlgo pins flask-socketio to async_mode="threading" in
# extensions.py, so greenlet/eventlet workers would deadlock the
# SocketIO layer. gthread keeps request threads and matches threading mode.
"$VENV_DIR/bin/python" -m pip install "gunicorn>=21" -q
ok "gunicorn (gthread) installed"

# ------------------------------------------------------------ 4. pov_engine
log "4. Installing pov_engine (editable)"
"$VENV_DIR/bin/python" -m pip install -e "$BASE_DIR/pov_engine" -q
"$VENV_DIR/bin/python" -c "import pov_engine; print('    pov_engine ->', pov_engine.__file__)"
ok "pov_engine installed"

# ------------------------------------------------------------------ 5. .env
log "5. Hardening .env for server deployment"
ENVF="$BASE_DIR/openalgo/.env"
[[ -f "$ENVF" ]] || { cp "$BASE_DIR/openalgo/.sample.env" "$ENVF"; warn "created .env from sample - YOU MUST add broker keys"; }

set_kv() {  # set_kv KEY VALUE  (replace or append, unquoted)
    local k="$1" v="$2"
    if grep -qE "^${k}=" "$ENVF"; then
        sed -i "s|^${k}=.*|${k}=${v}|" "$ENVF"
    else
        printf '%s=%s\n' "$k" "$v" >> "$ENVF"
    fi
}

# Loopback bind + no debug: nginx terminates TLS and proxies to us.
set_kv FLASK_HOST_IP 127.0.0.1
set_kv FLASK_DEBUG   False
set_kv FLASK_ENV     production
set_kv WEBSOCKET_HOST 127.0.0.1
set_kv WEBSOCKET_PORT 8765
ok ".env hardened (FLASK_DEBUG=False, loopback bind)"

if ! grep -qE '^OPENALGO_API_KEY=.+' "$ENVF"; then
    warn "OPENALGO_API_KEY is empty in $ENVF - generate one in the UI after first start"
fi

# ------------------------------------------------------------- 6. systemd
log "6. Installing systemd units"
for unit in openalgo pov-dash pov-runner; do
    sudo cp "$REPO_DIR/deploy/${unit}.service" "/etc/systemd/system/${unit}.service"
    sudo sed -i "s|@APP_USER@|$APP_USER|g; s|@BASE_DIR@|$BASE_DIR|g" \
         "/etc/systemd/system/${unit}.service"
done
# pov-runner trades, so it must NOT autostart on boot: disable after enabling.
sudo systemctl daemon-reload
sudo systemctl enable openalgo pov-dash
sudo systemctl disable pov-runner || true
ok "units installed (openalgo + pov-dash enabled; pov-runner manual-only)"

# ---------------------------------------------------------------- 7. firewall
log "7. Configuring firewall (ufw)"
sudo ufw allow OpenSSH >/dev/null 2>&1 || true
sudo ufw allow "Nginx Full" >/dev/null 2>&1 || true
# Never expose 5000/8050/8765 publicly - nginx proxies instead.
sudo ufw --force enable >/dev/null 2>&1 || warn "ufw enable skipped"
ok "ufw: SSH + Nginx allowed; app ports stay internal"

cat <<EOF

============================================================================
 SETUP COMPLETE
============================================================================
 Base dir     : $BASE_DIR
 Venv         : $VENV_DIR
 Services     : openalgo, pov-dash   (pov-runner = manual, starts on demand)

 NEXT STEPS
   1. Edit secrets:      sudo nano $BASE_DIR/openalgo/.env
        - broker API keys
        - OPENALGO_API_KEY (generate in the UI afterwards)
        - APP_KEY / security keys
   2. Start the platform: sudo systemctl start openalgo
   3. Check health:       systemctl status openalgo
                          curl -s http://127.0.0.1:5000/auth/check-setup
   4. Start the dashboard: sudo systemctl start pov-dash
   5. Configure nginx:     sudo cp deploy/nginx.conf /etc/nginx/sites-available/my-trading-system
                           sudo ln -sf /etc/nginx/sites-available/my-trading-system /etc/nginx/sites-enabled/
                           sudo nginx -t && sudo systemctl reload nginx
   6. TLS:                 sudo apt install certbot python3-certbot-nginx
                           sudo certbot --nginx -d your-domain.com
   7. Run the engine once: sudo systemctl start pov-runner   (then stop it)
============================================================================
EOF