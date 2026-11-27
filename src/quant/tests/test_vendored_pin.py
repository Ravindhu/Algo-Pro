"""The pin covers the vendored OpenAlgo tree in CI (owner 2026-11-25).

scripts/ops/start_openalgo.py used to check a folder that does not exist in this repo, so the startup check never
saw the vendored margin patch. Fixed 2026-11-27: the launcher's OPENALGO_DIR now points at
src/quant/openalgo_platform, the same folder scripts/verify/upgrade_check.py pins, and
test_launcher_checks_the_vendored_tree below fails if the two ever drift back apart.
This test fails CI if a watched vendored file changes without the pin being re-recorded.

Flow stage: Adapters (broker product mapping)
Status: PHASE 3 IMPLEMENTED
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
V = ROOT / "src/quant/openalgo_platform"


def _pin_module():
    spec = importlib.util.spec_from_file_location("upgrade_check", ROOT / "scripts/verify/upgrade_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vendored_tree_matches_pin():
    mod = _pin_module()
    rolled = mod.committed_fingerprint()["_rolled"]  # committed HEAD, not the working copy
    assert mod.read_pin() == rolled, (
        "vendored OpenAlgo changed without a pin update: review the diff, then run "
        "scripts/verify/upgrade_check.py --update-pin"
    )


def test_launcher_checks_the_vendored_tree():
    """The launcher and the pin check must name the SAME folder (fixed 2026-11-27).

    Regression guard for the defect above: start_openalgo.py resolved ROOT/'openalgo', a
    git-ignored folder that never exists in a checkout, so the startup pin check silently
    never ran against the tree this repo actually ships.
    """
    import ast

    mod = _pin_module()
    # utf-8-sig: start_openalgo.py carries a BOM, which ast.parse rejects as U+FEFF.
    source = (ROOT / "scripts" / "ops" / "start_openalgo.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)

    # Find `OPENALGO_DIR = ROOT / "a" / "b" / ...` and fold the literal parts.
    parts = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "OPENALGO_DIR" for t in node.targets):
            parts = []
            cur = node.value
            while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
                if isinstance(cur.right, ast.Constant):
                    parts.insert(0, cur.right.value)
                cur = cur.left
            break

    assert parts is not None, "OPENALGO_DIR assignment not found in start_openalgo.py"
    assert pathlib.Path(*parts) == pathlib.Path(mod.VENDORED_REL), (
        f"launcher checks {pathlib.Path(*parts)} but the pin covers {mod.VENDORED_REL}"
    )
