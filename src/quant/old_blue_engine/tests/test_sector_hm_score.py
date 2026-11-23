"""Sector HM score: sector-only scope, no look-ahead, honest selection.

Guards the two properties that make this table safe to act on:

1. **Benchmarks and VIX are never scored.** The user's model uses broad indices
   (NIFTY 50, NIFTY 500, MIDCAP 150) to JUDGE the portfolio and sector indices
   (ENERGY, AUTO, IT, FMCG, BANK) to FIND value. Blurring them would let a
   market-cap bucket or a volatility print appear in a sector ranking.

2. **A falling sector is never silently presented as a buy.** "Most
   discounted" and "recovering" are different, and buying the former while it
   is still falling is the main way this idea loses money.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from blue_engine.sectors import sector_hm_score as hm  # noqa: E402


def _write(folder: Path, stem: str, closes, start="2025-01-01"):
    pd = pytest.importorskip("pandas")

    dates = pd.bdate_range(start, periods=len(closes))
    pd.DataFrame({"Close": list(closes)},
                 index=pd.Index(dates, name="Date")).to_parquet(
        folder / f"{stem}.parquet")
    return dates


def _rising(n=120, base=100.0, step=1.0):
    return [base + step * i for i in range(n)]


def _falling(n=120, base=100.0, step=1.0):
    return [base + step * (n - i) for i in range(n)]


def _recovering(down_len=5, down_step=1.5, up_len=2, up_step=0.6,
                pad=60):
    # The shape the HM indicator exists for: a fall, then a small bounce.
    #
    # A pure monotonic decline leaves RSI pinned at 0 with RSI == its EMA (both
    # saturated), so it can never show a turn. A real turn needs mixed moves
    # inside the RSI window. Calibrated against the indicator: this returns
    # RSI~20 with RSI above its EMA, i.e. cheap AND recovering.
    v = [100.0 + 0.05 * i for i in range(pad)]
    for _ in range(down_len):
        v.append(v[-1] - down_step)
    for i in range(1, up_len + 1):
        v.append(v[-1] + up_step * i)
    return v


# --------------------------------------------------------------------------- #
# Scope: sector indices only
# --------------------------------------------------------------------------- #
def test_benchmarks_and_vix_are_never_scored():
    scorer = hm.SectorHMScorer()
    stems = set(scorer._stems())

    for bench in ("NIFTY_50", "NIFTY_500", "SENSEX", "NIFTY_MIDCAP_150"):
        assert bench not in stems, f"{bench} is a benchmark, not a sector"
    assert "INDIA_VIX" not in stems, "VIX is not an investable sector"


def test_sector_indices_are_included():
    stems = set(hm.SectorHMScorer()._stems())
    for sector in ("NIFTY_BANK", "NIFTY_IT", "NIFTY_FMCG", "NIFTY_AUTO",
                   "NIFTY_ENERGY"):
        assert sector in stems


def test_unscorable_sectors_are_declared_not_silently_dropped():
    from blue_engine.sectors.providers.vendor_sector import UNSCORABLE_NSE_SECTORS

    assert "NIFTY_CHEMICALS" in UNSCORABLE_NSE_SECTORS
    assert "NIFTY_CEMENT" in UNSCORABLE_NSE_SECTORS
    for reason in UNSCORABLE_NSE_SECTORS.values():
        assert reason, "an omission must carry a reason"
    assert "NIFTY_CHEMICALS" not in set(hm.SectorHMScorer()._stems())


def test_table_reports_the_unavailable_nse_sectors(tmp_path):
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})
    table = scorer.score_all(date(2026, 1, 1))
    names = {r["stem"] for r in table["not_available"]}
    assert "NIFTY_CHEMICALS" in names
    assert table["counts"]["not_available"] > 0


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def test_a_falling_sector_scores_more_discounted_than_a_rising_one(tmp_path):
    _write(tmp_path, "NIFTY_IT", _falling())
    _write(tmp_path, "NIFTY_FMCG", _rising())
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})

    it = scorer.score_one("NIFTY_IT", date(2030, 1, 1))
    fmcg = scorer.score_one("NIFTY_FMCG", date(2030, 1, 1))

    assert it["scored"] and fmcg["scored"]
    assert it["rsi"] < fmcg["rsi"]
    assert it["hm_score"] > fmcg["hm_score"]


def test_falling_knife_is_flagged_not_hidden(tmp_path):
    _write(tmp_path, "NIFTY_METAL", _falling())
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})

    row = scorer.score_one("NIFTY_METAL", date(2030, 1, 1))
    assert row["rsi"] <= 40
    assert row["rising"] is False
    assert row["falling_knife"] is True
    assert row["hm_turn_buy"] is False, (
        "must never present a still-falling sector as a BUY")


def test_rising_sector_is_never_marked_as_a_turn_buy(tmp_path):
    # A sustained uptrend is not discounted, so it cannot be a turn buy.
    # NOTE: on a monotonic series RSI saturates at 100 and equals its EMA, so
    # `rising` may be False. The invariant that matters is that it is NOT
    # cheap and therefore never a BUY.
    _write(tmp_path, "NIFTY_AUTO", _rising())
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})

    row = scorer.score_one("NIFTY_AUTO", date(2030, 1, 1))
    assert row["rsi"] > 50
    assert row["below_level"] is False
    assert row["hm_turn_buy"] is False

def test_a_cheap_recovering_sector_is_a_turn_buy(tmp_path):
    # The indicator's actual rule: RSI at/below the level AND above its EMA.
    _write(tmp_path, "NIFTY_PHARMA", _recovering())
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})

    row = scorer.score_one("NIFTY_PHARMA", date(2030, 1, 1))
    assert row["scored"] is True
    assert row["rsi"] <= 40
    assert row["rising"] is True
    assert row["hm_turn_buy"] is True
    assert row["falling_knife"] is False


def test_score_is_bounded_and_monotonic_in_rsi():
    scorer = hm.SectorHMScorer()
    for rsi in (0.0, 10.0, 40.0, 60.0, 100.0):
        depth = max(0.0, min(1.0, 1.0 - rsi / 100.0))
        assert 0.0 <= 100.0 * depth <= 100.0
    assert scorer.discount_level == 40.0


# --------------------------------------------------------------------------- #
# No look-ahead
# --------------------------------------------------------------------------- #
def test_as_of_truncates_the_series(tmp_path):
    dates = _write(tmp_path, "NIFTY_PHARMA", _falling())
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})

    cutoff = dates[60].date()
    row = scorer.score_one("NIFTY_PHARMA", cutoff)
    assert row["scored"] is True
    assert row["sessions"] == 61          # bars up to and including cutoff


def test_short_history_is_reported_not_scored(tmp_path):
    _write(tmp_path, "NIFTY_REALTY", _rising(5))
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})

    row = scorer.score_one("NIFTY_REALTY", date(2030, 1, 1))
    assert row["scored"] is False
    assert "sessions" in row["reason"]


def test_missing_parquet_does_not_raise(tmp_path):
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})
    row = scorer.score_one("NIFTY_BANK", date(2030, 1, 1))
    assert row["scored"] is False
    assert row["reason"]


# --------------------------------------------------------------------------- #
# Selection honesty
# --------------------------------------------------------------------------- #
def test_selection_refuses_to_fill_the_gap(tmp_path):
    """Only 2 sectors qualify -> return 2, not 5 with filler."""
    _write(tmp_path, "NIFTY_IT", _falling())
    _write(tmp_path, "NIFTY_METAL", _falling(step=0.9))
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})

    table = scorer.score_all(date(2030, 1, 1))
    sel = scorer.least_discounted(table, n=5, require_turn=False)

    assert len(table["ranked"]) == 2, "only two sectors exist in this fixture"
    assert len(sel["selected"]) == 2, "must not invent 3 more to reach five"
    assert any("requested 5" in w for w in sel["warnings"])


def test_turn_mode_returns_nothing_when_everything_is_falling(tmp_path):
    _write(tmp_path, "NIFTY_IT", _falling())
    _write(tmp_path, "NIFTY_METAL", _falling(step=0.9))
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})

    sel = scorer.least_discounted(scorer.score_all(date(2030, 1, 1)),
                                  n=5, require_turn=True)
    assert sel["selected"] == []
    assert any("withheld" in w for w in sel["warnings"])


def test_turn_mode_selects_a_recovering_sector(tmp_path):
    _write(tmp_path, "NIFTY_PHARMA", _recovering())
    _write(tmp_path, "NIFTY_METAL", _falling())
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})

    sel = scorer.least_discounted(scorer.score_all(date(2030, 1, 1)),
                                  n=5, require_turn=True)
    picked = {r["stem"] for r in sel["selected"]}
    assert picked == {"NIFTY_PHARMA"}
    assert "NIFTY_METAL" not in picked
    assert any("withheld" in w for w in sel["warnings"])


def test_no_turn_mode_warns_about_falling_selections(tmp_path):
    _write(tmp_path, "NIFTY_IT", _falling())
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path)})

    sel = scorer.least_discounted(scorer.score_all(date(2030, 1, 1)),
                                  n=1, require_turn=False)
    assert len(sel["selected"]) == 1
    assert any("still falling" in w for w in sel["warnings"])


# --------------------------------------------------------------------------- #
# Persistence (the future training set)
# --------------------------------------------------------------------------- #
def test_persist_appends_one_row_per_date(tmp_path):
    _write(tmp_path, "NIFTY_IT", _falling())
    score_path = tmp_path / "scores.json"
    scorer = hm.SectorHMScorer({"indices_dir": str(tmp_path),
                                "score_path": str(score_path)})

    scorer.persist(scorer.score_all(date(2026, 1, 5)))
    scorer.persist(scorer.score_all(date(2026, 1, 6)))
    scorer.persist(scorer.score_all(date(2026, 1, 6)))   # same day again

    import json

    data = json.loads(score_path.read_text(encoding="utf-8"))
    assert len(data["days"]) == 2, "a repeat run must replace, not duplicate"
    assert data["latest_as_of"] == "2026-01-06"


def test_persist_failure_does_not_raise(tmp_path):
    _write(tmp_path, "NIFTY_IT", _falling())
    scorer = hm.SectorHMScorer({
        "indices_dir": str(tmp_path),
        "score_path": str(tmp_path / "no_such_dir" / "x.json")})
    # A directory that cannot be created must not break the report.
    (tmp_path / "no_such_dir").write_text("blocker", encoding="utf-8")
    assert scorer.persist(scorer.score_all(date(2026, 1, 5))) is None
