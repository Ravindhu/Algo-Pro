#!/usr/bin/env bash
# =============================================================================
# provision_desktop_remote.sh - desktop + remote access layer for the VPS
# =============================================================================
#
# WHAT THIS IS
#   The second half of a one-click Ubuntu launch. install_ubuntu.sh builds the
#   trading platform (OpenAlgo + blue_engine + nginx + systemd). THIS script
#   adds the human-access layer on top:
#
#     1. XFCE  ......... a lightweight desktop (so a GUI exists at all)
#     2. XRDP  ......... Remote Desktop server, so you connect from Windows/macOS
#     3. Google Chrome . the real .deb build, running inside XFCE
#     4. Node.js LTS ... required by PM2
#     5. Yarn .......... optional, only with --with-yarn
#     6. UFW ........... firewall, SAFE defaults (SSH + your RDP source only)
#     7. Remote stack .. openalgo + openalgo-ws + pov-dash as DESKTOP-ONLY units
#     8. Auto-start .... the remote stack comes up at boot (no login required)
#
# WHY THE REMOTE STACK IS SEPARATE FROM pov-runner
#   pov-runner is the ORDER-PLACING engine. AGENTS.md forbids resuming live
#   trading automatically after a reboot without operator checks, and
#   install_ubuntu.sh deliberately leaves it disabled. So this script enables a
#   *view* stack (platform + websocket + dashboard) but NEVER pov-runner. The
#   engine is still started by a human, or by the pre-open launcher, which has
#   its own smoke gate.
#
# WHY PM2 IS NOT USED FOR THE TRADING PROCESSES
#   systemd is already on the box, already supervises these units, already has
#   journald and cgroup accounting, and already restarts on failure. Adding PM2
#   on top of systemd gives two supervisors racing over one port. PM2 is
#   installed (you asked for it) and is used only for NON-trading helpers.
#   Supervisor-of-record for trading services = systemd.
#
# USAGE
#   sudo bash Servers/provision_desktop_remote.sh
#   sudo bash Servers/provision_desktop_remote.sh --no-chrome --no-rdp
#   sudo bash Servers/provision_desktop_remote.sh --rdp-allow 203.0.113.7/32
#   sudo bash Servers/provision_desktop_remote.sh --allow-any-rdp   # LOUD warning
#
# SAFETY DEFAULTS
#   * UFW default deny incoming; only 22/tcp is opened automatically.
#   * 3389/tcp (RDP) is opened ONLY for the CIDR you pass in --rdp-allow.
#     With no --rdp-allow, RDP stays CLOSED until you open it deliberately.
#   * xrdp STARTWM is rewritten to call startxfce4. The stock Debian/Ubuntu unit
#     tries to start a GNOME/Xorg session and gives a black screen.
#   * Nothing here touches _restored/, .env, or the API key.
#
# READING THE RESULT
#   Every step prints [OK] / [WARN] / [FAIL]. A [FAIL] at the end means that
#   feature is not usable yet - the script continues so one broken package does
#   not cost you the rest of the install.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_NAME="my-trading-system"
INSTALL_ROOT="/opt/${APP_NAME}"
SERVICE_USER="algo"
OPENALGO_PORT="5000"
DASH_PORT="8050"
WS_PORT="8765"
RDP_PORT="3389"

DO_DESKTOP=1
DO_RDP=1
DO_CHROME=1
DO_NODE=1
DO_YARN=0
DO_FIREWALL=1
DO_REMOTE_STACK=1
RDP_ALLOW=""            # CIDR(s), comma separated. Empty = do not open 3389.
ALLOW_ANY_RDP=0

FAILURES=0

# ------------------------------------------------------------------ output
C_RESET=$'\033[0m'; C_OK=$'\033[32m'; C_ERR=$'\033[31m'
C_WARN=$'\033[33m'; C_STEP=$'\033[36m'
step() { printf '\n%s==> %s%s\n' "$C_STEP" "$*" "$C_RESET"; }
ok()   { printf '    %s[OK]%s %s\n'   "$C_OK"   "$C_RESET" "$*"; }
warn() { printf '    %s[WARN]%s %s\n' "$C_WARN" "$C_RESET" "$*"; }
fail() { printf '    %s[FAIL]%s %s\n' "$C_ERR"  "$C_RESET" "$*"; FAILURES=$((FAILURES + 1)); }
note() { printf '          %s\n' "$*"; }

