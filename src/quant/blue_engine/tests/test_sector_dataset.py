"""Sector cross-section dataset: no look-ahead, honest labels, sector-only scope.

These lock the properties that decide whether any downstream backtest number
means anything. A leak here silently inflates every result, so the checks are
deliberately blunt: truncation must not change a single historical feature, the
tail must carry no labels, and a cross-section must never be narrower than the
minimum.

Fixtures are synthetic parquets written to tmp_path, so no network and no
dependency on the real vendor data.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from blue_engine.sectors import sector_dataset as sd  # noqa: E402

#: Real sector stems the builder will pick up (benchmarks are excluded).
SECTORS = ["NIFTY_BANK", "NIFTY_IT", "NIFTY_PHARMA", "NIFTY_AUTO",
           "NIFTY_METAL", "NIFTY_ENERGY"]


def _write(folder: Path, stem: str, closes, start="2025-01-01"):
    pd = pytest.importorskip("pandas")

    dates = pd.bdate_range(start, periods=len(closes))
    pd.DataFrame({"Close": list(closes)},
                 index=pd.Index(dates, name="Date")).to_parquet(
        folder / f"{stem}.parquet")
    return dates


def _series(n=120, base=100.0, drift=0.4):
    return [base + drift * i for i in range(n)]


def _panel(folder, n=120, start="2025-01-01"):
    """Six sectors with distinct drifts so the ranking is unambiguous."""
    dates = None
    for i, stem in enumerate(SECTORS):
        dates = _write(folder, stem, _series(n, drift=0.2 + 0.15 * i),
                       start=start)
    return dates


def _builder(folder, **cfg):
    cfg.setdefault("min_width", 5)
    cfg.setdefault("warmup", 30)
    return sd.SectorDatasetBuilder({"indices_dir": str(folder), **cfg})


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #
def test_builds_a_date_sector_panel(tmp_path):
    _panel(tmp_path)
    doc = _builder(tmp_path).build()

    assert doc["ok"] is True
    assert doc["provenance"]["n_sectors"] == len(SECTORS)
    assert doc["provenance"]["n_rows"] > 0
    assert doc["provenance"]["median_width"] == len(SECTORS)
    assert len(doc["rows"]) == doc["provenance"]["n_rows"]


def test_benchmarks_are_not_in_the_dataset(tmp_path):
    """A ranking must never contain NIFTY 50 or VIX."""
    _panel(tmp_path)
    _write(tmp_path, "NIFTY_50", _series(120, drift=0.3))
    _write(tmp_path, "INDIA_VIX", _series(120, base=14.0, drift=0.01))

    doc = _builder(tmp_path).build()
    sectors = set(doc["provenance"]["sectors"])
    assert "NIFTY_50" not in sectors
    assert "INDIA_VIX" not in sectors


def test_narrow_dates_are_dropped_and_counted(tmp_path):
    _panel(tmp_path, n=120)
    doc = _builder(tmp_path, min_width=99).build()
    # Nothing can be wide enough, so everything is filtered out.
    assert doc["rows"] == []
    assert doc["provenance"]["dates_dropped_narrow"] > 0


def test_unreadable_parquet_is_skipped_not_fatal(tmp_path):
    _panel(tmp_path)
    (tmp_path / "NIFTY_FMCG.parquet").write_bytes(b"not a parquet")

    doc = _builder(tmp_path).build()
    assert doc["ok"] is True
    assert "NIFTY_FMCG" not in doc["provenance"]["sectors"]


# --------------------------------------------------------------------------- #
# Look-ahead: the decisive property
# --------------------------------------------------------------------------- #
def test_features_are_identical_when_the_future_is_removed(tmp_path):
    """Truncating the panel must not change any historical feature.

    If a feature read a future bar, cutting that future away would alter it.
    """
    pd = pytest.importorskip("pandas")

    _panel(tmp_path, n=120)
    full = _builder(tmp_path).build()

    cut = pd.Timestamp("2025-04-01")
    orig = sd.SectorDatasetBuilder._load_panel

    def truncated(self):
        panel = orig(self)
        return panel[panel.index <= cut]

    sd.SectorDatasetBuilder._load_panel = truncated
    try:
        part = _builder(tmp_path).build()
    finally:
        sd.SectorDatasetBuilder._load_panel = orig

    full_map = {(r["as_of"], r["sector"]): r for r in full["rows"]}
    features = ["rsi", "ema_rsi", "hm_score", "rising", "below_level",
                "ret_5", "ret_20", "ret_60", "vol_20", "dist_ema20"]

    checked = 0
    for row in part["rows"]:
        ref = full_map[(row["as_of"], row["sector"])]
        for feat in features:
            assert row[feat] == pytest.approx(ref[feat], abs=1e-9), (
                f"{row['as_of']} {row['sector']} {feat} changed when the "
                f"future was removed - that is look-ahead")
        checked += 1
    assert checked > 0


def test_the_tail_carries_no_forward_labels(tmp_path):
    """The last `horizon` rows have no future, so their label MUST be None."""
    _panel(tmp_path)
    doc = _builder(tmp_path).build()

    dates = sorted({r["as_of"] for r in doc["rows"]})
    tail = set(dates[-5:])
    for row in doc["rows"]:
        if row["as_of"] in tail:
            assert row["fwd_5d"] is None
            assert row["label_5d"] is None

    head = [r for r in doc["rows"] if r["as_of"] == dates[0]]
    assert all(r["fwd_5d"] is not None for r in head)


def test_forward_return_matches_the_actual_price_change(tmp_path):
    """A label must equal the realised price move, not a proxy."""
    pd = pytest.importorskip("pandas")

    closes = _series(60)
    _write(tmp_path, "NIFTY_IT", closes)
    for stem in SECTORS[1:]:
        _write(tmp_path, stem, _series(60, drift=0.3))
    doc = _builder(tmp_path).build()

    row = next(r for r in doc["rows"]
               if r["sector"] == "NIFTY_IT" and r["fwd_5d"] is not None)
    close = pd.read_parquet(tmp_path / "NIFTY_IT.parquet")["Close"]
    idx = list(close.index.strftime("%Y-%m-%d")).index(row["as_of"])
    expected = close.iloc[idx + 5] / close.iloc[idx] - 1.0
    assert row["fwd_5d"] == pytest.approx(expected, rel=1e-9)


# --------------------------------------------------------------------------- #
# Cross-sectional labels
# --------------------------------------------------------------------------- #
def test_labels_are_per_date_only(tmp_path):
    """Each date must have its own top and bottom tercile, independently."""
    _panel(tmp_path)
    doc = _builder(tmp_path).build()

    by_date = {}
    for row in doc["rows"]:
        if row.get("label_5d") is not None:
            by_date.setdefault(row["as_of"], []).append(row)

    for as_of, group in by_date.items():
        labels = [r["label_5d"] for r in group]
        assert labels.count(1) >= 1, as_of
        assert labels.count(0) >= 1, as_of
        # The best forward return that date must be a label-1.
        best = max(group, key=lambda r: r["fwd_5d"])
        assert best["label_5d"] == 1


def test_middle_tercile_is_excluded_from_training(tmp_path):
    _panel(tmp_path)
    doc = _builder(tmp_path).build()

    by_date = {}
    for row in doc["rows"]:
        by_date.setdefault(row["as_of"], []).append(row)

    as_of, group = next((d, g) for d, g in sorted(by_date.items())
                        if all(r.get("label_5d") is not None
                               or r.get("label_5d") is None for r in g))
    ranked = sorted(group, key=lambda r: r["fwd_5d"], reverse=True)
    n = len(ranked)
    tier = max(1, n // 3)
    for i, row in enumerate(ranked):
        if tier <= i < n - tier:
            assert row["label_5d"] is None, (
                f"middle-tercile row {row['sector']} must not be labelled")


def test_a_too_narrow_date_gets_no_labels(tmp_path):
    _panel(tmp_path)
    doc = _builder(tmp_path, min_width=5).build()
    # With min_width 5 and 6 sectors, labels exist; raise the bar instead.
    doc2 = _builder(tmp_path, min_width=50).build()
    assert all(r["label_5d"] is None for r in doc2["rows"])
    assert any(r["label_5d"] is not None for r in doc["rows"])


# --------------------------------------------------------------------------- #
# Provenance honesty
# --------------------------------------------------------------------------- #
def test_date_range_is_the_first_and_last_date(tmp_path):
    # Regression: rows are grouped per sector, so rows[-1] is not the last date.
    # The field used to read rows[0]/rows[-1], silently reporting the range of
    # the LAST SECTOR's rows rather than the whole panel's.
    _panel(tmp_path)
    doc = _builder(tmp_path).build()

    dates = sorted({r["as_of"] for r in doc["rows"]})
    assert doc["provenance"]["date_range"] == [dates[0], dates[-1]]


def test_provenance_records_survivorship_dates(tmp_path):
    _panel(tmp_path, n=60, start="2025-01-01")
    _panel(tmp_path, n=60, start="2025-01-01")
    _write(tmp_path, "NIFTY_MNC", _series(30, drift=0.5), start="2025-03-03")

    doc = _builder(tmp_path).build()
    firsts = doc["provenance"]["first_date_per_sector"]
    assert firsts["NIFTY_BANK"] < firsts["NIFTY_MNC"], (
        "a late-starting sector must show its real start date, so the "
        "shorter history is visible rather than assumed away")


def test_empty_directory_reports_a_reason(tmp_path):
    doc = _builder(tmp_path).build()
    assert doc["ok"] is False
    assert doc["reason"]


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def test_persist_writes_a_reloadable_document(tmp_path):
    import json

    _panel(tmp_path)
    out = tmp_path / "ds.json"
    builder = _builder(tmp_path, dataset_path=str(out))
    doc = builder.build()
    path = builder.persist(doc)

    assert path is not None and path.is_file()
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["version"] == doc["version"]
    assert len(reloaded["rows"]) == len(doc["rows"])
