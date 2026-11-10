#!/usr/bin/env bash
# =============================================================================
# preopen_launch.sh - start the desk before the market opens, safely
# =============================================================================
#
# WHAT IT DOES, IN ORDER (every step must pass before the next one)
#   1. Guard the clock ......... nothing starts outside the pre-open window
#   2. Guard the lock .......... a second run cannot start a second engine
#   3. Start the platform ...... openalgo + openalgo-ws (idempotent)
#   4. Wait for the platform ... poll the HTTP port, not "did the process start"
#   5. Broker session .......... scripts/ops/scheduled_login.py (opt-in, TOTP)
#   6. Smoke gate .............. scripts/smoke_test.py must PASS
#   7. Start the engine ........ pov-dash + pov-runner, ONLY after 1-6 pass
#   8. Verify it answers ....... the engine gets a bounded time to listen
#   9. Reconcile ............... write a JSON record of what happened
#
# WHY EVERY STEP IS A GATE
#   An order-placing process that starts because "it was 08:50" is not an
#   automated desk, it is an unattended risk. Each gate fails CLOSED: the
#   default outcome of any error here is "the engine is not running", which is
#   always the safe state. AGENTS.md: Capital Protection before everything.
#
# WHAT IT DOES NOT DO
#   * It does not log in to the broker unless BROKER_AUTOLOGIN=1 (the login
#     script's own opt-in, honoured here - never overridden).
#   * It does not enable pov-runner at boot. Boot auto-start of an execution
#     engine is precisely what install_ubuntu.sh refuses to do.
#   * It does not run on weekends or NSE holidays it knows about (see below).
#
# MARKET CLOCK
#   NSE cash session: 09:15-15:30 IST. The script is scheduled at 08:50 IST.
#   `--force` bypasses the time window (a human decision, for testing).
#   There is no holiday calendar in this file. A holiday run is harmless -
#   the broker/login legs fail and the engine never starts - but it will log a
#   failure, so do not alert on it. Wire the calendar into step 1 later.
#
# USAGE
#   bash scripts/ops/preopen_launch.sh              # scheduled path
#   bash scripts/ops/preopen_launch.sh --check      # prerequisites only
#   bash scripts/ops/preopen_launch.sh --force      # ignore the clock window
#   bash scripts/ops/preopen_launch.sh --dry-run    # decide, start nothing
#   bash scripts/ops/preopen_launch.sh --no-engine  # platform + login only
#
# EXIT CODES
#   0  engine is up (or --check/--dry-run found no blocker)
#   10 outside the pre-open window and --force not given
#   11 another run holds the lock
#   12 platform did not answer
#   13 broker session not ready
#   14 smoke gate failed
#   15 engine did not answer in time
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 12

LOG_DIR="$ROOT/log/preopen"
STATE_DIR="$ROOT/log/preopen"
mkdir -p "$LOG_DIR" "$STATE_DIR"
LOCK_FILE="$STATE_DIR/preopen.lock"
RESULT_JSON="$STATE_DIR/last_preopen.json"
LOG_FILE="$LOG_DIR/$(date +%F).log"

# Ports/venv come from the installer's own defaults; override in deploy.env.
OPENALGO_PORT="${OPENALGO_PORT:-5000}"
DASH_PORT="${DASH_PORT:-8050}"
WS_PORT="${WS_PORT:-8765}"
VENV_PY="${VENV_PY:-$ROOT/openalgo/.venv/bin/python}"
SERVICE_USER="${SERVICE_USER:-algo}"

OPEN_HHMM="0915"
EARLIEST_HHMM="0800"
LATEST_HHMM="0930"          # after this the open has passed; do not start

FORCE=0; CHECK_ONLY=0; DRY_RUN=0; START_ENGINE_WANTED=1; RUNNER_GATED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)      FORCE=1; shift ;;
    --check)      CHECK_ONLY=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --no-engine)  START_ENGINE_WANTED=0; shift ;;
    --runner-gated) RUNNER_GATED=1; shift ;;   # run_preopen_once.py already checked day and time
    -h|--help)    sed -n '2,52p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
done

