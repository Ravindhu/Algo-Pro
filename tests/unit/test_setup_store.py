"""Trade setup store: validation and atomic save (risk to settings.yaml, symbol cap to risk_limits.yaml)."""
import pytest
import yaml

from quant.core.config import RiskLimits
from quant.core.setup_store import apply_setup_updates, read_setup, strategy_names


@pytest.fixture
def paths(tmp_path):
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "alpha.yaml").write_text("strategy: alpha\n", encoding="utf-8")
    (strategies / "beta.yaml").write_text("strategy: beta\n", encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text("mode: paper\nsizing:\n  risk_per_trade: 0.0025\ndashboard:\n  pnl_visible_default: true\n",
                        encoding="utf-8")
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("blocked: false\nmax_daily_loss: 1000\n", encoding="utf-8")
    return settings, strategies, limits


def test_strategy_names_come_from_yaml_files(paths):
    _, strategies, _ = paths
    assert strategy_names(strategies) == ["alpha", "beta"]
    assert strategy_names(strategies.parent / "missing") == []


def test_read_setup_returns_current_values(paths):
    settings, strategies, limits = paths
    assert read_setup(settings, strategies, limits) == {
        "risk_per_trade": 0.0025, "aggressive_share": 0.6, "max_symbols_per_portfolio": None, "strategy": None,
        "strategies": ["alpha", "beta"], "bot_strategies": [], "available_bot_strategies": ["engulfing", "engulfing_fail", "ocl_seq", "ocl_sell_fail_top_seq", "ocl_sell_fail_bottom_seq"], "live_max_capital_inr": None,
        "brokerage": None, "brokerage_example": None,
        "htf_interval": None, "mtf_interval": None, "ltf_interval": None,
        "timeframe_options": ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "75m", "125m", "1d", "1w", "1M"],
        "capital_inr": None, "max_daily_loss_pct": None, "max_strategy_loss_pct": None,
        "max_daily_loss": 1000, "max_strategy_loss": None, "mtf_borrowed_inr": None}


def test_save_writes_risk_strategy_and_symbol_cap_to_their_files(paths):
    settings, strategies, limits = paths
    result = apply_setup_updates(settings, strategies, limits,
                                 {"risk_per_trade": 0.005, "max_symbols_per_portfolio": 3, "strategy": "beta"})
    assert result["new"] == {"risk_per_trade": 0.005, "max_symbols_per_portfolio": 3, "strategy": "beta"}
    assert result["old"]["max_symbols_per_portfolio"] is None
    data = yaml.safe_load(settings.read_text(encoding="utf-8"))
    assert data["mode"] == "paper" and data["dashboard"] == {"pnl_visible_default": True}
    assert data["sizing"]["risk_per_trade"] == 0.005
    assert data["trade_setup"] == {"strategy": "beta"}
    saved = RiskLimits.from_yaml(limits)
    assert saved.max_symbols_per_portfolio == 3 and saved.max_daily_loss == 1000  # other limits kept


def test_saved_risk_is_what_the_code_reads(paths):
    settings, strategies, limits = paths
    apply_setup_updates(settings, strategies, limits, {"risk_per_trade": 0.0050})
    assert RiskLimits.risk_per_trade(settings) == 0.005


def test_whole_number_float_is_accepted_for_symbols(paths):
    settings, strategies, limits = paths
    apply_setup_updates(settings, strategies, limits, {"max_symbols_per_portfolio": 2.0})
    assert RiskLimits.from_yaml(limits).max_symbols_per_portfolio == 2


@pytest.mark.parametrize("bad", [
    {"risk_per_trade": 0}, {"risk_per_trade": 0.06}, {"risk_per_trade": True}, {"risk_per_trade": "0.1"},
    {"max_symbols_per_portfolio": 0}, {"max_symbols_per_portfolio": 101}, {"max_symbols_per_portfolio": 2.5},
    {"max_symbols_per_portfolio": True}, {"strategy": "gamma"}, {"strategy": ""}, {"unknown": 1}, {},
])
def test_bad_values_are_refused_and_files_are_unchanged(paths, bad):
    settings, strategies, limits = paths
    before = (settings.read_text(encoding="utf-8"), limits.read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        apply_setup_updates(settings, strategies, limits, bad)
    assert (settings.read_text(encoding="utf-8"), limits.read_text(encoding="utf-8")) == before


# ---- loss budgets as % of capital (owner 2026-11-19) ----
from quant.core.setup_store import BUDGET_OWNED_LIMITS  # noqa: E402


def _budget_paths(tmp_path):
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "alpha.yaml").write_text("strategy: alpha\n", encoding="utf-8")
    settings = tmp_path / "settings.yaml"
    settings.write_text("sizing:\n  risk_per_trade: 0.0025\n", encoding="utf-8")
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("blocked: false\nmax_daily_loss: null\nmax_strategy_loss: null\n", encoding="utf-8")
    return settings, strategies, limits


