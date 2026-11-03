"""Build flow_svg.js from quant_system_flow_v5.svg.

Wraps each stage box in <g class="stg"> with data-label and data-row.
Adds class "edge" to arrows. Run after editing the SVG source.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "quant_system_flow_v5.svg"
OUT = Path(__file__).resolve().parent / "flow_svg.js"

BOX = re.compile(
    r'<rect x="(\d+)" y="(\d+)" width="210" height="58" rx="10"([^>]*)/>'
    r'(<text[^>]*>[^<]*</text>)(<text[^>]*>[^<]*</text>)'
)


def build() -> str:
    svg = SRC.read_text(encoding="utf-8")
    svg = re.sub(r"<metadata>.*?</metadata>", "", svg, flags=re.S)
    rows = sorted({int(m.group(2)) for m in BOX.finditer(svg)})

    def box(m: re.Match) -> str:
        x, y, extra, t1, t2 = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        label = re.search(r">([^<]*)</text>", t1).group(1)
        row = rows.index(int(y))
        return (
            f'<g class="stg" data-label="{label}" data-row="{row}" data-col="{x}">'
            f'<rect x="{x}" y="{y}" width="210" height="58" rx="10"{extra}/>{t1}{t2}</g>'
        )

    svg = BOX.sub(box, svg)
    svg = re.sub(r'<path (d="[^"]*" fill="none" stroke="[^"]*" stroke-width="2"[^>]*marker-end)',
                 r'<path class="edge" \1', svg)
    svg = svg.replace('<svg xmlns="http://www.w3.org/2000/svg"',
                      '<svg id="flowsvg-root" xmlns="http://www.w3.org/2000/svg"', 1)
    return svg


if __name__ == "__main__":
    markup = build()
    OUT.write_text("window.FLOW_SVG=" + json.dumps(markup) + ";\n", encoding="utf-8")
    print("wrote", OUT, len(markup), "bytes")
