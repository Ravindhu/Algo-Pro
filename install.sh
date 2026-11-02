#!/usr/bin/env bash
# =============================================================================
# install.sh - ONE-COMMAND INSTALL for an Ubuntu / Debian server.
#
# Run as root. It fetches this repo if needed, then runs
# Servers/install_ubuntu.sh (packages, OpenAlgo, Python env, services, nginx,
# TLS, smoke test). All real logic stays in install_ubuntu.sh.
#
#   MTS_REF=<reviewed-tag-or-sha> sudo -E bash install.sh --domain trade.example.com --email you@example.com
#   MTS_REF=<reviewed-tag-or-sha> sudo -E bash install.sh --allow-plain-http   # LAN lab only, no TLS
#
# Or keep your options in Servers/deploy.env (copy deploy.env.example first):
#   MTS_REF=<reviewed-tag-or-sha> sudo -E bash install.sh
#
# SUPPLY-CHAIN PIN (audit 2026-11-03): this script runs code as root. MTS_REF
# must name a reviewed tag or commit SHA, so the installed code is fixed.
# Without it the script stops. MTS_ALLOW_UNPINNED=1 installs the moving
# default branch instead; use it only for throwaway lab hosts.
#
# Install location: /opt/my-trading-system. Override with MTS_ROOT=/path, and
# if deploy.env sets MTS_ROOT, export the same value before running.
#
# Safe defaults are unchanged: TLS is required unless you opt out, and no
# trading starts (the engine stays stopped until you start it).
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/Ravindhu/my-trading-system.git"
INSTALL_ROOT="${MTS_ROOT:-/opt/my-trading-system}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_REF="${MTS_REF:-}"

if [[ $EUID -ne 0 ]]; then
  echo "[ABORT] run with sudo:  sudo bash install.sh" >&2
  exit 1
fi

# Fail closed: an unpinned root install is refused unless explicitly allowed.
if [[ -z "$REPO_REF" && "${MTS_ALLOW_UNPINNED:-0}" != "1" ]]; then
  echo "[ABORT] MTS_REF is not set. Pin to a reviewed tag or commit SHA, e.g.:" >&2
  echo "        MTS_REF=<sha> sudo -E bash install.sh" >&2
  echo "        (MTS_ALLOW_UNPINNED=1 installs the moving default branch.)" >&2
  exit 1
fi
if [[ -z "$REPO_REF" ]]; then
  echo "[WARN] MTS_ALLOW_UNPINNED=1: installing the moving default branch" >&2
fi

# 1. git is needed to fetch the repo.
if ! command -v git >/dev/null 2>&1; then
  echo "==> installing git"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git ca-certificates >/dev/null
fi

# 2. Make sure the install root holds this repo, at the pinned ref.
#    Full clone (not --depth 1) so a commit SHA can be checked out.
if [[ "$SCRIPT_DIR" != "$INSTALL_ROOT" ]]; then
  if [[ -d "$INSTALL_ROOT/.git" ]]; then
    echo "==> using existing checkout: $INSTALL_ROOT"
  elif [[ -e "$INSTALL_ROOT" && -n "$(ls -A "$INSTALL_ROOT" 2>/dev/null || true)" ]]; then
    echo "[ABORT] $INSTALL_ROOT exists and is not a checkout of this repo." >&2
    echo "        Move it aside, or set MTS_ROOT to another path." >&2
    exit 1
  else
    echo "==> cloning $REPO_URL -> $INSTALL_ROOT"
    git clone "$REPO_URL" "$INSTALL_ROOT"
  fi
fi

# Check out the pinned ref. A missing or unknown ref stops the install here.
if [[ -n "$REPO_REF" && -d "$INSTALL_ROOT/.git" ]]; then
  git -C "$INSTALL_ROOT" fetch --quiet --tags origin
  git -C "$INSTALL_ROOT" checkout --quiet --detach "$REPO_REF"
  echo "==> pinned to $(git -C "$INSTALL_ROOT" rev-parse --short HEAD) ($REPO_REF)"
fi

# 3. Carry a local deploy.env across if it was not in the clone.
if [[ ! -f "$INSTALL_ROOT/Servers/deploy.env" && -f "$SCRIPT_DIR/Servers/deploy.env" ]]; then
  install -m 600 "$SCRIPT_DIR/Servers/deploy.env" "$INSTALL_ROOT/Servers/deploy.env"
fi

# 4. Hand over to the real installer, passing every flag through.
exec bash "$INSTALL_ROOT/Servers/install_ubuntu.sh" "$@"
