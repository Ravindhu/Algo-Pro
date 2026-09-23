#!/usr/bin/env bash
# ============================================================================
#  add_pov_engine.sh -- plug the POV engine into an OpenAlgo install.
#
#  RUN THIS **AFTER** the official installer:
#      mkdir -p ~/openalgo-install && cd ~/openalgo-install
#      wget https://raw.githubusercontent.com/marketcalls/openalgo/main/install/install.sh
#      chmod +x install.sh && sudo ./install.sh      # <-- does packages, uv,
#                                                    #     nginx, TLS, systemd, .env
#      cd ~/my-trading-system
#      sudo ./deploy/add_pov_engine.sh               # <-- THIS FILE (only extra step)
#
#  What it adds on top of the official install:
#    1. detects the official layout (/var/python/openalgo)
#    2. applies your 65-file custom POV platform patch (POV order API, pov_db,
#       OCL screener, frontend pages) - with 3-way fallback and hard failure
#    3. installs the pov_terminal blueprint + app.py wiring (idempotent)
#    4. installs pov_engine into OpenAlgo's OWN venv (uv/pip editable)
#    5. adds the engine env vars to OpenAlgo's .env (append-only, never
#       overwrites your secrets)
#    6. creates pov-dash.service (autostart) + pov-runner.service (MANUAL only)
#    7. restarts OpenAlgo and runs a verification report
#
#  Re-run after every `sudo ./update.sh` (their updater runs git pull and does
#  NOT re-apply your custom patch).
# ============================================================================
set -euo pipefail

B="\033[1;36m"; G="\033[1;32m"; Y="\033[1;33m"; R="\033[1;31m"; N="\033[0m"
step() { printf "\n${B}==> %s${N}\n" "$*"; }
ok()   { printf "    ${G}[OK] %s${N}\n" "$*"; }
warn() { printf "    ${Y}[WARN] %s${N}\n" "$*"; }
die()  { printf "    ${R}[FAIL] %s${N}\n" "$*" >&2; exit 1; }
ask()  { local p="$1" d="${2:-}" v; read -r -p "    $p${d:+ [$d]}: " v; printf '%s' "${v:-$d}"; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH="$REPO_DIR/patches/openalgo-custom.patch"
BLUEPRINT="$REPO_DIR/patches/pov_terminal.py"
ENGINE_SRC="$REPO_DIR/pov_engine"
BASELINE_FILE="$REPO_DIR/deploy/.baseline"
DEFAULT_BASELINE="5390dcd898ed597b85477068e61e28813824c819"

# ---------------------------------------------------------------- privileges
if [[ $EUID -ne 0 ]]; then
    die "Run with sudo (the official install lives under /var/python and is owned
    by the service user):   sudo ./deploy/add_pov_engine.sh"
fi

# ------------------------------------------------------------ 1. find install
step "1. Locating the OpenAlgo installation"
CANDIDATES=(
    "/var/python/openalgo"
    "/var/python/openalgo-flask/openalgo"
    "/opt/openalgo"
)
OA_DIR=""
for c in "${CANDIDATES[@]}"; do
    if [[ -f "$c/app.py" && -d "$c/.venv" ]]; then OA_DIR="$c"; break; fi
done
if [[ -z "$OA_DIR" ]]; then
    # discover a systemd unit and read its WorkingDirectory
    for unit in $(systemctl list-unit-files 'openalgo*.service' --no-legend 2>/dev/null | awk '{print $1}'); do
        WD=$(systemctl show -p WorkingDirectory --value "$unit" 2>/dev/null || true)
        if [[ -n "$WD" && -f "$WD/app.py" ]]; then OA_DIR="$WD"; break; fi
    done
fi
[[ -n "$OA_DIR" ]] || die "OpenAlgo install not found. Run the official install.sh first."
OA_DIR="$(cd "$OA_DIR" && pwd)"
printf "    Found: %s\n" "$OA_DIR"

# service user owns the deployment (www-data on Ubuntu/Debian)
SVC_USER=$(stat -c '%U' "$OA_DIR")
[[ "$SVC_USER" == "root" ]] && SVC_USER="www-data"
printf "    Service user: %s\n" "$SVC_USER"

VENV="$OA_DIR/.venv"
PY="$VENV/bin/python"
[[ -x "$PY" ]] || die "no python in $VENV - is this a valid OpenAlgo install?"
ok "layout detected"
# ------------------------------------------------------------- 2. patch check
step "2. Recording upstream state before patching"
[[ -f "$PATCH" ]] || die "patch missing: $PATCH"
BASELINE=$(cat "$BASELINE_FILE" 2>/dev/null || echo "$DEFAULT_BASELINE")
HEAD_NOW=$(git -C "$OA_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")
printf "    upstream HEAD : %s\n" "$HEAD_NOW"
printf "    patch baseline: %.8s\n" "$BASELINE"
if [[ "$HEAD_NOW" != "${BASELINE:0:7}" && "$HEAD_NOW" != "${BASELINE:0:8}" ]]; then
    warn "HEAD differs from the patch baseline."
    warn "The patch was cut against $(printf '%.8s' "$BASELINE")."
    warn "It will be tried with a 3-way merge; if that fails you must re-cut it"
    warn "on your dev box with: ./deploy/rebuild_patch.sh"
