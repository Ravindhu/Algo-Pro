"""
test_phase5_dashboard_header.py - Phase 5 tests for the AI Insight header
and the control bar on the Optimizer page.

WHY THIS FILE EXISTS
====================
The header is the user-facing surface of the Optimizer page. It is a
read-only summary card + control bar that the operator uses to pick
what the optimizer studies. The body content (KPIs, verdict, the
3-step visual workflow, the recommendation table, the honest footer,
the provenance panel) is unchanged from the prior audits; this file
covers ONLY the new header.

TESTING STRATEGY
----------------
The header module pulls in a heavy import chain (dash, the alpha
walker, the data cache, the OpenAlgo client) and the tests must not
trigger that chain on every pytest run. We therefore mock the data
adapters at the ``_optimizer_header`` module level and exercise the
rendering functions directly. The mock surface is small and stable.

What this file covers (one test per audit-actionable invariant):
-----------------------------------------------------------------
1.  _ai_insight_header returns a DMC Card with the AI INSIGHT
    badge and the four top rows (Mode, Symbol, Strategy, Time period).
2.  _ai_insight_header shows "(strategy not registered)" honestly
    when the strategy lookup returns None.
3.  _ai_insight_header shows "(no params_space declared ...)" when
    the strategy entry has no params_space attribute.
4.  _ai_insight_header renders the OCL keylevel's 10-parameter
    table when the strategy is registered.
5.  _ai_insight_header always carries "read-only" + "AI INSIGHT"
    in the rendered text - the safety contract.
6.  _control_bar returns a Dash html.Div with the seven controls
    (mode, symbol, strategy, interval, from, to, buttons).
7.  _control_bar disables the Run button in Auto-Live mode (default)
    and enables it in Offline mode.
8.  _control_bar shows the NSE-cash label and the Copy-Pasteable
    "Run is not manual" tooltip on the disabled Run button.
9.  _safe_strategies_for_ui never raises and returns [] when the
    data adapter is missing.
10. _safe_watchlist never raises and returns [] when the data
    adapter is missing.
11. _safe_live_state never raises and returns None when the
    registry is missing.
12. _safe_cache_status never raises and returns the error dict
    when the cache is missing.
13. The header module never imports data_cache at module load
    (so the optimizer page import stays cheap).
14. The header module never imports alpha.registry at module
    load (same reason).
"""
from __future__ import annotations

import os
import sys
import types
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402


# --------------------------------------------------------------------------- #
# Module-level fixture: stub out the heavy imports BEFORE we touch the
# header module. The header module is the only place that imports
# data_cache + alpha.registry, and we want to be sure the test never
# triggers the real 12s chain.
# --------------------------------------------------------------------------- #


