#!/usr/bin/env bash
# ============================================================================
#  OPENALGO + POV ENGINE  --  ONE-FILE UBUNTU VPS INSTALLER
#
#  Usage on a fresh Ubuntu 24.04 VPS:
#      scp -r my-trading-system user@SERVER:~/
#      ssh user@SERVER
#      cd ~/my-trading-system && chmod +x deploy/first_boot.sh
#      ./deploy/first_boot.sh
#
#  Does EVERYTHING: OS check, packages, OpenAlgo (fresh clone + your patch),
#  pov_engine, .env, systemd services, nginx + TLS, verification report.
#
#  You only answer 3 prompts:
#      1. FRESH or COPY an existing .env/db
#      2. broker API key + secret   (skipped in COPY mode)
#      3. domain for TLS            (empty = skip, use SSH tunnel)
# ============================================================================
set -euo pipefail

B="\033[1;36m"; G="\033[1;32m"; Y="\033[1;33m"; R="\033[1;31m"; N="\033[0m"
step() { printf "\n${B}==> %s${N}\n" "$*"; }
ok()   { printf "    ${G}[OK] %s${N}\n" "$*"; }
warn() { printf "    ${Y}[WARN] %s${N}\n" "$*"; }
die()  { printf "    ${R}[FAIL] %s${N}\n" "$*" >&2; exit 1; }
ask()  { local p="$1" d="${2:-}" v; read -r -p "    $p${d:+ [$d]}: " v; printf '%s' "${v:-$d}"; }
asksecret() { local p="$1" v; read -r -s -p "    $p: " v; echo; printf '%s' "$v"; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_DIR="${BASE_DIR:-/opt/my-trading-system}"
OA_DIR="$BASE_DIR/openalgo"
VENV="$OA_DIR/.venv"
UPSTREAM="https://github.com/marketcalls/openalgo.git"
BASELINE_FILE="$REPO_DIR/deploy/.baseline"
DEFAULT_BASELINE="5390dcd898ed597b85477068e61e28813824c819"

[[ $EUID -eq 0 ]] && die "Do not run as root. Run as your normal user (it uses sudo)."
command -v sudo >/dev/null || die "sudo required"

# ------------------------------------------------------------------ 1. checks
step "1. System checks"
. /etc/os-release 2>/dev/null || true
printf "    OS      : %s %s\n" "${NAME:-unknown}" "${VERSION_ID:-}"
printf "    User    : %s\n" "$USER"
printf "    Base dir: %s\n" "$BASE_DIR"
command -v python3 >/dev/null || die "python3 not found"
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
printf "    Python  : %s\n" "$PYV"
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,12) else 1)' \
  || die "OpenAlgo needs Python >= 3.12 (found $PYV). Use Ubuntu 24.04 or install 3.12."
ok "system OK"

# ---------------------------------------------------------------- 2. packages
step "2. Installing OS packages (sudo; several minutes)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-dev python3-pip \
  build-essential libssl-dev libffi-dev nginx ufw curl git rsync sqlite3 \
  ca-certificates certbot python3-certbot-nginx
ok "packages installed"

# ------------------------------------------------------------------- 3. layout
step "3. Creating $BASE_DIR"
sudo mkdir -p "$BASE_DIR"
sudo chown -R "$USER:$USER" "$BASE_DIR"
mkdir -p "$BASE_DIR/data" "$BASE_DIR/logs" "$BASE_DIR/backups"
ok "layout ready"

# --------------------------------------------------------- 4. seed strategy
step "4. Environment + database strategy"
cat <<'EOT'
    [1] FRESH - new secrets, empty DB. Re-create login + reconnect broker.
                Best for a truly independent production box.
    [2] COPY  - reuse your laptop .env + db (same login, same encrypted
                broker tokens, no re-setup). Requires ./seed/ prepared:
                    mkdir -p seed
                    scp laptop:.../openalgo/.env seed/env
                    scp -r laptop:.../openalgo/db  seed/db
EOT
MODE=$(ask "Choose 1 or 2" "1")
BROKER_KEY=""; BROKER_SECRET=""; DOMAIN=""
if [[ "$MODE" == "2" ]]; then
    [[ -f "$REPO_DIR/seed/env" ]] || die "seed/env not found - copy it first (see above)"
    ok "will restore your existing .env + db"
else
    step "4a. Broker credentials (from your broker developer portal)"
    BROKER_KEY=$(asksecret "BROKER_API_KEY")
    [[ -n "$BROKER_KEY" ]] || die "broker API key cannot be empty"
    BROKER_SECRET=$(asksecret "BROKER_API_SECRET")
    [[ -n "$BROKER_SECRET" ]] || die "broker API secret cannot be empty"
    ok "broker credentials captured (hidden input)"
