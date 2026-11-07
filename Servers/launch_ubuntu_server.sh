#!/usr/bin/env bash
# =============================================================================
# launch_ubuntu_server.sh - ONE click: whole server from a bare Ubuntu VPS
# =============================================================================
#
#   sudo bash Servers/launch_ubuntu_server.sh
#
# Everything below runs in order. Each stage is skippable, idempotent, and
# reports [OK]/[WARN]/[FAIL] without aborting the ones after it - one broken
# package must not cost you the rest of the machine.
#
#   STAGE 1  Preflight ............ Ubuntu/Debian, root, RAM/disk sanity
#   STAGE 2  Platform ............. install_ubuntu.sh (OpenAlgo + engine + nginx)
#   STAGE 3  Desktop/Remote ....... provision_desktop_remote.sh (XFCE, xrdp,
#                                   Chrome, Node, PM2, UFW)
#   STAGE 4  Pre-open schedule .... weekday 08:45 launch, via systemd timer or
#                                   cron (see "WHY THE DEFAULT IS A TIMER")
#   STAGE 5  Backup schedule ...... daily .env + db backup, 16:00 IST
#   STAGE 6  Verify ............... ports, units, and one honest summary
#
# WHY THE DEFAULT IS A TIMER, NOT CRON
#   You asked for a systemd *or* PM2 supervisor and a cron *or* schedule. The
#   facts that decide it:
#     * systemd + journald already supervise this stack; PM2 would fight it.
#     * a systemd timer inherits cgroup accounting, logs to journald, can run
#       with `Persistent=true` (catches up after downtime) and has an explicit
#       RandomizedDelaySec. cron has none of that and mails nothing by default.
#   Both are installed: a timer by default, plus a commented cron line in
#   /etc/cron.d/mts-preopen if you prefer the crontab. Pick one - never both.
#
# WHY pov-runner IS STILL NOT ENABLED AT BOOT
#   Stage 2 and stage 4 start the engine *by clock*, behind a smoke gate.
#   A reboot never starts the order-placing engine by itself. That is the
#   AGENTS.md rule ("must not automatically resume live trading"), not an
#   oversight - do not "fix" it by enabling the unit.
#
# THE ONE SCRIPT (install + update + start). Run it again any time - it is safe.
#   * Repo missing at INSTALL_ROOT?  clones it there, then runs that copy.
#   * Platform not installed?        installs it (stage 2).
#   * Platform installed?            safe update: fast-forward only, refuses during
#                                    NSE hours (09:15-15:30 IST) or while pov-runner
#                                    runs, then restarts openalgo, openalgo-ws, pov-dash.
#
# FRESH SERVER (two lines)
#   git clone https://github.com/Ravindhu/Algo-Pro.git /opt/my-trading-system
#   sudo bash /opt/my-trading-system/Servers/launch_ubuntu_server.sh --domain trade.example.com --email you@example.com
#
# USAGE
#   sudo bash Servers/launch_ubuntu_server.sh --domain trade.example.com --email you@example.com
#   sudo bash Servers/launch_ubuntu_server.sh --allow-plain-http  # LAN lab only, no TLS
#   sudo bash Servers/launch_ubuntu_server.sh --start-only        # boot job: start platform, no update, no engine
#   sudo bash Servers/launch_ubuntu_server.sh --skip-platform     # remote only
#   sudo bash Servers/launch_ubuntu_server.sh --skip-desktop
#   sudo bash Servers/launch_ubuntu_server.sh --rdp-allow 203.0.113.7/32
#   sudo bash Servers/launch_ubuntu_server.sh --dry-run           # print stages
#   sudo bash Servers/launch_ubuntu_server.sh --start-engine      # + start now
#
# PREREQUISITES (stage 1 checks them, does not fix them)
#   * Servers/deploy.env       (copy from deploy.env.example)
#   * $INSTALL_ROOT/_restored/.env + db/  (secrets - copy with scp)
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_NAME="my-trading-system"
INSTALL_ROOT="/opt/${APP_NAME}"
SERVICE_USER="algo"
OPENALGO_PORT="5000"
DASH_PORT="8050"
WS_PORT="8765"
RDP_PORT="3389"
PREOPEN_TIME="08:45"          # 30 min before the 09:15 open
BACKUP_TIME="16:00"           # after the 15:30 close

