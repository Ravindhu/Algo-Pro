"""The pin covers the vendored OpenAlgo tree in CI (owner 2026-11-25).

scripts/ops/start_openalgo.py checks a folder that does not exist in this repo, so the startup check never saw the
vendored margin patch. This test fails CI if a watched vendored file changes without the pin being re-recorded.

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
