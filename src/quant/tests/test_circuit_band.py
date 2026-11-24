"""Price band from the broker quote -> circuit_pct / asm_gsm for the screen file.

The bug these lock down (2026-11-24): the Screener.in payload never carried a
circuit limit or a surveillance flag, so ``screen_rows`` dropped all 14 rows and
``data/screened/latest.json`` was written as ``{"results": []}``. The bot saw no
symbols and no strategy could fire. Nothing may invent either field.
"""
import pytest

from quant.data.circuit_band import (
    MAX_CIRCUIT, SYMMETRY_TOL, band_from_quote, enrich_candidates,
)


def quote(prev_close=None, upper=None, lower=None, **extra):
    data = dict(extra)
    if prev_close is not None:
        data["prev_close"] = prev_close
    if upper is not None:
        data["upper_circuit_limit"] = upper
    if lower is not None:
        data["lower_circuit_limit"] = lower
    return {"status": "success", "data": data}


# --------------------------------------------------------------------------- #
# band maths
# --------------------------------------------------------------------------- #
def test_twenty_percent_band():
    out = band_from_quote(quote(1500, 1800, 1200))
    assert out["circuit_pct"] == 0.20 and out["prev_close"] == 1500.0
    assert out["reason"] == ""


def test_five_percent_band_is_accepted():
    """SME / Band-2 names trade at 5%; a 20%-only rule would wrongly drop them."""
    assert band_from_quote(quote(100, 105, 95))["circuit_pct"] == 0.05


def test_band_uses_supplied_prev_close_over_the_quote():
    """The caller's verified daily close wins, so the ratio matches the bot's own check.

    The band in the quote is built from close 1000 (800/1200). Passing 1150 makes the
    ratio disagree with the band, which is exactly what must be detected.
    """
    agreeing = band_from_quote(quote(1000, 1200, 800), prev_close=1000.0)
    assert agreeing["prev_close"] == 1000.0 and agreeing["circuit_pct"] == 0.20

    disagreeing = band_from_quote(quote(1000, 1200, 800), prev_close=1150.0)
    assert disagreeing["prev_close"] == 1150.0
    assert disagreeing["circuit_pct"] is None
    assert "asymmetric" in disagreeing["reason"]


def test_a_stale_prev_close_outside_the_band_is_refused():
    """A previous close the exchange did not use must not yield an invented band."""
    out = band_from_quote(quote(1000, 1200, 800), prev_close=1500.0)
    assert out["circuit_pct"] is None
    assert out["reason"] == "previous close outside the quoted band"


def test_band_falls_back_to_quote_close():
    out = band_from_quote({"status": "success",
                           "data": {"close": 100, "upper_circuit_limit": 110,
                                    "lower_circuit_limit": 90}})
    assert out["circuit_pct"] == 0.10


# --------------------------------------------------------------------------- #
# every failure shape must return None with a reason - never a guess
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reply,needle", [
    ({"status": "error"}, "not successful"),
    (None, "not successful"),
    ({"status": "success", "data": {}}, "no circuit limits"),
    (quote(100), "no circuit limits"),
    (quote(None, 120, 80), "previous close"),
    (quote(0, 120, 80), "previous close"),
    (quote(100, 120, 90), "asymmetric"),
    (quote(100, 200, 50), "asymmetric"),
    ({"status": "success", "data": {"prev_close": 100, "upper_circuit_limit": 150,
                                    "lower_circuit_limit": 50}}, "implausible"),
])
def test_band_fails_closed(reply, needle):
    out = band_from_quote(reply)
    assert out["circuit_pct"] is None
    assert needle in out["reason"]


def test_implausible_band_reports_its_band_when_symmetric():
    """A symmetric but too-wide band (e.g. a derivative) is refused on width alone."""
    out = band_from_quote(quote(100, 200, 50))
    assert out["circuit_pct"] is None and out["reason"]


def test_nonsense_numbers_are_not_coerced():
    out = band_from_quote(quote(100, "1800", True))
    assert out["circuit_pct"] is None


def test_symmetry_tolerance_is_tight_enough_to_catch_a_bad_band():
    """A band whose two halves differ must be refused: one of them is a data fault."""
    # up 0.2000 vs down 0.1950 -> 0.005 apart, exactly at SYMMETRY_TOL: refused.
    at_edge = band_from_quote(quote(100, 120, 80.5))
    assert at_edge["circuit_pct"] is None and "asymmetric" in at_edge["reason"]
    # A true 20% band is symmetric and passes.
    assert band_from_quote(quote(100, 120, 80))["circuit_pct"] == 0.20
    # A visibly lopsided band is refused too.
    outside = band_from_quote(quote(100, 120, 79.0))
    assert outside["circuit_pct"] is None
    assert SYMMETRY_TOL == 0.005
    assert MAX_CIRCUIT == 0.20


