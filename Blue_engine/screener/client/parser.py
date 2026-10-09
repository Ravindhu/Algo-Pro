"""
parser.py - the HTML -> Python parsers for the screener-india Python port.

A 1:1 port of ``vendor/screener-india/src/utils/parser.ts`` (node-html-parser ->
``lxml``/``BeautifulSoup``). Every selector, every fallback chain, and every
normalisation is preserved deliberately: if screener.in changes its markup, we
want the Python port to break in EXACTLY the same place as upstream, so one
upstream fix ports straight across.

WHAT THIS MODULE GUARANTEES
---------------------------
* Pure functions: HTML string in, plain Python out. No network, no cache, no
  logging side effects, no global state.
* Every ``parse_*`` returns a *defensive* value on missing markup (``[]``,
  ``None``, or ``{}``) rather than raising. A missing ``section#ratios`` is a
  normal condition on a page that does not have that tab, not a crash.

NUMERIC PARSING IS **SEPARATE** FROM HTML PARSING (see ``numbers.py``)
--------------------------------------------------------------------
Upstream returns raw strings straight from the DOM ("\u20b91,23,456 Cr.", "18.5",
"-", ""). We keep that contract here - ``parse_*`` returns strings - and do the
"is this actually a number, and is it safe to compare?" thinking in
``numbers.py``. Mixing the two is how screeners end up silently treating "-"
as zero and passing garbage through a debt filter.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from bs4.element import Tag

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Collapse all whitespace runs to a single space and trim.

    Port of upstream ``normalizeCell``. screener.in's tables are full of
    newlines and non-breaking indentation; without this, every header would be
    "\\n        Market Cap\\n      " and no column lookup would ever match.
    """
    return _WS_RE.sub(" ", (text or "").replace("\xa0", " ")).strip()


def parse_html(html: str) -> BeautifulSoup:
    """Port of upstream ``parseHtml``. ``lxml`` is used when available."""
    try:
        return BeautifulSoup(html or "", "lxml")
    except Exception:  # noqa: BLE001 - lxml missing/broken -> stdlib parser
        return BeautifulSoup(html or "", "html.parser")


def _q(node: Optional[Tag], selector: str) -> Optional[Tag]:
    """``node.querySelector`` equivalent, None-safe."""
    if node is None:
        return None
    found = node.select_one(selector)
    return found if isinstance(found, Tag) else None


def _qall(node: Optional[Tag], selector: str) -> List[Tag]:
    """``node.querySelectorAll`` equivalent, None-safe, Tag-only."""
    if node is None:
        return []
    return [el for el in node.select(selector) if isinstance(el, Tag)]


# --------------------------------------------------------------------------- #
# Company page
# --------------------------------------------------------------------------- #

def parse_title(root: BeautifulSoup) -> str:
    """Port of upstream ``parseTitle``.

    ``<title>`` is e.g. "Reliance Industries - Balance Sheet, Annual Report, ..."
    so we split on "|" then "-" and keep the first fragment. Note this yields the
    *company name*, not the ticker - ``get_company`` keeps the requested symbol.
    """
    title_tag = root.find("title")
    if title_tag is None:
        return ""
    text = title_tag.get_text() or ""
    return text.split("|")[0].split("-")[0].strip()


def parse_top_ratios(root: BeautifulSoup) -> List[Dict[str, Any]]:
    """Port of upstream ``parseTopRatios``.

    Returns the ``ul#top-ratios`` list: Market Cap, Current Price, Stock P/E,
    Book Value, Dividend Yield, ROCE, ROE (exact set depends on the company).

    The ``id`` attribute on each ``<li>`` is preserved because it is the most
    stable machine key on the page. The ``name`` is human text and *will* drift
    ("Stock P/E" vs "P/E"); ``numbers.ratio_lookup`` therefore matches on
    normalised name AND on the known id aliases.
    """
    out: List[Dict[str, Any]] = []
    for li in _qall(root, "ul#top-ratios li"):
        name_el = _q(li, ".name") or _q(li, "span:first-child")
        value_el = _q(li, ".value") or _q(li, "span:last-child")
        item: Dict[str, Any] = {
            "name": _norm(name_el.get_text()) if name_el else "",
            "value": _norm(value_el.get_text()) if value_el else "",
        }
        li_id = li.get("id")
        if li_id:
            item["id"] = str(li_id)
        out.append(item)
    return out


