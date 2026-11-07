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
        "risk_per_trade": 0.0025, "max_symbols_per_portfolio": None, "strategy": None,
        "strategies": ["alpha", "beta"]}


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
