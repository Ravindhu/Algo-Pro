#!/usr/bin/env bash
# ============================================================================
#  pov_activate.sh -- FINAL STEP: link the POV engine to your OpenAlgo account.
#
#  Run this AFTER you have logged into the OpenAlgo UI with your broker OTP
#  and generated an OpenAlgo API key.
#
#  Usage:
#      cd ~/my-trading-system
#      sudo ./deploy/pov_activate.sh
#
#  It asks you ONE question (paste your OpenAlgo API key), then:
#    1. verifies the key actually works against the running platform
#    2. writes it to BOTH .env files (platform + engine), append-only/safe
#    3. configures the engine's HOST_SERVER / WEBSOCKET_URL
#    4. restarts openalgo + pov-dash
#    5. checks the broker session is live
#    6. optionally starts the engine (asks yes/no, defaults to NO)
#
#  Re-runnable: paste a new key any time (e.g. after rotating/regenerating).
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
ENGINE_ENV="$REPO_DIR/.env"          # what the POV engine reads
BASELINE_FILE="$REPO_DIR/deploy/.baseline"

[[ $EUID -ne 0 ]] && die "Run with sudo:  sudo ./deploy/pov_activate.sh"

# ------------------------------------------------- 1. locate the platform
step "1. Locating the OpenAlgo installation"
OA_DIR=""
for c in /var/python/openalgo /var/python/openalgo-flask/openalgo /opt/openalgo; do
    [[ -f "$c/app.py" && -d "$c/.venv" ]] && { OA_DIR="$c"; break; }
done
if [[ -z "$OA_DIR" ]]; then
    for unit in $(systemctl list-unit-files 'openalgo*.service' --no-legend 2>/dev/null | awk '{print $1}'); do
        WD=$(systemctl show -p WorkingDirectory --value "$unit" 2>/dev/null || true)
        [[ -n "$WD" && -f "$WD/app.py" ]] && { OA_DIR="$WD"; break; }
    done
fi
[[ -n "$OA_DIR" ]] || die "OpenAlgo install not found. Run the official install.sh + add_pov_engine.sh first."
OA_DIR="$(cd "$OA_DIR" && pwd)"
PLATFORM_ENV="$OA_DIR/.env"
PY="$OA_DIR/.venv/bin/python"
SVC_USER=$(stat -c '%U' "$OA_DIR"); [[ "$SVC_USER" == "root" ]] && SVC_USER="www-data"
printf "    platform : %s\n" "$OA_DIR"
printf "    service  : %s\n" "$SVC_USER"
ok "found"

# ------------------------------------------------- 2. detect transport
step "2. Detecting how the platform is served"
SOCK="$OA_DIR/openalgo.sock"
if [[ -S "$SOCK" ]]; then
    TRANSPORT="socket"; HOST_FOR_ENGINE="http://127.0.0.1:5000"
    ok "gunicorn unix socket: $SOCK"
else
    TRANSPORT="tcp"
    ok "tcp 127.0.0.1:5000"
fi

probe() {
    if [[ -S "$SOCK" ]]; then
        curl -fsS --unix-socket "$SOCK" http://localhost/auth/check-setup >/dev/null 2>&1 && return 0
    fi
    curl -fsS http://127.0.0.1:5000/auth/check-setup >/dev/null 2>&1 && return 0
    return 1
}
probe || die "OpenAlgo is not responding. Start it first:  sudo systemctl start openalgo"
ok "platform is up"
# ------------------------------------------------- 3. ask for the API key
step "3. OpenAlgo API key"
cat <<'EOT'
    Generate it in the UI:
      Keys & Reports  ->  API Keys  ->  generate / copy
    (You must be logged in with your broker OTP first.)
EOT
printf "\n"