fi

# already patched?
if [[ -f "$OA_DIR/restx_api/volume_participation.py" ]]; then
    ALREADY=1
    ok "POV platform files already present (re-run detected)"
else
    ALREADY=0
fi

# ---------------------------------------------------------------- 3. apply
step "3. Applying the custom POV platform patch"
if [[ "$ALREADY" == "1" ]]; then
    warn "skipping patch (already applied). Use --force-patch to re-apply."
    if [[ "${1:-}" == "--force-patch" ]]; then
        warn "forcing re-apply"
        ALREADY=0
    fi
fi

if [[ "$ALREADY" == "0" ]]; then
    # work on the repo as its owner to avoid git "dubious ownership" errors
    git config --global --add safe.directory "$OA_DIR" 2>/dev/null || true

    if git -C "$OA_DIR" apply --check --whitespace=nowarn "$PATCH" 2>/dev/null; then
        git -C "$OA_DIR" apply --whitespace=nowarn "$PATCH"
        ok "patch applied CLEANLY ($(grep -c '^diff --git' "$PATCH") files)"
    elif git -C "$OA_DIR" apply --3way --whitespace=nowarn "$PATCH" 2>/dev/null; then
        ok "patch applied via 3-way merge"
        if grep -rl '^<<<<<<<' "$OA_DIR" --include='*.py' 2>/dev/null | head -5; then
            die "CONFLICT MARKERS FOUND - resolve them before starting OpenAlgo"
        fi
    else
        warn "patch did not apply. Diagnosing which file failed:"
        git -C "$OA_DIR" apply --check --verbose --whitespace=nowarn "$PATCH" 2>&1 | grep -iE 'error|does not apply' | head -10 || true
        die "PATCH FAILED - do NOT start the platform. Re-cut it on your dev box:
        1. on the laptop:  ./deploy/rebuild_patch.sh
        2. commit + copy patches/openalgo-custom.patch to the server
        3. re-run this script"
    fi
fi

# ------------------------------------------------------------ 4. blueprint
step "4. Installing the POV terminal blueprint + app.py wiring"
[[ -f "$BLUEPRINT" ]] || die "missing $BLUEPRINT"
cp "$BLUEPRINT" "$OA_DIR/blueprints/pov_terminal.py"
ok "blueprints/pov_terminal.py installed"

APP="$OA_DIR/app.py"
IMP='from blueprints.pov_terminal import pov_terminal_bp'
REG='app.register_blueprint(pov_terminal_bp)'

if grep -qF "$IMP" "$APP"; then
    ok "app.py import already present"
else
    A=$(grep -n '^from blueprints\.' "$APP" | tail -1 | cut -d: -f1)
    [[ -n "$A" ]] || die "cannot find a blueprints import anchor in app.py (upstream changed shape)"
    sed -i "${A}a ${IMP}" "$APP"
    ok "app.py: import injected after line $A"
fi

if grep -qF "$REG" "$APP"; then
    ok "app.py registration already present"
else
    A=$(grep -n 'app\.register_blueprint(' "$APP" | tail -1 | cut -d: -f1)
    [[ -n "$A" ]] || die "cannot find register_blueprint anchor in app.py"
    IND=$(sed -n "${A}p" "$APP" | sed -E 's/^([[:space:]]*).*/\1/')
    sed -i "${A}a ${IND}${REG}" "$APP"
    ok "app.py: register_blueprint injected after line $A (indent preserved)"
fi

# syntax gate - never leave a broken app.py behind
"$PY" -c "import ast,sys; ast.parse(open('$APP',encoding='utf-8').read())" \
  || die "app.py is not valid Python after patching - restore it from git and re-run"
ok "app.py parses cleanly"
# --------------------------------------------------------------- 5. engine
step "5. Installing pov_engine into OpenAlgo's own venv"
[[ -d "$ENGINE_SRC" ]] || die "pov_engine source not found at $ENGINE_SRC"
"$PY" -m pip install --quiet -e "$ENGINE_SRC" \
  || die "pov_engine install failed"
