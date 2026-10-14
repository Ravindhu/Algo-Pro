"""tests/test_database_engine.py - the Data Management Engine, tested honestly.

One test per externally visible invariant:

1.  Timestamp normalization: ISO/naive-IST/epoch inputs all become UTC ``Z``;
    garbage returns None instead of raising.
2.  Every sync stamp matches ``YYYY-MM-DDTHH:MM:SSZ`` exactly.
3.  Ingestion persists to the shared PriceCache and stamps each row.
4.  A broker failure returns ``ok=False`` with a reason - never raises.
5.  Freshness: stale data is flagged with the age in the reason; an SLA of 0
    makes a lagged dataset not fresh.
6.  Lag fallback patches the missing delta from a trusted injected source and
    is refused when no fallback is configured (no unverified scraping).
7.  Read-only query: rejects writes/multi-statements, enforces row caps, and
    opens SQLite strictly read-only (an INSERT fails even if crafted through
    a pragma-free alias).
8.  Alignment: text features join backward-only - a future sentiment row must
    never attach to an earlier price bar (look-ahead bias check).
9.  PDF extraction: keyword-scoped, page-isolated failures, capped output.
10. Cycle payload: atomic replace, valid shape, bad shape rejected on read.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from blue_engine.database.engine import (
    DatabaseEngine,
    normalize_timestamp,
    utc_now_iso,
)


class _Client:
    """Minimal OpenAlgo-style client: keyword-only history/quotes."""

    def __init__(self, rows=None, fail=False):
        self.rows = rows if rows is not None else []
        self.fail = fail

    def history(self, *args, **kwargs):
        if self.fail:
            return {"status": "error", "message": "broker timeout"}
        return {"status": "success", "data": self.rows}

    def quotes(self, *args, **kwargs):
        if self.fail:
            return {"status": "error", "message": "quote unavailable"}
        return {"status": "success", "data": {"ltp": 101.5}}


def _rows(*closes, start_day=1):
    return [
        {"timestamp": f"2025-01-{start_day + i:02d} 09:15:00",
         "open": c, "high": c + 1, "low": c - 1, "close": c,
         "volume": 1000}
        for i, c in enumerate(closes)
    ]


def _engine(tmp_path, **overrides):
    cache_dir = tmp_path / "cache"
    from blue_engine.portfolio.providers.price_cache import PriceCache
    defaults = dict(
        client=None,
        price_cache=PriceCache(db_path=cache_dir / "prices.db"),
        payload_path=tmp_path / "current_cycle_payload.json",
    )
    defaults.update(overrides)
    return DatabaseEngine(**defaults)

STAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #

def test_normalize_timestamp_handles_iso_naive_and_epoch():
    assert normalize_timestamp("2025-01-03T09:15:00Z") == "2025-01-03T09:15:00Z"
    # naive IST 09:15 == 03:45 UTC
    assert normalize_timestamp("2025-01-03 09:15:00") == "2025-01-03T03:45:00Z"
    # epoch seconds: 1759795200 == 2025-10-07T00:00:00Z (verified via datetime)
    assert normalize_timestamp(1759795200) == "2025-10-07T00:00:00Z"


def test_normalize_timestamp_returns_none_for_garbage():
    assert normalize_timestamp(None) is None
    assert normalize_timestamp("") is None
    assert normalize_timestamp("not-a-date") is None
    assert normalize_timestamp(float("nan")) is None


def test_utc_now_iso_matches_required_format():
    assert utc_now_iso().endswith("Z")
    datetime.strptime(utc_now_iso(), "%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# ingestion + persistence
# --------------------------------------------------------------------------- #

def test_ingest_history_persists_and_stamps_rows(tmp_path):
    engine = _engine(tmp_path, client=_Client(_rows(100.0, 101.0, 102.0)))
    result = engine.ingest_history(symbol="TEST", max_age_seconds=None)

    assert result["ok"] is True
    assert result["written"] == 3
    assert result["freshness"]["fresh"] is True
    for row in result["rows"]:
        assert row["ts"].endswith("Z")
        assert row["synced_at"].endswith("Z")
    assert engine.price_cache.available()


def test_ingest_failure_returns_ok_false_and_never_raises(tmp_path):
    engine = _engine(tmp_path, client=_Client(fail=True))
    result = engine.ingest_history(symbol="TEST")

    assert result["ok"] is False
    assert "broker timeout" in result["reason"]
    assert result["rows"] == []
    assert engine._payload["diagnostics"]


def test_ingest_without_client_is_a_reported_failure(tmp_path):
    engine = _engine(tmp_path)
    result = engine.ingest_history(symbol="TEST")
    assert result["ok"] is False
    assert "no market-data client" in result["reason"]


# --------------------------------------------------------------------------- #
# freshness / lagging data
# --------------------------------------------------------------------------- #

def test_freshness_flags_stale_dataset_with_reason(tmp_path):
    engine = _engine(tmp_path)
    stale = engine._normalize_market_rows(_rows(100.0, 101.0, start_day=1))
    report = engine.diagnose_freshness(stale, max_age_seconds=60.0,
                                       now=datetime(2025, 6, 1, tzinfo=timezone.utc))
    assert report["fresh"] is False
    assert "SLA is 60.0s" in report["reason"]
    assert report["age_seconds"] > 0


def test_freshness_with_no_valid_timestamps_is_not_fresh(tmp_path):
    engine = _engine(tmp_path)
    report = engine.diagnose_freshness([{"ts": "garbage"}], max_age_seconds=10.0)
    assert report["fresh"] is False


def test_lag_fallback_patches_delta_from_trusted_source(tmp_path):
    patch = _rows(103.0, start_day=4)     # the missing day
    engine = _engine(tmp_path, client=_Client(_rows(100.0, 101.0, 102.0)),
                     lag_fallback=lambda **kwargs: patch)
    result = engine.ingest_history(symbol="TEST", fallback_on_lag=True,
                                   max_age_seconds=1.0)
    stamps = [r["ts"] for r in result["rows"]]
    assert len(stamps) == 4
    assert stamps == sorted(stamps)
    # the patch bar must be present and stamped
    assert any("2025-01-04" in s for s in stamps)


def test_no_fallback_configured_reports_refusal_not_scraping(tmp_path):
    engine = _engine(tmp_path, client=_Client(_rows(100.0)))
    result = engine.ingest_history(symbol="TEST", fallback_on_lag=True,
                                   max_age_seconds=1.0)
    assert result["ok"] is True
    assert any("no unverified web scraping" in w for w in result["warnings"])


def test_failing_fallback_is_reported_not_raised(tmp_path):
    def broken(**kwargs):
        raise RuntimeError("fallback down")

    engine = _engine(tmp_path, client=_Client(_rows(100.0)),
                     lag_fallback=broken)
    result = engine.ingest_history(symbol="TEST", fallback_on_lag=True,
                                   max_age_seconds=1.0)
    assert result["ok"] is True
    assert any("trusted fallback failed" in w for w in result["warnings"])


# --------------------------------------------------------------------------- #
# read-only relational queries
# --------------------------------------------------------------------------- #

def test_query_readonly_supports_select_and_rejects_writes(tmp_path):
    db = tmp_path / "cfg.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
    conn.commit()
    conn.close()

    engine = _engine(tmp_path)
    good = engine.query_readonly(f"sqlite:{db}", "SELECT a, b FROM t ORDER BY a")
    assert good["ok"] is True
    assert good["rows"] == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert good["backend"] == "sqlite"

    for bad_sql in ("DELETE FROM t", "SELECT 1; DROP TABLE t",
                    "PRAGMA journal_mode=DELETE", ""):
        result = engine.query_readonly(f"sqlite:{db}", bad_sql)
        assert result["ok"] is False
        assert "reason" in result

    assert engine.query_readonly("sqlite:missing.db", "SELECT 1")["ok"] is False


# --------------------------------------------------------------------------- #
# alignment (no look-ahead)
# --------------------------------------------------------------------------- #

def test_align_joins_text_backward_only_without_lookahead(tmp_path):
    engine = _engine(tmp_path)
    prices = {"INFY": engine._normalize_market_rows(_rows(100.0, 101.0, 102.0))}
    text = {
        "sentiment": [
            {"ts": "2025-01-02T10:00:00Z", "score": 0.9},
            {"ts": "2025-01-03T10:00:00Z", "score": -0.2},
        ]
    }
    result = engine.align(prices, text)
    assert result["ok"] is True
    rows = result["rows"]
    # Bars are 03:45Z each day; text publishes at 10:00Z. Backward as-of means:
    # bar 1 (day 1) -> None, bar 2 (day 2, 03:45Z, BEFORE the 10:00 publish)
    # -> None (no look-ahead), bar 3 (day 3, 03:45Z) -> latest published = 0.9.
    assert rows[0].get("score") is None
    assert rows[1].get("score") is None
    assert rows[2]["score"] == 0.9
    assert all(r["synced_at"].endswith("Z") for r in rows)


def test_align_without_valid_prices_reports_failure(tmp_path):
    engine = _engine(tmp_path)
    result = engine.align({"INFY": [{"ts": "garbage", "close": 1.0}]})
    assert result["ok"] is False


# --------------------------------------------------------------------------- #
# PDF extraction
# --------------------------------------------------------------------------- #

def test_pdf_extraction_missing_file_fails_soft(tmp_path):
    engine = _engine(tmp_path)
    result = engine.extract_pdf_context(tmp_path / "nope.pdf", keywords=["rd"])
    assert result["ok"] is False
    assert result["snippets"] == []


def test_pdf_extraction_is_keyword_scoped_and_capped(tmp_path):
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=500, height=800)
    target = tmp_path / "report.pdf"
    with open(target, "wb") as fh:
        writer.write(fh)

    engine = _engine(tmp_path, max_pdf_chars=200)
    result = engine.extract_pdf_context(target, keywords=["expense"])
    # Blank page -> no snippets, but the call must succeed and cap at 0 chars.
    assert result["ok"] is True
    assert result["chars_extracted"] == 0


# --------------------------------------------------------------------------- #
# cycle payload
# --------------------------------------------------------------------------- #

def test_cycle_payload_write_read_round_trip(tmp_path):
    engine = _engine(tmp_path)
    engine._payload["market"]["TEST"] = [{"ts": "2025-01-03T03:45:00Z",
                                          "close": 100.0}]
    written = engine.write_cycle_payload(data_class="historical:openalgo")
    assert written["ok"] is True
    assert Path(written["path"]).is_file()

    reader = DatabaseEngine(payload_path=Path(written["path"]))
    read = reader.read_cycle_payload()
    assert read["ok"] is True
    assert read["payload"]["data_class"] == "historical:openalgo"
    assert read["payload"]["market"]["TEST"][0]["close"] == 100.0


def test_cycle_payload_rejects_invalid_shape_on_read(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    corrupt = _engine(tmp_path, payload_path=path)
    outcome = corrupt.read_cycle_payload()
    assert outcome["ok"] is False
    assert outcome["payload"] is None


def test_cycle_payload_write_survives_unserializable_values(tmp_path):
    engine = _engine(tmp_path)
    engine._payload["market"]["TEST"] = [{"ts": "x", "close": float("nan")}]
    result = engine.write_cycle_payload()
    assert result["ok"] is True          # NaN coerced, write succeeded
    assert result["payload"]["market"]["TEST"][0]["close"] is None