RUN_PLATFORM=1
RUN_DESKTOP=1
RUN_SCHEDULE=1
RUN_BACKUP=1
DRY_RUN=0
START_ENGINE=0
START_ONLY=0             # boot job: start units, no code update, no engine
RDP_ALLOW=""
DESKTOP_ARGS=()
PLATFORM_ARGS=()          # passed to install_ubuntu.sh (TLS, domain)
REPO_URL="https://github.com/Ravindhu/Algo-Pro.git"
ORIG_ARGS=("$@")          # kept for the bootstrap re-run below

FAILURES=()

# ------------------------------------------------------------------ output
C_RESET=$'\033[0m'; C_OK=$'\033[32m'; C_ERR=$'\033[31m'
C_WARN=$'\033[33m'; C_STEP=$'\033[36m'
step() { printf '\n%s==> %s%s\n' "$C_STEP" "$*" "$C_RESET"; }
ok()   { printf '    %s[OK]%s %s\n'   "$C_OK"   "$C_RESET" "$*"; }
warn() { printf '    %s[WARN]%s %s\n' "$C_WARN" "$C_RESET" "$*"; }
fail() { printf '    %s[FAIL]%s %s\n' "$C_ERR"  "$C_RESET" "$*"; FAILURES+=("$*"); }
note() { printf '          %s\n' "$*"; }
run()  { if [[ "$DRY_RUN" -eq 1 ]]; then note "[dry-run] $*"; else "$@"; fi; }

# ------------------------------------------------------------------ args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-platform)  RUN_PLATFORM=0; shift ;;
    --skip-desktop)   RUN_DESKTOP=0; shift ;;
    --skip-schedule)  RUN_SCHEDULE=0; shift ;;
    --skip-backup)    RUN_BACKUP=0; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --start-engine)   START_ENGINE=1; shift ;;
    --start-only)     START_ONLY=1; RUN_DESKTOP=0; RUN_SCHEDULE=0; RUN_BACKUP=0; shift ;;
    --rdp-allow)      RDP_ALLOW="${2:-}"; shift 2 ;;
    --allow-any-rdp)  DESKTOP_ARGS+=("--allow-any-rdp"); shift ;;
    --no-chrome)      DESKTOP_ARGS+=("--no-chrome"); shift ;;
    --no-rdp)         DESKTOP_ARGS+=("--no-rdp"); shift ;;
    --domain)         PLATFORM_ARGS+=("--domain" "${2:-}"); shift 2 ;;
    --email)          PLATFORM_ARGS+=("--email" "${2:-}"); shift 2 ;;
    --allow-plain-http) PLATFORM_ARGS+=("--allow-plain-http"); shift ;;
    -h|--help)        sed -n '2,70p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
done
# NOTE: plain `[[ ... ]] && cmd` as the LAST line would leave the script's exit
# status = 1 whenever the test is false. Use `if` for anything a caller checks.
if [[ -n "$RDP_ALLOW" ]]; then DESKTOP_ARGS+=("--rdp-allow" "$RDP_ALLOW"); fi

if [[ $EUID -ne 0 ]]; then echo "run with sudo"; exit 1; fi

# deploy.env: same parser as the two stage scripts (no `source` - the file is
# deploy-time writable and must not be able to execute shell).
if [[ -f "$SCRIPT_DIR/deploy.env" ]]; then
  while IFS='=' read -r _k _v; do
    [[ "$_k" =~ ^[A-Z_]+$ ]] || continue
    _v="${_v%\"}"; _v="${_v#\"}"; _v="${_v%\'}"; _v="${_v#\'}"
    case "$_k" in
      MTS_ROOT)          [[ -n "$_v" ]] && INSTALL_ROOT="$_v" ;;
      MTS_USER)          [[ -n "$_v" ]] && SERVICE_USER="$_v" ;;
      MTS_OPENALGO_PORT) [[ -n "$_v" ]] && OPENALGO_PORT="$_v" ;;
      MTS_DASH_PORT)     [[ -n "$_v" ]] && DASH_PORT="$_v" ;;
      MTS_WS_PORT)       [[ -n "$_v" ]] && WS_PORT="$_v" ;;
    esac
  done < <(grep -E '^[A-Z_]+=' "$SCRIPT_DIR/deploy.env" 2>/dev/null || true)
fi

# run a command as the service user when that user exists (git pulls, pip)
as_service() {
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then sudo -u "$SERVICE_USER" "$@"; else "$@"; fi
}

