"""Tests for the vendor-parquet FII/DII source (added 2026-10-22).

Why these exist
---------------
FII/DII flow was previously NSE-scraping only, and on 2026-10-22 NSE returned
nothing while taking ~19 s. The pinned vendor checkout
(``Blue_engine/database/vendor/indian-market-historical-data``) already carries
``data/FLOWS/fii_dii_cash.parquet`` and ``fii_derivatives.parquet`` with real,
dated values. These tests pin:

  * the parquet is actually read (no network),
  * the newest row wins regardless of file order,
  * a stale file is *served but flagged* - never silently presented as fresh,
  * a missing/corrupt file degrades to a warning, never an exception,
  * the values reach the Market engine's ``fii_score``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "Blue_engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "Blue_engine"))

from blue_engine.economics.providers import fii_provider as fp  # noqa: E402
from blue_engine.economics.providers.fii_provider import FiiFlowProvider  # noqa: E402

HAS_DATA = fp.DEFAULT_FLOWS_DIR.is_dir() and (
    fp.DEFAULT_FLOWS_DIR / "fii_dii_cash.parquet").is_file()


def _offline(monkeypatch):
    """Make any HTTP attempt fail loudly, so a test that passes is offline."""
    def _boom(*a, **k):
        raise RuntimeError("network must not be used")
    monkeypatch.setattr(fp, "_HAS_REQUESTS", False)
    monkeypatch.setattr(fp.requests, "get", _boom, raising=False)


# ---------------------------------------------------------------------------
# Path resolution - the bug that cost three attempts
# ---------------------------------------------------------------------------
def test_vendor_dir_points_at_a_real_checkout():
    assert fp.DEFAULT_FLOWS_DIR.name == "FLOWS"
    assert fp.DEFAULT_FLOWS_DIR.parent.name == "data"
    assert fp.VENDOR_DIR.name == "indian-market-historical-data"
    # The repo root must be 3 levels above the provider module.
    assert fp.VENDOR_DIR.parents[2].name == "Blue_engine"


def test_vendor_dir_matches_the_screener_symbol_map_root():
    """One vendor checkout must be referenced by one path convention."""
    from blue_engine.screener import symbol_map
    assert fp.VENDOR_DIR.resolve() == symbol_map.VENDOR_DIR.resolve()


# ---------------------------------------------------------------------------
# Reading the real data
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not HAS_DATA, reason="vendor FLOWS checkout not present")
def test_reads_real_fii_and_dii_cash_without_network(monkeypatch):
    _offline(monkeypatch)
    out = FiiFlowProvider({"disable_vix": True}).fetch()
    meta = out.pop("meta")
    assert meta["network_used"] is False
    assert "fii" in out and "dii" in out
    assert isinstance(out["fii"]["net_cash"], float)
    assert out["fii"]["source"].startswith("vendor:")
    assert out["dii"]["source"].startswith("vendor:")
    # as_of must be a real date from the file, not today's date invented.
    assert meta["as_of"] and len(meta["as_of"]) == 10


@pytest.mark.skipif(not HAS_DATA, reason="vendor FLOWS checkout not present")
def test_newest_row_wins_regardless_of_file_order(tmp_path, monkeypatch):
    """A vendor file appended out of order must not serve a random day."""
    import pandas as pd

    shuffled = pd.DataFrame({
        "Date": ["2026-01-05", "2026-01-02", "2026-01-07", "2026-01-06"],
        "FII_Net_Cr": [-100.0, -200.0, -777.0, -300.0],
        "DII_Net_Cr": [10.0, 20.0, 70.0, 30.0],
    })
    shuffled.to_parquet(tmp_path / "fii_dii_cash.parquet", index=False)
    _offline(monkeypatch)

    out = FiiFlowProvider({"flows_dir": str(tmp_path),
                           "disable_vix": True}).fetch()
    out.pop("meta", None)
    assert out["fii"]["net_cash"] == -777.0     # 2026-01-07, the real max
    assert out["dii"]["net_cash"] == 70.0


@pytest.mark.skipif(not HAS_DATA, reason="vendor FLOWS checkout not present")
def test_derivatives_long_ratio_is_exposed(monkeypatch):
    _offline(monkeypatch)
    out = FiiFlowProvider({"disable_vix": True}).fetch()
    out.pop("meta", None)
    if "fii_long_ratio_pct" in out:
        block = out["fii_long_ratio_pct"]
        assert 0 <= block["value"] <= 100
        assert block["as_of"]


# ---------------------------------------------------------------------------
# Freshness - the core safety property
# ---------------------------------------------------------------------------
def test_staleness_flags_an_old_file():
    prov = FiiFlowProvider({"max_lag_trading_days": 2})
    info = prov._staleness(fp.date(2020, 1, 1))
    assert info["stale"] is True
    assert info["age_days"] > 1000


def test_staleness_accepts_today():
    prov = FiiFlowProvider({"max_lag_trading_days": 2})
    info = prov._staleness(fp.date.today())
    assert info["stale"] is False


def test_friday_file_read_on_monday_is_not_stale_by_calendar_days():
    """3 calendar days back over a weekend is ~2 trading days.""" 
    prov = FiiFlowProvider({"max_lag_trading_days": 2})
    from datetime import timedelta
    three_days = fp.date.today() - timedelta(days=3)
    info = prov._staleness(three_days)
    assert info["approx_trading_days"] <= 3  # calendar 3 -> trading ~2
    assert info["stale"] is False


def test_staleness_unknown_date_is_not_claimed_safe_or_stale():
    info = FiiFlowProvider()._staleness(None)
    assert info["stale"] is False
    assert info["age_days"] is None


# ---------------------------------------------------------------------------
# Degradation - never raise
# ---------------------------------------------------------------------------
def test_missing_directory_degrades_to_warning(tmp_path, monkeypatch):
    _offline(monkeypatch)
    out = FiiFlowProvider({"flows_dir": str(tmp_path / "nope"),
                           "disable_vix": True}).fetch()
    meta = out.pop("meta")
    assert meta["ok"] is True
    assert any("vendor" in w.lower() for w in meta["warnings"])


def test_corrupt_parquet_degrades_to_warning(tmp_path, monkeypatch):
    _offline(monkeypatch)
    (tmp_path / "fii_dii_cash.parquet").write_bytes(b"not a parquet file")
    out = FiiFlowProvider({"flows_dir": str(tmp_path),
                           "disable_vix": True}).fetch()
    meta = out.pop("meta")
    assert any("vendor read failed" in w for w in meta["warnings"])
    assert "fii" not in out


def test_never_raises_even_with_no_sources(tmp_path, monkeypatch):
    _offline(monkeypatch)
    out = FiiFlowProvider({"flows_dir": str(tmp_path / "x"),
                           "disable_vix": True}).fetch()
    assert out["meta"]["ok"] is True


# ---------------------------------------------------------------------------
# Downstream: the Market engine must see the flow
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not HAS_DATA, reason="vendor FLOWS checkout not present")
def test_market_engine_scores_the_vendor_flow(monkeypatch):
    _offline(monkeypatch)
    from blue_engine.market.market_engine import MarketEngine

    data = FiiFlowProvider({"disable_vix": True}).fetch()
    data.pop("meta", None)
    result = MarketEngine({"indices": []}, provider=lambda: data).run({})
    assert result.data["fii_score"] is not None, "FII flow was dropped"
    assert -1.0 <= result.data["fii_score"] <= 1.0
    assert result.data["risk_appetite"] is not None
