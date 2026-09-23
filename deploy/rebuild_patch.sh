#!/usr/bin/env bash
# ============================================================================
# rebuild_patch.sh - Regenerate patches/openalgo-custom.patch from your DEV box
#
# Run this on the machine where your customized openalgo tree lives, whenever
# you change custom platform code (POV REST API, pov_db, OCL screener, etc.).
# Then commit the refreshed patch and deploy it.
#
#   ./deploy/rebuild_patch.sh
#
# It records the current upstream HEAD as the new baseline and writes it to
# deploy/.baseline so install_openalgo.sh picks it up automatically.
# ============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OA_DIR="${OA_DIR:-$REPO_DIR/openalgo}"
PATCH_FILE="$REPO_DIR/patches/openalgo-custom.patch"
BASELINE_FILE="$REPO_DIR/deploy/.baseline"

log() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
ok()  { printf '\033[1;32m    [OK] %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m    [FAIL] %s\033[0m\n' "$*" >&2; exit 1; }

[[ -d "$OA_DIR/.git" ]] || die "no git repo at $OA_DIR"

# Guard: never bake secrets into the patch.
for f in .env .sample.env; do
    if git -C "$OA_DIR" status --short | grep -qE "^.. ${f}$"; then
        die "$f is modified - REFUSING to cut a patch containing secrets. Revert it first."
    fi
done

log "Staging all local changes"
git -C "$OA_DIR" add -A

log "Cutting binary-safe patch"
git -C "$OA_DIR" diff --cached --binary HEAD > "$PATCH_FILE"
git -C "$OA_DIR" reset -q

COUNT=$(grep -c '^diff --git' "$PATCH_FILE" || true)
SIZE=$(du -h "$PATCH_FILE" | cut -f1)
git -C "$OA_DIR" rev-parse HEAD > "$BASELINE_FILE"

ok "patch: $PATCH_FILE ($COUNT files, $SIZE)"
ok "baseline: $(cat "$BASELINE_FILE")"

log "Sanity: verify the patch applies to a pristine baseline"
TMP=$(mktemp -d)
if git -C "$OA_DIR" worktree add -q --detach "$TMP/wt" HEAD 2>/dev/null; then
    if git -C "$TMP/wt" apply --check --whitespace=nowarn "$PATCH_FILE" 2>/dev/null; then
        ok "patch applies cleanly to a fresh checkout of $BASELINE_FILE"
    else
        printf '\033[1;33m    [WARN] patch does not apply cleanly to a pristine baseline\033[0m\n'
    fi
    git -C "$OA_DIR" worktree remove --force "$TMP/wt" 2>/dev/null || true
fi
rm -rf "$TMP"

log "NOW: update BASELINE_COMMIT in deploy/install_openalgo.sh if it changed:"
echo "    $(cat "$BASELINE_FILE")"