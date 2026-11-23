from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from blue_engine.screener.annual_reports import (
    enrich_company_from_report,
    extract_annual_report_data,
    select_annual_report,
)


def _pdf_reader_stub(monkeypatch, text):
    fake_pdf = types.ModuleType("pypdf")
    fake_pdf.PdfReader = lambda *_args, **_kwargs: SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda: text)]
    )
    monkeypatch.setitem(sys.modules, "pypdf", fake_pdf)


def test_extracts_annual_report_fields_in_crore_units(monkeypatch):
    _pdf_reader_stub(monkeypatch, """
    Statement of financial position (₹ in Crores)
    Cash and cash equivalents 2,500 2,100
    Non-controlling interests 75 60
    Intangible assets 140 120
    Contingent liabilities 35 25
    """)

    result = extract_annual_report_data(b"%PDF-fixture")

    assert result["fields"]["cash_and_equivalents"]["value"] == 2500
    assert result["fields"]["minority_interest"]["value"] == 75
    assert result["fields"]["intangible_assets"]["value"] == 140
    assert result["fields"]["contingent_liabilities"]["value"] == 35
    assert result["missing_fields"] == []


def test_extracts_and_normalizes_million_units(monkeypatch):
    _pdf_reader_stub(monkeypatch, """
    Amounts in INR million
    Cash and cash equivalents 12,500 10,000
    """)

    result = extract_annual_report_data(
        b"%PDF-fixture", fields=["cash_and_equivalents"])

    assert result["fields"]["cash_and_equivalents"]["value"] == 1250


def test_chooses_latest_annual_report_and_resolves_relative_url():
    report = select_annual_report([
        {"title": "Annual Report 2023-24", "url": "/reports/2024.pdf"},
        {"title": "Annual Report 2024-25", "url": "/reports/2025.pdf"},
    ], "https://www.screener.in/company/TEST/")

    assert report == {
        "title": "Annual Report 2024-25",
        "url": "https://www.screener.in/reports/2025.pdf",
        "year": "2025",
    }


def test_annual_report_gap_fill_preserves_only_measured_values(monkeypatch):
    _pdf_reader_stub(monkeypatch, """
    Balance sheet (₹ in Crores)
    Cash and cash equivalents 2,500 2,100
    Non-controlling interests 75 60
    Intangible assets 140 120
    Contingent liabilities 35 25
    """)
    company = {
        "symbol": "TESTCO",
        "source_url": "https://www.screener.in/company/TESTCO/",
        "documents": [{
            "title": "Annual Report 2024-25",
            "url": "https://reports.example/annual-report.pdf",
        }],
        "profit_loss": {"headers": [], "rows": []},
        "balance_sheet": {"headers": [], "rows": []},
        "cash_flow": {"headers": [], "rows": []},
        "ratios": {"headers": [], "rows": []},
    }
    fetches = []

    def fetch_pdf(url, *, max_bytes):
        fetches.append((url, max_bytes))
        return b"%PDF-fixture", None

    outcome = enrich_company_from_report(company, fetch_pdf)

    assert outcome["status"] == "complete"
    assert set(outcome["filled"]) == {
        "cash_and_equivalents", "minority_interest",
        "intangible_assets", "contingent_liabilities",
    }
    assert outcome["missing_after"] == []
    assert company["annual_report_data"]["cash_and_equivalents"]["value"] == 2500
    assert len(fetches) == 1
