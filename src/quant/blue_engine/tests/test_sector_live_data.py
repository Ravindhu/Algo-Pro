"""Live sector data: vendor repo -> sector_market_data -> SectorEngine.

Locks the audit finding of 2026-10-23: the sector engine had NO production data
path, so it returned ok=False and wrote all sectors to multiplier 0.0 on every
real run. These tests cover the fix and the properties that make it safe.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from blue_engine.sectors.providers.vendor_sector import (  # noqa: E402
    DEFAULT_INDICES_DIR,
    VendorSectorProvider,
)
from blue_engine.sectors.sector_engine import SectorEngine  # noqa: E402


def _synthetic(folder: Path, *, rows: int = 60, drift: float = 0.001) -> None:
    """Write small index parquets the provider can read without the vendor repo."""
    pd = pytest.importorskip("pandas")
    import numpy as np

    folder.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2026-01-01", periods=rows)
    for i, stem in enumerate(("NIFTY_50", "NIFTY_BANK", "NIFTY_IT")):
        close = 10000.0 * np.cumprod(np.full(rows, 1.0 + drift * (i + 1)))
        pd.DataFrame({"Close": close, "Adj Close": close},
                     index=pd.Index(dates, name="Date")).to_parquet(
            folder / f"{stem}.parquet")


def _engine(tmp_path: Path, **provider_cfg) -> SectorEngine:
    """Engine with a configured vendor provider instance injected.

    The provider must be an INSTANCE (it is called as ``provider()``); passing
    the class would construct a default-configured provider and silently ignore
    ``provider_cfg``.
    """
    return SectorEngine({"lookback_window": 14, "agent_enabled": False,
                         "limits_path": str(tmp_path / "l.json"),
                         "snapshot_path": str(tmp_path / "s.json")},
                        provider=VendorSectorProvider(provider_cfg))


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #
def test_provider_aligns_sector_and_benchmark_lengths(tmp_path):
    _synthetic(tmp_path)
    data = VendorSectorProvider({"indices_dir": str(tmp_path)}).fetch()

    assert data["ok"] is True
    bench = data["benchmark_closes"]
    assert len(bench) >= 20
    for name, closes in data["sector_closes"].items():
        assert len(closes) == len(bench), f"{name} is not aligned to the benchmark"


def test_provider_labels_data_as_historical_not_live(tmp_path):
    _synthetic(tmp_path)
    data = VendorSectorProvider({"indices_dir": str(tmp_path)}).fetch()

    assert data["data_classification"] == "historical_vendor"
    assert data["as_of"]


def test_provider_never_reads_rows_after_as_of(tmp_path):
    """NO LOOK-AHEAD: an as_of in the past must truncate the series."""
    _synthetic(tmp_path)
    provider = VendorSectorProvider({"indices_dir": str(tmp_path),
                                     "as_of": "2026-02-02"})
    data = provider.fetch()

    assert data["ok"] is True
    assert data["as_of"] <= "2026-02-02"


def test_provider_degrades_when_the_dir_is_missing(tmp_path):
    data = VendorSectorProvider({"indices_dir": str(tmp_path / "nope")}).fetch()
    assert data["ok"] is False
    assert any("missing" in w for w in data["warnings"])


def test_provider_skips_a_missing_sector_without_failing(tmp_path):
    _synthetic(tmp_path)
    (tmp_path / "NIFTY_IT.parquet").unlink()

    data = VendorSectorProvider({"indices_dir": str(tmp_path),
                                 "sectors": {"NIFTY_BANK": "NIFTY_BANK",
                                             "NIFTY_IT": "NIFTY_IT"}}
                                ).fetch()

    assert data["ok"] is True, "one missing sector must not remove the others"
    assert "NIFTY_IT" not in data["sector_closes"]
    assert any("NIFTY_IT" in w for w in data["warnings"])


# --------------------------------------------------------------------------- #
# Engine wiring - the actual audit finding
# --------------------------------------------------------------------------- #
def test_engine_now_produces_data_from_the_provider(tmp_path):
    """The regression: no sector_market_data in context still yields a result."""
    _synthetic(tmp_path)
    engine = _engine(tmp_path, indices_dir=str(tmp_path))

    result = engine.run({})          # NOTE: no sector_market_data supplied

    assert result.ok is True, result.error
    assert result.data["sector_matrix"], "matrix should be built from vendor data"
    assert set(result.data["sector_matrix"][0]) >= {
        "sector", "relative_strength_score", "relative_ratio"}


def test_engine_reports_the_data_classification(tmp_path):
    _synthetic(tmp_path)
    engine = SectorEngine({"lookback_window": 14, "agent_enabled": False,
                          "limits_path": str(tmp_path / "l.json"),
                          "snapshot_path": str(tmp_path / "s.json")},
                         provider=VendorSectorProvider(
                             {"indices_dir": str(tmp_path)}))

    result = engine.run({})

    joined = " ".join(result.warnings)
    assert "historical_vendor" in joined, (
        "vendor history must never be presented as live data")


def test_context_data_still_wins_over_the_provider(tmp_path):
    """An explicit context dict must not be overridden by the provider."""
    _synthetic(tmp_path)
    engine = _engine(tmp_path, indices_dir=str(tmp_path))
    supplied = {
        "benchmark_closes": [100.0 + i for i in range(30)],
        "sector_closes": {"MY_SECTOR": [100.0 + 2 * i for i in range(30)]},
    }

    result = engine.run({"sector_market_data": supplied})

    assert [r["sector"] for r in result.data["sector_matrix"]] == ["MY_SECTOR"]


def test_engine_fails_closed_but_holds_allocation_without_any_data(tmp_path):
    """No provider, no context: report failure, but do not zero the sleeve."""
    engine = SectorEngine({"lookback_window": 14, "agent_enabled": False,
                          "limits_path": str(tmp_path / "l.json"),
                          "snapshot_path": str(tmp_path / "s.json"),
                          "provider": False})

    result = engine.run({})          # no provider injected, no context data

    assert result.ok is False
    # No sector names could be resolved, so there is nothing to hold: the
    # multipliers must be EMPTY, not zeroed entries for sectors we never saw.
    assert result.data["sector_allocation_multipliers"] == {}


# --------------------------------------------------------------------------- #
# The vendor repo itself
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not DEFAULT_INDICES_DIR.is_dir(),
                    reason="vendor repo not present in this checkout")
def test_real_vendor_repo_yields_a_usable_matrix():
    """Guard: the data that ships with the project actually wires through."""
    engine = SectorEngine({"lookback_window": 14, "agent_enabled": False})

    result = engine.run({})

    assert result.ok is True, result.error
    assert len(result.data["sector_matrix"]) >= 2