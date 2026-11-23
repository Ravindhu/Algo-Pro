"""Conservative annual-report gap filling for Screener.in company snapshots."""
from __future__ import annotations

import io
import re
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import urljoin, urlparse

_FIELD_PATTERNS = {
    "cash_and_equivalents": (
        re.compile(r"\bcash\s+and\s+cash\s+equivalents\b", re.I),
        re.compile(r"\bcash\s+and\s+bank\s+balances?\b", re.I),
    ),
    "minority_interest": (
        re.compile(r"\bnon[\s-]*controlling\s+interests?\b", re.I),
        re.compile(r"\bminority\s+interests?\b", re.I),
    ),
    "intangible_assets": (
        re.compile(r"\bintangible\s+assets?\b", re.I),
        re.compile(r"\bgoodwill\b", re.I),
    ),
    "contingent_liabilities": (
        re.compile(r"\bcontingent\s+liabilities\b", re.I),
        re.compile(r"\bcontingencies\b", re.I),
    ),
}
_NUMBER_RE = re.compile(r"(?<![A-Za-z])\(?-?\d[\d,]*(?:\.\d+)?\)?")
_UNIT_RE = re.compile(
    r"(?:₹|INR|Rs\.?|rupees?)\s*(?:in\s*)?"
    r"(crores?|cr\.?|millions?|mn\.?|thousands?|lakhs?|lacs?)\b",
    re.I,
)
_UNIT_SCALE = {
    "crore": 1.0, "crores": 1.0, "cr": 1.0, "cr.": 1.0,
    "million": 0.1, "millions": 0.1, "mn": 0.1, "mn.": 0.1,
    "thousand": 0.0001, "thousands": 0.0001,
    "lakh": 0.01, "lakhs": 0.01, "lac": 0.01, "lacs": 0.01,
}
_TARGET_LABELS = {
    "cash_and_equivalents": "cash and cash equivalents",
    "minority_interest": "non-controlling interest",
    "intangible_assets": "intangible assets / goodwill",
    "contingent_liabilities": "contingent liabilities",
}
_INPUT_FIELD = {
    "cash_and_equivalents": "cash_and_equivalents",
    "minority_interest": "minority_interest",
    "intangible_assets": "intangibles",
    "contingent_liabilities": "contingent_liabilities",
}


def missing_report_fields(company: Dict[str, Any]) -> list[str]:
    """Return report fields not already established by Screener or a prior report."""
    from .valuations import build_inputs

    inputs = build_inputs(company)
    return [
        field for field in _TARGET_LABELS
        if getattr(inputs, _INPUT_FIELD[field]) is None
    ]


def select_annual_report(documents: Iterable[Dict[str, Any]],
                         company_url: str) -> Optional[Dict[str, str]]:
    """Select the most recent annual-report link, preferring an explicit year."""
    reports = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        title = " ".join(str(document.get("title") or "").split())
        raw_url = str(document.get("url") or "").strip()
        if not title or not raw_url or "annual report" not in title.lower():
            continue
        url = urljoin(company_url, raw_url)
        years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", title)]
        for start, end in re.findall(
                r"\b((?:19|20)\d{2})\s*[-–]\s*(\d{2}|(?:19|20)\d{2})\b",
                title):
            years.append(int(start[:2] + end) if len(end) == 2 else int(end))
        reports.append({
            "title": title,
            "url": url,
            "year": str(max(years)) if years else "",
        })
    if not reports:
        return None
    reports.sort(key=lambda item: (bool(item["year"]), item["year"], item["title"]),
                 reverse=True)
    return reports[0]


def _scale_for(text: str) -> Optional[float]:
    match = _UNIT_RE.search(text)
    if not match:
        return None
    unit = match.group(1).lower().rstrip(".")
    return _UNIT_SCALE.get(unit)


def _amounts(text: str) -> list[float]:
    values = []
    for token in _NUMBER_RE.findall(text):
        clean = token.replace(",", "")
        if clean.startswith("(") and clean.endswith(")"):
            clean = "-" + clean[1:-1]
        try:
            value = float(clean)
        except ValueError:
            continue
        if 1900 <= value <= 2100 and value.is_integer():
            continue
        values.append(value)
    return values