"$PY" -c "import pov_engine; print('    pov_engine ->', pov_engine.__file__)"
# dashboard extras (dash/plotly/pandas) - needed by pov-dash.service
"$PY" -m pip install --quiet dash plotly pandas numpy 2>/dev/null || warn "dashboard extras install had issues"
"$PY" -c "import pov_engine.dashboard.dash_app" 2>/dev/null \
  && ok "engine + dashboard importable" \
  || warn "dashboard import failed - pov-dash may not start (engine still fine)"

# ------------------------------------------------------------------ 6. env
step "6. Wiring the engine's env vars into OpenAlgo's .env"
ENVF="$OA_DIR/.env"
[[ -f "$ENVF" ]] || die "no .env at $ENVF - did the official install finish?"
cp "$ENVF" "$ENVF.bak_$(date +%Y%m%d_%H%M%S)"
ok "backed up .env before touching it"

# NEVER overwrite existing keys - only add what is missing (your secrets stay).
add_if_missing() {
    local k="$1" v="$2"
    if grep -qE "^${k}[[:space:]]*=" "$ENVF"; then
        printf "    [keep] %s already set\n" "$k"
    else
        printf '%s=%s\n' "$k" "$v" >> "$ENVF"
        printf "    [add ] %s=%s\n" "$k" "$v"
    fi
}
# The engine talks to the platform over loopback.
add_if_missing POV_DATA_DIR     "/var/lib/pov-engine"
add_if_missing POV_DASH_HOST    "127.0.0.1"
add_if_missing POV_DASH_PORT    "8050"

# report the values the engine needs (already set by the official installer)
for k in HOST_SERVER WEBSOCKET_URL OPENALGO_API_KEY FLASK_HOST_IP FLASK_DEBUG; do
    line=$(grep -E "^${k}[[:space:]]*=" "$ENVF" | tail -1 || true)
    if [[ -n "$line" ]]; then
        case "$k" in
            OPENALGO_API_KEY) printf "    [seen] %s = <set>\n" "$k" ;;
            *)                printf "    [seen] %s\n" "$line" ;;
        esac
    else
        warn "$k not found in .env"
    fi
done

# engine state dir
mkdir -p /var/lib/pov-engine
chown "$SVC_USER:$SVC_USER" /var/lib/pov-engine
ok "engine state dir: /var/lib/pov-engine (owned by $SVC_USER)"

# --------------------------------------------------------------- 7. services
step "7. Creating pov-dash + pov-runner services"
create_unit() {
    local name="$1" desc="$2" cmd="$3" restart="$4" enable="$5"
    cat > "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=${desc}
After=network-online.target openalgo.service
Wants=network-online.target openalgo.service

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${OA_DIR}
EnvironmentFile=-${ENVF}
Environment=TZ=Asia/Kolkata
Environment=POV_DATA_DIR=/var/lib/pov-engine
ExecStart=${cmd}
Restart=${restart}
RestartSec=10
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${name}
EOF
    if [[ "$enable" == "yes" ]]; then
        cat >> "/etc/systemd/system/${name}.service" <<'EOF'

[Install]
WantedBy=multi-user.target
EOF
    fi
    printf "    wrote /etc/systemd/system/%s.service\n" "$name"
}

create_unit "pov-dash" \
    "POV engine Dash terminal (read-only dashboard)" \
    "${PY} -m pov_engine.dashboard.dash_app" \
    "always" "yes"

# pov-runner sends LIVE ORDERS: never autostart, never auto-restart.
create_unit "pov-runner" \
    "POV execution engine (LIVE ORDER SENDER - manual start only)" \
    "${PY} -m pov_engine.runner ${ENGINE_SRC}/input/master.json" \
    "no" "no"

systemctl daemon-reload
systemctl enable --quiet pov-dash
systemctl disable --quiet pov-runner 2>/dev/null || true
ok "pov-dash enabled; pov-runner DISABLED (must be started manually to trade)"
# ------------------------------------------------------------------ 8. restart
step "8. Restarting OpenAlgo to pick up the patch"
systemctl restart openalgo

# --- health probe -----------------------------------------------------------
# The official install.sh serves the app over a GUNICORN UNIX SOCKET
# (/var/python/openalgo/openalgo.sock) - NOT tcp 127.0.0.1:5000. Older or DIY
# installs use TCP. Probe whichever this deployment actually provides.
SOCK="$OA_DIR/openalgo.sock"
HEALTH_PATH="/auth/check-setup"

probe() {
    if [[ -S "$SOCK" ]]; then
        curl -fsS --unix-socket "$SOCK" "http://localhost${HEALTH_PATH}" >/dev/null 2>&1 && return 0
    fi
    curl -fsS "http://127.0.0.1:5000${HEALTH_PATH}" >/dev/null 2>&1 && return 0
    if [[ -n "${DOMAIN:-}" ]]; then
        curl -fsS "https://${DOMAIN}${HEALTH_PATH}" >/dev/null 2>&1 && return 0
    fi
    return 1
}

