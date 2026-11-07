"""Trade setup store: validation and atomic save to settings.yaml. No FastAPI needed."""
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
    return settings, strategies


def test_strategy_names_come_from_yaml_files(paths):
    _, strategies = paths
    assert strategy_names(strategies) == ["alpha", "beta"]
    assert strategy_names(strategies.parent / "missing") == []


def test_read_setup_returns_current_values(paths):
    settings, strategies = paths
    state = read_setup(settings, strategies)
    assert state == {"risk_per_trade": 0.0025, "max_symbols_per_portfolio": None, "strategy": None,
                     "strategies": ["alpha", "beta"]}


def test_save_writes_all_three_and_keeps_other_keys(paths):
    settings, strategies = paths
    result = apply_setup_updates(settings, strategies,
                                 {"risk_per_trade": 0.005, "max_symbols_per_portfolio": 3, "strategy": "beta"})
    assert result["new"] == {"risk_per_trade": 0.005, "max_symbols_per_portfolio": 3, "strategy": "beta"}
    data = yaml.safe_load(settings.read_text(encoding="utf-8"))
    assert data["mode"] == "paper" and data["dashboard"] == {"pnl_visible_default": True}
    assert data["sizing"]["risk_per_trade"] == 0.005
    assert data["trade_setup"] == {"max_symbols_per_portfolio": 3, "strategy": "beta"}


def test_saved_risk_is_what_the_code_reads(paths):
    settings, strategies = paths
    apply_setup_updates(settings, strategies, {"risk_per_trade": 0.0050})
    assert RiskLimits.risk_per_trade(settings) == 0.005


def test_whole_number_float_is_accepted_for_symbols(paths):
    settings, strategies = paths
    apply_setup_updates(settings, strategies, {"max_symbols_per_portfolio": 2.0})
    assert yaml.safe_load(settings.read_text(encoding="utf-8"))["trade_setup"]["max_symbols_per_portfolio"] == 2


@pytest.mark.parametrize("bad", [
    {"risk_per_trade": 0}, {"risk_per_trade": 0.06}, {"risk_per_trade": True}, {"risk_per_trade": "0.1"},
    {"max_symbols_per_portfolio": 0}, {"max_symbols_per_portfolio": 101}, {"max_symbols_per_portfolio": 2.5},
    {"max_symbols_per_portfolio": True}, {"strategy": "gamma"}, {"strategy": ""}, {"unknown": 1}, {},
])
def test_bad_values_are_refused_and_file_is_unchanged(paths, bad):
    settings, strategies = paths
    before = settings.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        apply_setup_updates(settings, strategies, bad)
    assert settings.read_text(encoding="utf-8") == before