API_KEY=""
for attempt in 1 2 3; do
    API_KEY=$(asksecret "Paste your OpenAlgo API key (input hidden)")
    API_KEY="$(printf '%s' "$API_KEY" | tr -d '[:space:]')"
    if [[ -z "$API_KEY" ]]; then
        warn "empty - try again ($attempt/3)"
        continue
    fi
    if [[ ${#API_KEY} -lt 16 ]]; then
        warn "that looks too short (${#API_KEY} chars) - OpenAlgo keys are ~64 chars ($attempt/3)"
    fi

    # ---- verify it really works before writing anything ----
    printf "    verifying against the live platform...\n"
    VERIFY=$(
        cd "$OA_DIR" && sudo -u "$SVC_USER" "$PY" - "$API_KEY" <<'PYEOF' 2>&1
import os, sys
key = sys.argv[1]
os.environ["OPENALGO_API_KEY"] = key
try:
    from openalgo import api
    c = api(api_key=key, host="http://127.0.0.1:5000")
    r = c.analyzerstatus()
    status = r.get("status") if isinstance(r, dict) else None
    if status == "success":
        print("VALID")
    elif status == "error":
        print("INVALID:" + str(r.get("message", r))[:160])
    else:
        print("INVALID:unexpected response: " + str(r)[:160])
except Exception as e:
    print("INVALID:" + type(e).__name__ + ": " + str(e)[:160])
PYEOF
    )

    case "$VERIFY" in
        VALID*)
            ok "API key is VALID (verified with analyzerstatus)"
            break
            ;;
        INVALID:*)
            warn "key rejected -> ${VERIFY#INVALID:}"
            if [[ $attempt -lt 3 ]]; then
                warn "check the key, or that you are logged into the broker in the UI"
                API_KEY=""
                continue
            else
                die "3 attempts failed. Nothing was changed. Re-run when you have a working key."
            fi
            ;;
        *)
            warn "could not verify (${VERIFY}) - will save anyway after confirmation"
            A=$(ask "Save this key unverified? (yes/no)" "no")
            [[ "${A,,}" == "yes" ]] || die "aborted by user - nothing changed"
            break
            ;;
    esac
done

# ------------------------------------------------- 4. write it everywhere
step "4. Writing the key to both .env files"

set_kv_file() {   # set_kv_file <file> <key> <value>
    local f="$1" k="$2" v="$3"
    [[ -f "$f" ]] || { printf '%s=%s\n' "$k" "$v" > "$f"; return; }
    if grep -qE "^${k}[[:space:]]*=" "$f"; then
        sed -i "s|^${k}.*|${k}=${v}|" "$f"
    else
        printf '%s=%s\n' "$k" "$v" >> "$f"
    fi
}

# platform .env
cp "$PLATFORM_ENV" "$PLATFORM_ENV.bak_$(date +%Y%m%d_%H%M%S)"
set_kv_file "$PLATFORM_ENV" OPENALGO_API_KEY "$API_KEY"
ok "written to $PLATFORM_ENV  (backup saved)"

# engine .env (what pov_engine reads)
[[ -f "$ENGINE_ENV" ]] || cp "$REPO_DIR/.env.example" "$ENGINE_ENV" 2>/dev/null || true
cp "$ENGINE_ENV" "$ENGINE_ENV.bak_$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
set_kv_file "$ENGINE_ENV" OPENALGO_API_KEY "$API_KEY"
set_kv_file "$ENGINE_ENV" HOST_SERVER       "http://127.0.0.1:5000"
set_kv_file "$ENGINE_ENV" WEBSOCKET_URL     "ws://127.0.0.1:8765"
ok "written to $ENGINE_ENV  (+ HOST_SERVER, WEBSOCKET_URL)"

chmod 600 "$PLATFORM_ENV" "$ENGINE_ENV" 2>/dev/null || true
chown "$SVC_USER:$SVC_USER" "$PLATFORM_ENV" 2>/dev/null || true
ok "permissions tightened (600)"
# ------------------------------------------------- 5. restart services
step "5. Restarting services"
systemctl restart openalgo
for i in $(seq 1 20); do sleep 3; probe && break; [[ $i -eq 20 ]] && { journalctl -u openalgo -n 30 --no-pager; die "OpenAlgo did not come back after restart"; }; done
ok "openalgo restarted"
systemctl restart pov-dash 2>/dev/null && ok "pov-dash restarted" || warn "pov-dash not running (check: journalctl -u pov-dash)"

# ------------------------------------------------- 6. broker session check
step "6. Broker session check"
BROKER=$(
    cd "$OA_DIR" && sudo -u "$SVC_USER" "$PY" - "$API_KEY" <<'PYEOF' 2>&1
import os, sys
os.environ["OPENALGO_API_KEY"] = sys.argv[1]
try:
    from openalgo import api
    c = api(api_key=sys.argv[1], host="http://127.0.0.1:5000")
    r = c.funds()
    if isinstance(r, dict) and r.get("status") == "success":
        d = r.get("data") or {}
        avail = d.get("availablecash") or d.get("available_balance") or "?"
        print("LIVE:" + str(avail))
    else:
        print("EXPIRED:" + str(r)[:140])
except Exception as e:
    print("EXPIRED:" + type(e).__name__ + ": " + str(e)[:140])
PYEOF
)
case "$BROKER" in
    LIVE:*)
        ok "broker session is LIVE - available cash: ${BROKER#LIVE:}"
        ;;
    EXPIRED:*)
        warn "broker session not usable yet -> ${BROKER#EXPIRED:}"
        warn "log in to the broker in the UI (OTP). Sessions expire daily ~03:00 IST."
        ;;
    *)
        warn "could not determine broker state: $BROKER"
        ;;