# -----------------------------------------------------------------------------
# BOOTSTRAP - make sure the repo lives at INSTALL_ROOT, then run that copy.
# Replaces the old install.sh clone shim. MTS_REEXEC stops any loop.
# -----------------------------------------------------------------------------
if [[ "$REPO_ROOT" != "$INSTALL_ROOT" && -z "${MTS_REEXEC:-}" ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    note "[dry-run] would clone or pull $REPO_URL into $INSTALL_ROOT and re-run from there"
  else
    if ! command -v git >/dev/null 2>&1; then
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq && apt-get install -y -qq git ca-certificates >/dev/null \
        || { echo "[ABORT] could not install git" >&2; exit 1; }
    fi
    if [[ -d "$INSTALL_ROOT/.git" ]]; then
      as_service git -C "$INSTALL_ROOT" pull --ff-only \
        || echo "[WARN] repo not updated in $INSTALL_ROOT - kept current code" >&2
    elif [[ -e "$INSTALL_ROOT" && -n "$(ls -A "$INSTALL_ROOT" 2>/dev/null || true)" ]]; then
      echo "[ABORT] $INSTALL_ROOT exists and is not a git checkout. Move it aside." >&2
      exit 1
    else
      git clone --depth 1 "$REPO_URL" "$INSTALL_ROOT" \
        || { echo "[ABORT] git clone failed" >&2; exit 1; }
    fi
    if [[ ! -f "$INSTALL_ROOT/Servers/deploy.env" && -f "$REPO_ROOT/Servers/deploy.env" ]]; then
      install -m 600 "$REPO_ROOT/Servers/deploy.env" "$INSTALL_ROOT/Servers/deploy.env"
    fi
    export MTS_REEXEC=1
    exec bash "$INSTALL_ROOT/Servers/launch_ubuntu_server.sh" "${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}"
  fi
fi

printf '\n%s\n%s\n' "================================================================" \
  " my-trading-system : Ubuntu server launch"
printf '%s\n' "================================================================"
note "repo        : $REPO_ROOT"
note "install root: $INSTALL_ROOT"
note "service user: $SERVICE_USER"
note "dry run     : $([[ "$DRY_RUN" -eq 1 ]] && echo yes || echo no)"

# =============================================================================
# STAGE 1 - Preflight
# =============================================================================
step "1/6  Preflight"
if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  ok "OS: ${PRETTY_NAME:-$ID}"
else
  fail "cannot read /etc/os-release"
fi
command -v systemctl >/dev/null && ok "systemd present" || fail "systemd missing"

MEM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
DISK_GB=$(df -Pk / | awk 'NR==2 {printf "%d", $4/1024/1024}')
if (( MEM_MB < 3000 )); then
  warn "RAM ${MEM_MB} MB. XFCE+xrdp+Chrome+OpenAlgo+engine want ~3-4 GB."
  warn "add swap (see stage 1 of DEPLOY_UBUNTU.md) or expect OOM kills."
else
  ok "RAM ${MEM_MB} MB"
fi
if (( DISK_GB < 12 )); then
  warn "free disk ${DISK_GB} GB - tight for two venvs + Chrome + desktop"
else
  ok "free disk ${DISK_GB} GB"
fi

if [[ -f "$SCRIPT_DIR/deploy.env" ]]; then
  ok "Servers/deploy.env present"
else
  warn "Servers/deploy.env missing - using defaults"
  note "cp $SCRIPT_DIR/deploy.env.example $SCRIPT_DIR/deploy.env && chmod 600 ..."
fi

if [[ -e "$INSTALL_ROOT/_restored/.env" && -d "$INSTALL_ROOT/_restored/db" ]]; then
  ok "_restored/.env + db present (the married pair)"
elif [[ "$RUN_PLATFORM" -eq 1 ]]; then
  warn "_restored/ state missing - install_ubuntu.sh will warn and the broker"
  warn "session will not exist. Copy it BEFORE stage 2 if you have it:"
  note "scp -r .\\_restored\\.env .\\_restored\\db user@<vps>:/tmp/mts-state/"
fi

# -----------------------------------------------------------------------------
# UPDATE - used when the platform is already installed (stage 2 calls this).
# Same gates as scripts/ops/upgrade_openalgo.ps1: no update in NSE hours, never
# touch a running engine, fast-forward only. pov-runner is never restarted here.
# Exchange holidays are not checked, so run it outside hours on any day.
# -----------------------------------------------------------------------------
update_platform() {
  if [[ "$DRY_RUN" -eq 1 ]]; then note "[dry-run] would update the platform"; return 0; fi
  local dow hm oa_venv="$INSTALL_ROOT/openalgo/.venv"
  dow="$(TZ=Asia/Kolkata date +%u)"
  hm="$(TZ=Asia/Kolkata date +%H%M)"
  if (( dow <= 5 )) && (( 10#$hm >= 915 && 10#$hm <= 1530 )); then
    warn "update refused: NSE market hours (09:15-15:30 IST). Run it after 15:30."
    return 0
  fi
  if systemctl is-active --quiet pov-runner; then
    warn "update refused: pov-runner is running. Stop it first: sudo systemctl stop pov-runner"
    return 0
  fi
  as_service git -C "$INSTALL_ROOT" pull --ff-only || warn "repo pull failed - kept current code"
  as_service git -C "$INSTALL_ROOT/openalgo" pull --ff-only || warn "openalgo pull failed - kept current code"
  as_service "$oa_venv/bin/python" -m pip install -q -r "$INSTALL_ROOT/openalgo/requirements.txt" \
    || { fail "pip install (openalgo requirements) failed"; return 0; }
  if [[ -f "$INSTALL_ROOT/Blue_engine/pyproject.toml" ]]; then
    as_service "$oa_venv/bin/python" -m pip install -q -e "$INSTALL_ROOT/Blue_engine[charts]" \
      || { fail "pip install (Blue_engine) failed"; return 0; }
  fi
  systemctl restart openalgo openalgo-ws pov-dash || warn "restart of platform units failed"
  ok "platform updated and restarted (pov-runner untouched)"
}

# =============================================================================
# STAGE 2 - Platform
# =============================================================================
step "2/6  Platform (OpenAlgo + blue_engine + nginx + systemd)"
if [[ "$RUN_PLATFORM" -eq 1 ]]; then
  if [[ -d "$INSTALL_ROOT/openalgo/.git" ]]; then
    if [[ "$START_ONLY" -eq 1 ]]; then
      ok "start-only run: no code update, no engine"
      run systemctl start openalgo openalgo-ws pov-dash || fail "platform start failed (stage 2)"
    else
      ok "platform already installed at $INSTALL_ROOT - running the safe update"
      update_platform
    fi
  elif [[ "$START_ONLY" -eq 1 ]]; then
    fail "--start-only needs an installed platform. Run the launcher without it first."
  else
    run env DEBIAN_FRONTEND=noninteractive bash "$SCRIPT_DIR/install_ubuntu.sh" \
      "${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"}" \
      || fail "install_ubuntu.sh failed (stage 2)"
  fi
else
  warn "skipped (--skip-platform)"
fi

# =============================================================================
# STAGE 3 - Desktop + remote access
# =============================================================================
step "3/6  Desktop + remote access (XFCE, xrdp, Chrome, Node, PM2, UFW)"
if [[ "$RUN_DESKTOP" -eq 1 ]]; then
  run bash "$SCRIPT_DIR/provision_desktop_remote.sh" "${DESKTOP_ARGS[@]+"${DESKTOP_ARGS[@]}"}" \
    || fail "provision_desktop_remote.sh reported failures (stage 3)"
else
  warn "skipped (--skip-desktop)"
fi

# =============================================================================
# STAGE 4 - Pre-open schedule
# =============================================================================
step "4/6  Pre-open schedule (weekdays ${PREOPEN_TIME} IST)"

PREOPEN_SH="$INSTALL_ROOT/scripts/ops/preopen_launch.sh"
if [[ -f "$REPO_ROOT/Servers/install_ubuntu.sh" && -f "$REPO_ROOT/scripts/ops/preopen_launch.sh" ]]; then
  # The installer copies the repo into $INSTALL_ROOT; if this launcher ran from
  # a /tmp copy, make sure the schedule points at a file that exists.
  if [[ ! -f "$PREOPEN_SH" ]]; then
    run mkdir -p "$INSTALL_ROOT/scripts/ops"
    run cp -f "$REPO_ROOT/scripts/ops/preopen_launch.sh" "$PREOPEN_SH"
  fi
  run chmod 755 "$PREOPEN_SH"
  ok "pre-open launcher: $PREOPEN_SH"
else
  warn "preopen_launch.sh not found in the repo - schedule skipped"
  RUN_SCHEDULE=0
fi

if [[ "$RUN_SCHEDULE" -eq 1 ]]; then
  # --- systemd timer (default) --------------------------------------------- #
  cat > /etc/systemd/system/pov-preopen.service <<UNIT
[Unit]
Description=Pre-open desk launch (platform, broker session, smoke gate, engine)
After=network-online.target openalgo.service
Wants=network-online.target

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_ROOT}
# The engine start needs systemctl. A user service cannot start system units,
# so pov-runner/pov-dash are started through a narrowly scoped sudo rule
# installed below. Nothing else is sudo-able for this user.
Environment=OPENALGO_PORT=${OPENALGO_PORT}
Environment=DASH_PORT=${DASH_PORT}
Environment=WS_PORT=${WS_PORT}
ExecStart=/bin/bash ${PREOPEN_SH}
TimeoutStartSec=30min
StandardOutput=journal
StandardError=journal
UNIT

  cat > /etc/systemd/system/pov-preopen.timer <<'UNIT'
[Unit]
Description=Run pov-preopen on NSE weekdays at 08:45 IST

[Timer]
# Persistent=true: if the VPS was off at 08:45, run once on the next boot -
# still inside the launcher's own 08:00-09:30 gate, so it fails closed after
# the open instead of starting an engine mid-session.
OnCalendar=Mon..Fri *-*-* 08:45:00
Persistent=true
RandomizedDelaySec=30
Unit=pov-preopen.service

[Install]
WantedBy=timers.target
UNIT

  # A user service (Type=oneshot, User=algo) cannot start system units. Grant
  # ONLY these verbs on ONLY these units. This is the least privilege that
  # makes the timer work without running the whole launcher as root.
  cat > /etc/sudoers.d/mts-preopen <<SUDO
# Managed by my-trading-system/Servers/launch_ubuntu_server.sh
${SERVICE_USER} ALL=(root) NOPASSWD: /bin/systemctl start openalgo, /bin/systemctl start openalgo-ws, /bin/systemctl start pov-dash, /bin/systemctl start pov-runner, /bin/systemctl is-active pov-runner, /bin/systemctl is-active pov-dash
SUDO
  chmod 440 /etc/sudoers.d/mts-preopen
  visudo -cf /etc/sudoers.d/mts-preopen >/dev/null 2>&1 \
    && ok "sudo rule valid (/etc/sudoers.d/mts-preopen)" \
    || fail "sudoers rule is invalid - run visudo -cf /etc/sudoers.d/mts-preopen"

  # --- cron alternative (present but commented) ---------------------------- #
  cat > /etc/cron.d/mts-preopen <<CRON
# Alternative to pov-preopen.timer. Enable ONE of the two, never both.
# 45 8 * * 1-5 ${SERVICE_USER} cd ${INSTALL_ROOT} && /bin/bash ${PREOPEN_SH} >> ${INSTALL_ROOT}/log/preopen/cron.log 2>&1
CRON
  chmod 644 /etc/cron.d/mts-preopen
  ok "/etc/cron.d/mts-preopen written (commented - pick timer OR cron)"

  # Boot job: start the platform at every boot. No code update, no engine.
  cat > /etc/systemd/system/mts-boot-start.service <<UNIT
[Unit]
Description=My trading system: start platform at boot (no update, no engine)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash ${INSTALL_ROOT}/Servers/launch_ubuntu_server.sh --start-only
TimeoutStartSec=15min

[Install]
WantedBy=multi-user.target
UNIT
  run systemctl daemon-reload
  run systemctl enable mts-boot-start.service >/dev/null 2>&1 \
    && ok "mts-boot-start.service enabled (platform starts at boot)" \
    || fail "could not enable mts-boot-start.service"
  run systemctl enable pov-preopen.timer >/dev/null 2>&1 \
    && ok "pov-preopen.timer enabled" \
    || fail "could not enable pov-preopen.timer"
  run systemctl start pov-preopen.timer >/dev/null 2>&1 || warn "timer start failed"
  if systemctl list-timers pov-preopen.timer --no-pager 2>/dev/null | grep -q pov-preopen; then
    systemctl list-timers pov-preopen.timer --no-pager 2>/dev/null | sed 's/^/          /'
  fi
fi

# =============================================================================
# STAGE 5 - Backup schedule
# =============================================================================
step "5/6  Backup schedule (daily ${BACKUP_TIME} IST)"
if [[ "$RUN_BACKUP" -eq 1 ]]; then
  run mkdir -p "$INSTALL_ROOT/backups"
  # .env and db/ are cryptographically married: back them up in the SAME run,
  # in the SAME directory, or a restore cannot decrypt the broker tokens.
  cat > /etc/cron.d/mts-backup <<CRON
# Managed by my-trading-system/Servers/launch_ubuntu_server.sh
# .env and db/ must be backed up together - one without the other cannot
# decrypt the broker session tokens.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 16 * * 1-5 ${SERVICE_USER} mkdir -p ${INSTALL_ROOT}/backups/\$(date +\%F) && cp -a ${INSTALL_ROOT}/_restored/.env ${INSTALL_ROOT}/_restored/db ${INSTALL_ROOT}/backups/\$(date +\%F)/ 2>/dev/null
CRON
  chmod 644 /etc/cron.d/mts-backup
  ok "/etc/cron.d/mts-backup written (Mon-Fri ${BACKUP_TIME})"
  note "retention is yours: add a weekly prune or the disk fills"
else
  warn "skipped (--skip-backup)"
fi

# =============================================================================
# STAGE 6 - Verify
# =============================================================================
step "6/6  Verify"
port_up() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && { exec 3>&- 3<&-; return 0; }; return 1; }

for pair in "openalgo:${OPENALGO_PORT}" "dash:${DASH_PORT}" "ws:${WS_PORT}" "xrdp:${RDP_PORT}"; do
  _n="${pair%%:*}"; _p="${pair##*:}"
  if port_up "$_p"; then ok "${_n} listening on ${_p}"; else warn "${_n} NOT listening on ${_p}"; fi
done

for u in openalgo openalgo-ws; do
  if systemctl is-active --quiet "$u"; then ok "${u}: active"; else warn "${u}: $(systemctl is-active "$u" 2>/dev/null)"; fi
done
for u in pov-runner pov-dash; do
  if systemctl is-enabled --quiet "$u" 2>/dev/null; then
    warn "${u}: ENABLED at boot - this should be disabled for pov-runner"
  else
    ok "${u}: not enabled at boot (correct)"
  fi
done
systemctl is-enabled --quiet pov-preopen.timer 2>/dev/null \
  && ok "pov-preopen.timer: enabled" || warn "pov-preopen.timer: not enabled"

if command -v ufw >/dev/null 2>&1; then
  ok "ufw: $(ufw status | head -1)"
fi

if [[ "$START_ENGINE" -eq 1 ]]; then
  step "Starting the engine now (--start-engine)"
  run systemctl start pov-dash pov-runner || warn "engine start failed"
else
  note "engine left stopped - start it with:"
  note "  sudo systemctl start pov-dash pov-runner"
  note "  # or wait for the 08:45 timer (which runs the smoke gate first)"
fi

REMOTE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
printf '\n%s\n' "================================================================"
cat <<SUMMARY
  DONE  -  ${APP_NAME}
===============================================================

  Dashboard   : https://<your-domain>/pov-dash/   (localhost-only on :${DASH_PORT})
  OpenAlgo    : https://<your-domain>/             (localhost-only on :${OPENALGO_PORT})
  RDP         : ${REMOTE_IP:-<ip>}:${RDP_PORT}  -> XFCE desktop
  Pre-open    : weekdays ${PREOPEN_TIME} IST  (systemd timer, smoke-gated)
  Logs        : journalctl -u pov-preopen  |  ${INSTALL_ROOT}/log/preopen/
  Backup      : ${INSTALL_ROOT}/backups/<date>/  (Mon-Fri ${BACKUP_TIME})

  WinSCP      : Windows-only. On the laptop: new SFTP session ->
                ${REMOTE_IP:-<ip>}:22, user ${SERVICE_USER}.
                For a Linux-side SFTP client: apt-get install filezilla

  RDP range   : $(if [[ -n "$RDP_ALLOW" ]]; then echo "open for $RDP_ALLOW"; else echo "CLOSED until you run: sudo ufw allow from <ip>/32 to any port ${RDP_PORT} proto tcp"; fi)

SUMMARY

if (( ${#FAILURES[@]} > 0 )); then
  printf '\n%s\n' "Failures:"; printf '  - %s\n' "${FAILURES[@]}"
  exit 1
fi
printf '%s\n' "All stages completed."