def test_budget_pct_writes_settings_and_derived_rupee_caps(tmp_path):
    settings, strategies, limits = _budget_paths(tmp_path)
    r = apply_setup_updates(settings, strategies, limits, {
        "capital_inr": 1000000, "max_daily_loss_pct": 0.02, "max_strategy_loss_pct": 0.015})
    lim = RiskLimits.from_yaml(limits)
    assert lim.max_daily_loss == 20000.0
    assert lim.max_strategy_loss == 15000.0
    saved = yaml.safe_load(settings.read_text(encoding="utf-8"))["risk_budget"]
    assert saved == {"capital_inr": 1000000.0, "max_daily_loss_pct": 0.02, "max_strategy_loss_pct": 0.015}
    assert r["new"]["max_daily_loss"] == 20000.0 and r["new"]["max_strategy_loss"] == 15000.0


def test_changing_capital_recomputes_both_rupee_caps(tmp_path):
    settings, strategies, limits = _budget_paths(tmp_path)
    apply_setup_updates(settings, strategies, limits, {
        "capital_inr": 1000000, "max_daily_loss_pct": 0.02, "max_strategy_loss_pct": 0.015})
    apply_setup_updates(settings, strategies, limits, {"capital_inr": 500000})
    lim = RiskLimits.from_yaml(limits)
    assert lim.max_daily_loss == 10000.0 and lim.max_strategy_loss == 7500.0


def test_read_setup_shows_budget_and_rupee_values(tmp_path):
    settings, strategies, limits = _budget_paths(tmp_path)
    apply_setup_updates(settings, strategies, limits, {
        "capital_inr": 1000000, "max_daily_loss_pct": 0.02, "max_strategy_loss_pct": 0.015})
    out = read_setup(settings, strategies, limits)
    assert out["max_daily_loss_pct"] == 0.02 and out["max_strategy_loss_pct"] == 0.015
    assert out["max_daily_loss"] == 20000.0 and out["max_strategy_loss"] == 15000.0


def test_pct_without_any_capital_base_is_refused_and_nothing_changes(tmp_path):
    settings, strategies, limits = _budget_paths(tmp_path)
    before = (settings.read_text(encoding="utf-8"), limits.read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="capital_inr first"):
        apply_setup_updates(settings, strategies, limits, {"max_daily_loss_pct": 0.02})
    assert (settings.read_text(encoding="utf-8"), limits.read_text(encoding="utf-8")) == before


@pytest.mark.parametrize("bad", [0, -0.01, 0.11, True, "0.02", float("nan")])
def test_bad_budget_pct_is_refused(tmp_path, bad):
    settings, strategies, limits = _budget_paths(tmp_path)
    with pytest.raises(ValueError):
        apply_setup_updates(settings, strategies, limits, {"max_daily_loss_pct": bad})


def test_symbol_cap_is_not_written_when_budget_is_refused(tmp_path):
    settings, strategies, limits = _budget_paths(tmp_path)
    before = limits.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        apply_setup_updates(settings, strategies, limits,
                            {"max_symbols_per_portfolio": 4, "max_daily_loss_pct": 0.02})
    assert limits.read_text(encoding="utf-8") == before


def test_budget_owned_rupee_caps_are_listed_for_the_api_guard():
    assert set(BUDGET_OWNED_LIMITS) == {"max_daily_loss", "max_strategy_loss"}


# ---- owner tree (2026-11-20; floor 30 % from 2026-11-26): aggressive share 30 % to 80 % ------
@pytest.mark.parametrize("bad", [0.29, 0.81, 0.0, 1.0, -0.6, True])
def test_aggressive_share_outside_the_range_is_refused(paths, bad):
    settings, strategies, limits = paths
    before = settings.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        apply_setup_updates(settings, strategies, limits, {"aggressive_share": bad})
    assert settings.read_text(encoding="utf-8") == before  # refused before anything is written


def test_aggressive_share_is_saved_under_cycle(paths):
    settings, strategies, limits = paths
    result = apply_setup_updates(settings, strategies, limits, {"aggressive_share": 0.7})
    assert result["new"] == {"aggressive_share": 0.7}
    data = yaml.safe_load(settings.read_text(encoding="utf-8"))
    assert data["cycle"]["aggressive_share"] == 0.7
    assert data["sizing"]["risk_per_trade"] == 0.0025  # other sections kept
    assert read_setup(settings, strategies, limits)["aggressive_share"] == 0.7


def test_aggressive_share_accepts_the_30_percent_floor(paths):
    settings, strategies, limits = paths
    apply_setup_updates(settings, strategies, limits, {"aggressive_share": 0.30})
    assert read_setup(settings, strategies, limits)["aggressive_share"] == 0.3