# deploy.env is the same file install_ubuntu.sh reads, so ports stay in sync.
DEPLOY_ENV="${SCRIPT_DIR}/deploy.env"
if [[ -f "$DEPLOY_ENV" ]]; then
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
  done < <(grep -E '^[A-Z_]+=' "$DEPLOY_ENV" 2>/dev/null || true)
fi

# ------------------------------------------------------------------ args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-desktop)   DO_DESKTOP=0; shift ;;
    --no-rdp)       DO_RDP=0; shift ;;
    --no-chrome)    DO_CHROME=0; shift ;;
    --no-node)      DO_NODE=0; shift ;;
    --with-yarn)    DO_YARN=1; shift ;;
    --no-firewall)  DO_FIREWALL=0; shift ;;
    --no-remote-stack) DO_REMOTE_STACK=0; shift ;;
    --rdp-allow)    RDP_ALLOW="${2:-}"; shift 2 ;;
    --allow-any-rdp) ALLOW_ANY_RDP=1; shift ;;
    -h|--help)
      sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) fail "unknown argument: $1 (try --help)"; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
command -v systemctl >/dev/null || { echo "systemd not found"; exit 1; }
# Imported by the one-click launcher: a bare `[[ ]] && ...` at end of script
# would make a successful run exit 1. `true` pins the status here.
true

step "Preflight"
[[ -d "$INSTALL_ROOT" ]] && ok "install root: $INSTALL_ROOT" \
  || warn "$INSTALL_ROOT missing - run install_ubuntu.sh first"
id -u "$SERVICE_USER" >/dev/null 2>&1 && ok "service user: $SERVICE_USER" \
  || warn "service user $SERVICE_USER missing"
command -v ufw >/dev/null 2>&1 || true
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
ok "architecture: $ARCH"

# =============================================================================
# 1. XFCE + XRDP
# =============================================================================
if [[ "$DO_DESKTOP" -eq 1 ]]; then
  step "Installing XFCE desktop (lightweight, ~700 MB)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || fail "apt-get update failed"
  # xfce4 + the pieces that make a remote session feel like a desktop:
  #   xfce4-terminal  - you need a terminal in the session
  #   dbus-x11        - the session bus; without it most GTK apps misbehave
  #   xorgxrdp        - Xorg driver xrdp needs; the classic bug is missing it
  #   x11-xserver-utils - xrandr, for screen resizing over RDP
  if apt-get install -y -qq \
      xfce4 xfce4-goodies xfce4-terminal dbus-x11 \
      xorgxrdp x11-xserver-utils desktop-file-utils xdg-utils >/dev/null; then
    ok "XFCE installed"
  else
    fail "XFCE install failed (see: apt-get install xfce4)"
  fi

  # A minimal, non-bloated default: no xscreensaver, no power manager fighting
  # an RDP session into suspend. Cheap and reversible.
  sudo -u "$SERVICE_USER" bash -c 'mkdir -p ~/.config/xfce4' 2>/dev/null || true
  ok "XFCE ready (no screen saver / no suspend policy applied by default)"
fi

if [[ "$DO_RDP" -eq 1 ]]; then
  step "Installing xrdp (Remote Desktop server, port $RDP_PORT)"
  if apt-get install -y -qq xrdp >/dev/null; then
    ok "xrdp installed"
  else
    fail "xrdp install failed"
  fi

  # --- the black-screen fix ------------------------------------------------- #
  # Debian/Ubuntu's packaged xrdp STARTWM starts a plain X session. Over RDP
  # that renders black, because no window manager is running. Rewriting it to
  # exec startxfce4 is the documented fix for XFCE + xrdp.
  cat > /etc/xrdp/startwm.sh <<'STARTWM'
#!/bin/sh
# Managed by my-trading-system/Servers/provision_desktop_remote.sh
# RDP sessions run XFCE. Do not replace with a bare X session: that shows black.
if [ -f /etc/profile ]; then . /etc/profile; fi
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP=XFCE
if [ -x /usr/bin/startxfce4 ]; then
  exec /usr/bin/startxfce4
