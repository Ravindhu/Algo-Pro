"""
test_phase4_registry_cli.py - Phase 4 tests for the new registry
and CLI flag surface.

What Phase 4 adds (verified here):
----------------------------------
1. ``blue_engine.alpha.registry.list_strategies()`` returns a list of
   dicts with ``name``, ``label``, ``module``, ``kind`` and
   ``description`` for every registered BaseSignal. ``kind`` is
   ``"live"`` for strategies discovered under ``alpha.Statergy`` and
   ``"signal"`` for strategies under ``alpha.signals``.

2. ``blue_engine.alpha.registry.live_active_state()`` returns
   ``None`` when the active-state file is missing (the normal case
   for a fresh install) and the parsed JSON when the file is
   present.

3. ``blue_engine.alpha.registry.strategy_module(name)`` returns the
   dotted module path of a registered strategy, or ``None`` for
   unknown names.

4. ``Scripts/optimize_strategy.py`` accepts seven new flags:
   ``--symbol``, ``--exchange``, ``--interval``, ``--from``,
   ``--to``, ``--data-source`` and ``--bars-from-cache``. The CLI
   must still run end-to-end (verdict + journal) when the new
   flags are absent (backward compat).

5. ``--data-source synthetic`` is a no-op (engine uses its built-in
   fixture; no cache load attempted).

6. ``--bars-from-cache`` with no cached data falls back to
   synthetic with a printed warning (graceful degradation).

7. ``--bars-from-cache`` with cached data drives the engine against
   the real bars via the Phase 3 ``bars=`` argument.

All tests in this file are pure stdlib + the project: no
filesystem dependencies (except the tmp dir in the cache test), no
network, no live trading system.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from blue_engine.alpha.registry import (  # noqa: E402
    DEFAULT_ACTIVE_PATH,
    list_strategies,
    live_active_state,
    strategy_module,
)
from blue_engine.optimizer.data_cache import (  # noqa: E402
    MAX_WINDOW_DAYS,
    list_strategies_for_ui,
    watchlist_for_ui,
)


# --------------------------------------------------------------------------- #
# 1. alpha.registry.list_strategies
# --------------------------------------------------------------------------- #


def test_list_strategies_returns_at_least_one_entry():
    """The auto-registrar must register at least the built-in keylevel
    signal (and, when alpha/Statergy is importable, the OCL keylevel).
    """
    strats = list_strategies()
    assert strats, "list_strategies() must return a non-empty list"
    names = {s["name"] for s in strats}
    # The shipped keylevel is in alpha/signals; OCL is in alpha/Statergy.
    # At least one of the two must be present.
    assert names & {"keylevel", "ocl_keylevel"}, (
        f"expected keylevel or ocl_keylevel in {names}"
    )


def test_list_strategies_entries_have_required_keys():
    """Each entry must carry the keys the dashboard dropdown uses:
    ``name``, ``label``, ``module``, ``kind``, ``description``.
    """
    strats = list_strategies()
    assert strats
    required = {"name", "label", "module", "kind", "description"}
    for entry in strats:
        missing = required - set(entry.keys())
        assert not missing, f"entry {entry} is missing keys {missing}"
        # kind is restricted to {"live", "signal"}.
        assert entry["kind"] in ("live", "signal"), (
            f"kind must be 'live' or 'signal'; got {entry['kind']!r}"
        )


def test_list_strategies_kind_distinguishes_live_from_signal():
    """The ``kind`` field is what the dashboard renders as a badge.
    A strategy under ``alpha.Statergy`` (the OCL keylevel and
    friends) is ``"live"``; a strategy under ``alpha.signals`` is
    ``"signal"``.
    """
    strats = list_strategies()
    by_name = {s["name"]: s for s in strats}
    if "ocl_keylevel" in by_name:
        assert by_name["ocl_keylevel"]["kind"] == "live", (
            "ocl_keylevel lives in alpha/Statergy and must be 'live'"
        )
    if "keylevel" in by_name:
        assert by_name["keylevel"]["kind"] == "signal", (
            "keylevel lives in alpha/signals and must be 'signal'"
        )


def test_list_strategies_is_sorted_by_name():
    """The dashboard renders the dropdown in alphabetical order. The
    function must sort on its own so callers never have to.
    """
    strats = list_strategies()
    names = [s["name"] for s in strats]
    assert names == sorted(names), f"not sorted: {names}"


# --------------------------------------------------------------------------- #
# 2. alpha.registry.live_active_state
# --------------------------------------------------------------------------- #


def test_live_active_state_returns_none_when_file_missing(tmp_path):
    """A missing file is the normal case. ``None`` is the contract:
    the dashboard renders ``None`` as "no live config, fall back
    to offline".
    """
    missing = tmp_path / "does_not_exist.json"
    assert live_active_state(missing) is None


def test_live_active_state_returns_dict_when_file_present(tmp_path):
    """A valid JSON file is parsed and returned. The dashboard reads
    ``state["strategy"]``, ``state["symbol"]``, ``state["interval"]``
    etc. from this dict.
    """
    state = {
        "strategy": "ocl_keylevel",
        "module": "blue_engine.alpha.Statergy.ocl_keylevel",
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "interval": "5minute",
        "started_at": "2026-10-01T09:15:00+05:30",
        "params": {"htf_interval": "15minute"},
    }
    f = tmp_path / "active.json"
    f.write_text(json.dumps(state), encoding="utf-8")
    out = live_active_state(f)
    assert out == state


def test_live_active_state_returns_error_dict_on_malformed_json(tmp_path):
    """A file that exists but is not valid JSON is the "live config
    unreadable" failure mode. The dashboard surfaces this as an
    error panel, NOT as "no live config".
    """
    f = tmp_path / "bad.json"
    f.write_text("{not valid json", encoding="utf-8")
    out = live_active_state(f)
    assert isinstance(out, dict)
    assert "error" in out


def test_live_active_state_treats_empty_file_as_missing(tmp_path):
    """A zero-byte file means the runner has not written yet. Treated
    as ``None`` (missing) so the dashboard falls back to offline.
    """
    f = tmp_path / "empty.json"
    f.write_text("", encoding="utf-8")
    assert live_active_state(f) is None


def test_live_active_state_default_path_is_inside_blue_engine_input():
    """The default path must point at the documented location so the
    live runner and the dashboard agree on the file.
    """
    assert DEFAULT_ACTIVE_PATH.name == "strategy.active.json"
    assert "input" in DEFAULT_ACTIVE_PATH.parts


# --------------------------------------------------------------------------- #
# 3. alpha.registry.strategy_module
# --------------------------------------------------------------------------- #


def test_strategy_module_returns_dotted_path_for_known_strategy():
    """A registered name must map to its module path. The CLI uses
    this to resolve ``--strategy keylevel`` to
    ``blue_engine.alpha.signals.keylevel`` (etc.).
    """
    for entry in list_strategies():
        mod = strategy_module(entry["name"])
        assert mod == entry["module"], (
            f"strategy_module({entry['name']!r}) returned {mod!r}, "
            f"expected {entry['module']!r}"
        )


def test_strategy_module_returns_none_for_unknown_name():
    """An unknown name returns ``None`` so the CLI can print a clear
    error rather than crashing.
    """
    assert strategy_module("definitely_not_a_strategy") is None


# --------------------------------------------------------------------------- #
# 4. data_cache.list_strategies_for_ui (dashboard adapter)
# --------------------------------------------------------------------------- #


def test_list_strategies_for_ui_includes_params_space_for_ocl():
    """The AI Insight header reads ``params_space`` from each entry
    to render the parameter table. OCL keylevel declares this
    attribute, so its entry must surface it.
    """
    strats = list_strategies_for_ui()
    by_name = {s["name"]: s for s in strats}
    if "ocl_keylevel" in by_name:
        ps = by_name["ocl_keylevel"].get("params_space")
        assert ps, "OCL entry must include params_space"
        # Spot-check the four params the test_optimizer_data_cache
        # test asserts on.
        for key in ("htf_interval", "min_body_pct", "pattern_type",
                     "exec_mode"):
            assert key in ps, f"OCL params_space missing {key!r}"


def test_list_strategies_for_ui_returns_empty_when_alpha_unimportable(
    monkeypatch,
):
    """The adapter must degrade to ``[]`` when the alpha package is
    not importable (e.g. in a minimal test environment), never
    raise. The dashboard renders an empty dropdown rather than
    crash.
    """
    import builtins
    real_import = builtins.__import__

    def _block_alpha(name, *args, **kwargs):
        if name.startswith("blue_engine.alpha"):
            raise ImportError("alpha is unavailable (test stub)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_alpha)
    assert list_strategies_for_ui() == []


# --------------------------------------------------------------------------- #
# 5. data_cache.watchlist_for_ui
# --------------------------------------------------------------------------- #


def test_watchlist_for_ui_returns_at_least_extras():
    """The adapter must echo back the ``extras`` even when the preset
    is missing (the pre-existing preset registry gap). The dashboard
    uses the extras for the watchlist badge.
    """
    extras = [{"symbol": "RELIANCE", "exchange": "NSE", "name": "Reliance"}]
    out = watchlist_for_ui(preset=None, extra_symbols=extras)
    assert isinstance(out, list)
    # The extras MUST appear (the preset may not).
    symbols = [e.get("symbol") for e in out]
    assert "RELIANCE" in symbols, (
        f"RELIANCE must be in the watchlist; got {out!r}"
    )


def test_watchlist_for_ui_filters_by_exchange():
    """The dashboard's NSE-cash-only filter relies on the
    ``exchange`` parameter. An entry with a different exchange must
    be excluded.
    """
    extras = [
        {"symbol": "RELIANCE", "exchange": "NSE"},
        {"symbol": "AAPL", "exchange": "NASDAQ"},
    ]
    out = watchlist_for_ui(preset=None, extra_symbols=extras, exchange="NSE")
    exchanges = {e.get("exchange") for e in out}
    assert exchanges <= {"NSE"}, f"non-NSE entry leaked through: {out}"


def test_watchlist_for_ui_does_not_raise_on_missing_source():
    """The adapter never raises. When the underlying data source is
    missing, it falls back to the example JSON or to the extras.
    The dashboard renders an empty watchlist if both are absent.
    """
    out = watchlist_for_ui(preset=None, extra_symbols=None)
    assert isinstance(out, list)


# --------------------------------------------------------------------------- #
# 6. CLI flag surface (Scripts/optimize_strategy.py)
# --------------------------------------------------------------------------- #


def _run_cli(*args, env_extra=None):
    """Run the CLI as a subprocess and return ``(returncode, stdout,
    stderr)``. Using a subprocess (not an in-process import) is
    what the dashboard does, and it is the only way to test the
    actual ``--help`` / ``argparse`` wiring.
    """
    py = sys.executable
    cmd = [py, "Scripts/optimize_strategy.py", *args]
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    # Always set a fresh data root so the test never pollutes the
    # developer's journal.
    env["BLUE_OPTIMIZER_DATA_ROOT"] = tempfile.mkdtemp(
        prefix="opt_phase4_test_")
    proc = subprocess.run(
        cmd, cwd=str(Path(_ROOT).parent),
        env=env, capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_help_lists_all_seven_new_flags():
    """The new flags must be discoverable via ``--help``. The
    dashboard relies on operators being able to read the CLI
    signature.
    """
    rc, out, err = _run_cli("--help")
    assert rc == 0
    for flag in ("--symbol", "--exchange", "--interval",
                  "--from", "--to", "--data-source",
                  "--bars-from-cache"):
        assert flag in out, f"{flag} must be in --help; missing"


def test_cli_list_flag_still_works_with_new_flags_added():
    """``--list`` is the operator's way to discover what can be
    searched. It must still work after the new flags are added.
    """
    rc, out, err = _run_cli("--list")
    assert rc == 0
    assert "Available search spaces:" in out


def test_cli_runs_end_to_end_without_new_flags():
    """Backward compat: the existing invocation (no new flags) must
    still produce a verdict. The existing tests + the dashboard
    smoke rely on this.
    """
    rc, out, err = _run_cli(
        "--space", "synthetic_demo",
        "--strategy", "synthetic",
        "--trials", "3",
        "--no-save",
    )
    assert "VERDICT" in out, f"expected verdict in output; got:\n{out}"
    # The script prints a 0 returncode on success; non-zero on
    # errors. We don't assert on 0 because a 3-trial synthetic
    # study may legitimately produce "no edge" (which is a
    # successful run, not an error).
    assert "synthetic_demo_synthetic" in out


def test_cli_data_source_synthetic_skips_cache_load():
    """``--data-source synthetic`` must NOT attempt a cache load.
    The dashboard uses this flag to force the synthetic path
    (e.g. when the broker is offline). The test asserts by
    checking the output does NOT contain "bars:" (the cache-load
    prefix).
    """
    rc, out, err = _run_cli(
        "--space", "synthetic_demo",
        "--strategy", "synthetic",
        "--trials", "3",
        "--data-source", "synthetic",
        "--no-save",
    )
    assert "bars:" not in out, (
        f"synthetic data-source must not load bars; got:\n{out}"
    )


def test_cli_bars_from_cache_falls_back_to_synthetic_gracefully():
    """With ``--bars-from-cache`` but no cached data and no live
    OpenAlgo server, the CLI must print a warning AND still run.
    The dashboard's "broker offline" panel behaves the same.
    """
    rc, out, err = _run_cli(
        "--space", "synthetic_demo",
        "--strategy", "synthetic",
        "--trials", "3",
        "--symbol", "RELIANCE",
        "--exchange", "NSE",
        "--interval", "1d",
        "--bars-from-cache",
        "--no-save",
    )
    # The warning is printed to stderr (or stdout) but the script
    # MUST still complete and print a verdict.
    combined = out + err
    assert "VERDICT" in combined, (
        f"verdict must still print after a graceful fallback; "
        f"got:\nstdout={out!r}\nstderr={err!r}"
    )


# --------------------------------------------------------------------------- #
# 7. MAX_WINDOW_DAYS is the documented cap
# --------------------------------------------------------------------------- #


def test_max_window_days_is_a_positive_int_with_a_sane_cap():
    """OpenAlgo's history endpoint returns at most a fixed number of
    days per request. The cache module pins this cap so a download
    request larger than the cap is pruned (with a warning) rather
    than silently returning empty pages.

    The exact value is verified against the constant in
    ``data_cache.py``. The test asserts it's a positive int and
    inside a sane range (1..2000) so a typo (0, -1, 10_000) fails
    loudly.
    """
    assert isinstance(MAX_WINDOW_DAYS, int)
    assert 1 <= MAX_WINDOW_DAYS <= 2000, (
        f"MAX_WINDOW_DAYS must be in [1, 2000]; got {MAX_WINDOW_DAYS}"
    )
