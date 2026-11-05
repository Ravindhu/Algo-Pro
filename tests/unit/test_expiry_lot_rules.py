"""Owner rules (2026-11-06): delivery has no expiry; intraday expires same day; lot size is SME-only."""
from datetime import date
from pathlib import Path

import pytest

from quant.core.expiry_rules import (
    LotSizeUnknown, UnknownProduct, expiry_day, load_sme_lots, lot_size, must_close_by,
)

ROOT = Path(__file__).resolve().parents[2]
SME_CSV = ROOT / "config" / "calendar" / "sme_lot_sizes.csv"
D = date(2026, 11, 6)


def test_delivery_has_no_expiry():
    assert expiry_day("delivery", D) is None
    assert expiry_day("CNC", D) is None


def test_intraday_expires_same_day():
    assert expiry_day("intraday", D) == D
    assert expiry_day("MIS", D) == D
    assert must_close_by("MIS", D) == D


def test_unknown_product_refused():
    with pytest.raises(UnknownProduct):
        expiry_day("BTST", D)


def test_non_sme_lot_is_one_share_even_with_table_present():
    assert lot_size("INFY", "EQ", {"XYZ": 500}) == 1
    assert lot_size("INFY", "", {}) == 1


def test_sme_symbol_uses_owner_table():
    assert lot_size("abc", "SME", {"ABC": 300}) == 300


def test_sme_symbol_without_lot_is_refused():
    with pytest.raises(LotSizeUnknown):
        lot_size("ABC", "SME", {})


def test_shipped_sme_table_is_empty_so_sme_is_refused():
    table = load_sme_lots(SME_CSV)
    assert table == {}
    with pytest.raises(LotSizeUnknown):
        lot_size("ANYSME", "SME", table)


def test_sme_csv_loads_rows(tmp_path):
    p = tmp_path / "sme.csv"
    p.write_text("symbol,lot_size\nabc,300\n", encoding="utf-8")
    assert load_sme_lots(p) == {"ABC": 300}


def test_bad_sme_lot_row_refused(tmp_path):
    p = tmp_path / "sme.csv"
    p.write_text("symbol,lot_size\nabc,0\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_sme_lots(p)