# ------------------------------------------------------------------ logging
ts() { date '+%Y-%m-%d %H:%M:%S%z'; }
log()  { printf '%s  %-6s %s\n' "$(ts)" "$1" "$2" | tee -a "$LOG_FILE"; }
info() { log INFO  "$*"; }
ok()   { log OK    "$*"; }
warn() { log WARN  "$*"; }
err()  { log ERROR "$*"; }

write_result() {          # write_result <exit_code> <stage> <detail>
  {
    printf '{\n'
    printf '  "at": "%s",\n'            "$(ts)"
    printf '  "exit": %s,\n'            "${1:-0}"
    printf '  "stage": "%s",\n'         "${2:-unknown}"
    printf '  "detail": "%s",\n'        "$(printf '%s' "${3:-}" | sed 's/"/\\"/g')"
    printf '  "host": "%s",\n'          "$(hostname)"
    printf '  "engine_requested": %s\n' "$START_ENGINE_WANTED"
    printf '}\n'
  } > "$RESULT_JSON"
}

# record_only: write the JSON record without tripping `set -e` in the caller.
# Defined BEFORE the clock gate, which calls it first (was defined later: silent bug).
record_only() { write_result "$@" || true; }

# ------------------------------------------------------------------ 1. clock
# Weekday + a window around the open. `date +%u`: 1=Mon ... 7=Sun.
DOW="$(date +%u)"
NOW_HHMM="$(date +%H%M)"

if [[ "$FORCE" -eq 0 && "$RUNNER_GATED" -eq 0 ]]; then
  if [[ "$DOW" -gt 5 ]]; then
    info "weekend (day $DOW) - nothing to do"
    record_only 10 clock "weekend"
    exit 10
  fi
  if (( 10#$NOW_HHMM < 10#$EARLIEST_HHMM )) || (( 10#$NOW_HHMM > 10#$LATEST_HHMM )); then
    info "outside the pre-open window ($EARLIEST_HHMM-$LATEST_HHMM IST), now $NOW_HHMM"
    record_only 10 clock "outside window"
    exit 10
  fi
fi
ok "pre-open window: now $NOW_HHMM (open $OPEN_HHMM)"

# ------------------------------------------------------------------ 2. lock
# flock on FD 9. A stale lock from a killed run is released by the kernel when
# that process dies, so there is no stale-lock cleanup to get wrong.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  warn "another preopen_launch.sh holds $LOCK_FILE - refusing to start a second engine"
  record_only 11 lock "already running"
  exit 11
fi
ok "lock acquired"

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  info "--check: prerequisites only"
  [[ -x "$VENV_PY" ]] && ok "venv python: $VENV_PY" || err "venv python missing: $VENV_PY"
  for u in openalgo openalgo-ws pov-dash pov-runner; do
    if systemctl cat "$u.service" >/dev/null 2>&1; then ok "unit $u present"; else warn "unit $u missing"; fi
  done
  [[ -x "$VENV_PY" ]] || exit 12
  ok "--check complete"
  exit 0
fi

# ------------------------------------------------------------------ helpers
port_open() {       # port_open <port> <timeout_s>
  local _p="$1" _t="${2:-1}" _i=0
  while (( _i < _t )); do
    if (exec 3<>"/dev/tcp/127.0.0.1/${_p}") 2>/dev/null; then exec 3>&- 3<&-; return 0; fi
    sleep 1; _i=$((_i + 1))
  done
  return 1
}

wait_port() {       # wait_port <name> <port> <timeout_s>
  local _name="$1" _p="$2" _t="$3"
  if port_open "$_p" "$_t"; then ok "$_name listening on $_p"; return 0; fi
  err "$_name never listened on $_p within ${_t}s"
  return 1
}

srv() {             # run a systemctl verb as root-aware
  if [[ $EUID -eq 0 ]]; then systemctl "$@"; else sudo systemctl "$@"; fi
}

maybe() {           # echo the command, or just print it under --dry-run
  if [[ "$DRY_RUN" -eq 1 ]]; then info "[dry-run] $*"; return 0; fi
  "$@"
}


# ------------------------------------------------------------------ 3. platform
info "starting the platform (openalgo, openalgo-ws)"
maybe srv start openalgo openalgo-ws

# ------------------------------------------------------------------ 4. wait
# OpenAlgo takes ~200 s cold on a small VPS: it bridges _restored/, runs the
# upgrade check and loads the master contract. 300 s is the measured ceiling.
if [[ "$DRY_RUN" -eq 0 ]]; then
  if ! wait_port "openalgo" "$OPENALGO_PORT" 300; then
    record_only 12 platform "openalgo never listened"
    exit 12
  fi
  if ! wait_port "websocket (via openalgo)" "$WS_PORT" 120; then
    warn "websocket proxy not listening - continuing, the SDK will report it"
  fi
else
  info "[dry-run] would wait for $OPENALGO_PORT and $WS_PORT"
fi

# ------------------------------------------------------------------ 5. login
# The schedule itself is the opt-in: BROKER_AUTOLOGIN=1 is set for this run only.
# We never pass --enable and never pass --force: those are human decisions.
if [[ -x "$VENV_PY" ]]; then
  info "broker session: scheduled_login.py (honours BROKER_AUTOLOGIN)"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "[dry-run] $VENV_PY scripts/ops/scheduled_login.py"
  else
    LOGIN_LOG="$LOG_DIR/$(date +%F)_login.log"
    if BROKER_AUTOLOGIN=1 "$VENV_PY" "$ROOT/scripts/ops/scheduled_login.py" >>"$LOGIN_LOG" 2>&1; then
      ok "broker session ready"
    else
      _rc=$?
      case "$_rc" in
        2) warn "login not opted in (BROKER_AUTOLOGIN != 1) - continuing" ;;
        *) err "login failed (exit $_rc) - see $LOGIN_LOG"
           record_only 13 login "exit $_rc"
           exit 13 ;;
      esac
    fi
  fi