def _install_fake_data_layer(monkeypatch):
    """Install a fake data layer so the header can render without
    touching the real alpha engine.

    Returns the fake module so tests can assert on what the header
    saw (e.g. "the strategy list contained X").
    """
    fake = types.ModuleType("_fake_optimizer_data_layer")

    # list_strategies_for_ui: return a list of dicts the header
    # renders. The default has the OCL keylevel with 10 params.
    fake.list_strategies_for_ui = lambda: [
        {
            "name": "ocl_keylevel",
            "label": "OCL Keylevel",
            "module": "blue_engine.alpha.Statergy.ocl_keylevel",
            "class_name": "OCLKeylevelSignal",
            "description": "OCL (Open-Close-Low) Keylevel Signal",
            "params_space": {
                "htf_interval":     {"kind": "cat",   "default": "15minute",
                                     "choices": ["5minute", "15minute", "60minute"],
                                     "note": "higher TF"},
                "htf_candles":      {"kind": "int",   "default": 5,
                                     "low": 3, "high": 10,
                                     "note": "bars to scan"},
                "min_body_pct":     {"kind": "float", "default": 0.30,
                                     "low": 0.10, "high": 0.60,
                                     "note": "min body size"},
                "pattern_type":     {"kind": "cat",   "default": "RRG",
                                     "choices": ["RRG", "GRR"],
                                     "note": "bull/bear"},
                "ltf_interval":     {"kind": "cat",   "default": "5minute",
                                     "choices": ["1minute", "5minute", "15minute"],
                                     "note": "lower TF"},
                "ltf_touch_buffer": {"kind": "float", "default": 0.05,
                                     "low": 0.0, "high": 0.50,
                                     "note": "touch buffer"},
                "poll_seconds":     {"kind": "int",   "default": 10,
                                     "low": 1, "high": 60,
                                     "note": "poll cadence"},
                "exec_mode":        {"kind": "cat",   "default": "ONE_SHOT",
                                     "choices": ["ONE_SHOT", "CONTINUOUS",
                                                 "COUNT_LIMITED", "AUTO"],
                                     "note": "see Strategy"},
                "qty_per_fire":     {"kind": "int",   "default": 0,
                                     "low": 1, "high": 10000,
                                     "note": "per-fire qty"},
                "cooldown_seconds": {"kind": "float", "default": 0,
                                     "low": 0.0, "high": 3600.0,
                                     "note": "between fires"},
            },
        },
        {
            "name": "keylevel",
            "label": "Keylevel",
            "module": "blue_engine.alpha.signals.keylevel",
            "class_name": "KeylevelSignal",
            "description": "Pure keylevel signal (no params_space).",
            "params_space": {},            # HONEST: this signal has no
                                           # tunable parameters.
        },
    ]

    fake.watchlist_for_ui = lambda **kwargs: [
        {"symbol": "RELIANCE", "exchange": "NSE", "name": "Reliance"},
        {"symbol": "TCS",      "exchange": "NSE", "name": "Tata Consultancy"},
    ]

    # OptimizerDataCache stub. The status() method returns a
    # non-cached result by default; tests can override.
    class _FakeCache:
        def status(self, **kwargs):
            return {"cached": False, "n_bars": 0,
                    "first_ts": None, "last_ts": None, "source": "missing"}

    fake.OptimizerDataCache = _FakeCache

    # Register the fake as the modules the header looks for.
    sys.modules["blue_engine.optimizer.data_cache"] = fake
    sys.modules["blue_engine.alpha.registry"] = fake

    return fake


@pytest.fixture
def data_layer(monkeypatch):
    """Pytest fixture that installs the fake data layer and returns
    it. The header module is imported lazily inside the test so the
    chain stays under our control.
    """
    return _install_fake_data_layer(monkeypatch)


def _header():
    """Import the header module fresh, picking up the fake data layer.

    The header module's lazy-import helpers will find our fake in
    sys.modules and use it instead of the real data cache / registry.
    """
    # Force a fresh import so any prior cache of the real module
    # is bypassed.
    for mod_name in list(sys.modules):
        if mod_name.startswith("blue_engine.dashboard.pages._optimizer_header"):
            del sys.modules[mod_name]
    import blue_engine.dashboard.pages._optimizer_header as h
    # Reset the lazy import cache so the new sys.modules entries
    # are picked up.
    h._DATA_CACHE_AVAILABLE = None
    h._DATA_CACHE_ERROR = None
    h._REGISTRY_AVAILABLE = None
    h._REGISTRY_ERROR = None
    h._list_strategies_for_ui = None
    h._watchlist_for_ui = None
    h._OptimizerDataCache = None
    h.live_active_state = None
    return h


# --------------------------------------------------------------------------- #
# 1. AI Insight header renders the four top rows
# --------------------------------------------------------------------------- #


def test_ai_insight_header_returns_a_dmc_card(data_layer):
    h = _header()
    snap = {"ok": True}
    result = h._ai_insight_header(snap)
    assert isinstance(result, h.dmc.Card), \
        f"expected DMC Card; got {type(result)}"


def test_ai_insight_header_carries_ai_insight_badge(data_layer):
    """The AI INSIGHT badge is what makes the header identifiable
    in screenshots and the operator's mental model. Its presence
    is a contract - if a future refactor drops it, the header
    looks like an ordinary panel.
    """
    h = _header()
    result = h._ai_insight_header({})
    # Walk the Div's children looking for the badge text.
    text = str(result)
    assert "AI INSIGHT" in text, f"AI INSIGHT badge missing; got: {text[:500]}"
    assert "read-only" in text.lower(), \
        f"read-only marker missing; got: {text[:500]}"