UP=0
for i in $(seq 1 30); do
    sleep 3
    if probe; then UP=1; break; fi
done
if [[ $UP -eq 1 ]]; then
    if [[ -S "$SOCK" ]]; then
        ok "OpenAlgo responding (gunicorn unix socket: $SOCK)"
        printf "    %s\n" "$(curl -fsS --unix-socket "$SOCK" "http://localhost${HEALTH_PATH}" || true)"
    else
        ok "OpenAlgo responding (tcp 127.0.0.1:5000)"
        printf "    %s\n" "$(curl -fsS "http://127.0.0.1:5000${HEALTH_PATH}" || true)"
    fi
else
    printf "    ${R}OpenAlgo did not come up. Last 40 log lines:${N}\n"
    journalctl -u openalgo -n 40 --no-pager || true
    die "Roll back with:  cd $OA_DIR && sudo git checkout -- app.py && sudo systemctl restart openalgo
    (your .env and db were NOT modified by this script except added keys)"
fi

systemctl restart pov-dash 2>/dev/null || warn "pov-dash not started (check: journalctl -u pov-dash)"

# ------------------------------------------------------------ 9. verification
step "9. VERIFICATION REPORT"
FAILED=0
chk() {
    if eval "$2" >/dev/null 2>&1; then printf "    ${G}[PASS]${N} %s\n" "$1"
    else printf "    ${R}[FAIL]${N} %s\n" "$1"; FAILED=$((FAILED+1)); fi
}

chk "openalgo service active"            "systemctl is-active --quiet openalgo"
chk "pov-dash service active"            "systemctl is-active --quiet pov-dash"
chk "pov-runner NOT enabled at boot"     "! systemctl is-enabled --quiet pov-runner"
chk "OpenAlgo health probe responds"      "probe"
chk "POV order REST API present"         "[ -f $OA_DIR/restx_api/volume_participation.py ]"
chk "POV service layer present"          "[ -f $OA_DIR/services/volume_participation_service.py ]"
chk "ledger backend database.pov_db"     "cd $OA_DIR && sudo -u $SVC_USER $PY -c 'from database import pov_db'"
chk "POV blueprint wired in app.py"      "grep -q pov_terminal_bp $OA_DIR/app.py"
chk "POV terminal blueprint file"        "[ -f $OA_DIR/blueprints/pov_terminal.py ]"
chk "app.py is valid Python"             "$PY -c 'import ast;ast.parse(open(\"$APP\",encoding=\"utf-8\").read())'"
chk "pov_engine importable"              "$PY -c 'import pov_engine'"
chk "pov dashboard importable"           "$PY -c 'import pov_engine.dashboard.dash_app'"
chk "engine state dir exists"            "[ -d /var/lib/pov-engine ]"
chk "no hardcoded API key in platform"   "! grep -rq '69db48261cfb98c1' $OA_DIR --include='*.py'"
chk "OPENALGO_API_KEY in .env"           "grep -qE '^OPENALGO_API_KEY=.+' $ENVF"

echo
echo "============================================================================"
if [[ $FAILED -eq 0 ]]; then
    printf " ${G}POV ENGINE PLUGGED IN - ALL CHECKS PASSED${N}\n"
else
    printf " ${Y}COMPLETED WITH %s FAILED CHECK(S) (see above)${N}\n" "$FAILED"
fi
echo "============================================================================"
echo " Platform : $OA_DIR"
echo " Venv     : $VENV   (pov_engine installed here)"
echo " Env file : $ENVF   (backup: *.bak_*)"
echo " State    : /var/lib/pov-engine"
echo " Services : openalgo, pov-dash      (pov-runner = manual only)"
echo
echo " NEXT STEPS"
echo "  1. Open your dashboard and log in to the broker once."
echo "  2. Generate an API key (Keys & Reports -> API Keys) and add it to .env:"
echo "       sudo nano $ENVF        # OPENALGO_API_KEY=..."
echo "       sudo systemctl restart openalgo pov-dash"
echo "  3. Tune the engine config:"
echo "       sudo nano $ENGINE_SRC/input/master.json"
echo "  4. Run the engine in ANALYZE mode for one full cycle before going live:"
echo "       sudo systemctl start pov-runner && sudo journalctl -fu pov-runner"
echo
echo " IMPORTANT - after any platform update:"
echo "   cd ~/openalgo-install && sudo ./update.sh   # their updater: git pull + migrate"
echo "   cd ~/my-trading-system && sudo ./deploy/add_pov_engine.sh --force-patch"
echo "   ^ their update does NOT re-apply your custom POV patch. Always re-run this."
echo "============================================================================"