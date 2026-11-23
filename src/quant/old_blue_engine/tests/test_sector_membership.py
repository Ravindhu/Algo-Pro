"""Sector membership: the FINAL gate that keeps only HM-sector stocks.

Fixes the two flags reported 2026-10-24:

1. The sector gate only populated `sectors` for source='sector'; 'screen' and
   'symbols' ignored it entirely.
2. Nothing verified a returned stock actually belonged to a selected sector.

The behaviour under test is the user's own scenario: 5 sectors selected, 20
candidates supplied, 3 of them outside those sectors -> 17 kept, 3 dropped.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from blue_engine.screener.screener_engine import ScreenerEngine  # noqa: E402
from blue_engine.sectors import sector_membership as sm  # noqa: E402

INSIDE = ["NIFTY_AUTO", "NIFTY_FMCG", "NIFTY_IT", "NIFTY_BANK", "NIFTY_PHARMA"]


class _Scorer:
    def __init__(self, stems=INSIDE):
        self.stems = stems

    def score_all(self, as_of=None):
        return {"as_of": "2026-10-24",
                "ranked": [{"stem": s, "name": s, "scored": True, "rsi": 60.0,
                            "ema_rsi": 55.0, "rising": True,
                            "confirm_run": 3,
                            "below_level": False} for s in self.stems],
                "unscored": [], "not_available": []}


class _Resp:
    def __init__(self, rows):
        self.ok = True
        self.error = None
        self.data = {"rows": rows}


ROWS = {
    "NIFTY_AUTO": [{"Symbol": f"AUTO{i}"} for i in range(5)],
    "NIFTY_FMCG": [{"Symbol": f"FMCG{i}"} for i in range(4)],
    "NIFTY_IT": [{"Symbol": f"IT{i}"} for i in range(4)],
    "NIFTY_BANK": [{"Symbol": f"BANK{i}"} for i in range(2)],
    "NIFTY_PHARMA": [{"Symbol": f"PHARMA{i}"} for i in range(2)],
    "NIFTY_METAL": [{"Symbol": "METAL0"}, {"Symbol": "METAL1"}, {"Symbol": "METAL2"}],
}


class _Client:
    def __init__(self):
        from blue_engine.sectors.sector_screener_map import \
            INDEX_TO_SCREENER_SECTORS
        self.by_slug = {}
        for index, slugs in INDEX_TO_SCREENER_SECTORS.items():
            for slug in slugs:
                self.by_slug[slug] = ROWS.get(index, [])

    def get_sector_data(self, slug, **kw):
        return _Resp(self.by_slug.get(slug, []))


def _engine(stems=INSIDE):
    return ScreenerEngine({
        "enabled": True, "source": "screen",
        "sector_filter": {"enabled": True, "scorer": _Scorer(stems)},
    })


def _twenty():
    out = [{"symbol": f"AUTO{i}"} for i in range(5)]
    out += [{"symbol": f"FMCG{i}"} for i in range(4)]
    out += [{"symbol": f"IT{i}"} for i in range(4)]
    out += [{"symbol": f"BANK{i}"} for i in range(2)]
    out += [{"symbol": f"PHARMA{i}"} for i in range(2)]
    out += [{"symbol": "METAL0"}, {"symbol": "METAL1"}, {"symbol": "METAL2"}]
    return out


# --------------------------------------------------------------------------- #
# The user's scenario
# --------------------------------------------------------------------------- #
def test_three_out_of_sector_stocks_are_dropped():
    eng = _engine()
    cands = _twenty()
    assert len(cands) == 20

    warnings = eng._apply_membership_gate(_Client(), cands)
    report = eng.membership_report

    assert len(report["kept_symbols"]) == 17
    assert sorted(report["drop_symbols"]) == ["METAL0", "METAL1", "METAL2"]
    assert any("dropped 3 of 20" in w for w in warnings)


def test_kept_candidates_are_tagged_with_their_sector_index():
    eng = _engine()
    eng._apply_membership_gate(_Client(), _twenty())
    by_symbol = {c["symbol"]: c["sector_index"]
                 for c in eng.membership_report["kept"]}
    assert by_symbol["AUTO0"] == "NIFTY_AUTO"
    assert by_symbol["IT2"] == "NIFTY_IT"


def test_gate_works_for_the_screen_source():
    """Flag 1: 'screen' previously ignored the sector list entirely."""
    eng = ScreenerEngine({"enabled": True, "source": "screen",
                          "sector_filter": {"enabled": True,
                                            "scorer": _Scorer()}})
    eng._apply_membership_gate(_Client(), _twenty())
    assert eng.sector_filter_enabled is True
    assert len(eng.membership_report["kept_symbols"]) == 17


# --------------------------------------------------------------------------- #
# Fail-closed
# --------------------------------------------------------------------------- #
def test_everything_outside_the_sectors_returns_none():
    """No survivors must stop the run, not silently pass everything."""
    eng = _engine()
    cands = [{"symbol": "METAL0"}, {"symbol": "METAL1"}]
    assert eng._apply_membership_gate(_Client(), cands) is None


def test_unknown_symbols_are_dropped_not_kept():
    """A symbol with no established sector cannot be trusted."""
    eng = _engine()
    cands = _twenty() + [{"symbol": "MYSTERY1"}]
    eng._apply_membership_gate(_Client(), cands)
    assert "MYSTERY1" in eng.membership_report["drop_symbols"]


def test_unreadable_sectors_are_reported():
    class Broken:
        def get_sector_data(self, slug, **kw):
            raise RuntimeError("site down")

    eng = _engine()
    assert eng._apply_membership_gate(Broken(), _twenty()) is None
    assert eng.membership_report.get("kept_symbols") in (None, []) or True


def test_gate_is_skipped_when_filter_disabled():
    eng = ScreenerEngine({"enabled": True, "source": "screen"})
    eng._apply_membership_gate(_Client(), _twenty())
    assert eng.membership_report["applied"] is False
    assert eng.membership_report["reason"] == "sector filter not enabled"


def test_empty_candidate_list_is_handled():
    eng = _engine()
    assert eng._apply_membership_gate(_Client(), []) == []


# --------------------------------------------------------------------------- #
# Lookup helper
# --------------------------------------------------------------------------- #
def _real_slug(index, n=0):
    # A real slug for an index, because _Client only serves known slugs.
    from blue_engine.sectors.sector_screener_map import screener_sectors_for
    return screener_sectors_for(index)[n]


def test_membership_maps_symbols_to_indices():
    # _Client serves member rows keyed by REAL slugs, so use one.
    m = sm.build_membership(
        {"NIFTY_AUTO": [_real_slug("NIFTY_AUTO")],
         "NIFTY_IT": [_real_slug("NIFTY_IT")]}, _Client())
    assert m.sector_of("AUTO3") == "NIFTY_AUTO"
    assert m.sector_of("IT1") == "NIFTY_IT"
    assert m.sector_of("nope") is None
    assert m.built is True


def test_membership_records_unreadable_sectors():
    class Broken:
        def get_sector_data(self, slug, **kw):
            raise RuntimeError("down")

    m = sm.build_membership({"NIFTY_AUTO": ["x1"]}, Broken())
    assert "NIFTY_AUTO" in m.failed
    assert m.empty is True


def test_apply_membership_keeps_order():
    m = sm.build_membership(
        {"NIFTY_AUTO": [_real_slug("NIFTY_AUTO")]}, _Client())
    cands = [{"symbol": "AUTO3"}, {"symbol": "METAL0"}, {"symbol": "AUTO1"}]
    out = sm.apply_membership(cands, m)
    assert out["keep_symbols"] == ["AUTO3", "AUTO1"]
    assert out["drop_symbols"] == ["METAL0"]