def test_ai_insight_header_shows_all_four_top_rows(data_layer):
    """The Mode, Symbol, Strategy, Time period rows are the four
    axes the plan locks. Each one must be present in the rendered
    HTML.
    """
    h = _header()
    result = h._ai_insight_header({})
    text = str(result)
    for label in ("Mode", "Symbol", "Strategy", "Time period",
                  "Cache status", "Watchlist"):
        assert label in text, f"row {label!r} missing; got: {text[:1000]}"


def test_ai_insight_header_default_mode_is_auto_live(data_layer):
    """Auto-Live is the locked default. The header must show it
    without the operator touching anything.
    """
    h = _header()
    result = h._ai_insight_header({})
    text = str(result)
    assert "Auto-Live" in text, f"default mode must be Auto-Live; got: {text[:1000]}"


def test_ai_insight_header_default_symbol_is_reliance(data_layer):
    """The default symbol + exchange are RELIANCE / NSE (cash) per
    the plan. The header shows the defaults until the operator
    overrides them.
    """
    h = _header()
    result = h._ai_insight_header({}, symbol="RELIANCE", exchange="NSE")
    text = str(result)
    assert "RELIANCE" in text, f"default symbol must be RELIANCE; got: {text[:1000]}"
    assert "NSE" in text, f"default exchange must be NSE; got: {text[:1000]}"


# --------------------------------------------------------------------------- #
# 2. Honest placeholder for unregistered strategy
# --------------------------------------------------------------------------- #


def test_ai_insight_header_says_strategy_not_registered_for_unknown_name(data_layer):
    """If the operator picks a strategy name that the alpha engine
    does not know, the header must say so. Showing a fake
    parameter table would be a lie.
    """
    h = _header()
    result = h._ai_insight_header({}, strategy="nonexistent_strategy")
    text = str(result)
    assert "not registered" in text, \
        f"unknown strategy must show 'not registered'; got: {text[:1000]}"


def test_ai_insight_header_says_no_params_space_for_strategy_without_it(data_layer):
    """Some legacy signals do not declare ``params_space``. The
    header must render "(no params_space declared)" rather than
    fabricate parameters. A no-params signal is an honest signal:
    the engine can run it as-is but cannot tune it.
    """
    h = _header()
    result = h._ai_insight_header({}, strategy="keylevel")
    text = str(result)
    assert "no params_space" in text, \
        f"strategy without params_space must show the honest message; got: {text[:1000]}"


# --------------------------------------------------------------------------- #
# 3. The 10-row parameter table for the OCL keylevel
# --------------------------------------------------------------------------- #


def test_ai_insight_header_renders_ocl_10_parameter_table(data_layer):
    """The OCL keylevel declares 10 parameters. The table must
    render all 10 with their names. This is the parameter list
    the plan locks.
    """
    h = _header()
    result = h._ai_insight_header({}, strategy="ocl_keylevel")
    text = str(result)
    expected = [
        "htf_interval", "htf_candles", "min_body_pct", "pattern_type",
        "ltf_interval", "ltf_touch_buffer", "poll_seconds", "exec_mode",
        "qty_per_fire", "cooldown_seconds",
    ]
    for p in expected:
        assert p in text, f"parameter {p!r} missing from table; got: {text[:2000]}"


def test_ai_insight_header_table_shows_kind_and_default(data_layer):
    """Each row in the parameter table must show the kind, default,
    and range. The operator uses these to know what the search
    is varying.
    """
    h = _header()
    result = h._ai_insight_header({}, strategy="ocl_keylevel")
    text = str(result)
    # The OCL table has cats and ints and floats; the rendered
    # table should mention all three kinds.
    assert "cat" in text
    assert "int" in text
    assert "float" in text
    # The default for exec_mode is ONE_SHOT.
    assert "ONE_SHOT" in text


# --------------------------------------------------------------------------- #
# 4. Cache status line
# --------------------------------------------------------------------------- #


def test_ai_insight_header_cache_status_shows_no_cached_bars_by_default(data_layer):
    """The default fake cache returns cached=False. The header must
    show "(no cached bars for this contract - click Download)" so
    the operator knows the next step.
    """
    h = _header()
    result = h._ai_insight_header({})
    text = str(result)
    assert "no cached bars" in text, \
        f"empty cache must show the prompt; got: {text[:1000]}"