fi
exec /etc/X11/Xsession
STARTWM
  chmod 755 /etc/xrdp/startwm.sh
  ok "xrdp STARTWM -> startxfce4"

  # The policykit prompt ("Authentication required to create a colour profile")
  # appears on every login and blocks the session. Allow the algo user's colour
  # management without a password.
  mkdir -p /etc/polkit-1/localauthority/50-local.d
  cat > /etc/polkit-1/localauthority/50-local.d/45-allow-colord.pkla <<'PKLA'
[Allow Colord All Users]
Identity=unix-user:*
Action=org.freedesktop.color-manager.create-device;org.freedesktop.color-manager.create-profile;org.freedesktop.color-manager.delete-device;org.freedesktop.color-manager.delete-profile;org.freedesktop.color-manager.modify-device;org.freedesktop.color-manager.modify-profile
ResultAny=no
ResultInactive=no
ResultActive=yes
PKLA
  ok "colord polkit prompt suppressed"

  usermod -aG ssl-cert xrdp 2>/dev/null || true
  systemctl enable xrdp >/dev/null 2>&1 || warn "xrdp enable failed"
  systemctl restart xrdp 2>/dev/null || warn "xrdp restart failed"

  if ss -ltn 2>/dev/null | grep -q ":${RDP_PORT} "; then
    ok "xrdp listening on $RDP_PORT"
  else
    fail "xrdp not listening on $RDP_PORT (journalctl -u xrdp -n 60)"
  fi

  note "Connect from Windows:  mstsc  ->  <server-ip>:${RDP_PORT}"
  note "Log in as ${SERVICE_USER} (or your own sudo user) - XFCE starts."
fi

# =============================================================================
# 2. Google Chrome
# =============================================================================
if [[ "$DO_CHROME" -eq 1 ]]; then
  step "Installing Google Chrome (official .deb)"
  if command -v google-chrome >/dev/null 2>&1; then
    ok "google-chrome already installed: $(google-chrome --version 2>/dev/null || echo '?')"
  else
    # Download to a temp file, install from the local file. Piping a remote
    # script into a shell is what installers get wrong; this does not do that.
    TMP_DEB="$(mktemp /tmp/google-chrome-XXXXXX.deb)"
    if curl -fsSL --max-time 600 \
        -o "$TMP_DEB" \
        "https://dl.google.com/linux/direct/google-chrome-stable_current_${ARCH}.deb"; then
      if apt-get install -y -qq "$TMP_DEB" >/dev/null 2>&1; then
        ok "Google Chrome installed: $(google-chrome --version 2>/dev/null || echo '?')"
      else
        # The .deb pulls fonts + deps from the network; a transient failure here
        # is retried once with apt-get -f install.
        apt-get install -y -qf >/dev/null 2>&1 || true
        if dpkg -s google-chrome-stable >/dev/null 2>&1; then
          ok "Google Chrome installed on retry"
        else
          fail "Chrome .deb install failed"
        fi
      fi
    else
      fail "Chrome download failed (dl.google.com unreachable from this host)"
    fi
    rm -f "$TMP_DEB"
  fi

  # Chrome runs as root only with --no-sandbox, which is a security downgrade.
  # The desktop user runs it normally; no launcher hack is installed on purpose.
  ok "run Chrome as ${SERVICE_USER}, never as root"
fi

