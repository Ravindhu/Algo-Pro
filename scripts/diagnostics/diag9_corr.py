"""Temp probe: WHY did the cross-section show no signal?

Hypothesis: the 5 members are INDEX SLEEVES, which are highly correlated. A
cross-section only removes variance if the members actually differ; ranking 5
near-identical large-cap indices leaves almost no idiosyncratic component for a
feature to predict.

This measures the pairwise correlation of the members' 5-day forward returns.
If they are ~0.9 the hypothesis holds and the machinery is fine but the input
is wrong. If they are ~0.3 the hypothesis is dead and something else is wrong.

Read-only.
"""
import math
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.ml.data_sources.price_cache import PriceCacheSource
from blue_engine.ml.features import cross_section as cs

src = PriceCacheSource()
SYMBOLS = ["NIFTY", "BANKNIFTY", "IT", "PHARMA", "AUTO"]
series = {s: src.fetch(s, None, None).rows for s in SYMBOLS}
series = {s: r for s, r in series.items() if r}

panel = cs.build_panel(series)
print("panel: %d instruments x %d dates" % (len(panel.symbols), panel.n_dates))
print()

H = 5


def fwd_returns(rows, horizon):
    closes = [float(r["close"]) for r in rows if r.get("close") is not None]
    out = []
    for i in range(len(closes) - horizon):
        out.append(closes[i + horizon] / closes[i] - 1.0)
    return out


rets = {}
for s in panel.symbols:
    rows = [panel.series[s][d] for d in panel.dates]
    rets[s] = fwd_returns(rows, H)
n = min(len(v) for v in rets.values())
rets = {s: v[:n] for s, v in rets.items()}


def corr(a, b):
    m = len(a)
    ma, mb = sum(a) / m, sum(b) / m
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (m - 1)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a) / (m - 1))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b) / (m - 1))
    return cov / (sa * sb) if sa > 0 and sb > 0 else 0.0


names = panel.symbols
print("pairwise correlation of %d-day forward returns" % H)
print("        " + "".join("%9s" % s[:8] for s in names))
vals = []
for i, a in enumerate(names):
    row = "%-8s" % a[:8]
    for j, b in enumerate(names):
        if j < i:
            row += " " * 9
            continue
        c = 1.0 if i == j else corr(rets[a], rets[b])
        if i != j:
            vals.append(c)
        row += "%9.3f" % c
    print(row)

print()
print("mean pairwise correlation = %.3f" % (sum(vals) / len(vals)))
print("min  pairwise correlation = %.3f" % min(vals))
print("max  pairwise correlation = %.3f" % max(vals))
print()
if sum(vals) / len(vals) > 0.6:
    print("VERDICT: members are HIGHLY correlated. A cross-section of near-")
    print("         identical large-cap index sleeves has almost no")
    print("         idiosyncratic component left to rank. The machinery is")
    print("         sound; the INPUT is wrong. A real cross-section needs many")
    print("         loosely-correlated single names (stocks), not 5 indices.")
else:
    print("VERDICT: members are only moderately correlated, so the cross-")
    print("         section DOES have idiosyncratic variance. The absence of")
    print("         signal is then a genuine finding about these features,")
    print("         not an artefact of correlated inputs.")