"""
portfolio/exposure.py — Phase 3a: what are we actually exposed to?

Why this file exists
--------------------
The allocator speaks in *sleeves* ("IT 18%, BANKNIFTY 22%"). The desk thinks in
*risk*: how much of my money is in one sector, how much beta am I carrying, and
what happens if that bet breaks? Those are different questions with different
answers, and the gap between them is where concentration risk hides.

Concretely: three sleeves — IT, MIDCAP and a small-cap basket — can look
diversified by weight while being *one* bet (Indian mid/small-cap tech), because
they share a common driver. This module is what exposes that.

What it computes
----------------
* **exposure_by_sleeve**  — weight of each sleeve, long and short split.
* **exposure_by_sector**  — aggregate sleeve weights into GICS-style buckets
  (using ``DEFAULT_SLEEVES``), so "IT 18% + MIDCAP 12%" surfaces as
  "Information Technology 30%" when MIDCAP is tech-heavy.
* **long_short_split**    — gross long vs gross short, net exposure.
* **beta_exposure**       — weight-weighted portfolio beta vs a benchmark, and
  the currency/delta-equivalent notional ("₹4.6 lakh of NIFTY beta").
* **concentration**       — HHI, effective number of bets (1/HHI), and the
  largest single sleeve/sector share.
* **lookthrough**         — unwind sleeve weights into *sector* weights when a
  sleeve's constituents are known, so an index sleeve is not treated as an
  opaque block.

Design notes
------------
* **Pure.** Takes weight dicts + optional betas/constituents; returns dicts.
* **Degrade, never halt.** An unknown sleeve is bucketed as ``"UNKNOWN"`` with a
  warning rather than raising — a new sleeve must not blank the dashboard.
* **Sector map is data, not code.** ``DEFAULT_SLEEVES`` is overridable via
  config so the desk can point at its own taxonomy without a code change.

India specifics
---------------
The default buckets follow the NSE sector indices the desk actually trades
(NIFTY BANK, NIFTY IT, NIFTY PHARMA, NIFTY AUTO, NIFTY METAL, NIFTY ENERGY,
NIFTY FMCG, NIFTY REALTY), plus a "Mid/Small Cap" bucket that is genuinely a
different risk factor (liquidity + size), not a sector.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from ..core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SLEEVES", "sector_map", "sector_of", "exposure_by_sleeve",
    "exposure_by_sector", "long_short_split", "beta_exposure",
    "concentration", "lookthrough", "analyse",
]


#: Sleeve -> (bucket, note). ``None`` bucket means "treat the sleeve name as its
#: own bucket". Keys are matched case-insensitively and by substring, so
#: "NIFTYIT", "IT" and "NIFTY IT" all resolve to the same sleeve.
#:
#: The buckets mirror the NSE sector indices the desk trades. "Mid/Small Cap" is
#: deliberately its own bucket: size/liquidity is a distinct risk factor, and
#: lumping a mid-cap sleeve into "Diversified" hides the single biggest source of
#: drawdown in Indian retail books.
DEFAULT_SLEEVES: Dict[str, Dict[str, Any]] = {
    "NIFTY":     {"bucket": "Large Cap / Diversified", "beta": 1.00},
    "BANKNIFTY": {"bucket": "Financials", "beta": 1.15},
    "BANK":      {"bucket": "Financials", "beta": 1.15},
    "FINANCE":   {"bucket": "Financials", "beta": 1.15},
    "IT":        {"bucket": "Information Technology", "beta": 0.90},
    "TECH":      {"bucket": "Information Technology", "beta": 0.90},
    "PHARMA":    {"bucket": "Healthcare", "beta": 0.70},
    "HEALTH":    {"bucket": "Healthcare", "beta": 0.70},
    "AUTO":      {"bucket": "Consumer Discretionary", "beta": 1.10},
    "FMCG":      {"bucket": "Consumer Staples", "beta": 0.65},
    "METALS":    {"bucket": "Materials", "beta": 1.25},
    "METAL":     {"bucket": "Materials", "beta": 1.25},
    "ENERGY":    {"bucket": "Energy", "beta": 1.05},
    "OIL":       {"bucket": "Energy", "beta": 1.05},
    "REALTY":    {"bucket": "Real Estate", "beta": 1.30},
    "MIDCAP":    {"bucket": "Mid/Small Cap", "beta": 1.20},
    "SMALLCAP":  {"bucket": "Mid/Small Cap", "beta": 1.30},
    "PSU":       {"bucket": "Public Sector", "beta": 1.10},
    "INFRA":     {"bucket": "Infrastructure", "beta": 1.15},
    "COMMODITY": {"bucket": "Commodities", "beta": 0.30},
    "GOLD":      {"bucket": "Commodities", "beta": 0.10},
    "CASH":      {"bucket": "Cash", "beta": 0.00},
}


def sector_map(config: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Dict[str, Any]]:
    """Resolve the sleeve->bucket map (config overrides the default per key)."""
    out = {k.upper(): dict(v) for k, v in DEFAULT_SLEEVES.items()}
    cfg = (config or {}).get("sleeves")
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            key = str(k).upper()
            if isinstance(v, dict):
                out[key] = {**out.get(key, {}), **v}
            elif isinstance(v, str):
                out[key] = {**out.get(key, {}), "bucket": v}
    return out


def sector_of(sleeve: str, config: Optional[Dict[str, Any]] = None
              ) -> Tuple[str, Optional[float]]:
    """Return ``(bucket, beta)`` for a sleeve.

    Matching order matters and is deliberate:

    1. **exact** name match (``IT`` -> IT),
    2. otherwise the **rightmost** substring match, longest first at that
       position.

    Rightmost rather than longest is essential. NSE sleeve names qualify a
    family name at the *end* — ``NIFTY IT``, ``NIFTY PHARMA``, ``NIFTY AUTO`` —
    so the qualifier is the informative token. Longest-match alone fails on
    ``"NIFTYIT"``, where the generic ``"NIFTY"`` (5 chars) is longer than the
    specific ``"IT"`` (2) and would file an IT sleeve under Large Cap, silently
    corrupting the one report this module exists to get right.

    Unknown sleeves become their own bucket (better an obvious ``WEIRD_NEW`` in
    the report than a silent misclassification).
    """
    name = str(sleeve or "").strip().upper()
    if not name:
        return "UNKNOWN", None
    smap = sector_map(config)
    if name in smap:
        return str(smap[name].get("bucket") or name), safe_float(
            smap[name].get("beta"))
    best_key: Optional[str] = None
    best_score: Tuple[int, int] = (-1, -1)
    for key in smap:
        if not key:
            continue
        pos = name.rfind(key)
        if pos < 0:
            continue
        # Prefer the RIGHTMOST match, then the LONGEST at that position.
        #
        # Why rightmost rather than longest: NSE sleeve names qualify a family
        # name at the END — "NIFTY IT", "NIFTY PHARMA", "NIFTY AUTO". The
        # qualifier is the informative token, so the match nearest the end wins.
        # Longest-match alone still fails on "NIFTYIT", where the generic family
        # token "NIFTY" (5) is longer than the specific qualifier "IT" (2) and
        # would file an IT sleeve under Large Cap.
        score = (pos, len(key))
        if score > best_score:
            best_score = score
            best_key = key
    if best_key is not None:
        meta = smap[best_key]
        return str(meta.get("bucket") or best_key), safe_float(meta.get("beta"))
    return name, None


# --------------------------------------------------------------------------- #
# Exposure
# --------------------------------------------------------------------------- #

def exposure_by_sleeve(weights: Optional[Dict[str, float]],
                       marks: Optional[Dict[str, float]] = None
                       ) -> Dict[str, Any]:
    """Per-sleeve exposure, with long/short split and currency values.

    ``marks`` is an optional ``{sleeve: price}`` map; when supplied, notional
    values are reported alongside the weights (weights alone cannot tell a PM
    *how much money* sits in a sleeve).
    """
    w = {k: (safe_float(v) or 0.0) for k, v in (weights or {}).items()}
    if not w:
        return {"ok": False, "sleeves": {}, "gross": 0.0, "net": 0.0,
                "warnings": ["no weights supplied"]}
    gross = sum(abs(v) for v in w.values())
    net = sum(w.values())
    rows: Dict[str, Dict[str, Any]] = {}
    for sleeve, val in sorted(w.items(), key=lambda kv: -abs(kv[1])):
        row: Dict[str, Any] = {
            "weight": round(val, 6),
            "abs_weight": round(abs(val), 6),
            "share_of_gross": round(abs(val) / gross, 6) if gross else None,
            "side": "LONG" if val > 0 else ("SHORT" if val < 0 else "FLAT"),
        }
        if marks and sleeve in marks:
            px = safe_float(marks.get(sleeve))
            if px is not None:
                row["notional"] = round(val * px, 2)
        rows[sleeve] = row
    return {"ok": True, "sleeves": rows, "gross": round(gross, 6),
            "net": round(net, 6), "n_sleeves": len(rows), "warnings": []}


def exposure_by_sector(weights: Optional[Dict[str, float]], *,
                       config: Optional[Dict[str, Any]] = None,
                       capital: Optional[float] = None) -> Dict[str, Any]:
    """Aggregate sleeve weights into sector buckets.

    This is the number that actually matters for concentration: two sleeves in
    the same bucket are one bet, however the allocator labelled them.
    """
    w = {k: (safe_float(v) or 0.0) for k, v in (weights or {}).items()}
    if not w:
        return {"ok": False, "sectors": {}, "warnings": ["no weights supplied"]}
    cap = safe_float(capital)
    buckets: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    for sleeve, val in w.items():
        bucket, _beta = sector_of(sleeve, config)
        if bucket == sleeve.upper() and bucket not in sector_map(config):
            warnings.append(f"'{sleeve}' has no sector mapping; "
                            f"bucketed as its own")
        b = buckets.setdefault(bucket, {"weight": 0.0, "abs_weight": 0.0,
                                        "sleeves": [], "long": 0.0,
                                        "short": 0.0})
        b["weight"] += val
        b["abs_weight"] += abs(val)
        b["sleeves"].append(sleeve)
        if val >= 0:
            b["long"] += val
        else:
            b["short"] += val
    gross = sum(b["abs_weight"] for b in buckets.values())
    for name, b in buckets.items():
        b["weight"] = round(b["weight"], 6)
        b["abs_weight"] = round(b["abs_weight"], 6)
        b["share_of_gross"] = (round(b["abs_weight"] / gross, 6) if gross else None)
        b["sleeves"] = sorted(b["sleeves"])
        b["long"] = round(b["long"], 6)
        b["short"] = round(b["short"], 6)
        if cap:
            b["notional"] = round(b["weight"] * cap, 2)
    ordered = dict(sorted(buckets.items(), key=lambda kv: -kv[1]["abs_weight"]))
    return {"ok": True, "sectors": ordered, "gross": round(gross, 6),
            "n_sectors": len(ordered), "warnings": warnings}


def long_short_split(weights: Optional[Dict[str, float]]) -> Dict[str, Any]:
    """Gross long, gross short and net exposure (the book's shape)."""
    w = {k: (safe_float(v) or 0.0) for k, v in (weights or {}).items()}
    longs = sum(v for v in w.values() if v > 0)
    shorts = sum(-v for v in w.values() if v < 0)
    gross = longs + shorts
    return {
        "ok": bool(w),
        "gross_long": round(longs, 6),
        "gross_short": round(shorts, 6),
        "gross": round(gross, 6),
        "net": round(longs - shorts, 6),
        "net_to_gross": (round((longs - shorts) / gross, 6) if gross else None),
        "n_long": sum(1 for v in w.values() if v > 0),
        "n_short": sum(1 for v in w.values() if v < 0),
        "warnings": [],
    }


def beta_exposure(weights: Optional[Dict[str, float]], *,
                  betas: Optional[Dict[str, float]] = None,
                  benchmark: str = "NIFTY",
                  capital: Optional[float] = None,
                  config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Portfolio beta and its currency-equivalent NIFTY exposure.

    Beta comes from, in priority order: an explicit ``betas`` map (measured from
    history by ``risk/beta.py``), then the desk's configured sleeve beta, then
    1.0. Mixing *measured* and *assumed* betas is flagged, because a report that
    silently blends the two is worse than one that says which it used.
    """
    w = {k: (safe_float(v) or 0.0) for k, v in (weights or {}).items()}
    if not w:
        return {"ok": False, "beta": None, "warnings": ["no weights supplied"]}
    gross = sum(abs(v) for v in w.values()) or 1.0
    cfg_betas: Dict[str, float] = {}
    for sleeve in w:
        _bucket, b = sector_of(sleeve, config)
        if b is not None:
            cfg_betas[sleeve] = float(b)

    used: Dict[str, float] = {}
    measured, assumed = 0, 0
    for sleeve in w:
        if betas and sleeve in betas and safe_float(betas.get(sleeve)) is not None:
            used[sleeve] = float(betas[sleeve])
            measured += 1
        elif sleeve in cfg_betas:
            used[sleeve] = cfg_betas[sleeve]
            assumed += 1
        else:
            used[sleeve] = 1.0
            assumed += 1
    beta = sum((w[s] / gross) * used[s] for s in w)
    warnings: List[str] = []
    if measured and assumed:
        warnings.append(f"beta blends {measured} measured and {assumed} assumed "
                        f"value(s); pass a full 'betas' map for a clean number")
    elif assumed:
        warnings.append("all sleeve betas are desk assumptions, not measured")
    cap = safe_float(capital)
    out: Dict[str, Any] = {
        "ok": True,
        "beta": round(beta, 4),
        "benchmark": benchmark,
        "betas_used": {k: round(v, 4) for k, v in used.items()},
        "n_measured": measured,
        "n_assumed": assumed,
        "warnings": warnings,
    }
    if cap:
        # "This book behaves like Rs X of NIFTY": beta x capital.
        out["beta_notional"] = round(beta * cap, 2)
    return out


def concentration(weights: Optional[Dict[str, float]], *,
                  config: Optional[Dict[str, Any]] = None
                  ) -> Dict[str, Any]:
    """HHI, effective number of bets, and the largest bucket share.

    ``effective_bets = 1 / HHI``: a book of 10 equal sleeves scores 10; a book
    that is 90% one sleeve scores ~1.2 however many lines it has. This is the
    single most useful concentration number and it is not visible in a weight
    table.
    """
    w = {k: abs(safe_float(v) or 0.0) for k, v in (weights or {}).items()}
    total = sum(w.values())
    if total <= 0:
        return {"ok": False, "hhi": None, "effective_bets": None,
                "warnings": ["no weights supplied"]}
    shares = {k: v / total for k, v in w.items()}
    hhi = sum(s * s for s in shares.values())
    largest = max(shares.items(), key=lambda kv: kv[1])

    sect = exposure_by_sector(weights, config=config)
    sec_share = None
    sec_name = None
    if sect.get("ok"):
        best = max(sect["sectors"].items(),
                   key=lambda kv: kv[1]["abs_weight"])
        sec_name = best[0]
        sec_share = best[1]["share_of_gross"]
    return {
        "ok": True,
        "hhi": round(hhi, 6),
        "effective_bets": round(1.0 / hhi, 4) if hhi > 0 else None,
        "largest_sleeve": largest[0],
        "largest_sleeve_share": round(largest[1], 6),
        "largest_sector": sec_name,
        "largest_sector_share": sec_share,
        "n_sleeves": len(w),
        "warnings": [],
    }


def lookthrough(weights: Optional[Dict[str, float]],
                constituents: Optional[Dict[str, Dict[str, float]]], *,
                config: Optional[Dict[str, Any]] = None
                ) -> Dict[str, Any]:
    """Unwind sleeve weights into *constituent* sector weights.

    An index sleeve is not one exposure. ``NIFTY`` is ~35% Financials by
    construction, so a book that is 60% NIFTY is already ~21% Financials before
    any explicit bank sleeve is added. Without look-through, a desk adds a
    BANKNIFTY sleeve believing it is diversifying when it is concentrating.

    ``constituents`` maps ``{sleeve: {sector: fraction}}``. The fractions for a
    sleeve should sum to 1; if they do not, we renormalise and warn, because a
    half-specified map would silently *understate* every sector weight.
    """
    w = {k: (safe_float(v) or 0.0) for k, v in (weights or {}).items()}
    if not w:
        return {"ok": False, "sectors": {}, "warnings": ["no weights supplied"]}
    cons = constituents or {}
    if not cons:
        return {"ok": False, "sectors": {}, "coverage": 0.0,
                "warnings": ["no constituent map supplied; cannot look through"]}

    out: Dict[str, float] = {}
    warnings: List[str] = []
    covered = 0.0
    for sleeve, val in w.items():
        spec = cons.get(sleeve)
        if not spec:
            # Fall back to the sleeve's own bucket so the book still balances.
            bucket, _ = sector_of(sleeve, config)
            out[bucket] = out.get(bucket, 0.0) + val
            warnings.append(f"'{sleeve}' has no constituent map; used its own "
                            f"bucket")
            continue
        total = sum(abs(safe_float(f) or 0.0) for f in spec.values())
        if total <= 0:
            bucket, _ = sector_of(sleeve, config)
            out[bucket] = out.get(bucket, 0.0) + val
            warnings.append(f"'{sleeve}' constituent map is empty; used its bucket")
            continue
        if abs(total - 1.0) > 0.01:
            warnings.append(f"'{sleeve}' constituents sum to {total:.3f}; "
                            f"renormalised")
        for sector, frac in spec.items():
            f = (safe_float(frac) or 0.0) / total
            out[sector] = out.get(sector, 0.0) + val * f
        covered += abs(val)
    gross = sum(abs(v) for v in w.values()) or 1.0
    ordered = dict(sorted(((k, round(v, 6)) for k, v in out.items()),
                          key=lambda kv: -abs(kv[1])))
    return {"ok": True, "sectors": ordered,
            "coverage": round(covered / gross, 4),
            "warnings": warnings}


def analyse(weights: Optional[Dict[str, float]], *,
            betas: Optional[Dict[str, float]] = None,
            capital: Optional[float] = None,
            benchmark: str = "NIFTY",
            constituents: Optional[Dict[str, Dict[str, float]]] = None,
            config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One call: the full exposure picture for the dashboard's Exposure panel.

    Bundles sleeve/sector exposure, long-short shape, beta, concentration and
    (when a constituent map is available) look-through. Never raises.
    """
    try:
        sect = exposure_by_sector(weights, config=config, capital=capital)
        out = {
            "ok": bool(sect.get("ok")),
            "by_sleeve": exposure_by_sleeve(weights),
            "by_sector": sect,
            "long_short": long_short_split(weights),
            "beta": beta_exposure(weights, betas=betas, benchmark=benchmark,
                                  capital=capital, config=config),
            "concentration": concentration(weights, config=config),
            "warnings": list(sect.get("warnings") or []),
        }
        if constituents:
            out["lookthrough"] = lookthrough(weights, constituents, config=config)
            out["warnings"].extend(out["lookthrough"].get("warnings") or [])
        return out
    except Exception as exc:  # noqa: BLE001 - a panel must never blank the page
        logger.warning("exposure.analyse failed: %r", exc)
        return {"ok": False, "by_sleeve": {}, "by_sector": {}, "long_short": {},
                "beta": {}, "concentration": {},
                "warnings": [f"exposure analysis failed: {exc}"]}