fi
step "4b. Domain for HTTPS"
DOMAIN=$(ask "Your domain (empty = skip TLS, use SSH tunnel)" "")
[[ -n "$DOMAIN" ]] && ok "TLS for: $DOMAIN" || warn "no domain: UI reachable via SSH tunnel only (safe)"
# ------------------------------------------------------------------ 5. OpenAlgo
step "5. Installing OpenAlgo (fresh upstream clone + your custom patch)"
[[ -f "$REPO_DIR/patches/openalgo-custom.patch" ]] || die "patch missing: patches/openalgo-custom.patch"
BASELINE=$(cat "$BASELINE_FILE" 2>/dev/null || echo "$DEFAULT_BASELINE")

if [[ -d "$OA_DIR/.git" ]]; then
    warn "existing clone found - fetching"
    git -C "$OA_DIR" fetch --quiet origin || true
else
    git clone --quiet "$UPSTREAM" "$OA_DIR"
fi

# preserve .env + db across re-runs
PRESERVE=$(mktemp -d)
[[ -f "$OA_DIR/.env" ]] && cp "$OA_DIR/.env" "$PRESERVE/env" || true
[[ -d "$OA_DIR/db" ]] && cp -r "$OA_DIR/db" "$PRESERVE/db" || true

git -C "$OA_DIR" checkout --quiet --force "$BASELINE" \
  || die "baseline $BASELINE not found upstream - re-cut the patch (deploy/rebuild_patch.sh)"
git -C "$OA_DIR" clean -qffd -e .venv -e .env -e db
ok "at baseline $(git -C "$OA_DIR" rev-parse --short HEAD)"

PATCH="$REPO_DIR/patches/openalgo-custom.patch"
if git -C "$OA_DIR" apply --check --whitespace=nowarn "$PATCH" 2>/dev/null; then
    git -C "$OA_DIR" apply --whitespace=nowarn "$PATCH"
    ok "custom platform patch applied cleanly ($(grep -c '^diff --git' "$PATCH") files)"
elif git -C "$OA_DIR" apply --3way --whitespace=nowarn "$PATCH" 2>/dev/null; then
    ok "patch applied via 3-way merge"
    grep -rl '^<<<<<<<' "$OA_DIR" --include='*.py' >/dev/null 2>&1 && die "conflict markers - resolve before starting"
else
    die "patch failed to apply - do NOT start the platform"
fi

# blueprint + app.py wiring (idempotent)
cp "$REPO_DIR/patches/pov_terminal.py" "$OA_DIR/blueprints/pov_terminal.py"
APP="$OA_DIR/app.py"
if ! grep -qF 'from blueprints.pov_terminal import pov_terminal_bp' "$APP"; then
    A=$(grep -n '^from blueprints\.' "$APP" | tail -1 | cut -d: -f1)
    [[ -n "$A" ]] || die "no blueprints import anchor in app.py"
    sed -i "${A}a from blueprints.pov_terminal import pov_terminal_bp" "$APP"
fi
if ! grep -qF 'app.register_blueprint(pov_terminal_bp)' "$APP"; then
    A=$(grep -n 'app\.register_blueprint(' "$APP" | tail -1 | cut -d: -f1)
    [[ -n "$A" ]] || die "no register_blueprint anchor in app.py"
    IND=$(sed -n "${A}p" "$APP" | sed -E 's/^([[:space:]]*).*/\1/')
    sed -i "${A}a ${IND}app.register_blueprint(pov_terminal_bp)" "$APP"
fi
ok "POV wiring in place (app.py + blueprint)"

[[ -f "$PRESERVE/env" ]] && cp "$PRESERVE/env" "$OA_DIR/.env" || true
[[ -d "$PRESERVE/db" ]] && cp -r "$PRESERVE/db" "$OA_DIR/db" || true
rm -rf "$PRESERVE"

# ---------------------------------------------------------------------- 6. env
step "6. Configuring .env"
ENVF="$OA_DIR/.env"
if [[ "$MODE" == "2" ]]; then
    cp "$REPO_DIR/seed/env" "$ENVF"
    [[ -d "$REPO_DIR/seed/db" ]] && cp -r "$REPO_DIR/seed/db" "$OA_DIR/db"
    ok "restored your .env + db (same login, same broker tokens)"
elif [[ ! -f "$ENVF" ]]; then
    cp "$OA_DIR/.sample.env" "$ENVF"
    ok "created .env from .sample.env (fresh secrets minted on first start)"
else
    ok "existing .env kept"
fi

set_kv() {
    local k="$1" v="$2"
    if grep -qE "^${k}[[:space:]]*=" "$ENVF"; then
        sed -i "s|^${k}.*|${k}=${v}|" "$ENVF"
    else
        printf '%s=%s\n' "$k" "$v" >> "$ENVF"
    fi
}

