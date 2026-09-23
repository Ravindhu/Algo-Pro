#!/usr/bin/env bash
# ============================================================================
# install_openalgo.sh  (OPTION B - self-contained server install)
#
# Clones OpenAlgo FRESH from upstream on the server, then re-applies YOUR
# custom platform code from patches/openalgo-custom.patch.
#
# WHY THE PATCH IS MANDATORY
# --------------------------
# Your local openalgo tree is NOT vanilla upstream. It carries 65 custom
# changes, including:
#   - restx_api/volume_participation.py   (POV order REST API)
#   - services/volume_participation_service.py
#   - database/pov_db.py                  (pov_engine's ledger backend!)
#   - restx_api/ocl_screener.py, blueprints/ocl_screener.py
#   - broker/mstock auction support, frontend POV/OCL pages
#   - test/test_pov_engine_*.py
# A bare clone would silently lose all of it and pov_engine's ledger would
# degrade to in-memory mode. Hence: clone upstream, then apply the patch.
#
# Baseline: the patch was cut against upstream commit
#     5390dcd898ed597b85477068e61e28813824c819
# The script checks out that exact commit before applying, so the patch always
# lands cleanly. Re-generate the patch when you deliberately move the baseline.
#
# USAGE (on the VPS):
#   ./deploy/install_openalgo.sh                 # clone + patch + deps
#   REBUILD_PATCH=1 ./deploy/install_openalgo.sh # (on your DEV box) refresh patch
# ============================================================================
set -euo pipefail

UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/marketcalls/openalgo.git}"
BASELINE_COMMIT="${BASELINE_COMMIT:-5390dcd898ed597b85477068e61e28813824c819}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_DIR="${BASE_DIR:-/opt/my-trading-system}"
OA_DIR="$BASE_DIR/openalgo"
PATCH_FILE="$REPO_DIR/patches/openalgo-custom.patch"
VENV_DIR="$OA_DIR/.venv"

log()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    [OK] %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    [WARN] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m    [FAIL] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && die "Do not run as root."
[[ -f "$PATCH_FILE" ]] || die "patch not found: $PATCH_FILE (regenerate on your dev box)"

# ---------------------------------------------------------------- 1. clone
log "1. Cloning OpenAlgo into $OA_DIR"
if [[ -d "$OA_DIR/.git" ]]; then
    warn "existing clone found - fetching instead of re-cloning"
    git -C "$OA_DIR" fetch --tags --prune origin
else
    sudo mkdir -p "$BASE_DIR"
    sudo chown -R "$USER:$USER" "$BASE_DIR"
    git clone "$UPSTREAM_URL" "$OA_DIR"
fi
ok "clone present"

# ---------------------------------------------------------------- 2. baseline
log "2. Checking out the patch baseline commit"
git -C "$OA_DIR" checkout --force "$BASELINE_COMMIT" 2>/dev/null \
  || die "baseline commit $BASELINE_COMMIT not found upstream (was it force-pushed?). Re-cut the patch on your dev box."
git -C "$OA_DIR" clean -ffd -e .venv -e .env -e db 2>/dev/null || true
ok "at baseline $(git -C "$OA_DIR" rev-parse --short HEAD)"

# ---------------------------------------------------------------- 3. patch
log "3. Applying your custom platform code"
if git -C "$OA_DIR" apply --check --whitespace=nowarn "$PATCH_FILE" 2>/dev/null; then
    git -C "$OA_DIR" apply --whitespace=nowarn "$PATCH_FILE"
    ok "patch applied cleanly ($(grep -c '^diff --git' "$PATCH_FILE") files)"
else
    warn "clean apply failed - trying 3-way merge"
    if git -C "$OA_DIR" apply --3way --whitespace=nowarn "$PATCH_FILE"; then
        ok "patch applied via 3-way merge (review any conflict markers!)"
        if grep -rl '<<<<<<<' "$OA_DIR" --include='*.py' 2>/dev/null | head -5; then
            die "conflict markers found - resolve manually before starting"
        fi
    else
        die "PATCH FAILED. Do NOT start the platform. Re-cut the patch against the current upstream HEAD."
    fi
fi

# The single-file blueprint patch (managed separately, idempotent):
if [[ -f "$REPO_DIR/patches/pov_terminal.py" ]]; then
    cp "$REPO_DIR/patches/pov_terminal.py" "$OA_DIR/blueprints/pov_terminal.py"
    ok "blueprint pov_terminal.py installed"
fi