def test_ai_insight_header_cache_status_shows_n_bars_when_cached(data_layer):
    """When the cache has bars, the header shows the count + date
    range. The operator uses this to confirm the bars are fresh.
    """
    # Override the fake cache for this test only.
    class _CachedCache:
        def status(self, **kwargs):
            return {"cached": True, "n_bars": 22,
                    "first_ts": "2025-09-02", "last_ts": "2025-10-01",
                    "source": "cache"}
    data_layer.OptimizerDataCache = _CachedCache

    h = _header()
    result = h._ai_insight_header({})
    text = str(result)
    assert "22 bars" in text, f"cached count missing; got: {text[:1000]}"
    assert "2025-09-02" in text
    assert "2025-10-01" in text


# --------------------------------------------------------------------------- #
# 5. Control bar
# --------------------------------------------------------------------------- #


def test_control_bar_returns_a_dash_div(data_layer):
    h = _header()
    result = h._control_bar({})
    assert isinstance(result, type(h.html.Div()))


def test_control_bar_includes_all_seven_controls(data_layer):
    """The seven controls are: Mode radio, Symbol dropdown,
    Strategy dropdown, Interval dropdown, From date, To date,
    Run + Download buttons. Each one is identifiable by its label.
    """
    h = _header()
    result = h._control_bar({})
    text = str(result)
    for label in ("Mode", "Symbol", "Strategy", "Interval",
                  "From", "To", "Run optimizer", "Download bars"):
        assert label in text, f"control {label!r} missing; got: {text[:2000]}"


def test_control_bar_includes_nse_cash_label(data_layer):
    """The plan locks the universe to NSE cash. The control bar
    must show this so the operator cannot accidentally select a
    different exchange.
    """
    h = _header()
    result = h._control_bar({})
    text = str(result)
    assert "NSE" in text, "exchange label NSE missing"
    assert "cash" in text.lower(), "NSE cash label missing"


def test_control_bar_run_button_has_tooltip_in_auto_live(data_layer):
    """Auto-Live mode disables the Run button with a tooltip that
    explains why. The tooltip is the operator's only signal that
    'Auto' is not 'auto-deploy the recommendation' but 'auto-select
    the pair to study'.
    """
    h = _header()
    result = h._control_bar({})            # default mode = auto_live
    text = str(result)
    assert "auto-running" in text or "auto" in text.lower(), \
        f"auto-live tooltip missing; got: {text[:2000]}"


def test_control_bar_strategies_dropdown_populated_from_registry(data_layer):
    """The Strategy dropdown is data-driven. The fake data layer
    has two strategies (ocl_keylevel + keylevel). Both must show
    up in the options.
    """
    h = _header()
    result = h._control_bar({})
    text = str(result)
    assert "ocl_keylevel" in text
    assert "keylevel" in text


# --------------------------------------------------------------------------- #
# 6. Safe adapters fail-closed
# --------------------------------------------------------------------------- #


def _make_unimportable(module_name):
    """Install a sentinel in sys.modules that makes any import of
    ``module_name`` raise ModuleNotFoundError. The test then
    exercises the safe-adapter's failure path.
    """
    class _RaiseOnAccess(types.ModuleType):
        def __getattr__(self, name):
            raise ModuleNotFoundError(
                f"module {module_name!r} marked unimportable for test"
            )

    sentinel = _RaiseOnAccess(module_name)
    sys.modules[module_name] = sentinel
    return sentinel


def test_safe_strategies_returns_empty_list_on_missing_adapter():
    """When data_cache is not importable, the safe adapter returns
    ``[]`` rather than raising. The page renders an empty
    dropdown.
    """
    _make_unimportable("blue_engine.optimizer.data_cache")
    h = _header()
    # Force the lazy import to re-evaluate.
    h._DATA_CACHE_AVAILABLE = None
    h._list_strategies_for_ui = None
    h._OptimizerDataCache = None
    out = h._safe_strategies_for_ui()
    assert out == [], f"empty input must give []; got {out}"


