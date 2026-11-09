"""Every block title drawn in quant_system_flow_v5.svg must appear in docs/quant_system_flow_v5.md.

Replaces the missing tests/unit/test_layout.py that the flow doc refers to (2026-11-09).
Also checks that no two blocks overlap on the canvas.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SVG = ROOT / "quant_system_flow_v5.svg"
DOC = ROOT / "docs" / "quant_system_flow_v5.md"


def _block_titles(svg_text):
    """Titles are the first <text> after each block <rect>, in the block's own row."""
    titles = []
    for m in re.finditer(r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"[^>]*/>'
                         r'<text x="([\d.]+)" y="([\d.]+)"[^>]*>([^<]+)</text>', svg_text):
        if float(m.group(4)) == 58.0:  # blocks are 58 px high; legend swatches and the small-items panel are not
            titles.append(m.group(7).strip())
    return titles


def test_every_svg_block_title_is_in_the_flow_doc():
    titles = _block_titles(SVG.read_text(encoding="utf-8"))
    assert len(titles) > 50
    doc = DOC.read_text(encoding="utf-8").lower()
    missing = [t for t in titles if t.lower() not in doc]
    assert missing == [], missing


def test_no_two_blocks_overlap():
    svg = SVG.read_text(encoding="utf-8")
    rects = [tuple(map(float, m)) for m in re.findall(
        r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg)]
    rects = [r for r in rects if r[3] == 58.0]
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            overlap = a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
            assert not overlap, (a, b)


def test_svg_is_well_formed_xml():
    import xml.etree.ElementTree as ET
    ET.fromstring(SVG.read_text(encoding="utf-8"))