def parse_table(section: Optional[Tag]) -> Optional[Dict[str, Any]]:
    """Port of upstream ``parseTable`` - one ``section#<tab>`` -> a financial table.

    Duplicates the counter-intuitive part of upstream on purpose: the header row
    is read from ``thead tr`` and FALLS BACK TO THE FIRST ``tr``, and the body
    rows are ``tbody tr`` falling back to "every row except the first". screener.in
    emits tables both ways depending on the tab, and the fallback is what makes
    one function work for both.

    Returns ``{"headers": [...], "rows": [{header: cell, ...}]}`` or ``None``
    when the section/table is absent.
    """
    if section is None:
        return None
    table = _q(section, "table")
    if table is None:
        return None

    headers: List[str] = []
    header_row = _q(table, "thead tr") or _q(table, "tr")
    if header_row is not None:
        for cell in _qall(header_row, "th, td"):
            headers.append(_norm(cell.get_text()))

    body_rows = _qall(table, "tbody tr")
    if not body_rows:
        all_trs = _qall(table, "tr")
        body_rows = all_trs[1:] if all_trs else []

    rows: List[Dict[str, str]] = []
    for tr in body_rows:
        cells = _qall(tr, "td")
        if not cells:
            continue
        row: Dict[str, str] = {}
        for i, cell in enumerate(cells):
            key = headers[i] if i < len(headers) else f"col_{i}"
            row[key] = _norm(cell.get_text())
        rows.append(row)

    return {"headers": headers, "rows": rows}


def parse_analysis(root: BeautifulSoup) -> Optional[Dict[str, Any]]:
    """Port of upstream ``parseAnalysis`` - pros, cons and the description."""
    section = _q(root, "section#analysis")
    if section is None:
        return None

    pros = [_norm(li.get_text()) for li in _qall(section, ".pros li")]
    cons = [_norm(li.get_text()) for li in _qall(section, ".cons li")]
    desc_el = _q(section, "p")
    return {
        "pros": [p for p in pros if p],
        "cons": [c for c in cons if c],
        "description": _norm(desc_el.get_text()) if desc_el else "",
    }


def parse_documents(root: BeautifulSoup) -> List[Dict[str, str]]:
    """Port of upstream ``parseDocuments`` - annual reports / presentations."""
    out: List[Dict[str, str]] = []
    for a in _qall(root, "section#documents a[href]"):
        title = _norm(a.get_text())
        url = str(a.get("href") or "")
        if title and url:
            out.append({"title": title, "url": url})
    return out


def parse_peers(root: BeautifulSoup) -> List[Dict[str, str]]:
    """Port of upstream ``parsePeers`` - the peer comparison table.

    Note the asymmetry with ``parse_table``: upstream reads peer headers from
    ``thead tr`` ONLY (no first-row fallback). Kept identical.
    """
    section = _q(root, "section#peers")
    if section is None:
        return []
    table = _q(section, "table")
    if table is None:
        return []

    headers: List[str] = []
    header_row = _q(table, "thead tr")
    if header_row is not None:
        for th in _qall(header_row, "th"):
            headers.append(_norm(th.get_text()))

    rows: List[Dict[str, str]] = []
    for tr in _qall(table, "tbody tr"):
        cells = _qall(tr, "td")
        if not cells:
            continue
        row: Dict[str, str] = {}
        for i, cell in enumerate(cells):
            key = headers[i] if i < len(headers) else f"col_{i}"
            row[key] = _norm(cell.get_text())
        rows.append(row)
    return rows


def parse_section_ids(root: BeautifulSoup) -> List[str]:
    """Port of upstream ``parseSectionIds`` - the tab ids present on the page."""
    return [str(s.get("id")) for s in _qall(root, "section[id]") if s.get("id")]


# --------------------------------------------------------------------------- #
# Sector / screen / data tables
# --------------------------------------------------------------------------- #

#: ``/market/<slug>`` where slug is a deep hierarchical code, e.g.
#: ``IN02/IN0201/IN020101/IN020101002``. Upstream captures everything after
#: ``/market/`` so the value can be fed straight back into ``get_sector_data``.
_SECTOR_HREF_RE = re.compile(r"/market/(.+?)/?$")

#: ``/screens/<id>/<slug>`` - the id + slug pair ``get_screen_details`` needs.
_SCREEN_HREF_RE = re.compile(r"/screens/(\d+)/([^/?#]+)")