# =============================================================================
# 3. Node.js (+ optional Yarn) - needed by PM2
# =============================================================================
if [[ "$DO_NODE" -eq 1 ]]; then
  step "Installing Node.js LTS (NodeSource) + PM2"
  NODE_MAJOR="$(curl -fsSL --max-time 30 https://deb.nodesource.com/setup_lts.x \
                2>/dev/null | grep -oE 'NODE_MAJOR_VERSION=[0-9]+' | head -1 | cut -d= -f2)"
  NODE_MAJOR="${NODE_MAJOR:-22}"
  if command -v node >/dev/null 2>&1; then
    ok "node already installed: $(node -v)"
  else
    # NodeSource's setup script is the officially documented install path and is
    # fetched over HTTPS only. We run it in a subshell so a failure is caught.
    if curl -fsSL --max-time 120 "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" \
        | bash - >/dev/null 2>&1; then
      apt-get install -y -qq nodejs >/dev/null 2>&1 \
        && ok "Node.js installed: $(node -v)" \
        || fail "nodejs package install failed"
    else
      # Fallback: the distro package. Older, but PM2 works on it.
      warn "NodeSource script unreachable - falling back to the distro nodejs"
      apt-get install -y -qq nodejs npm >/dev/null 2>&1 \
        && ok "Node.js installed (distro): $(node -v)" \
        || fail "no Node.js could be installed"
    fi
  fi

  if command -v npm >/dev/null 2>&1; then
    if npm install -g --silent pm2 >/dev/null 2>&1; then
      ok "pm2 installed: $(pm2 -v 2>/dev/null || echo '?')"
      # pm2 startup prints a command; we run it so the PM2 daemon (used for
      # NON-trading helpers only) survives a reboot for the user who runs it.
      if sudo -u "$SERVICE_USER" -H bash -lc \
           "pm2 startup systemd -u ${SERVICE_USER} --hp /home/${SERVICE_USER}" \
           >/dev/null 2>&1; then
        ok "pm2 systemd hook installed for ${SERVICE_USER}"
      else
        warn "pm2 startup hook not installed (optional)"
      fi
    else
      fail "npm install -g pm2 failed"
    fi
  else
    fail "npm missing - PM2 not installed"
  fi

  if [[ "$DO_YARN" -eq 1 ]]; then
    step "Installing Yarn (optional)"
    if command -v npm >/dev/null 2>&1 && npm install -g --silent yarn >/dev/null 2>&1; then
      ok "yarn installed: $(yarn -v 2>/dev/null || echo '?')"
    else
      warn "yarn install failed (optional - ignored)"
    fi
  fi
fi

# =============================================================================
# 4. Remote stack: platform + dashboard as DESKTOP-ONLY units
# =============================================================================
# These units deliberately do NOT declare Requires=pov-runner and are NOT the
# order-placing engine. They make the box usable over RDP without a terminal.
if [[ "$DO_REMOTE_STACK" -eq 1 ]] && [[ -d "$INSTALL_ROOT" ]]; then
  step "Enabling the view stack at boot (NO trading engine)"
  for unit in openalgo openalgo-ws pov-dash; do
    if systemctl list-unit-files "${unit}.service" >/dev/null 2>&1 \
       && systemctl cat "${unit}.service" >/dev/null 2>&1; then
      systemctl enable "${unit}" >/dev/null 2>&1 \
        && ok "${unit}: enabled at boot" \
        || warn "${unit}: enable failed"
    else
      warn "${unit}.service not present (run install_ubuntu.sh first)"
    fi
  done

  # pov-runner must stay DISABLED. Assert it, do not assume it.
  if systemctl is-enabled pov-runner.service >/dev/null 2>&1; then
    systemctl disable pov-runner.service >/dev/null 2>&1 \
      && ok "pov-runner: disabled (order-placing engine is human-start only)" \
      || warn "pov-runner is enabled and could not be disabled - CHECK THIS"
  else
    ok "pov-runner: not enabled (correct)"
  fi
else
  [[ "$DO_REMOTE_STACK" -eq 1 ]] && warn "remote stack skipped: $INSTALL_ROOT missing"
fi

