"""Build flow_svg.js from quant_system_flow_v5.svg.

The source is a Graphviz DFD: every block is already wrapped in <g class="stg" data-label>
and every arrow is <g class="edge">. This step only adds the popup root id and writes
window.FLOW_SVG for the dashboard. Run after editing the SVG source.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "quant_system_flow_v5.svg"
OUT = ROOT / "dashboard" / "assets" / "flow_svg.js"


def build() -> str:
    svg = SRC.read_text(encoding="utf-8")
    svg = re.sub(r"<metadata>.*?</metadata>", "", svg, flags=re.S)
    if 'id="flowsvg-root"' not in svg:
        svg = svg.replace("<svg ", '<svg id="flowsvg-root" ', 1)
    return svg


if __name__ == "__main__":
    markup = build()
    OUT.write_text("window.FLOW_SVG=" + json.dumps(markup) + ";\n", encoding="utf-8")
    print("wrote", OUT, len(markup), "bytes")
