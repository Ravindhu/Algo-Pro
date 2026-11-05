"""
render.py - rich-based dashboard rendering.

All rendering is done with the rich library. The dashboard layer
just calls :func:`build_layout` once, then updates its inner
``renderable`` attributes on each event.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .candlestick import candle_sparkline
from .data_source import AlertEvent, MatchEvent, PriceTick, TradeEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


def _style_change(change_pct: float) -> str:
    if change_pct > 0:
        return "green"
    if change_pct < 0:
        return "red"
    return "dim"


def _style_distance(distance_pct: Optional[float]) -> str:
    """Closer-to-trigger = warmer color."""
    if distance_pct is None:
        return "dim"
    a = abs(distance_pct)
    if a < 0.1:
        return "bold red"
    if a < 0.5:
        return "red"
    if a < 1.0:
        return "yellow"
    if a < 3.0:
        return "white"
    return "dim"


def _style_state(state: str) -> str:
    s = (state or "").upper()
    if s in ("ACCUMULATING",):
        return "bold green"
    if s in ("SLOW",):
        return "yellow"
    if s in ("STOPPED",):
        return "cyan"
    if s in ("EXITED", "COMPLETED"):
        return "red"
    if s in ("MATCHED",):
        return "magenta"
    return "dim"


# ---------------------------------------------------------------------------
# Universe table
# ---------------------------------------------------------------------------


def render_universe_table(
    rows: Sequence[Dict[str, Any]],
    *,
    title: str = "Watchlist",
    max_rows: int = 50,
    reveal_pnl: bool = True,
) -> Table:
    """Render the main universe table.

    Each row dict is shaped by :class:`LiveDashboard._build_universe_row`.
    P&L is shown by default (owner decision 0007). Pass ``reveal_pnl=False`` to hide it.
    """
    table = Table(title=title, expand=True, header_style="bold cyan")
    table.add_column("Symbol", style="bold", no_wrap=True, min_width=10)
    table.add_column("LTP", justify="right")
    table.add_column("Chg%", justify="right")
    table.add_column("Bid", justify="right")
    table.add_column("Ask", justify="right")
    table.add_column("Spr%", justify="right")
    table.add_column("Vol", justify="right")
    table.add_column("Pattern", justify="center")
    table.add_column("Trigger", justify="right")
    table.add_column("Dist%", justify="right")
    table.add_column("Pos", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("State", justify="center", min_width=11)

    for row in rows[:max_rows]:
        sym = str(row.get("symbol", ""))
        exch = str(row.get("exchange", ""))
        ltp = row.get("ltp")
        chg = row.get("change_pct", 0.0)
        bid = row.get("bid", 0.0)
        ask = row.get("ask", 0.0)
        spread_pct = row.get("spread_pct")
        vol = row.get("volume", 0)
        pattern = row.get("pattern") or "-"
        trigger = row.get("trigger_level")
        distance = row.get("distance_pct")
        pos = row.get("position", 0)
        pnl = row.get("unrealized_pnl", 0.0)
        state = row.get("state", "WATCHING")

        ltp_text = Text(f"{ltp:,.2f}") if isinstance(ltp, (int, float)) and ltp > 0 else Text("-", style="dim")
        chg_text = Text(f"{chg:+.2f}%", style=_style_change(chg if isinstance(chg, (int, float)) else 0))
        bid_text = Text(f"{bid:.2f}", style="dim") if bid else Text("-", style="dim")
        ask_text = Text(f"{ask:.2f}", style="dim") if ask else Text("-", style="dim")
        if isinstance(spread_pct, (int, float)):
            spread_text = Text(f"{spread_pct:.3f}%", style="dim")
        else:
            spread_text = Text("-", style="dim")
        vol_text = Text(f"{vol:,}") if vol else Text("-", style="dim")
        trigger_text = Text(f"{trigger:.2f}", style="magenta") if isinstance(trigger, (int, float)) and trigger else Text("-", style="dim")
        if isinstance(distance, (int, float)):
            dist_text = Text(f"{distance:+.2f}%", style=_style_distance(distance))
        else:
            dist_text = Text("-", style="dim")
        pos_text = Text(f"{pos}", style="bold") if pos else Text("-", style="dim")
        if not reveal_pnl:
            pnl_text = Text("hidden", style="dim")
        elif isinstance(pnl, (int, float)) and pos:
            pnl_text = Text(f"{pnl:+,.0f}", style="green" if pnl >= 0 else "red")
        else:
            pnl_text = Text("-", style="dim")
        state_text = Text(state, style=_style_state(state))

        table.add_row(
            f"{sym} {exch}",
            ltp_text,
            chg_text,
            bid_text,
            ask_text,
            spread_text,
            vol_text,
            pattern,
            trigger_text,
            dist_text,
            pos_text,
            pnl_text,
            state_text,
        )
    return table


# ---------------------------------------------------------------------------
# Detail panel
# ---------------------------------------------------------------------------


def render_detail_panel(
    row: Optional[Dict[str, Any]],
    candles: Sequence[Dict[str, float]],
    *,
    reveal_pnl: bool = True,
) -> Panel:
    """Render the per-symbol detail panel (selected row). P&L shown unless ``reveal_pnl`` is False."""
    if not row:
        return Panel(
            Text("No symbol selected. Press a key in the dashboard to focus a symbol.", style="dim italic"),
            title="Detail",
            border_style="blue",
        )
    sym = row.get("symbol", "?")
    exch = row.get("exchange", "?")
    ltp = row.get("ltp", 0)
    high = row.get("high", 0)
    low = row.get("low", 0)
    open_ = row.get("open", 0)
    prev = row.get("prev_close", 0)
    vol = row.get("volume", 0)
    pattern = row.get("pattern") or "no OCL match"
    trigger = row.get("trigger_level")
    state = row.get("state", "WATCHING")
    pos = row.get("position", 0)
    avg = row.get("avg_price")
    pnl = row.get("unrealized_pnl", 0)
    pnl_style = "green" if isinstance(pnl, (int, float)) and pnl >= 0 else "red"
    pnl_str = f"{pnl:+,.0f}" if isinstance(pnl, (int, float)) and pos else "-"
    if not reveal_pnl:
        pnl_str, pnl_style = "hidden", "dim"

    header = Text()
    header.append(f"{sym} {exch}", style="bold cyan")
    header.append("  LTP ")
    header.append(f"{ltp:,.2f}" if ltp else "-", style="bold")
    header.append("  Chg ")
    chg = row.get("change_pct", 0) or 0
    header.append(f"{chg:+.2f}%", style=_style_change(chg))
    header.append(f"  Vol {vol:,}" if vol else "")

    info = Table.grid(padding=(0, 1))
    info.add_column(style="dim", justify="right")
    info.add_column()
    info.add_row("Open", f"{open_:,.2f}" if open_ else "-")
    info.add_row("High", f"{high:,.2f}" if high else "-")
    info.add_row("Low", f"{low:,.2f}" if low else "-")
    info.add_row("Prev Close", f"{prev:,.2f}" if prev else "-")
    info.add_row("Pattern", pattern)
    info.add_row("Trigger", f"{trigger:,.2f}" if trigger else "-")
    info.add_row("State", Text(state, style=_style_state(state)))
    info.add_row("Position", f"{pos} @ {avg:,.2f}" if pos and avg else "-")
    info.add_row("Unrealized P&L", Text(pnl_str, style=pnl_style))

    body = Group(header, Text(""), info)

    if candles:
        try:
            spark = candle_sparkline(candles, width=2, height=3)
        except Exception:
            spark = "(candle render error)"
        body = Group(body, Text(""), Text("HTF candles:", style="dim"), Text(spark))

    return Panel(body, title=f"Detail - {sym}", border_style="blue")


# ---------------------------------------------------------------------------
# Alerts feed
# ---------------------------------------------------------------------------


def render_alerts_panel(alerts: Sequence[AlertEvent], max_lines: int = 12) -> Panel:
    """Render the alerts feed (matches + alerts + trade intents)."""
    if not alerts:
        return Panel(
            Text("No alerts yet.", style="dim italic"),
            title="Alerts",
            border_style="magenta",
        )
    text = Text()
    # Show newest first.
    for ev in list(reversed(alerts[-max_lines:])):
        ts = getattr(ev, "ts", 0)
        from datetime import datetime, timezone
        try:
            tstr = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")
        except Exception:
            tstr = "--:--:--"
        if isinstance(ev, MatchEvent):
            text.append(f"{tstr}  ", style="dim")
            text.append("MATCH  ", style="bold magenta")
            text.append(f"{ev.symbol} {ev.exchange}  ")
            text.append(f"{ev.pattern} ", style="bold")
            text.append(f"-> {ev.action} ", style="bold cyan")
            text.append(f"@ {ev.trigger_level:.2f}", style="magenta")
            if ev.touched:
                text.append("  TOUCHED", style="bold red")
            text.append("\n")
        elif isinstance(ev, TradeEvent):
            text.append(f"{tstr}  ", style="dim")
            side_style = "bold green" if ev.side == "BUY" else "bold red"
            text.append("TRADE  ", style="bold yellow")
            text.append(f"{ev.side} ", style=side_style)
            text.append(f"{ev.qty} {ev.symbol} {ev.exchange} @ {ev.price:.2f}  ")
            text.append(f"state={ev.state}", style="dim")
            text.append("\n")
        elif isinstance(ev, AlertEvent):
            sev_style = {"info": "blue", "warn": "yellow", "error": "bold red"}.get(ev.severity, "white")
            text.append(f"{tstr}  ", style="dim")
            text.append(f"ALERT  ", style=sev_style)
            text.append(f"{ev.message}\n", style=sev_style)
        else:
            text.append(f"{tstr}  {ev}\n")
    return Panel(text, title="Alerts", border_style="magenta")


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------


def render_status_bar(
    *,
    connected: bool,
    quote_refresh: float,
    scan_refresh: float,
    n_symbols: int,
    realized_pnl: float,
    unrealized_pnl: float,
    last_quote_at: Optional[float],
    cash: float,
) -> Panel:
    parts = []
    conn_text = Text()
    conn_text.append(" " if connected else " X ", style=("bold green" if connected else "bold red"))
    conn_text.append("CONNECTED  " if connected else "DISCONNECTED  ")
    parts.append(conn_text)
    parts.append(Text(f"Symbols {n_symbols}  ", style="bold"))
    parts.append(Text(f"Quote {quote_refresh:.0f}s  Scan {scan_refresh:.0f}s  ", style="dim"))
    if last_quote_at:
        from datetime import datetime, timezone
        try:
            t = datetime.fromtimestamp(last_quote_at, tz=timezone.utc).strftime("%H:%M:%S")
        except Exception:
            t = "--:--:--"
        parts.append(Text(f"Last quote {t}  ", style="dim"))
    parts.append(Text(f"Cash {cash:,.0f}  ", style="dim"))
    pnl_style = "green" if realized_pnl + unrealized_pnl >= 0 else "red"
    parts.append(Text(f"Realized {realized_pnl:+,.0f}  ", style="green" if realized_pnl >= 0 else "red"))
    parts.append(Text(f"Unrealized {unrealized_pnl:+,.0f}", style=pnl_style))
    return Panel(Text("  ").join(parts), border_style="green", title="OCL Trading Dashboard")


# ---------------------------------------------------------------------------
# Top-level layout
# ---------------------------------------------------------------------------


def build_layout() -> Layout:
    """Build the dashboard's outer Layout. Inner panels are populated
    by the LiveDashboard render loop.
    """
    layout = Layout(name="root")
    layout.split_column(
        Layout(name="status", size=3),
        Layout(name="body", ratio=1),
    )
    layout["body"].split_row(
        Layout(name="universe", ratio=3),
        Layout(name="right", ratio=2),
    )
    layout["right"].split_column(
        Layout(name="detail", ratio=1),
        Layout(name="alerts", ratio=1),
    )
    return layout