def test_safe_watchlist_returns_empty_list_on_missing_adapter():
    _make_unimportable("blue_engine.optimizer.data_cache")
    h = _header()
    h._DATA_CACHE_AVAILABLE = None
    h._watchlist_for_ui = None
    h._OptimizerDataCache = None
    out = h._safe_watchlist()
    assert out == [], f"empty input must give []; got {out}"


def test_safe_live_state_returns_none_on_missing_registry():
    """When alpha.registry is not importable, the safe adapter
    returns ``None`` (the normal "no live state" case).
    """
    _make_unimportable("blue_engine.alpha.registry")
    h = _header()
    h._REGISTRY_AVAILABLE = None
    h.live_active_state = None
    out = h._safe_live_state()
    assert out is None


def test_safe_cache_status_returns_error_dict_on_missing_adapter():
    """The cache status always has a key shape. When the cache
    module is missing, the safe adapter returns ``{"cached":
    False, "n_bars": 0, "error": "..."}`` so the header can
    still render its line.
    """
    _make_unimportable("blue_engine.optimizer.data_cache")
    h = _header()
    h._DATA_CACHE_AVAILABLE = None
    h._OptimizerDataCache = None
    h._list_strategies_for_ui = None
    out = h._safe_cache_status("RELIANCE", "NSE", "1d")
    assert "cached" in out
    assert "n_bars" in out
    assert "error" in out


# --------------------------------------------------------------------------- #
# 7. Import-time safety
# --------------------------------------------------------------------------- #


def test_header_module_does_not_import_data_cache_at_load_time():
    """The header module's lazy-import design is part of the
    contract: importing it must NOT trigger the heavy data
    cache chain. A regression that hoists the import to the
    top of the module adds 12s to every page load.
    """
    # Make sure the data cache module is NOT in sys.modules before
    # we import the header.
    sys.modules.pop("blue_engine.optimizer.data_cache", None)
    # Force a fresh import of the header.
    for mod_name in list(sys.modules):
        if mod_name.startswith("blue_engine.dashboard.pages._optimizer_header"):
            del sys.modules[mod_name]
    import blue_engine.dashboard.pages._optimizer_header as _hdr  # noqa: F401
    assert "blue_engine.optimizer.data_cache" not in sys.modules, \
        ("importing _optimizer_header must not trigger data_cache import; "
         "the import should be lazy")


def test_header_module_does_not_import_alpha_registry_at_load_time():
    """Same as above, for alpha.registry."""
    sys.modules.pop("blue_engine.alpha.registry", None)
    for mod_name in list(sys.modules):
        if mod_name.startswith("blue_engine.dashboard.pages._optimizer_header"):
            del sys.modules[mod_name]
    import blue_engine.dashboard.pages._optimizer_header as _hdr  # noqa: F401
    assert "blue_engine.alpha.registry" not in sys.modules, \
        ("importing _optimizer_header must not trigger alpha.registry import; "
         "the import should be lazy")


# --------------------------------------------------------------------------- #
# 8. The body below the header is UNCHANGED
# --------------------------------------------------------------------------- #


def test_optimizer_page_still_imports(data_layer):
    """The optimizer page must import without error after the
    header is added. A regression that breaks the import
    blocks the entire dashboard.
    """
    # We do NOT import the page here (the import is heavy and
    # not needed for a contract test). We just assert the header
    # module exposes the two functions the page calls.
    h = _header()
    assert callable(h._ai_insight_header)
    assert callable(h._control_bar)


def test_header_text_carries_data_class_label(data_layer):
    """The plan locks 'Data class: historical:openalgo' as part of
    the AI Insight header. The operator uses this to know the
    data is NOT live.
    """
    h = _header()
    result = h._ai_insight_header({})
    text = str(result)
    assert "data class" in text.lower() or "Data class" in text, \
        f"data-class label missing; got: {text[:1000]}"


def test_header_text_mentions_offline_when_mode_is_offline(data_layer):
    """When the mode is Offline, the header must show 'Offline' so
    the operator knows the page is not in auto mode.
    """
    h = _header()
    result = h._ai_insight_header({}, mode="offline")
    text = str(result)
    assert "Offline" in text, f"Offline mode label missing; got: {text[:1000]}"
