"""
flow.py - the trading system as graphs: one main neural shell plus one
neural shell per engine. Pure data, read-only, no framework imports.

Status meaning (shown in the UI legend)
  ok        the page that publishes this engine's data is healthy
  warn      the page answered but has nothing published yet (empty)
  bad       the page raised an error
  idle      no page is wired to this engine yet
  external  outside the system (broker, exchange, raw feeds)

Node layout is (col, row): col is the stage left to right, row is a 0..1
fraction top to bottom. The browser does the scaling.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from . import sections

Node = Tuple[str, str, int, float, str]   # id, label, col, row, kind
Edge = Tuple[str, str, str]               # from, to, kind (flow | feedback)

#: engine key -> (title, group, one-line blurb, section key or None)
ENGINES: Dict[str, Tuple[str, str, str, Optional[str]]] = {
    "economic": ("Economic engine", "frontline", "Macro regime and risk-on score from MoSPI, RBI and news.", "economic"),
    "sector": ("Sector engine", "frontline", "Relative strength and heat-map ranking of sectors.", "sectors"),
    "screener": ("Screener", "frontline", "Filters and ranks the universe for the strategies.", "screener"),
    "market": ("Market data", "frontline", "Quotes, depth and circuit health that every order depends on.", "terminal"),
    "portfolio": ("Portfolio engine", "frontline", "NAV, risk, attribution and position sizing.", "portfolio"),
    "database": ("Database", "frontline", "Ledger, kill-switch store and dashboard snapshots.", "infra"),
    "alpha": ("Alpha signals", "alpha", "Key-level and confirmation signals.", "analytics"),
    "ml": ("ML / DL models", "alpha", "Model training, walk-forward evaluation and registry.", "ml"),
    "optimizer": ("Optimizer", "alpha", "TPE search and IS/OOS validation. Report only.", "optimizer"),
    "strategy": ("Strategy", "execution", "Turns approved signals into trade intents.", "terminal"),
    "slicer": ("POV slicer", "execution", "Splits intents into child orders under VWAP and guard limits.", "terminal"),
    "exit": ("Exit engine", "execution", "Stops, targets and reverse-exit protection.", None),
    "risk": ("Risk gate", "risk", "Pre-trade caps, daily loss, drawdown and kill switch.", "risk"),
    "bridge": ("OpenAlgo bridge", "risk", "Sends approved orders to the broker through the SDK.", "infra"),
}

_STATUS_FROM_SECTION = {"ok": "ok", "empty": "warn", "error": "bad"}
_LIVE = frozenset({"ok", "external"})
_KINDS = frozenset({"external", "src", "proc", "store", "out"})
MAIN_COLS = 8

# ---------------------------------------------------------------- main graph
#: id, label, col, row, kind, engine key (None for external)
MAIN_NODES: List[Tuple[str, str, int, float, str, Optional[str]]] = [
    ("nse", "NSE/BSE quotes", 0, 0.10, "external", None),
    ("oafeed", "OpenAlgo WS feed", 0, 0.30, "external", None),
    ("macro", "MoSPI · RBI macro", 0, 0.50, "external", None),
    ("news", "Filings · NLP", 0, 0.70, "external", None),
    ("economic", "Economic", 1, 0.02, "proc", "economic"),
    ("sector", "Sector", 1, 0.20, "proc", "sector"),
    ("screener", "Screener", 1, 0.38, "proc", "screener"),
    ("market", "Market data", 1, 0.54, "proc", "market"),
    ("portfolio", "Portfolio", 1, 0.70, "proc", "portfolio"),
    ("database", "Database", 1, 0.88, "proc", "database"),
    ("alpha", "Alpha signals", 2, 0.18, "proc", "alpha"),
    ("ml", "ML / DL", 2, 0.45, "proc", "ml"),
    ("optimizer", "Optimizer", 2, 0.75, "proc", "optimizer"),
    ("strategy", "Strategy", 3, 0.45, "proc", "strategy"),
    ("slicer", "POV slicer", 4, 0.30, "proc", "slicer"),
    ("exit", "Exit engine", 4, 0.72, "proc", "exit"),
    ("gate", "Risk gate", 5, 0.30, "proc", "risk"),
    ("kill", "Kill switch", 5, 0.55, "proc", "risk"),
    ("ledger", "Ledger", 5, 0.82, "proc", "database"),
    ("bridge", "OpenAlgo bridge", 6, 0.45, "proc", "bridge"),
    ("broker", "Broker", 7, 0.35, "external", None),
    ("exchange", "Exchange", 7, 0.65, "external", None),
]

MAIN_EDGES: List[Edge] = [
    ("nse", "market", "flow"), ("oafeed", "market", "flow"), ("nse", "sector", "flow"),
    ("macro", "economic", "flow"), ("news", "economic", "flow"),
    ("economic", "alpha", "flow"), ("sector", "screener", "flow"), ("screener", "alpha", "flow"),
    ("market", "alpha", "flow"), ("market", "slicer", "flow"),
    ("ml", "alpha", "flow"), ("alpha", "strategy", "flow"), ("optimizer", "strategy", "feedback"),
    ("strategy", "slicer", "flow"), ("slicer", "gate", "flow"), ("exit", "gate", "flow"),
    ("portfolio", "gate", "feedback"), ("database", "ledger", "flow"),
    ("ledger", "gate", "feedback"), ("kill", "gate", "feedback"), ("ledger", "exit", "feedback"),
    ("gate", "bridge", "flow"), ("bridge", "broker", "flow"), ("broker", "exchange", "flow"),
    ("bridge", "ledger", "feedback"),
]

# ------------------------------------------------------------- engine graphs
#: engine key -> (nodes, edges). Every engine in ENGINES has one.
ENGINE_GRAPH: Dict[str, Tuple[List[Node], List[Edge]]] = {
    "economic": (
        [("macro", "MoSPI · RBI macro", 0, 0.3, "src"), ("news", "Filings · NLP", 0, 0.7, "src"),
         ("score", "Scoring · regime", 1, 0.5, "proc"), ("feat", "Stationarity · PCA", 2, 0.2, "proc"),
         ("models", "HMM · cluster · DL", 2, 0.7, "proc"), ("riskon", "Risk-on score", 3, 0.3, "out"),
         ("expo", "Exposure multiplier", 3, 0.7, "out")],
        [("macro", "score", "flow"), ("news", "score", "flow"), ("score", "feat", "flow"),
         ("score", "models", "flow"), ("feat", "riskon", "flow"), ("models", "expo", "flow")]),
    "sector": (
        [("closes", "Sector & index closes", 0, 0.3, "src"), ("vendor", "Vendor sector map", 0, 0.7, "src"),
         ("rs", "Mansfield RS", 1, 0.3, "proc"), ("hm", "Heat-map score", 1, 0.7, "proc"),
         ("adv", "LLM advisor (opt-in)", 2, 0.5, "proc"), ("veto", "Defensive veto", 2, 0.85, "proc"),
         ("limits", "Sector limits", 3, 0.5, "out")],
        [("closes", "rs", "flow"), ("vendor", "hm", "flow"), ("rs", "adv", "flow"),
         ("hm", "adv", "flow"), ("adv", "limits", "flow"), ("veto", "limits", "flow")]),
    "screener": (
        [("feeds", "NSE / vendor data", 0, 0.25, "src"), ("uni", "Universe", 0, 0.75, "src"),
         ("filt", "Liquidity & trend filter", 1, 0.5, "proc"), ("rank", "Ranking", 2, 0.5, "proc"),
         ("snap", "Snapshot store", 2, 0.85, "store"), ("bridge", "Screener bridge", 3, 0.5, "out")],
        [("feeds", "filt", "flow"), ("uni", "filt", "flow"), ("filt", "rank", "flow"),
         ("rank", "bridge", "flow"), ("rank", "snap", "flow")]),
    "market": (
        [("ws", "OpenAlgo WS", 0, 0.3, "src"), ("quotes", "Quote health", 1, 0.2, "proc"),
         ("sym", "Symbol master · lot size", 1, 0.8, "proc"), ("depth", "Depth & circuit", 2, 0.4, "proc"),
         ("vol", "Volume cache", 2, 0.85, "store"), ("out", "Pricing inputs → slicer", 3, 0.5, "out")],
        [("ws", "quotes", "flow"), ("ws", "vol", "flow"), ("quotes", "depth", "flow"),
         ("sym", "depth", "flow"), ("depth", "out", "flow"), ("vol", "out", "flow")]),
    "portfolio": (
        [("pos", "Broker positions", 0, 0.3, "src"), ("nav", "NAV & cash flows", 1, 0.3, "proc"),
         ("risk", "Vol · VaR · beta", 2, 0.15, "proc"), ("attr", "Attribution", 2, 0.5, "proc"),
         ("size", "Sizing (Kelly · vol target)", 2, 0.85, "proc"), ("out", "Exposure caps → gate", 3, 0.5, "out")],
        [("pos", "nav", "flow"), ("nav", "risk", "flow"), ("nav", "attr", "flow"),
         ("risk", "size", "flow"), ("size", "out", "flow"), ("attr", "out", "flow")]),
    "database": (
        [("ldb", "Ledger (sqlite)", 0, 0.25, "store"), ("kdb", "Kill-switch store", 0, 0.75, "store"),
         ("snap", "Dashboard snapshots", 1, 0.4, "store"), ("vend", "Vendor data", 1, 0.85, "store"),
         ("ok", "Health → gate", 2, 0.5, "out")],
        [("ldb", "snap", "flow"), ("snap", "ok", "flow"), ("kdb", "ok", "flow"), ("vend", "ok", "flow")]),
    "alpha": (
        [("kl", "Key-level signals", 0, 0.3, "proc"), ("hm", "Hilega-milega", 0, 0.75, "proc"),
         ("roc", "ROC confirmation", 1, 0.5, "proc"), ("state", "Confirm state", 2, 0.5, "store"),
         ("out", "Alpha signal", 3, 0.5, "out")],
        [("kl", "roc", "flow"), ("hm", "roc", "flow"), ("roc", "state", "flow"), ("state", "out", "flow")]),
    "ml": (
        [("data", "Feature matrix", 0, 0.5, "src"), ("mlp", "MLP · CNN", 1, 0.25, "proc"),
         ("rl", "RL (sandbox only)", 1, 0.75, "proc"), ("eval", "Walk-forward eval", 2, 0.5, "proc"),
         ("reg", "Model registry", 3, 0.25, "store"), ("out", "Signal quality", 3, 0.75, "out")],
        [("data", "mlp", "flow"), ("data", "rl", "flow"), ("mlp", "eval", "flow"),
         ("rl", "eval", "flow"), ("eval", "reg", "flow"), ("eval", "out", "flow")]),
    "optimizer": (
        [("space", "Search space", 0, 0.5, "src"), ("tpe", "TPE sampler", 1, 0.5, "proc"),
         ("bt", "Backtest (IS / OOS)", 2, 0.3, "proc"), ("obj", "Objective & gate", 2, 0.7, "proc"),
         ("rep", "Report (--apply to change)", 3, 0.5, "out")],
        [("space", "tpe", "flow"), ("tpe", "bt", "flow"), ("bt", "obj", "flow"),
         ("obj", "rep", "flow"), ("obj", "tpe", "feedback")]),
    "strategy": (
        [("alpha", "Alpha signals", 0, 0.25, "src"), ("ml", "ML quality", 0, 0.75, "src"),
         ("gate", "Entry gate (win-rate)", 1, 0.5, "proc"), ("pos", "Position state", 2, 0.5, "proc"),
         ("intent", "Trade intent → slicer", 3, 0.5, "out")],
        [("alpha", "gate", "flow"), ("ml", "gate", "flow"), ("gate", "pos", "flow"), ("pos", "intent", "flow")]),
    "slicer": (
        [("in", "Trade intent", 0, 0.5, "src"), ("band", "VWAP band · POV rate", 1, 0.2, "proc"),
         ("guards", "Pricing · depth cap · circuit", 2, 0.5, "proc"), ("child", "Child orders", 3, 0.3, "proc"),
         ("out", "Placeorder → gate", 4, 0.5, "out")],
        [("in", "band", "flow"), ("band", "guards", "flow"), ("guards", "child", "flow"), ("child", "out", "flow")]),
    "exit": (
        [("pos", "Open positions", 0, 0.5, "src"), ("stops", "Stop · target", 1, 0.25, "proc"),
         ("rev", "Reverse-exit guard", 1, 0.75, "proc"), ("eng", "Exit engine", 2, 0.5, "proc"),
         ("out", "Exit intent → gate", 3, 0.5, "out")],
        [("pos", "stops", "flow"), ("pos", "rev", "flow"), ("stops", "eng", "flow"),
         ("rev", "eng", "flow"), ("eng", "out", "flow")]),
    "risk": (
        [("expo", "Portfolio exposure", 0, 0.3, "src"), ("caps", "Qty · notional · exposure caps", 1, 0.3, "proc"),
         ("loss", "Daily loss · drawdown", 1, 0.7, "proc"), ("kill", "Kill switch (durable)", 2, 0.5, "store"),
         ("out", "Approve / reject", 3, 0.5, "out")],
        [("expo", "caps", "flow"), ("caps", "kill", "flow"), ("loss", "kill", "flow"), ("kill", "out", "flow")]),
    "bridge": (
        [("in", "Approved intent", 0, 0.5, "src"), ("oa", "OpenAlgo SDK", 1, 0.5, "proc"),
         ("brk", "Broker", 2, 0.3, "out"), ("exch", "Exchange", 2, 0.7, "out"),
         ("upd", "Order updates → ledger", 3, 0.5, "store")],
        [("in", "oa", "flow"), ("oa", "brk", "flow"), ("brk", "exch", "flow"), ("brk", "upd", "feedback")]),
}


# ---------------------------------------------------------------- helpers
def _payload(section: str) -> Dict[str, Any]:
    """Cached section payload (5 s TTL). Never raises."""
    return sections.cached_section(section)


def _engine_status(key: str) -> str:
    section = ENGINES[key][3]
    if section is None:
        return "idle"
    return _STATUS_FROM_SECTION.get(_payload(section).get("status"), "idle")


def _active(a: str, b: str) -> bool:
    """A pulse runs when both ends are live and at least one is healthy."""
    return a in _LIVE and b in _LIVE and ("ok" in (a, b))


def _cols(nodes: List[Dict[str, Any]]) -> int:
    return max((n["col"] for n in nodes), default=0) + 1


def _counts(statuses: List[str]) -> Dict[str, int]:
    out = {"ok": 0, "warn": 0, "bad": 0, "idle": 0}
    for s in statuses:
        if s in out:
            out[s] += 1
    return out


def engine_summary(key: str) -> Dict[str, Any]:
    title, group, blurb, _section = ENGINES[key]
    return {"key": key, "title": title, "group": group, "blurb": blurb,
            "status": _engine_status(key)}


# ---------------------------------------------------------------- builders
def build_flow() -> Dict[str, Any]:
    """The main neural shell: every stage, every engine, live status."""
    engine_status = {key: _engine_status(key) for key in ENGINES}
    nodes: List[Dict[str, Any]] = []
    for nid, label, col, row, kind, engine in MAIN_NODES:
        status = "external" if kind == "external" else engine_status[engine]
        nodes.append({"id": nid, "label": label, "col": col, "row": row,
                      "kind": kind, "engine": engine, "status": status})
    node_status = {n["id"]: n["status"] for n in nodes}
    edges = [{"from": a, "to": b, "kind": kind, "active": _active(node_status[a], node_status[b])}
             for a, b, kind in MAIN_EDGES]
    return {
        "cols": MAIN_COLS,
        "nodes": nodes,
        "edges": edges,
        "engines": [engine_summary(k) for k in ENGINES],
        "counts": _counts(list(engine_status.values())),
        "generated_at": time.time(),
    }


def build_engine(key: str) -> Optional[Dict[str, Any]]:
    """One engine's own neural shell plus its published details (or None)."""
    if key not in ENGINE_GRAPH:
        return None
    title, group, blurb, section = ENGINES[key]
    status = _engine_status(key)
    nodes_def, edges_def = ENGINE_GRAPH[key]
    nodes = [{"id": i, "label": label, "col": col, "row": row, "kind": kind,
              "status": "external" if kind == "src" else status}
             for i, label, col, row, kind in nodes_def]
    node_status = {n["id"]: n["status"] for n in nodes}
    edges = [{"from": a, "to": b, "kind": kind, "active": _active(node_status[a], node_status[b])}
             for a, b, kind in edges_def]
    return {
        "key": key, "title": title, "group": group, "blurb": blurb, "status": status,
        "cols": _cols(nodes), "nodes": nodes, "edges": edges,
        "section": section,
        "payload": _payload(section) if section else None,
    }
