"""X-12: optimizer runs are written to the quant trial log (AGENTS: every run must be logged).

Flow stage: Research (trial logger, DSR input). Status: TEST.
Hand check: two scored trials with Sharpe 1.0 and 3.0 have sample variance 2.0
(mean 2, squared deviations 1 + 1, divided by n-1 = 1). The failed trial counts
as a trial but not in the variance.
"""
from types import SimpleNamespace

import pytest

from quant.core.blue_bridge import BlueEngineUnavailable, load
from quant.research.trial_log import TrialLog

try:
    ENG = load("optimizer.engine")
except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
    pytest.skip(f"Blue optimizer unavailable: {exc}", allow_module_level=True)


def _trial(params, sharpe, ok):
    return SimpleNamespace(params=params, mean_sharpe=sharpe,
                           n_obs=240 if ok else 0, ok=ok,
                           value=sharpe if ok else None)


def test_append_writes_trial_rows_and_one_run_row(tmp_path):
    path = tmp_path / "trial_log.jsonl"
    study = SimpleNamespace(trials=[
        _trial({"fast": 5}, 1.0, True),
        _trial({"fast": 9}, None, False),
        _trial({"fast": 7}, 3.0, True),
    ])
    cfg = SimpleNamespace(n_trials=10, n_folds=4, cost_bps=10.0, seed=7,
                          mae_penalty_per_pct=0.02, mfe_capture_weight=0.1)
    space = SimpleNamespace(to_dict=lambda: {"names": ["fast"]})

    written = ENG.append_trial_log(path, strategy="demo", study=study, cfg=cfg,
                                   space=space, backend="builtin_tpe",
                                   verdict={"ok": True})
    assert written == 4  # 3 trial rows + 1 run row

    log = TrialLog(path)
    assert log.count("demo") == 3                              # failed trial included
    assert log.sharpe_variance("demo") == pytest.approx(2.0)   # failed trial excluded

    runs = log.runs("demo")
    assert len(runs) == 1
    assert runs[0]["n_trials_run"] == 3
    assert runs[0]["n_folds"] == 4
    assert runs[0]["sampler"] == "builtin_tpe"
    assert runs[0]["bounds"] == {"names": ["fast"]}
    assert runs[0]["mae_penalty_per_pct"] == 0.02
    assert runs[0]["verdict_ok"] is True


def test_run_rows_are_not_counted_as_trials(tmp_path):
    path = tmp_path / "trial_log.jsonl"
    study = SimpleNamespace(trials=[_trial({"x": 1}, 0.5, True)])
    cfg = SimpleNamespace(n_trials=5, n_folds=4, cost_bps=10.0, seed=1,
                          mae_penalty_per_pct=0.0, mfe_capture_weight=0.0)
    ENG.append_trial_log(path, strategy="s", study=study, cfg=cfg,
                         space=None, backend="builtin_tpe", verdict={})
    assert TrialLog(path).count() == 1


def test_default_engine_config_does_not_log():
    # Off by default so test runs never write into the research log.
    assert ENG.EngineConfig().trial_log_path is None