esac

# ------------------------------------------------- 7. verification report
step "7. VERIFICATION REPORT"
FAILED=0
chk() {
    if eval "$2" >/dev/null 2>&1; then printf "    ${G}[PASS]${N} %s\n" "$1"
    else printf "    ${R}[FAIL]${N} %s\n" "$1"; FAILED=$((FAILED+1)); fi
}
export API_KEY
chk "platform responds"                  "probe"
chk "openalgo service active"            "systemctl is-active --quiet openalgo"
chk "pov-dash service active"            "systemctl is-active --quiet pov-dash"
chk "key in platform .env"               "grep -qE '^OPENALGO_API_KEY=.+' $PLATFORM_ENV"
chk "key in engine .env"                 "grep -qE '^OPENALGO_API_KEY=.+' $ENGINE_ENV"
chk "engine .env has HOST_SERVER"        "grep -qE '^HOST_SERVER=' $ENGINE_ENV"
chk "engine .env has WEBSOCKET_URL"      "grep -qE '^WEBSOCKET_URL=' $ENGINE_ENV"
chk ".env files are chmod 600"           "[ \"\$(stat -c %a $PLATFORM_ENV)\" = '600' ] && [ \"\$(stat -c %a $ENGINE_ENV)\" = '600' ]"
chk "SDK call succeeds with the key"     "cd $OA_DIR && sudo -u $SVC_USER $PY -c \"import os;os.environ['OPENALGO_API_KEY']='\$API_KEY';from openalgo import api;c=api(api_key='\$API_KEY',host='http://127.0.0.1:5000');assert c.analyzerstatus().get('status')=='success'\""
chk "pov_engine importable"              "$PY -c 'import pov_engine'"
chk "pov-runner NOT enabled at boot"     "! systemctl is-enabled --quiet pov-runner"

if [[ -f "$REPO_DIR/scripts/smoke_test.py" ]]; then
    printf "\n    running the engine smoke test...\n"
    if ( cd "$OA_DIR" && sudo -u "$SVC_USER" env OPENALGO_API_KEY="$API_KEY" \
         HOST_SERVER="http://127.0.0.1:5000" "$PY" "$REPO_DIR/scripts/smoke_test.py" ); then
        printf "    ${G}[PASS]${N} engine smoke test\n"
    else
        printf "    ${Y}[WARN]${N} engine smoke test reported issues (see above)\n"
    fi
fi

echo
echo "============================================================================"
if [[ $FAILED -eq 0 ]]; then
    printf " ${G}POV ENGINE IS LINKED AND READY${N}\n"
else
    printf " ${Y}LINKED WITH %s FAILED CHECK(S) - review above${N}\n" "$FAILED"
fi
echo "============================================================================"
echo " Platform  : $OA_DIR"
echo " Engine env: $ENGINE_ENV"
echo " Dashboard : systemctl status pov-dash"
echo " Auth mode : broker session ${BROKER%%:*}"

# ------------------------------------------------- 8. optional engine start
echo
echo " The engine is NOT running yet. Sending live orders is always a deliberate act."
echo
echo "   Recommended first run - put the platform in ANALYZE mode, then:"
echo "       sudo systemctl start pov-runner"
echo "       sudo journalctl -fu pov-runner"
echo "       sudo systemctl stop pov-runner     # when the test cycle is done"
echo
START=$(ask "Start the engine now? (yes/no)" "no")
if [[ "${START,,}" == "yes" ]]; then
    warn "starting pov-runner - ensure the platform is in ANALYZE mode for the first cycle"
    systemctl start pov-runner
    sleep 5
    systemctl --no-pager --lines=15 status pov-runner || true
    echo
    printf "    follow the log with:  sudo journalctl -fu pov-runner\n"
    printf "    stop it with:         sudo systemctl stop pov-runner\n"
else
    ok "engine left stopped (start it when ready: sudo systemctl start pov-runner)"
fi

echo
echo "============================================================================"
echo " NEXT"
echo "  1. Whitelist this server's STATIC IP in your broker portal (SEBI rule)."
echo "  2. Tune the strategy:  sudo nano $REPO_DIR/pov_engine/input/master.json"
echo "  3. First cycle in ANALYZE mode, then go live at minimum size."
echo "  4. After any platform update:  sudo ./deploy/add_pov_engine.sh --force-patch"
echo "     (their update.sh does git pull and does NOT re-apply your POV patch)"
echo "============================================================================"