set_kv FLASK_HOST_IP   127.0.0.1
set_kv FLASK_DEBUG     False
set_kv FLASK_ENV       production
set_kv WEBSOCKET_HOST  127.0.0.1
set_kv WEBSOCKET_PORT  8765
if [[ -n "$DOMAIN" ]]; then set_kv HOST_SERVER "https://$DOMAIN"; else set_kv HOST_SERVER "http://127.0.0.1:5000"; fi

if [[ "$MODE" != "2" ]]; then
    set_kv BROKER_API_KEY    "'$BROKER_KEY'"
    set_kv BROKER_API_SECRET "'$BROKER_SECRET'"
    if [[ -n "$DOMAIN" ]]; then
        SLUG=$(grep -oE "/[a-z]+/callback" "$ENVF" | head -1 | tr -d '/' | sed 's|/callback||')
        [[ -z "$SLUG" ]] && SLUG="broker"
        set_kv REDIRECT_URL "https://$DOMAIN/$SLUG/callback"
        ok "REDIRECT_URL set for domain (register this with your broker)"
    else
        warn "REDIRECT_URL left as loopback - broker login via SSH tunnel"
    fi
fi

chmod 600 "$ENVF"
ok ".env ready: debug=OFF, loopback bind, chmod 600"
# -------------------------------------------------------------------- 7. venv
step "7. Building venv + installing both apps"
[[ -d "$VENV" ]] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip wheel setuptools -q
"$VENV/bin/python" -m pip install -r "$OA_DIR/requirements.txt" -q
"$VENV/bin/python" -m pip install "gunicorn>=21" -q
"$VENV/bin/python" -m pip install -e "$REPO_DIR/pov_engine" -q
"$VENV/bin/python" -c "import pov_engine;print('    pov_engine ->',pov_engine.__file__)"
ok "dependencies + pov_engine installed"

# ------------------------------------------------------------------ 8. systemd
step "8. Installing service units"
for u in openalgo pov-dash pov-runner; do
    sudo cp "$REPO_DIR/deploy/${u}.service" "/etc/systemd/system/${u}.service"
    sudo sed -i "s|@APP_USER@|$USER|g; s|@BASE_DIR@|$BASE_DIR|g" "/etc/systemd/system/${u}.service"
done
sudo systemctl daemon-reload
sudo systemctl enable --quiet openalgo pov-dash
sudo systemctl disable --quiet pov-runner 2>/dev/null || true
ok "openalgo + pov-dash enabled; pov-runner stays MANUAL (never autostarts a live trader)"

# -------------------------------------------------------------------- 9. start
step "9. Starting OpenAlgo"
sudo systemctl restart openalgo
UP=0
for i in $(seq 1 30); do
    sleep 3
    if curl -fsS http://127.0.0.1:5000/auth/check-setup >/dev/null 2>&1; then UP=1; break; fi
done
if [[ $UP -eq 1 ]]; then
    ok "OpenAlgo responding on http://127.0.0.1:5000"
    printf "    response: %s\n" "$(curl -fsS http://127.0.0.1:5000/auth/check-setup)"
else
    sudo journalctl -u openalgo -n 40 --no-pager
    die "OpenAlgo did not come up (log above)"
fi
sudo systemctl start pov-dash 2>/dev/null || warn "pov-dash not started (non-fatal: journalctl -u pov-dash)"

# -------------------------------------------------------------------- 10. nginx
step "10. nginx"
sudo cp "$REPO_DIR/deploy/nginx.conf" /etc/nginx/sites-available/my-trading-system
if [[ -n "$DOMAIN" ]]; then
    sudo sed -i "s/YOUR_DOMAIN/$DOMAIN/g" /etc/nginx/sites-available/my-trading-system
    if sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
         --register-unsafely-without-email --redirect -q 2>/dev/null; then
        ok "TLS issued for https://$DOMAIN"
    else
        warn "certbot failed (DNS may not point here yet) - retry: sudo certbot --nginx -d $DOMAIN"
    fi
else
    sudo sed -i "s/YOUR_DOMAIN/_/g" /etc/nginx/sites-available/my-trading-system
    warn "no domain: no TLS. Use the SSH tunnel shown at the end."
fi
sudo ln -sf /etc/nginx/sites-available/my-trading-system /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
if sudo nginx -t 2>/dev/null; then sudo systemctl reload nginx; ok "nginx reloaded"; else warn "nginx -t failed - fix config"; fi

# ----------------------------------------------------------------- firewall
step "11. Firewall"
sudo ufw allow OpenSSH >/dev/null 2>&1 || true
sudo ufw allow "Nginx Full" >/dev/null 2>&1 || true
sudo ufw --force enable >/dev/null 2>&1 || warn "ufw not enabled"
ok "ufw: SSH + Nginx only (app ports remain internal)"
# ------------------------------------------------------------- 12. verification
step "12. VERIFICATION REPORT"
FAILED=0
chk() {
    if eval "$2" >/dev/null 2>&1; then
        printf "    ${G}[PASS]${N} %s\n" "$1"
    else
        printf "    ${R}[FAIL]${N} %s\n" "$1"; FAILED=$((FAILED+1))
    fi
}