else
  err "no venv python at $VENV_PY - cannot log in"
  record_only 13 login "venv missing"
  exit 13
fi

# ------------------------------------------------------------------ 6. smoke
if [[ -x "$VENV_PY" ]]; then
  info "smoke gate"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    info "[dry-run] $VENV_PY scripts/smoke_test.py"
  else
    SMOKE_LOG="$LOG_DIR/$(date +%F)_smoke.log"
    if "$VENV_PY" "$ROOT/scripts/smoke_test.py" >>"$SMOKE_LOG" 2>&1; then
      ok "smoke test PASSED"
    else
      err "smoke test FAILED - engine will NOT start (see $SMOKE_LOG)"
      record_only 14 smoke "smoke test failed"
      exit 14
    fi
  fi
fi

# ------------------------------------------------------------------ 7. engine
if [[ "$START_ENGINE_WANTED" -eq 0 ]]; then
  info "--no-engine: platform and session are up, engine left stopped"
  record_only 0 engine "skipped by flag"
  exit 0
fi

# auto engine start: read the dashboard toggle at run time (fails closed to OFF)
if [[ "${PREOPEN_ENGINE_ALLOWED:-1}" != "0" ]] && [[ -x "$VENV_PY" ]] && [[ "$("$VENV_PY" "$ROOT/scripts/ops/read_engine_flag.py" 2>/dev/null)" == "1" ]]; then
  info "auto engine start is ON in the saved settings"
else
  info "auto engine start is OFF - engine left stopped; use the dashboard START ENGINE button"
  record_only 0 engine "auto engine start off"
  exit 0
fi

info "starting the engine (pov-dash, pov-runner)"
maybe srv start pov-dash
maybe srv start pov-runner

# ------------------------------------------------------------------ 8. verify
if [[ "$DRY_RUN" -eq 0 ]]; then
  if ! wait_port "pov-dash" "$DASH_PORT" 240; then
    warn "dashboard not answering - engine may still be reconciling positions"
  else
    ok "dashboard answered"
  fi
  # pov-runner prints to its own log; unit state is the cheap proof of life.
  if srv is-active --quiet pov-runner; then
    ok "pov-runner active"
  else
    err "pov-runner is NOT active"
    record_only 15 engine "pov-runner not active"
    exit 15
  fi
fi

# ------------------------------------------------------------------ 9. record
ok "pre-open launch complete"
record_only 0 done "engine up"
exit 0