def extract_annual_report_data(pdf_bytes: bytes,
                               fields: Optional[Iterable[str]] = None,
                               *, max_pages: int = 500) -> Dict[str, Any]:
    """Extract audited-row candidates from a PDF; ambiguous or unitless values stay missing.

    Amounts are normalized to crore rupees. When a report row has multiple
    columns, the first number after its label is recorded as the current period,
    consistent with the statement's current-year-first column order.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "pypdf is required for annual-report valuation enrichment; "
            'install Blue_engine with the "screener" extra') from exc
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("annual report response is not a PDF")

    requested = set(fields or _FIELD_PATTERNS)
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    pages = reader.pages[:max_pages]
    page_texts = []
    for index, page in enumerate(pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            page_texts.append((index, "", f"{type(exc).__name__}: {exc}"))
        else:
            page_texts.append((index, text, ""))

    extracted: Dict[str, Any] = {}
    unresolved = set(requested)
    for page_number, text, page_error in page_texts:
        if not text:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for field in tuple(unresolved):
            for line_index, line in enumerate(lines):
                matched_pattern = next(
                    (pattern for pattern in _FIELD_PATTERNS[field]
                     if pattern.search(line)),
                    None,
                )
                if matched_pattern is None:
                    continue
                label_match = matched_pattern.search(line)
                assert label_match is not None
                tail = line[label_match.end():]
                amounts = _amounts(tail)
                if not amounts:
                    continue
                context = "\n".join(lines[max(0, line_index - 3):line_index + 2])
                scale = _scale_for(context) or _scale_for(text[:3000])
                if scale is None:
                    continue
                value = amounts[0] * scale
                if value < 0:
                    continue
                extracted[field] = {
                    "value": value,
                    "unit": "INR crore",
                    "page": page_number,
                    "label": line[:160],
                    "selection": "first numeric amount after matched row label",
                    "period": "current-year column inferred from report column order",
                    "source": "screener_annual_report",
                }
                unresolved.discard(field)
                break

    return {
        "fields": extracted,
        "missing_fields": sorted(unresolved),
        "pages_read": len(pages),
        "page_errors": [
            {"page": page, "error": error}
            for page, _, error in page_texts if error
        ],
        "unit_policy": "Only explicit INR crore/lakh/million/thousand units are accepted.",
    }


def enrich_company_from_report(
        company: Dict[str, Any], fetch_pdf: Callable[[str], tuple],
        *, max_pdf_bytes: int = 25 * 1024 * 1024) -> Dict[str, Any]:
    """Download one Screener-listed annual report and fill only absent fields."""
    missing_before = missing_report_fields(company)
    outcome: Dict[str, Any] = {
        "status": "not_needed" if not missing_before else "incomplete",
        "missing_before": missing_before,
        "filled": [],
        "missing_after": list(missing_before),
        "report": None,
        "error": None,
    }
    if not missing_before:
        company["annual_report_enrichment"] = outcome
        return outcome

    company_url = str(company.get("source_url") or "")
    report = select_annual_report(company.get("documents") or [], company_url)
    if report is None:
        outcome["error"] = "No annual-report link was found on the Screener company page."
        company["annual_report_enrichment"] = outcome
        return outcome

    parsed_url = urlparse(report["url"])
    company_origin = urlparse(company_url)
    if (not company_origin.scheme or not company_origin.netloc
            or parsed_url.scheme not in ("http", "https")
            or parsed_url.username or parsed_url.password
            or (parsed_url.netloc.lower() != company_origin.netloc.lower()
                and parsed_url.scheme != "https")):
        outcome["error"] = (
            "Annual-report links must use Screener's origin or a public HTTPS URL.")
        outcome["report"] = {"title": report["title"], "url": report["url"]}
        company["annual_report_enrichment"] = outcome
        return outcome

    outcome["report"] = {"title": report["title"], "url": report["url"],
                         "year": report["year"]}
    pdf_bytes, error = fetch_pdf(report["url"], max_bytes=max_pdf_bytes)
    if error or not pdf_bytes:
        outcome["error"] = error or "Annual-report download returned an empty file."
        company["annual_report_enrichment"] = outcome
        return outcome
    if len(pdf_bytes) > max_pdf_bytes:
        outcome["error"] = f"Annual report exceeded the {max_pdf_bytes}-byte limit."
        company["annual_report_enrichment"] = outcome
        return outcome

    try:
        extraction = extract_annual_report_data(pdf_bytes, missing_before)
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        company["annual_report_enrichment"] = outcome
        return outcome

    existing = company.setdefault("annual_report_data", {})
    for field, record in extraction["fields"].items():
        if field not in missing_before or not isinstance(record, dict):
            continue
        if record.get("value") is None:
            continue
        existing[field] = record
        outcome["filled"].append(field)
    outcome["missing_after"] = missing_report_fields(company)
    outcome["status"] = "complete" if not outcome["missing_after"] else "partial"
    outcome["extraction"] = extraction
    company["annual_report_enrichment"] = outcome
    return outcome


__all__ = [
    "missing_report_fields",
    "select_annual_report",
    "extract_annual_report_data",
    "enrich_company_from_report",
]
