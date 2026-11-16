"""The optimiser must not record a study from a leaked bar series.

WHY THIS EXISTS
---------------
`research/leakage_gate.py` was built and tested in the 2026-11-14 stage work but
called by NOTHING on the optimisation path: the engine writes its `trial` and
`optimizer_run` rows inside `search()`, so there was no gate at all. A study on
an unsorted or unstamped series therefore landed in the trial log looking exactly
like a good one.

Evidence from leaked features is worse than no evidence - it looks good and is
wrong - so the refusal has to happen BEFORE the search, which is what these tests
pin.

The tests drive `run_optimisation` with real bars, because the defect being
guarded is ordering: a gate placed after `search()` would still pass a unit test
of the gate itself.
"""
from __future__ import annotations

import json

import pytest

from quant.research.optimizer import leakage_verdict, run_optimisation

pytest.importorskip("optuna", reason="the study path needs the Optuna backend")


def _good(n: int = 30):
    """A sorted, stamped daily series - what a clean cache looks like."""
    return [{"ts": f"2025-09-{d:02d}", "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.0 + d, "volume": 1000}
            for d in range(1, n + 1)]


def _rows_in(path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()] if path.exists() else []


# --------------------------------------------------------------------------- #
# The verdict function
# --------------------------------------------------------------------------- #
def test_a_clean_series_passes_but_is_not_called_clean():
    """The shift test cannot run without a strategy's build_features.

    So a real series is INCOMPLETE, and the gate says so rather than pretending.
    An unrun check is never a pass.
    """
    out = leakage_verdict(_good())
    assert out["gate"] == "pass"
    assert out["verdict"] == "incomplete"
    assert "shift" in (out["checks_missing"] or [])
    assert out["fatal"] == 0


def test_no_bars_is_reported_as_synthetic_not_clean():
    out = leakage_verdict(None)
    assert out["verdict"] == "synthetic"
    assert out["gate"] == "n/a"


def test_an_unsorted_series_blocks():
    out = leakage_verdict([{"ts": "2025-10-07", "close": 1.5},
                           {"ts": "2025-10-06", "close": 1.6}])
    assert out["gate"] == "block"
    assert out["fatal"] >= 1


def test_a_missing_timestamp_blocks():
    out = leakage_verdict([{"close": 1.5}, {"close": 1.6}])
    assert out["gate"] == "block"


def test_an_empty_timestamp_blocks():
    out = leakage_verdict([{"ts": "", "close": 1.5},
                           {"ts": "2025-10-07", "close": 1.6}])
    assert out["gate"] == "block"


def test_duplicate_timestamps_warn_but_do_not_block():
    """The engine rates duplicates DEGRADED (a screen), not FATAL.

    Blocking on it would refuse most real caches, and a gate operators disable
    protects nothing. The caller is told the count instead.
    """
    out = leakage_verdict([{"ts": "2025-10-07", "close": 1.5},
                           {"ts": "2025-10-07", "close": 1.6}])
    assert out["gate"] == "pass"
    assert out["suspect"] >= 1


# --------------------------------------------------------------------------- #
# The ordering guarantee: refuse BEFORE anything is written
# --------------------------------------------------------------------------- #
def test_a_leaked_series_writes_nothing_to_the_trial_log(tmp_path):
    log = tmp_path / "trials.jsonl"
    result = run_optimisation(
        "synthetic_demo", "synthetic", trial_log_path=log,
        bars=[{"ts": "2025-10-07", "close": 1.5},
              {"ts": "2025-10-06", "close": 1.6}],
        engine_config={"n_trials": 5})

    assert result.ok is False
    assert "leakage gate" in (result.error or "")
    # THE POINT: no file at all, so no contaminated row can ever be read back.
    assert not log.exists(), "a refused study must not create the trial log"


def test_an_unstamped_series_writes_nothing(tmp_path):
    log = tmp_path / "trials.jsonl"
    result = run_optimisation(
        "synthetic_demo", "synthetic", trial_log_path=log,
        bars=[{"close": 1.5}, {"close": 1.6}],
        engine_config={"n_trials": 5})
    assert result.ok is False
    assert not log.exists()


def test_a_clean_series_still_records(tmp_path):
    """The gate must not block real work - the guard against over-refusing."""
    log = tmp_path / "trials.jsonl"
    result = run_optimisation(
        "synthetic_demo", "synthetic", trial_log_path=log,
        bars=_good(), engine_config={"n_trials": 6})

    assert result.leak_gate.get("gate") == "pass"
    rows = _rows_in(log)
    assert rows, "a passed study must still write its evidence"
    kinds = {r.get("kind") for r in rows}
    assert "optimizer_run" in kinds and "trial" in kinds


def test_the_leak_verdict_is_carried_on_the_result(tmp_path):
    """Provenance: a reader can tell a gated run from an ungated one."""
    result = run_optimisation(
        "synthetic_demo", "synthetic", trial_log_path=tmp_path / "t.jsonl",
        bars=_good(), engine_config={"n_trials": 3})
    d = result.to_dict()
    assert d["leak_gate"]["verdict"] == "incomplete"
    assert d["leak_gate"]["gate"] == "pass"


def test_skip_leak_gate_is_explicit_and_visible(tmp_path):
    """An override must be a deliberate act with a name, not a silent default."""
    log = tmp_path / "trials.jsonl"
    result = run_optimisation(
        "synthetic_demo", "synthetic", trial_log_path=log,
        bars=[{"ts": "2025-10-07", "close": 1.5},
              {"ts": "2025-10-06", "close": 1.6}],
        engine_config={"n_trials": 3}, skip_leak_gate=True)

    assert result.leak_gate.get("verdict") == "disabled"
    assert result.leak_gate.get("gate") == "n/a"
    assert log.exists(), "the override must actually let the study run"
