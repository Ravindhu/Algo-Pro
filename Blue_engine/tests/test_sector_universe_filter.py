"""Sector universe filter: the measured gate, honest fail-closed behaviour.

The rule under test is RSI > EMA with NO discount cap, because that is what the
out-of-sample data supported (0.518 / 0.535 hit rate vs 0.500 random, where the
RSI<=40 cap scored 0.454 / 0.446). These tests protect that choice from being
quietly reverted to the more intuitive but losing version.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from blue_engine.sectors import sector_screener_map as smap  # noqa: E402
from blue_engine.sectors import sector_universe_filter as suf  # noqa: E402


class _FakeScorer:
    """Returns a fixed table so the gate logic is tested without any data."""

    def __init__(self, rows):
        self._rows = rows

    def score_all(self, as_of=None):
        return {"as_of": (as_of or date(2026, 1, 1)).isoformat(),
                "ranked": self._rows, "unscored": [], "not_available": []}


def _row(stem, rsi, ema, scored=True):
    return {"stem": stem, "name": stem, "scored": scored, "rsi": rsi,
            "ema_rsi": ema, "rising": rsi > ema, "confirm_run": 3,
            "below_level": rsi <= 40,
            "rising_flag": rsi > ema}


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def test_gate_selects_turning_sectors_at_any_level():
    """RSI>EMA qualifies even when the sector is NOT below 40."""
    scorer = _FakeScorer([
        _row("NIFTY_AUTO", 81.4, 74.1),    # strong, turning up
        _row("NIFTY_IT", 78.1, 73.6),
    ])
    out = suf.SectorUniverseFilter({"scorer": scorer}).qualifying_sectors()
    assert out["ok"] is True
    assert out["selected"] == ["NIFTY_AUTO", "NIFTY_IT"]
    assert "RSI > EMA" in out["rule"]


def test_gate_excludes_sectors_still_falling():
    scorer = _FakeScorer([
        _row("NIFTY_METAL", 20.0, 25.0),   # deeply discounted but falling
    ])
    out = suf.SectorUniverseFilter({"scorer": scorer}).qualifying_sectors()
    assert out["selected"] == []


def test_the_discount_cap_is_off_by_default():
    """The cap measured NEGATIVE, so it must not be applied silently."""
    scorer = _FakeScorer([_row("NIFTY_AUTO", 81.4, 74.1)])
    f = suf.SectorUniverseFilter({"scorer": scorer})
    assert f.require_discount is False
    assert f.qualifying_sectors()["selected"] == ["NIFTY_AUTO"]

    capped = suf.SectorUniverseFilter({"scorer": scorer,
                                       "require_discount": True})
    assert capped.qualifying_sectors()["selected"] == [], (
        "with the cap on, an extended sector must be excluded - proving the "
        "cap is what was filtering real signals")


def test_legacy_below_level_mode_still_available():
    scorer = _FakeScorer([
        _row("NIFTY_FMCG", 23.9, 21.0),
        _row("NIFTY_AUTO", 81.4, 74.1),
    ])
    out = suf.SectorUniverseFilter(
        {"scorer": scorer, "mode": "below_level"}).qualifying_sectors()
    assert out["selected"] == ["NIFTY_FMCG"]
    assert "40" in out["rule"]


def test_unscored_rows_are_ignored():
    scorer = _FakeScorer([_row("NIFTY_CEMENT", 50, 40, scored=False)])
    assert suf.SectorUniverseFilter({"scorer": scorer}
                                    ).qualifying_sectors()["selected"] == []


# --------------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------------- #
def test_empty_selection_yields_empty_universe_not_all_sectors():
    """Silently widening the universe would remove the gate."""
    scorer = _FakeScorer([_row("NIFTY_METAL", 20.0, 25.0)])
    out = suf.SectorUniverseFilter({"scorer": scorer}).screener_slugs()
    assert out["slugs"] == []
    assert out["sectors"] == []
    assert any("EMPTY" in w for w in out["warnings"])


def test_scorer_failure_degrades_to_empty_with_a_reason():
    class Boom:
        def score_all(self, as_of=None):
            raise RuntimeError("data gone")

    out = suf.SectorUniverseFilter({"scorer": Boom()}).screener_slugs()
    assert out["ok"] is False
    assert out["slugs"] == []
    assert "RuntimeError" in out["reason"]


def test_unmapped_sector_is_reported_not_silently_dropped():
    """NIFTY PSE passes the gate but has no screener sector."""
    scorer = _FakeScorer([_row("NIFTY_PSE", 61.9, 57.2),
                          _row("NIFTY_AUTO", 81.4, 74.1)])
    out = suf.SectorUniverseFilter({"scorer": scorer}).screener_slugs()
    assert "NIFTY_AUTO" in out["sectors"]
    assert "NIFTY_PSE" not in out["sectors"]
    assert any(u["sector"] == "NIFTY_PSE" for u in out["unmapped"])
    assert any("NIFTY_PSE" in w for w in out["warnings"])


# --------------------------------------------------------------------------- #
# Slug expansion
# --------------------------------------------------------------------------- #
def test_slugs_are_deduplicated_and_expanded():
    scorer = _FakeScorer([_row("NIFTY_BANK", 60, 50),
                          _row("NIFTY_PSU_BANK", 61, 50)])
    out = suf.SectorUniverseFilter({"scorer": scorer}).screener_slugs()
    assert len(out["slugs"]) == len(set(out["slugs"]))
    # PSU Bank's slug is also inside NIFTY_BANK's list -> deduped once.
    assert "IN05/IN0501/IN050102/IN050102001" in out["slugs"]


def test_every_mapped_index_has_slugs_and_unmapped_have_none():
    for index in smap.mapped_indices():
        assert smap.screener_sectors_for(index), index
        assert smap.is_mapped(index)
    for index in smap.UNMAPPED_SECTORS:
        assert not smap.is_mapped(index)
        assert smap.unmapped_reason(index)


def test_slug_format_looks_like_an_in_path():
    """A malformed slug would 404 and silently screen nothing."""
    for index, slugs in smap.INDEX_TO_SCREENER_SECTORS.items():
        for slug in slugs:
            parts = slug.split("/")
            assert len(parts) == 4, f"{index}: {slug}"
            assert all(p.startswith("IN") and p[2:].isdigit()
                       for p in parts), f"{index}: {slug}"