def parse_sector_links(root: BeautifulSoup) -> List[Dict[str, str]]:
    """Port of upstream ``parseSectorLinks``.

    Deduplicates by slug (the market page links each sector several times, once
    per tree level and once in the sidebar).
    """
    seen: set = set()
    out: List[Dict[str, str]] = []
    for a in _qall(root, "a[href]"):
        href = str(a.get("href") or "")
        if "/market/" not in href:
            continue
        match = _SECTOR_HREF_RE.search(href)
        if not match:
            continue
        name = _norm(a.get_text())
        if not name:
            continue  # nav links with no label
        slug = match.group(1).rstrip("/")
        if slug in seen:
            continue
        seen.add(slug)
        out.append({
            "name": name,
            "slug": slug,
            "url": href if href.startswith("http") else f"https://www.screener.in{href}",
        })
    return out


def parse_data_table(root: BeautifulSoup) -> Dict[str, List[Any]]:
    """Port of upstream ``parseDataTable`` - sector and screen result grids.

    Header find is tri-valued, exactly like upstream: prefer ``thead tr``, else
    use the first ``tbody`` row *if that row contains ``<th>``*, else no header.
    Rows that are themselves the header (``<th>`` in tbody) are then skipped, so
    the header never leaks in as a data row.
    """
    table = (_q(root, "[data-page-results] table")
             or _q(root, "table.data-table")
             or _q(root, "table"))
    if table is None:
        return {"columns": [], "rows": []}

    columns: List[str] = []
    thead_row = _q(table, "thead tr")
    all_body = _qall(table, "tbody tr")
    first_body = all_body[0] if all_body else None
    header_row = thead_row
    if header_row is None and first_body is not None and _qall(first_body, "th"):
        header_row = first_body

    if header_row is not None:
        for th in _qall(header_row, "th"):
            columns.append(_norm(th.get_text()))

    rows: List[Dict[str, str]] = []
    for tr in all_body:
        if tr is first_body and _qall(tr, "th"):
            continue  # header row living inside tbody
        cells = _qall(tr, "td")
        if not cells:
            continue
        row: Dict[str, str] = {}
        for i, cell in enumerate(cells):
            key = columns[i] if i < len(columns) else f"col_{i}"
            row[key] = _norm(cell.get_text())
        rows.append(row)

    return {"columns": columns, "rows": rows}


def parse_pagination(root: BeautifulSoup) -> Dict[str, int]:
    """Port of upstream ``parsePagination`` - reads the max ``?page=N`` link.

    ``current_page`` is hard-coded to 1 upstream (the caller knows which page it
    asked for). Kept identical so the two ports stay diff-able.
    """
    max_page = 1
    for a in _qall(root, ".pagination a[href]"):
        href = str(a.get("href") or "")
        m = re.search(r"[?&]page=(\d+)", href)
        if m:
            max_page = max(max_page, int(m.group(1)))
    return {"current_page": 1, "total_pages": max_page}


def parse_screen_items(root: BeautifulSoup) -> List[Dict[str, Any]]:
    """Port of upstream ``parseScreenItems`` - the public screens directory.

    Title and description live inside ONE anchor, separated by newlines. The
    description falls back to a sibling ``<p>`` when the anchor has only a title.
    """
    items: List[Dict[str, Any]] = []
    for a in _qall(root, "a[href]"):
        href = str(a.get("href") or "")
        match = _SCREEN_HREF_RE.search(href)
        if not match:
            continue
        parts = [_norm(p) for p in (a.get_text() or "").split("\n")]
        parts = [p for p in parts if p]
        title = parts[0] if parts else ""
        if len(parts) > 1:
            description = " ".join(parts[1:]).strip()
        else:
            parent = a.parent
            p_el = _q(parent, "p") if isinstance(parent, Tag) else None
            description = _norm(p_el.get_text()) if p_el else ""
        items.append({
            "id": int(match.group(1)),
            "slug": match.group(2),
            "title": title,
            "description": description,
            "url": href if href.startswith("http") else f"https://www.screener.in{href}",
        })
    return items


__all__ = [
    "parse_html",
    "parse_title",
    "parse_top_ratios",
    "parse_table",
    "parse_analysis",
    "parse_documents",
    "parse_peers",
    "parse_section_ids",
    "parse_sector_links",
    "parse_data_table",
    "parse_pagination",
    "parse_screen_items",
]
