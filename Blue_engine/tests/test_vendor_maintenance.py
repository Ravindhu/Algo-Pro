"""Vendor data maintenance: registry coverage, scrub, atomic merge, staleness.

Locks the two findings of 2026-10-24:

1. The vendor index list was incomplete - the sector engine had NO
   relative-strength reading for AUTO, METALS, ENERGY, FMCG or REALTY even
   though the portfolio universe allocates to all five.
2. One upstream partial bar (Open/High/Low present, Close NaN) made the aligned
   join drop the date for every sector, and the engine then reported
   ``data_classification=unknown`` / ``as_of=None``.

These tests must pass with NO network: the fetcher is injected.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from blue_engine.jobs import vendor_maintenance as vm  # noqa: E402
from blue_engine.sectors.providers import vendor_sector as vs  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _frame(rows=30, start="2026-01-01", value=100.0):
    pd = pytest.importorskip("pandas")
    dates = pd.bdate_range(start, periods=rows)
    return pd.DataFrame(
        {"Open": value, "High": value, "Low": value, "Close": value,
         "Adj Close": value, "Volume": 0},
        index=pd.Index(dates, name="Date"))


class _FakeYF:
    """Minimal stand-in for yfinance: one Ticker class, returns a frame."""

    def __init__(self, frames):
        self._frames = frames
        outer = self

        class _T:
            def __init__(self, ticker):
                self.ticker = ticker

            def history(self, **kwargs):
                frame, exc = outer._frames[self.ticker]
                if exc:
                    raise exc
                return frame

        self.Ticker = _T


# --------------------------------------------------------------------------- #
# Registry coverage
# --------------------------------------------------------------------------- #
def test_every_universe_sleeve_has_a_sector_reading():
    """The gap found on 2026-10-24: AUTO/METALS/ENERGY/FMCG/REALTY were blind."""
    from blue_engine.portfolio.providers.universe import DEFAULT_UNIVERSE

    missing = [s for s in DEFAULT_UNIVERSE
               if s != "NIFTY" and s not in vs.DEFAULT_SECTORS]
    assert missing == [], f"sleeves with no relative-strength reading: {missing}"


def test_every_default_sector_has_a_registered_ticker():
    """A sleeve mapped to a stem that is never downloaded is a silent hole."""
    orphan = [stem for stem in vs.DEFAULT_SECTORS.values()
              if stem not in vs.INDEX_TICKERS]
    assert orphan == [], f"sector stems with no ticker: {orphan}"


def test_benchmark_is_registered_and_distinct_from_sectors():
    assert vs.DEFAULT_BENCHMARK in vs.INDEX_TICKERS
    assert vs.DEFAULT_BENCHMARK not in vs.DEFAULT_SECTORS.values()


def test_live_symbols_cover_every_default_sector():
    """A missing live symbol would silently fall back to the parquet stem."""
    missing = [s for s in vs.DEFAULT_SECTORS if s not in vs.SECTOR_LIVE_SYMBOLS]
    assert missing == [], f"sleeves with no SDK symbol for the live overlay: {missing}"


def test_ticker_registry_has_no_duplicate_tickers():
    tickers = list(vs.INDEX_TICKERS.values())
    assert len(tickers) == len(set(tickers)), "two stems share one Yahoo ticker"


def test_load_registry_filters_and_ignores_unknown_names(tmp_path):
    entries = vm.load_registry(tmp_path, only=["NIFTY_IT", "NIFTY_AUTO"])
    assert [e.stem for e in entries] == ["NIFTY_IT", "NIFTY_AUTO"]

    summary = vm.run_maintenance(only=["NOT_AN_INDEX"], indices_dir=tmp_path,
                                 check=True)
    assert any("unknown index" in w for w in summary["warnings"])


# --------------------------------------------------------------------------- #
# Scrub: the partial-bar bug
# --------------------------------------------------------------------------- #
def test_scrub_drops_a_partial_bar_and_names_it():
    pd = pytest.importorskip("pandas")
    frame = _frame(10)
    frame.loc[frame.index[-1], ["Close", "Adj Close"]] = float("nan")
    frame.loc[frame.index[-1], "Volume"] = 0

    cleaned, notes = vm.scrub_frame(frame)

    assert len(cleaned) == 9
    assert frame.index[-1] not in cleaned.index
    assert any("partial row" in n for n in notes)


def test_scrub_drops_non_positive_and_infinite_closes():
    pd = pytest.importorskip("pandas")
    frame = _frame(6)
    frame.loc[frame.index[1], "Close"] = 0.0
    frame.loc[frame.index[2], "Close"] = -5.0
    frame.loc[frame.index[3], "Close"] = float("inf")

    cleaned, _ = vm.scrub_frame(frame)
    assert len(cleaned) == 3
    assert (cleaned["Close"] > 0).all()


def test_scrub_is_a_no_op_on_clean_data():
    frame = _frame(8)
    cleaned, notes = vm.scrub_frame(frame)
    assert len(cleaned) == len(frame)
    assert notes == []


def test_scrub_handles_an_empty_or_missing_close_column():
    pd = pytest.importorskip("pandas")
    empty = pd.DataFrame()
    assert vm.scrub_frame(empty)[0] is empty
    no_close = pd.DataFrame({"Open": [1.0, 2.0]})
    _, notes = vm.scrub_frame(no_close)
    assert notes and "Close" in notes[0]


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #
def test_refresh_merges_without_truncating_history(tmp_path):
    pd = pytest.importorskip("pandas")
    entry = vm.RegistryEntry("NIFTY_IT", "^CNXIT", tmp_path / "NIFTY_IT.par")
    old = _frame(40, start="2025-11-01")
    old.to_parquet(entry.path)

    new = _frame(5, start="2026-01-01", value=101.0)
    result = vm.refresh_index(entry, yf=_FakeYF({"^CNXIT": (new, None)}))

    merged = pd.read_parquet(entry.path)
    assert result["ok"] is True
    assert len(merged) >= len(old)                 # history preserved
    assert merged.index[-1] == new.index[-1]       # and moved forward
    assert result["after"] == new.index[-1].strftime("%Y-%m-%d")


def test_refresh_never_lets_a_bad_bar_overwrite_a_good_one(tmp_path):
    """The exact 2026-10-13 failure: a NaN close must not replace a real one."""
    pd = pytest.importorskip("pandas")
    entry = vm.RegistryEntry("NIFTY_IT", "^CNXIT", tmp_path / "NIFTY_IT.parquet")
    good = _frame(30, value=100.0)
    good.to_parquet(entry.path)
    last_day = good.index[-1]

    bad = _frame(1, start=last_day.strftime("%Y-%m-%d"), value=999.0)
    bad.loc[last_day, ["Close", "Adj Close"]] = float("nan")
    vm.refresh_index(entry, yf=_FakeYF({"^CNXIT": (bad, None)}))

    merged = pd.read_parquet(entry.path)
    assert merged["Close"].isna().sum() == 0
    assert float(merged.loc[last_day, "Close"]) == 100.0


def test_refresh_reports_failure_when_the_feed_returns_nothing(tmp_path):
    entry = vm.RegistryEntry("NIFTY_IT", "^CNXIT", tmp_path / "NIFTY_IT.parquet")
    _frame(30).to_parquet(entry.path)

    result = vm.refresh_index(entry, yf=_FakeYF({"^CNXIT": (None, None)}))
    assert result["ok"] is True          # existing data is still usable
    assert any("no data returned" in n for n in result["notes"])


def test_refresh_never_raises_when_the_provider_throws(tmp_path):
    entry = vm.RegistryEntry("NIFTY_IT", "^CNXIT", tmp_path / "NIFTY_IT.parquet")
    boom = _FakeYF({"^CNXIT": (None, RuntimeError("network down"))})

    result = vm.refresh_index(entry, yf=boom)
    assert result["ok"] is False
    assert result["after"] is None


def test_dry_run_writes_nothing(tmp_path):
    entry = vm.RegistryEntry("NIFTY_IT", "^CNXIT", tmp_path / "NIFTY_IT.parquet")
    new = _frame(10)
    vm.refresh_index(entry, yf=_FakeYF({"^CNXIT": (new, None)}), dry_run=True)
    assert not entry.path.exists()


def test_refresh_flags_a_series_too_short_for_a_long_lookback(tmp_path):
    entry = vm.RegistryEntry("NIFTY_IT", "^CNXIT", tmp_path / "NIFTY_IT.parquet")
    result = vm.refresh_index(entry, yf=_FakeYF({"^CNXIT": (_frame(30), None)}))
    assert any("too short" in n for n in result["notes"])


# --------------------------------------------------------------------------- #
# Staleness + summary
# --------------------------------------------------------------------------- #
def test_stale_indices_separates_missing_from_old(tmp_path):
    from datetime import date
    old = vm.RegistryEntry("OLD", "^X", tmp_path / "OLD.parquet")
    missing = vm.RegistryEntry("GONE", "^Y", tmp_path / "GONE.parquet")
    fresh = vm.RegistryEntry("NEW", "^Z", tmp_path / "NEW.parquet")
    old_frame = _frame(10, start="2026-01-01")
    fresh_frame = _frame(10, start="2026-01-19")
    old_frame.to_parquet(old.path)
    fresh_frame.to_parquet(fresh.path)

    found = vm.stale_indices([old, missing, fresh], as_of=date(2026, 1, 20),
                             max_age_days=5)
    by_stem = {stem: age for stem, _last, age in found}
    # Age is measured from the LAST session actually in the file.
    assert by_stem["OLD"] == (date(2026, 1, 20) - old_frame.index[-1].date()).days
    assert by_stem["OLD"] > 5
    assert by_stem["GONE"] == -1          # missing is NOT "one day old"
    assert "NEW" not in by_stem


def test_run_maintenance_check_mode_touches_nothing(tmp_path):
    pd = pytest.importorskip("pandas")
    (tmp_path / "NIFTY_50.parquet").write_bytes(b"")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    summary = vm.run_maintenance(only=["NIFTY_50"], indices_dir=tmp_path,
                                 check=True)
    after = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    assert before == after
    assert summary["checked_only"] is True
    assert "master_csv" not in summary


def test_run_maintenance_rebuilds_the_master_csv(tmp_path):
    pd = pytest.importorskip("pandas")
    _frame(12).to_parquet(tmp_path / "NIFTY_50.parquet")
    vm.run_maintenance(only=["NIFTY_50"], indices_dir=tmp_path)

    master = tmp_path / "index_list.csv"
    assert master.is_file()
    text = master.read_text()
    assert "NIFTY_50" in text and "Total_Records" in text


# --------------------------------------------------------------------------- #
# Engine provenance (the "unknown" classification bug)
# --------------------------------------------------------------------------- #
def test_engine_accepts_the_vendor_classifications():
    """`historical_vendor` / `vendor+live_quote` used to be rewritten to unknown."""
    from blue_engine.sectors.sector_engine import _DATA_CLASSIFICATIONS

    assert "historical_vendor" in _DATA_CLASSIFICATIONS
    assert "vendor+live_quote" in _DATA_CLASSIFICATIONS


def test_engine_surfaces_as_of_and_classification(tmp_path):
    pd = pytest.importorskip("pandas")
    import numpy as np
    dates = pd.bdate_range("2026-01-01", periods=60)
    for i, stem in enumerate(("NIFTY_50", "NIFTY_BANK", "NIFTY_IT")):
        close = 1000.0 * np.cumprod(np.full(60, 1.0 + 0.002 * (i + 1)))
        pd.DataFrame({"Close": close, "Adj Close": close},
                     index=pd.Index(dates, name="Date")).to_parquet(
            tmp_path / f"{stem}.parquet")

    from blue_engine.sectors import sector_engine as se
    # ISOLATE THE CACHE. The provider persists to PriceCache and reads back
    # from it, and the cache is keyed by (symbol, exchange, interval) rather
    # than by directory. Without this the test would read whatever a previous
    # run stored for NIFTY_50 and report that date instead of the fixture's.
    class _EmptyCache:
        def rows(self, *a, **k):
            return []

    class _NoopDME:
        price_cache = _EmptyCache()

        def ingest_local_history(self, **kwargs):
            return {"ok": True, "warnings": []}

    cache_db = tmp_path / "prices.db"
    engine = se.SectorEngine(
        {"lookback_window": 14, "agent_enabled": False,
         "limits_path": str(tmp_path / "l.json"),
         "snapshot_path": str(tmp_path / "s.json")},
        provider=vs.VendorSectorProvider({
            "indices_dir": str(tmp_path),
            "database_engine": _NoopDME(),
            "cache_db": cache_db}))

    result = engine.run({})
    assert result.ok is True, result.error
    assert result.data["data_classification"] == "historical_vendor"
    assert result.data["as_of"] == dates[-1].strftime("%Y-%m-%d")