# =============================================================================
# 5. UFW firewall - default deny, SSH allowed, RDP allowed only if asked
# =============================================================================
if [[ "$DO_FIREWALL" -eq 1 ]]; then
  step "Configuring UFW"
  if ! command -v ufw >/dev/null 2>&1; then
    apt-get install -y -qq ufw >/dev/null 2>&1 || fail "ufw install failed"
  fi

  if command -v ufw >/dev/null 2>&1; then
    # Reset is intentional: a half-configured firewall from a previous run is
    # worse than a known one. Rules below are re-applied immediately.
    ufw --force reset >/dev/null 2>&1 || true
    ufw default deny incoming  >/dev/null 2>&1 || fail "ufw default deny failed"
    ufw default allow outgoing >/dev/null 2>&1 || true

    # SSH FIRST, before enabling - enabling a deny-all without 22 is the classic
    # way to lock yourself out of a VPS.
    ufw allow 22/tcp >/dev/null 2>&1 && ok "22/tcp (SSH) allowed"

    if [[ "$DO_RDP" -eq 1 ]]; then
      if [[ "$ALLOW_ANY_RDP" -eq 1 ]]; then
        ufw allow "${RDP_PORT}/tcp" >/dev/null 2>&1
        warn "RDP OPEN TO THE INTERNET ($RDP_PORT/tcp). Tunnel it over SSH or"
        warn "restrict it as soon as your IP is stable."
      elif [[ -n "$RDP_ALLOW" ]]; then
        IFS=',' read -ra _nets <<< "$RDP_ALLOW"
        for _net in "${_nets[@]}"; do
          _net="$(echo "$_net" | tr -d ' ')"
          [[ -z "$_net" ]] && continue
          ufw allow from "$_net" to any port "$RDP_PORT" proto tcp >/dev/null 2>&1 \
            && ok "RDP ${RDP_PORT}/tcp allowed from ${_net}" \
            || warn "could not add RDP rule for ${_net}"
        done
      else
        warn "RDP ${RDP_PORT}/tcp left CLOSED (no --rdp-allow given)"
        note "open it later:  sudo ufw allow from <your.ip>/32 to any port ${RDP_PORT} proto tcp"
        note "or tunnel it:   ssh -L ${RDP_PORT}:127.0.0.1:${RDP_PORT} user@server"
      fi
    fi

    # nginx serves the UI over HTTPS (443) and uses 80 for the certificate
    # challenge and the HTTP->HTTPS redirect. Open them only when the nginx
    # site is linked: the installer unlinks it when certificate issuance fails,
    # so a failed TLS setup leaves 80/443 closed.
    if [[ -e /etc/nginx/sites-enabled/my-trading-system ]]; then
      if ufw allow 80/tcp >/dev/null 2>&1 && ufw allow 443/tcp >/dev/null 2>&1; then
        ok "80/tcp and 443/tcp allowed (nginx HTTPS)"
      else
        warn "could not open 80/443 - nginx HTTPS will not be reachable"
      fi
    else
      note "nginx site not linked - 80/443 stay closed (DEPLOY_UBUNTU.md S10)"
    fi

    ufw --force enable >/dev/null 2>&1 \
      && ok "ufw enabled" \
      || fail "ufw enable failed"

    ufw status numbered | sed 's/^/          /' || true
  fi
fi

# =============================================================================
# Summary
# =============================================================================
step "Desktop / remote summary"
REMOTE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<SUMMARY

  Desktop        : XFCE ($([[ "$DO_DESKTOP" -eq 1 ]] && echo installed || echo skipped))
  Remote access  : xrdp on ${RDP_PORT}/tcp ($([[ "$DO_RDP" -eq 1 ]] && echo enabled || echo skipped))
  Browser        : Google Chrome ($([[ "$DO_CHROME" -eq 1 ]] && echo installed || echo skipped))
  Node / PM2     : $(node -v 2>/dev/null || echo 'not installed') / $(pm2 -v 2>/dev/null || echo 'not installed')
  Boot stack     : openalgo, openalgo-ws, pov-dash (engine NOT auto-started)

  Connect (Windows) : mstsc  ->  ${REMOTE_IP:-<server-ip>}:${RDP_PORT}
  Connect (macOS)   : "Microsoft Remote Desktop for Mac" -> same address
  NOTE: no WinSCP binary exists for Ubuntu - WinSCP is Windows-only.
        Use WinSCP on the LAPTOP with SFTP protocol, port 22:
          host: ${REMOTE_IP:-<server-ip>}  user: ${SERVICE_USER}  protocol: SFTP
        or install FileZilla for a Linux-side SFTP client:
          sudo apt-get install -y filezilla

  Firewall reset an SSH rule?  sudo ufw allow 22/tcp
  RDP black screen?            journalctl -u xrdp -n 60 ; check /etc/xrdp/startwm.sh
  RDP login prompt loops?      the user must be in group "ssl-cert" (done here)

SUMMARY

if [[ "$FAILURES" -gt 0 ]]; then
  fail "$FAILURES step(s) failed - see [FAIL] lines above"
  exit 1
fi
ok "desktop + remote access ready"