# --------------------------------------------------------------------------- #
# enrichment: keeps every row, records why, never invents
# --------------------------------------------------------------------------- #
def test_no_reader_means_no_invented_band():
    out = enrich_candidates([{"symbol": "INFY", "selected": True}], None)
    row = out["candidates"][0]
    assert row["circuit_pct"] is None
    assert row["asm_gsm"] is None
    assert out["enriched"] == 0
    assert out["failed"][0]["reason"] == "no quote source configured"


def test_enrichment_sets_both_fields():
    book = {"INFY": quote(1500, 1800, 1200, asm=False)}
    out = enrich_candidates([{"symbol": "INFY", "selected": True}],
                            lambda s, e: book[s])
    row = out["candidates"][0]
    assert row["circuit_pct"] == 0.20 and row["asm_gsm"] is False
    assert out["enriched"] == 1 and out["failed"] == []


def test_unknown_surveillance_stays_unknown_not_false():
    """Collapsing this to False would let an ASM name pass the screen."""
    book = {"X": quote(100, 120, 80)}
    out = enrich_candidates([{"symbol": "X"}], lambda s, e: book[s])
    row = out["candidates"][0]
    assert row["circuit_pct"] == 0.20
    assert row["asm_gsm"] is None
    assert "ASM/GSM not published" in row["band_reason"]


def test_one_symbol_raising_does_not_stop_the_others():
    book = {"BAD": RuntimeError("boom"), "GOOD": quote(100, 120, 80, asm=False)}

    def reader(sym, exch):
        value = book[sym]
        if isinstance(value, Exception):
            raise value
        return value

    out = enrich_candidates([{"symbol": "BAD"}, {"symbol": "GOOD"}], reader)
    assert [c["symbol"] for c in out["candidates"]] == ["BAD", "GOOD"]
    assert out["candidates"][0]["circuit_pct"] is None
    assert "RuntimeError" in out["candidates"][0]["band_reason"]
    assert out["candidates"][1]["circuit_pct"] == 0.20
    assert out["enriched"] == 1


def test_prev_close_lookup_failure_falls_back_to_the_quote():
    book = {"INFY": quote(1500, 1800, 1200, asm=False)}

    def boom(_sym):
        raise RuntimeError("bars down")

    out = enrich_candidates([{"symbol": "INFY"}], lambda s, e: book[s],
                            prev_close_fn=boom)
    assert out["candidates"][0]["circuit_pct"] == 0.20


def test_rows_without_a_symbol_are_ignored():
    out = enrich_candidates([{"selected": True}, "junk", {"symbol": ""}], None)
    assert out["candidates"] == []


# --------------------------------------------------------------------------- #
# the whole chain: enrichment -> screen_rows -> the bot's contract
# --------------------------------------------------------------------------- #
def test_valid_rows_survive_into_the_screen_file():
    from quant.data.screener_engine import screen_rows

    book = {
        "INFY": quote(1500, 1800, 1200, asm=False),
        "BSE5": quote(100, 105, 95, asm=False),
        "ASMCO": quote(100, 120, 80, asm=True),
    }
    enriched = enrich_candidates([{"symbol": s, "selected": True} for s in book],
                                 lambda s, e: book[s])
    doc = screen_rows({"candidates": enriched["candidates"]})
    kept = {r["symbol"]: r["circuit_pct"] for r in doc["results"]}
    assert kept == {"INFY": 0.20, "BSE5": 0.05}
    assert doc["counts"] == {"considered": 3, "selected": 2, "dropped": 1}
    assert doc["dropped"] == [{"symbol": "ASMCO", "reason": "ASM/GSM flag"}]


def test_unenriched_rows_still_explain_the_empty_screen():
    """The exact 2026-11-24 production state: no source, so nothing is tradable."""
    from quant.data.screener_engine import screen_rows

    enriched = enrich_candidates(
        [{"symbol": "542046", "selected": True}, {"symbol": "MMEL", "selected": True}], None)
    doc = screen_rows({"candidates": enriched["candidates"]})
    assert doc["results"] == []
    assert doc["counts"] == {"considered": 2, "selected": 0, "dropped": 2}
    assert all(d["reason"] == "circuit_pct missing" for d in doc["dropped"])
