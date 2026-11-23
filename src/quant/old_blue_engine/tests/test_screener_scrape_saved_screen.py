from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from blue_engine.screener import scrape_saved_screen as scraper


class _Client:
    def __init__(self, *, warning=None):
        self.warning = warning

    def get_screen_details(self, screen_id, slug, **kwargs):
        return SimpleNamespace(
            ok=True,
            error=None,
            warnings=[self.warning] if self.warning else [],
            data={
                "title": "Custom",
                "total_pages": 1,
                "rows": [
                    {"_symbol": "RELIANCE", "Name": "Reliance Industries",
                     "Mar Cap": "20,00,000"},
                ],
            },
        )

    def get_company(self, symbol, mode):
        return SimpleNamespace(
            ok=True,
            error=None,
            meta=None,
            data={
                "symbol": symbol,
                "mode": mode,
                "balance_sheet": {"headers": ["Mar 2025"], "rows": []},
            },
        )


def test_scrape_exports_filtered_rows_and_full_company_data(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda _: None)

    result = scraper.scrape_saved_screen(
        _Client(), 12345, "my-custom-filter", tmp_path)

    with (tmp_path / "filtered_summary.csv").open(
            encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    details = json.loads((tmp_path / "enriched_details.json").read_text(
        encoding="utf-8"))
    assert rows == [{
        "symbol": "RELIANCE",
        "Name": "Reliance Industries",
        "Mar Cap": "20,00,000",
        # Resolver columns appended by the exporter.
        "tradable_symbol": "RELIANCE",
        "exchange": "NSE",
        "isin": rows[0]["isin"],
        "symbol_resolved": "true",
    }]
    assert details["companies"][0]["data"]["balance_sheet"]["headers"] == [
        "Mar 2025"]
    assert result["companies"] == 1
    assert result["company_failures"] == 0


def test_incomplete_screen_pagination_does_not_export(tmp_path):
    with pytest.raises(RuntimeError, match="pagination was incomplete"):
        scraper.scrape_saved_screen(
            _Client(warning="Failed to fetch page 2"), 12345,
            "my-custom-filter", tmp_path)
    assert not list(tmp_path.iterdir())
