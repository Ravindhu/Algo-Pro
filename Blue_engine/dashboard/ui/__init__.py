"""
blue_engine.dashboard.ui - the shared Dash Mantine Components kit.

WHY THIS PACKAGE EXISTS
-----------------------
Before this package, all nine pages re-implemented the same four primitives
by hand, with class-name strings that only worked because ``pages/theme.py``
injected ~95 lines of hand-written CSS:

    terminal.py     _kpi(), _input_table()
    portfolio.py    (inline .kpi-card blocks) x2
    risk_control.py _status_pill(), (inline blocks), _risk_inputs()
    analytics.py    _execution_table(), (inline blocks)
    infrastructure.py _health_row(), _log_lines(), (inline blocks)
    ml_models.py    _kpi(), _pill(), _run_summary()
    optimizer.py    _kpi(), _pill(), _figure_block()
    analytics_portfolio.py _kpi(), _warnings_block()

That is eight copies of "a card with a label and a value". Dash Mantine
Components (DMC) replaces all of it with real components, so this package
holds exactly ONE implementation of each:

    mode.py       mode -> colour-scheme mapping (analyzer=dark, live=light)
    provider.py   the MantineProvider shell + the mode banner
    kpi.py        KpiCard / KpiRow
    panel.py      Panel (titled surface)
    table.py      DataTable (typed columns, no raw <table> markup)
    log_view.py   LogView (the raw log tail)
    inputs.py     the config-only control set (option A, see below)

READ-ONLY GUARANTEE
-------------------
Every control in ``inputs.py`` writes a PARAMETER (a number the engine reads
on its next cycle). Nothing in this package places, modifies or cancels an
order, and nothing here imports ``openalgo``. See ``inputs.py`` for the full
statement of the config-only contract.
"""
from __future__ import annotations

from blue_engine.dashboard.ui.kpi import KpiCard, KpiRow
from blue_engine.dashboard.ui.log_view import LogView
from blue_engine.dashboard.ui.mode import COLOR_SCHEMES, scheme_for_mode
from blue_engine.dashboard.ui.panel import Panel
from blue_engine.dashboard.ui.table import DataTable

__all__ = [
    "COLOR_SCHEMES",
    "DataTable",
    "KpiCard",
    "KpiRow",
    "LogView",
    "Panel",
    "scheme_for_mode",
]