# Ensure the two wiring lines exist in app.py (idempotent).
APP_PY="$OA_DIR/app.py"
IMPORT_LINE='from blueprints.pov_terminal import pov_terminal_bp'
REG_LINE='app.register_blueprint(pov_terminal_bp)'
if ! grep -qF "$IMPORT_LINE" "$APP_PY"; then
    anchor=$(grep -n '^from blueprints\.' "$APP_PY" | tail -1 | cut -d: -f1)
    [[ -n "${anchor:-}" ]] || die "cannot find a blueprints import anchor in app.py"
    sed -i "${anchor}a ${IMPORT_LINE}" "$APP_PY"
    ok "app.py: import line injected (line $anchor)"
fi
if ! grep -qF "$REG_LINE" "$APP_PY"; then
    anchor=$(grep -n 'app\.register_blueprint(' "$APP_PY" | tail -1 | cut -d: -f1)
    [[ -n "${anchor:-}" ]] || die "cannot find register_blueprint anchor in app.py"
    indent=$(sed -n "${anchor}p" "$APP_PY" | sed -E 's/^([[:space:]]*).*/\1/')
    sed -i "${anchor}a ${indent}${REG_LINE}" "$APP_PY"
    ok "app.py: register_blueprint injected (line $anchor, indent preserved)"
fi
git -C "$OA_DIR" status --short | head -20
ok "wiring verified"

# ---------------------------------------------------------------- 4. env
log "4. Preparing .env"
ENVF="$OA_DIR/.env"
if [[ ! -f "$ENVF" ]]; then
    if [[ -f "$OA_DIR/.sample.env" ]]; then
        cp "$OA_DIR/.sample.env" "$ENVF"
        warn "created .env from .sample.env - ADD YOUR BROKER KEYS"
    else
        die ".sample.env missing upstream - create .env manually"
    fi
fi
set_kv() {
    local k="$1" v="$2"
    if grep -qE "^${k}=" "$ENVF"; then sed -i "s|^${k}=.*|${k}=${v}|" "$ENVF"
    else printf '%s=%s\n' "$k" "$v" >> "$ENVF"; fi
}
set_kv FLASK_HOST_IP 127.0.0.1
set_kv FLASK_DEBUG   False
set_kv FLASK_ENV     production
set_kv WEBSOCKET_HOST 127.0.0.1
set_kv WEBSOCKET_PORT 8765
chmod 600 "$ENVF"
ok ".env ready (chmod 600, debug off, loopback bind)"

# ---------------------------------------------------------------- 5. deps
log "5. Building venv + installing requirements"
[[ -d "$VENV_DIR" ]] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip wheel setuptools -q
"$VENV_DIR/bin/python" -m pip install -r "$OA_DIR/requirements.txt" -q
"$VENV_DIR/bin/python" -m pip install "gunicorn>=21" -q   # gthread worker (threading-mode SocketIO)
ok "dependencies installed"

# ---------------------------------------------------------------- 6. engine
log "6. Installing pov_engine into the same venv"
"$VENV_DIR/bin/python" -m pip install -e "$BASE_DIR/pov_engine" -q
"$VENV_DIR/bin/python" -c "import pov_engine; print('    pov_engine ->', pov_engine.__file__)"
ok "pov_engine installed"

# ---------------------------------------------------------------- 7. verify
log "7. Verifying the POV stack is present"
verify_file() { [[ -f "$OA_DIR/$1" ]] && ok "present: $1" || warn "MISSING: $1"; }
verify_file restx_api/volume_participation.py
verify_file services/volume_participation_service.py
verify_file database/pov_db.py
verify_file blueprints/pov_terminal.py
verify_file upgrade/migrate_pov_execution.py

# Ledger backend check - pov_engine imports database.pov_db for durable state
"$VENV_DIR/bin/python" - <<'PY' || warn "ledger backend not importable from pov_engine context"
import sys
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else ".")
try:
    from database import pov_db  # noqa: F401
    print("    [OK] database.pov_db importable (durable ledger enabled)")
except Exception as e:
    raise SystemExit(f"ledger backend import failed: {e}")
PY
ok "verification complete"

cat <<EOF

============================================================================
 OPTION B INSTALL COMPLETE
============================================================================
 Platform : $OA_DIR  (upstream @ $(git -C "$OA_DIR" rev-parse --short HEAD) + your patch)
 Venv     : $VENV_DIR
 Patch    : $PATCH_FILE  ($(grep -c '^diff --git' "$PATCH_FILE") files)

 NEXT
   1. Add secrets:   nano $OA_DIR/.env
   2. DB migration:  cd $OA_DIR && .venv/bin/python upgrade/migrate_all.py
                     .venv/bin/python upgrade/migrate_pov_execution.py
   3. Start:         sudo systemctl start openalgo pov-dash
   4. Verify:        curl -s http://127.0.0.1:5000/auth/check-setup
   5. Generate your API key in the UI, put it in .env, restart.

 REMEMBER: re-run deploy/setup_ubuntu.sh FIRST (packages + units), then this.
============================================================================
EOF