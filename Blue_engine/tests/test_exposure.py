"""
tests/test_exposure.py — Phase 3a tests: exposure & concentration.

Covers sector bucketing, long/short shape, beta exposure (measured vs assumed),
concentration metrics, look-through, and the degradation paths.

Run::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q
    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_exposure.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio import exposure as ex  # noqa: E402


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# 1. Sector bucketing
# --------------------------------------------------------------------------- #
def test_sector_of_exact_and_substring():
    _check(ex.sector_of("IT")[0] == "Information Technology", "exact IT")
    _check(ex.sector_of("NIFTY IT")[0] == "Information Technology",
           "substring NIFTY IT should map to IT")
    _check(ex.sector_of("BANKNIFTY")[0] == "Financials", "BANKNIFTY banks")
    _check(ex.sector_of("SMALLCAP")[0] == "Mid/Small Cap",
           "SMALLCAP is its own size bucket")


def test_unknown_sleeve_becomes_its_own_bucket():
    bucket, beta = ex.sector_of("WEIRD_NEW_THING")
    _check(bucket == "WEIRD_NEW_THING", f"unknown -> own bucket, got {bucket}")
    _check(beta is None, "no assumed beta for an unknown sleeve")


def test_sector_map_config_override():
    cfg = {"sleeves": {"IT": {"bucket": "Technology", "beta": 0.95}}}
    _check(ex.sector_of("IT", cfg)[0] == "Technology", "config should override")
    _check(ex.sector_of("IT", cfg)[1] == 0.95, "config beta should override")


def test_exposure_by_sector_aggregates_same_bucket():
    """IT + MIDCAP must NOT be treated as independent — MIDCAP shares the bucket."""
    w = {"IT": 0.15, "NIFTYIT": 0.10, "BANKNIFTY": 0.20}
    out = ex.exposure_by_sector(w)
    it = out["sectors"]["Information Technology"]
    _check(abs(it["weight"] - 0.25) < 1e-9,
           f"IT + NIFTYIT should aggregate to 0.25, got {it['weight']}")
    _check(sorted(it["sleeves"]) == ["IT", "NIFTYIT"],
           f"both sleeves listed: {it['sleeves']}")


def test_exposure_by_sector_reports_concentration_share():
    w = {"BANKNIFTY": 0.30, "IT": 0.10}
    out = ex.exposure_by_sector(w)
    fin = out["sectors"]["Financials"]
    _check(abs(fin["share_of_gross"] - 0.75) < 1e-9,
           f"banks are 0.30 of 0.40 gross = 75%, got {fin['share_of_gross']}")


def test_exposure_by_sector_empty_degrades():
    out = ex.exposure_by_sector({})
    _check(out["ok"] is False, "no weights -> ok False")
    _check(out["warnings"], "should explain why")


def test_exposure_by_sector_warns_on_unmapped_sleeve():
    out = ex.exposure_by_sector({"ZZZ_NEW": 0.2})
    _check(any("no sector mapping" in w for w in out["warnings"]),
           f"unmapped sleeve should be flagged: {out['warnings']}")


# --------------------------------------------------------------------------- #
# 2. Sleeve exposure
# --------------------------------------------------------------------------- #
def test_exposure_by_sleeve_shares_and_side():
    w = {"A": 0.30, "B": -0.10}
    out = ex.exposure_by_sleeve(w)
    _check(out["gross"] == 0.40, f"gross = 0.30 + 0.10 = 0.40, got {out['gross']}")
    _check(out["net"] == 0.20, f"net = 0.20, got {out['net']}")
    _check(out["sleeves"]["A"]["side"] == "LONG", "positive weight is LONG")
    _check(out["sleeves"]["B"]["side"] == "SHORT", "negative weight is SHORT")
    _check(abs(out["sleeves"]["A"]["share_of_gross"] - 0.75) < 1e-9,
           "A is 0.30/0.40 of gross")


def test_exposure_by_sleeve_orders_by_absolute_weight():
    out = ex.exposure_by_sleeve({"SMALL": 0.05, "BIG": 0.50})
    _check(list(out["sleeves"])[0] == "BIG", "largest exposure first")


# --------------------------------------------------------------------------- #
# 3. Long/short shape
# --------------------------------------------------------------------------- #
def test_long_short_split():
    w = {"A": 0.40, "B": 0.20, "C": -0.15}
    out = ex.long_short_split(w)
    _check(abs(out["gross_long"] - 0.60) < 1e-9, "longs = 0.40+0.20")
    _check(abs(out["gross_short"] - 0.15) < 1e-9, "shorts = 0.15")
    _check(abs(out["net"] - 0.45) < 1e-9, "net = 0.60 - 0.15")
    _check(abs(out["net_to_gross"] - (0.45 / 0.75)) < 1e-9,
           "net/gross ratio")
    _check(out["n_long"] == 2 and out["n_short"] == 1, "counts")


def test_long_short_all_cash_is_neutral():
    out = ex.long_short_split({})
    _check(out["ok"] is False, "empty book -> ok False")
    _check(out["gross"] == 0.0, "no exposure")


# --------------------------------------------------------------------------- #
# 4. Beta exposure
# --------------------------------------------------------------------------- #
def test_beta_from_measured_map():
    w = {"A": 0.5, "B": 0.5}
    out = ex.beta_exposure(w, betas={"A": 1.2, "B": 0.8})
    _check(abs(out["beta"] - 1.0) < 1e-9,
           f"0.5*1.2 + 0.5*0.8 = 1.0, got {out['beta']}")
    _check(out["n_measured"] == 2 and out["n_assumed"] == 0, "all measured")


def test_beta_falls_back_to_desk_assumption():
    w = {"BANKNIFTY": 1.0}
    out = ex.beta_exposure(w)
    _check(abs(out["beta"] - 1.15) < 1e-9,
           f"BANKNIFTY desk beta is 1.15, got {out['beta']}")
    _check(out["n_assumed"] == 1, "should be counted as assumed")
    _check(any("assumptions" in x for x in out["warnings"]),
           f"assumed betas must be flagged: {out['warnings']}")


def test_beta_flags_a_blend_of_measured_and_assumed():
    """A report that silently blends measured and assumed betas is misleading."""
    w = {"A": 0.5, "BANKNIFTY": 0.5}
    out = ex.beta_exposure(w, betas={"A": 1.0})
    _check(out["n_measured"] == 1 and out["n_assumed"] == 1, "one of each")
    _check(any("blends" in x for x in out["warnings"]),
           f"a mixed basis must be flagged: {out['warnings']}")


def test_beta_notional_is_beta_times_capital():
    out = ex.beta_exposure({"BANKNIFTY": 1.0}, capital=1_000_000.0)
    _check(abs(out["beta_notional"] - 1_150_000.0) < 1.0,
           f"1.15 x 10 lakh = 11.5 lakh, got {out['beta_notional']}")


def test_beta_of_cash_sleeve_is_zero():
    out = ex.beta_exposure({"CASH": 1.0, "GOLD": 1.0})
    # 0.5*0.0 + 0.5*0.10 = 0.05
    _check(abs(out["beta"] - 0.05) < 1e-9,
           f"cash is beta-free, gold near-zero: got {out['beta']}")


def test_beta_empty_degrades():
    out = ex.beta_exposure({})
    _check(out["ok"] is False and out["beta"] is None, "no weights -> no beta")


# --------------------------------------------------------------------------- #
# 5. Concentration
# --------------------------------------------------------------------------- #
def test_concentration_equal_weights_gives_n_effective_bets():
    w = {f"S{i}": 0.25 for i in range(4)}
    out = ex.concentration(w)
    _check(abs(out["hhi"] - 0.25) < 1e-9,
           f"4 equal sleeves -> HHI 0.25, got {out['hhi']}")
    _check(abs(out["effective_bets"] - 4.0) < 1e-9,
           f"effective bets should be 4, got {out['effective_bets']}")


def test_concentration_single_sleeve_is_one_bet():
    out = ex.concentration({"ONLY": 1.0})
    _check(abs(out["effective_bets"] - 1.0) < 1e-9,
           f"one sleeve = one bet, got {out['effective_bets']}")
    _check(out["largest_sleeve_share"] == 1.0, "100% in one sleeve")


def test_concentration_reports_largest_sector():
    w = {"BANKNIFTY": 0.40, "BANK": 0.10, "IT": 0.10, "PHARMA": 0.05}
    out = ex.concentration(w)
    _check(out["largest_sector"] == "Financials",
           f"banks aggregate to 0.50, got {out['largest_sector']}")
    _check(abs(out["largest_sector_share"] - 0.5 / 0.65) < 1e-6,
           f"sector share wrong: {out['largest_sector_share']}")


def test_concentration_ignores_sign():
    """Concentration is about exposure magnitude; a short is still a bet."""
    out = ex.concentration({"A": -0.90, "B": 0.10})
    _check(abs(out["effective_bets"] - 1.2195) < 0.01,
           f"a 90% short is concentrated, got {out['effective_bets']}")


def test_concentration_empty_degrades():
    out = ex.concentration({})
    _check(out["ok"] is False and out["hhi"] is None, "no weights -> None")


# --------------------------------------------------------------------------- #
# 6. Look-through
# --------------------------------------------------------------------------- #
def test_lookthrough_splits_an_index_sleeve_into_components():
    """A 60% NIFTY sleeve is NOT one bet — it is ~35% Financials by construction."""
    w = {"NIFTY": 0.60, "BANKNIFTY": 0.20}
    cons = {"NIFTY": {"Financials": 0.35, "Information Technology": 0.15,
                      "Energy": 0.10, "Large Cap / Diversified": 0.40},
            "BANKNIFTY": {"Financials": 1.0}}
    out = ex.lookthrough(w, cons)
    _check(out["ok"] is True, f"look-through should work: {out}")
    fin = out["sectors"]["Financials"]
    # 0.60*0.35 + 0.20*1.0 = 0.21 + 0.20 = 0.41
    _check(abs(fin - 0.41) < 1e-6,
           f"true Financials exposure is 0.41, got {fin}")


def test_lookthrough_renormalises_and_warns_on_partial_map():
    w = {"X": 0.50}
    cons = {"X": {"A": 0.30, "B": 0.30}}      # sums to 0.60, not 1.0
    out = ex.lookthrough(w, cons)
    _check(any("renormalised" in x for x in out["warnings"]),
           f"a partial map must be flagged: {out['warnings']}")
    _check(abs(out["sectors"]["A"] - 0.25) < 1e-6,
           f"0.30/0.60 of 0.50 = 0.25, got {out['sectors']['A']}")


def test_lookthrough_without_a_map_degrades():
    out = ex.lookthrough({"NIFTY": 1.0}, None)
    _check(out["ok"] is False, "no map -> ok False, not a crash")
    _check(out["coverage"] == 0.0, "nothing covered")


def test_lookthrough_coverage_reported():
    w = {"A": 0.5, "B": 0.5}
    cons = {"A": {"X": 1.0}}                  # B unmapped
    out = ex.lookthrough(w, cons)
    _check(abs(out["coverage"] - 0.5) < 1e-9,
           f"half the book is covered, got {out['coverage']}")


# --------------------------------------------------------------------------- #
# 7. One-call analyse
# --------------------------------------------------------------------------- #
def test_analyse_has_all_sections():
    w = {"NIFTY": 0.30, "BANKNIFTY": 0.20, "IT": 0.10}
    out = ex.analyse(w, capital=1_000_000.0)
    _check(out["ok"] is True, f"analyse should succeed: {out.get('warnings')}")
    for key in ("by_sleeve", "by_sector", "long_short", "beta",
                "concentration"):
        _check(key in out, f"analyse missing '{key}'")
    _check(out["beta"]["beta"] is not None, "beta should compute")


def test_analyse_includes_lookthrough_when_map_given():
    w = {"NIFTY": 1.0}
    out = ex.analyse(w, constituents={"NIFTY": {"Financials": 0.35,
                                                "Other": 0.65}})
    _check("lookthrough" in out, "look-through should be included when mapped")
    _check(out["lookthrough"]["ok"] is True, "look-through should succeed")


def test_analyse_empty_degrades():
    out = ex.analyse({})
    _check(out["ok"] is False, "empty book -> ok False")
    _check(isinstance(out["warnings"], list), "warnings must be a list")


# --------------------------------------------------------------------------- #
# Runner (works without pytest)
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if fn.__code__.co_argcount:        # needs pytest fixtures
                print(f"SKIP {name} (needs pytest fixtures)")
                continue
            fn()
            print(f"PASS {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: {exc!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())