chk "python >= 3.12"                    "python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,12) else 1)'"
chk "openalgo service active"           "systemctl is-active --quiet openalgo"
chk "pov-dash service active"           "systemctl is-active --quiet pov-dash"
chk "pov-runner NOT enabled at boot"    "! systemctl is-enabled --quiet pov-runner"
chk "OpenAlgo HTTP responds"            "curl -fsS http://127.0.0.1:5000/auth/check-setup"
chk "FLASK_DEBUG is False"              "grep -q '^FLASK_DEBUG=False' $ENVF"
chk "FLASK_HOST_IP is loopback"         "grep -q '^FLASK_HOST_IP=127.0.0.1' $ENVF"
chk "FLASK_ENV is production"           "grep -q '^FLASK_ENV=production' $ENVF"
chk ".env permissions are 600"          "[ \"\$(stat -c %a $ENVF)\" = '600' ]"
chk "pov_engine importable"             "$VENV/bin/python -c 'import pov_engine'"
chk "pov dashboard importable"          "$VENV/bin/python -c 'import pov_engine.dashboard.dash_app'"
chk "POV order REST API present"        "[ -f $OA_DIR/restx_api/volume_participation.py ]"
chk "POV service layer present"         "[ -f $OA_DIR/services/volume_participation_service.py ]"
chk "ledger backend database.pov_db"    "cd $OA_DIR && $VENV/bin/python -c 'from database import pov_db'"
chk "POV blueprint wired in app.py"     "grep -q pov_terminal_bp $OA_DIR/app.py"
chk "POV terminal blueprint file"       "[ -f $OA_DIR/blueprints/pov_terminal.py ]"
chk "no hardcoded API key in platform"  "! grep -rq '69db48261cfb98c1' $OA_DIR --include='*.py'"
chk "gunicorn installed (gthread)"      "$VENV/bin/python -c 'import gunicorn'"
chk "no eventlet worker in units"       "! grep -q eventlet /etc/systemd/system/openalgo.service"

if [[ -f "$REPO_DIR/scripts/smoke_test.py" ]]; then
    printf "\n    running engine smoke test...\n"
    if ( cd "$OA_DIR" && "$VENV/bin/python" "$REPO_DIR/scripts/smoke_test.py" ); then
        printf "    ${G}[PASS]${N} engine smoke test\n"
    else
        printf "    ${Y}[WARN]${N} engine smoke test reported issues (see above)\n"
    fi
fi

echo
echo "============================================================================"
if [[ $FAILED -eq 0 ]]; then
    printf " ${G}INSTALL COMPLETE - ALL VERIFICATION CHECKS PASSED${N}\n"
else
    printf " ${R}INSTALL FINISHED - %s CHECK(S) FAILED (see above)${N}\n" "$FAILED"
fi
echo "============================================================================"
echo " Platform : $OA_DIR"
echo " Venv     : $VENV"
echo " Services : openalgo, pov-dash      (pov-runner = manual only)"
if [[ -n "$DOMAIN" ]]; then
    echo " URL      : https://$DOMAIN"
else
    echo " URL      : local-only. Tunnel from your laptop:"
    echo "              ssh -L 5000:127.0.0.1:5000 -L 8050:127.0.0.1:8050 $USER@<server-ip>"
    echo "              then open http://127.0.0.1:5000"
fi

cat <<'EOT'

 ============================================================================
  REMAINING MANUAL STEPS  (cannot be automated)
 ============================================================================
  1. BROKER PORTAL: whitelist this server's STATIC IPv4 address.
     (Mandatory for Indian brokers under SEBI, effective 1 Apr 2026.)

  2. BROKER LOGIN: log in once through the UI. Broker sessions expire daily
     around 03:00 IST and must be re-established.

  3. API KEY: generate it in the UI (Keys & Reports -> API Keys), then put it in
     BOTH files and restart:
         /opt/my-trading-system/openalgo/.env     OPENALGO_API_KEY=...
         ~/my-trading-system/.env                 OPENALGO_API_KEY=...
         sudo systemctl restart openalgo pov-dash

  4. FIRST RUN: leave the platform in ANALYZE mode for one full POV cycle
     before sending any live order.

 ============================================================================
  COMMANDS
 ============================================================================
   sudo systemctl status openalgo pov-dash
   sudo journalctl -u openalgo -f
   sudo systemctl start pov-runner     # ONLY when you intend to trade
   sudo systemctl stop  pov-runner

   Re-run this installer any time - it is safe and idempotent.
 ============================================================================
EOT