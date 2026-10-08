"""
optimizer.py - Page: Strategy Optimizer (the parameter search terminal).

WHAT THIS PAGE ANSWERS, IN THE ORDER AN OPERATOR ASKS IT
-------------------------------------------------------
    1. Did a search happen, and what did it conclude?  -> KPI row + verdict banner
    2. Which parameters are driving the ship?          -> Step 1 importance bar
    3. What is each one's sweet spot?                  -> Step 2 slice plots
    4. How do they weave together at peak Sharpe?      -> Step 3 parallel coords
    5. Should I change the live configuration?         -> recommendation table

THE ORDER IS THE PRODUCT, NOT A LAYOUT CHOICE
---------------------------------------------
A 10-parameter study is unreadable if you look at all ten charts at once. The
page renders the three steps in the order the workflow prescribes, and Step 1
decides which parameters Steps 2 and 3 even show. Reading them out of order is
how a study stops being interpretable.

READ-ONLY, AND THAT MATTERS
---------------------------
Nothing here runs a study, promotes a parameter set, or writes to any live
configuration. Every number is read from the study journal the CLI wrote. An
operator leaving this page open all afternoon must be exactly as safe as one
who never opened it.

WHY THE PAGE READS THE JOURNAL INSTEAD OF RE-RUNNING
----------------------------------------------------
A study is a *measurement*. If the page recomputed it, the page and the report
could disagree about the same run - which is the one thing a provenance display
must never do. So this page shows exactly what the engine recorded.

GREY IS A FIRST-CLASS STATE
---------------------------
"Not computable" is NOT "OK". A study with too few trials, a missing space, or
an unreadable journal all render as an explicit grey/amber state with the
reason shown, never as a silently empty chart.
"""
from __future__ import annotations

import dash
import dash_mantine_components as dmc
from dash import dcc, html
from dash_mantine_components import Badge

from blue_engine.dashboard.pages import _data
from blue_engine.dashboard.pages import _optimizer_header as _opt_hdr
from blue_engine.dashboard.ui.kpi import KpiCard

dash.register_page(__name__, path="/optimizer", name="Optimizer", order=9)

# --- palette (mirrors the rest of the terminal) ---------------------------- #
_GREEN = "#22c55e"
_AMBER = "#f59e0b"
_RED = "#ef4444"
_GREY = "#94a3b8"

#: Verdict -> colour. The same rule the report uses, so the page and the
#: Markdown can never disagree about what a study concluded.
_VERDICT_COLOUR = {
    "ESTABLISHED": _GREEN,
    "SUGGESTIVE": _AMBER,
    "NOT ESTABLISHED": _RED,
    "NO EDGE FOUND": _RED,
    "NOT COMPUTABLE": _GREY,
}


def _kpi(label: str, value: str, colour: str | None = None) -> html.Div:
    """Thin shim over ``ui.kpi.KpiCard`` (keeps this page's call sites)."""
    return KpiCard(label, value, colour=colour)


def _pill(text: str, colour: str) -> Any:
    """A DMC badge.

    The old version took a raw hex colour and forced a slate background; DMC
    needs a named colour. We map the handful of hexes this page actually passes
    so the badges keep their meaning instead of all turning grey.
    """
    named = {
        "#22c55e": "green",
        "#ef4444": "red",
        "#f59e0b": "yellow",
        "#38bdf8": "blue",
        "#94a3b8": "gray",
        "#a855f7": "violet",
    }.get((colour or "").lower(), "gray")
    return Badge(str(text), color=named, variant="filled", size="sm",
                 radius="xl", className="pill")


def _num(value, digits: int = 4, default: str = "-") -> str:
    """Format a metric, rendering None as an explicit dash (never as 0.0)."""
    if value is None:
        return default
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _figure_block(fig_result, title: str) -> html.Div:
    """Render one plots.py result, honouring its ok=False contract.

    ``plots.py`` returns ``{"ok", "figure", "error"}`` rather than raising when
    plotly is missing or the input is too thin. An ``ok=False`` must render as a
    stated reason, NOT an empty chart - a blank panel on a trading dashboard
    reads as "nothing happened", which is a different and misleading claim.
    """
    if fig_result and fig_result.get("ok") and fig_result.get("figure") is not None:
        return dcc.Graph(figure=fig_result["figure"],
                         config={"displayModeBar": False})
    why = (fig_result or {}).get("error") or "not rendered"
    return html.Div(
        [html.H4(title),
         html.P(f"Not rendered: {why}",
                style={"color": _GREY, "fontSize": "12px"})],
        className="panel")


def _input_header(snap: dict, filters: dict,
                  study_name: str | None) -> html.Div:
    """Read-only filter header above the KPI row.

    The page is one journal at a time. The header lets the operator pick
    which existing study to view, grouped by three axes the desk cares about:

      1. ``symbol``   - the tradable (NIFTY50, BANKNIFTY, RELIANCE, ...),
                        parsed from the study name when it follows the
                        ``<SYMBOL>_<PERIOD>_<strategy>`` convention.
      2. ``time period`` - the date window (``2024Q1``, ``2024``, ``JAN``,
                        ...), parsed the same way.
      3. ``strategy`` - the search space the study optimised over, sourced
                        from the alpha engine's registered spaces
                        (``ocl_keylevel``, ``pov_slicer``,
                        ``portfolio_rebalance``, ``synthetic_demo``).

    The page never writes anything. Changing a dropdown only updates the
    URL search (?symbol=...&period=...&strategy=...&study=...); the next
    render reads that URL and re-asks ``optimizer_snapshot()`` for the
    matching study. The defaults are: the most recent study on disk.

    The header degrades honestly: when there are no studies at all, every
    dropdown shows the single "(no studies found)" entry and the operator
    sees the existing "Run a search" panel as before.
    """
    studies = filters.get("studies") or []
    if not studies:
        # The "no study" state is the existing path; do not stack a
        # useless header on top of it.
        return html.Div()

    # --- the four axes -------------------------------------------------- #
    # STUDY is the actual journal to load. The other three are labels the
    # operator uses to find one - the page derives the actual study name
    # from the most recent matching study on disk.
    study_options = [{"label": s, "value": s} for s in studies]
    # Preselect the current study (or the newest if none was asked for).
    current_study = study_name or snap.get("name") or (studies[0] if studies else None)
    study_value = current_study if current_study in studies else (studies[0] if studies else None)

    strategy_options = [{"label": s, "value": s} for s in (filters.get("strategies") or [])]
    symbol_options = [{"label": s, "value": s} for s in (filters.get("symbols") or [])]
    period_options = [{"label": p, "value": p} for p in (filters.get("periods") or [])]

    # When the symbol/period/strategy axes are empty (no study follows the
    # naming convention), show a single "(none detected)" entry so the
    # dropdown does not collapse into a misleadingly-empty widget.
    if not strategy_options:
        strategy_options = [{"label": "(none detected)", "value": "__none__", "disabled": True}]
    if not symbol_options:
        symbol_options = [{"label": "(none detected)", "value": "__none__", "disabled": True}]
    if not period_options:
        period_options = [{"label": "(none detected)", "value": "__none__", "disabled": True}]

    # --- the row layout -------------------------------------------------- #
    # A 4-column flex row: study | strategy | symbol | period. The "study"
    # column is the actual load selector; the other three are display
    # filters derived from the study-name convention. Flex wraps cleanly
    # on narrow viewports; the CSS theme does not define a grid class so
    # flex is the portable choice.
    col_style = {"flex": "1 1 0", "minWidth": "160px",
                 "paddingRight": "12px", "display": "inline-block"}
    return html.Div([
        html.Div([
            html.Label("Study",
                       style={"color": _GREY, "fontSize": "11px",
                              "display": "block", "marginBottom": "2px"}),
            dcc.Dropdown(
                id="optimizer-study-pick",
                options=study_options,
                value=study_value,
                clearable=False,
                placeholder="Pick a study journal...",
                style={"fontSize": "12px"},
            ),
        ], style=col_style),
        html.Div([
            html.Label("Strategy (from alpha engine)",
                       style={"color": _GREY, "fontSize": "11px",
                              "display": "block", "marginBottom": "2px"}),
            dcc.Dropdown(
                id="optimizer-strategy-pick",
                options=strategy_options,
                value=(filters.get("strategies") or [None])[0],
                clearable=True,
                placeholder="Any strategy",
                style={"fontSize": "12px"},
            ),
        ], style=col_style),
        html.Div([
            html.Label("Symbol",
                       style={"color": _GREY, "fontSize": "11px",
                              "display": "block", "marginBottom": "2px"}),
            dcc.Dropdown(
                id="optimizer-symbol-pick",
                options=symbol_options,
                value=(filters.get("symbols") or [None])[0],
                clearable=True,
                placeholder="Any symbol",
                style={"fontSize": "12px"},
            ),
        ], style=col_style),
        html.Div([
            html.Label("Time period",
                       style={"color": _GREY, "fontSize": "11px",
                              "display": "block", "marginBottom": "2px"}),
            dcc.Dropdown(
                id="optimizer-period-pick",
                options=period_options,
                value=(filters.get("periods") or [None])[0],
                clearable=True,
                placeholder="Any period",
                style={"fontSize": "12px"},
            ),
        ], style=col_style),
    ], style={"display": "flex", "alignItems": "flexEnd",
              "flexWrap": "wrap", "marginBottom": "14px"})


def _verdict_panel(snap: dict) -> html.Div:
    v = snap.get("verdict") or {}
    status = v.get("verdict") or "NO STUDY"
    colour = _VERDICT_COLOUR.get(status, _GREY)
    d = v.get("deflation") or {}

    rows = [
        html.Div([_pill(status, colour)],
                 style={"marginBottom": "10px"}),
    ]
    if v.get("reason"):
        rows.append(html.P(str(v["reason"]),
                           style={"color": _GREY, "fontSize": "12px"}))

    table = [
        ("Best trial", f"#{v.get('best_trial')}" if v.get("best_trial") is not None
         else "-"),
        ("Best mean-fold Sharpe", _num(v.get("best_mean_sharpe"))),
        ("Improvement vs defaults",
         _num(v.get("improvement_vs_baseline"), 4, "-")),
        ("Deflated Sharpe", _num(d.get("deflated_sharpe"))),
        ("Expected max from search", _num(d.get("expected_max_sharpe"))),
        ("Probability real",
         (f"{d['prob_real']} (bar {v.get('prob_real_bar')})"
          if d.get("prob_real") is not None else "-")),
        ("Return observations (n_obs)", str(d.get("n_obs", "-"))),
        # AUDIT FINDING D2 (2026-10-01). The cost-gate verdict is the only
        # number that answers "does the edge survive Indian transaction
        # costs" in one word. The Markdown report has had it since F2; the
        # dashboard now matches. ``-`` when the snapshot carries no gate
        # (a study with no scoreable trials, or an old journal). Walrus
        # binds the dict to ``cg`` so the next two cells can read the
        # same value without re-doing the lookup.
        ("Cost gate (best trial)",
         (cg.get("verdict", "-") if (cg := snap.get("cost_gate_best"))
          else "-")),
        ("Cost gate (gross / cost / net bps)",
         ((f"{cg['gross_bps']} / {cg['cost_bps']} / {cg['net_bps']}")
          if (cg := snap.get("cost_gate_best")) and cg.get("gross_bps") is not None
          else "-")),
        ("Scoreable / run", f"{snap.get('n_ok', 0)} / {snap.get('n_trials', 0)}"),
        ("Failed trials", str(snap.get("n_failed", 0))),
    ]
    body = html.Table(
        [html.Tr([html.Td(k, style={"color": _GREY, "paddingRight": "14px"}),
                  html.Td(str(val))]) for k, val in table],
        style={"fontSize": "12px", "borderCollapse": "collapse"})
    rows.append(body)
    if d.get("verdict"):
        rows.append(html.P(str(d["verdict"]),
                           style={"color": colour, "fontSize": "12px",
                                  "marginTop": "8px"}))

    # --- AUDIT FINDING D5 (2026-10-01). Per-trial drill-down.
    # The audit's D5 intent is "show what the best trial actually saw".
    # The journal carries per-trial aggregates (mean/std/min Sharpe, n_obs,
    # cost-gate verdict) but NOT the per-fold list itself - the engine
    # stores aggregates on the TrialRecord and the per-fold list lives only
    # on the ObjectiveResult for the duration of one trial. So the honest
    # drill-down is the per-trial stats from snap["trials"] for the TOP
    # trials by mean Sharpe. This is the data the snapshot actually has
    # and is what an operator would want when asking "which of the top
    # trials is the most stable?" and "which has the smallest n_obs?"
    trials = snap.get("trials") or []
    # Restrict to scoreable trials with a numeric mean_sharpe.
    scoreable = [t for t in trials
                 if t.get("ok") and isinstance(t.get("mean_sharpe"), (int, float))]
    scoreable.sort(key=lambda t: t.get("mean_sharpe") or 0.0, reverse=True)
    top_n = scoreable[:5]                       # cap at 5 rows
    if top_n:
        rows.append(_per_trial_drilldown(top_n, best_trial_number=v.get("best_trial")))
    return html.Div(rows, className="panel")


def _per_trial_drilldown(top_trials: list, *, best_trial_number: int = None) -> html.Details:
    """A collapsible per-trial drill-down.

    Why an expander: the page is already long (verdict + 3 steps + recommendation
    + provenance + footer), and the drill-down is reference material, not headline
    material. A ``html.Details`` keeps the page compact while still making the
    answer to "which trial is which, and how stable was it?" one click away.

    Why a top-5 cap: a 200-trial study that dumps 200 rows here would defeat
    the purpose. Five is the most a reader can compare by eye; anything more
    should be a CSV export (out of scope for this audit).
    """
    header = html.Tr([
        html.Th("#",        style={"textAlign": "left",  "paddingRight": "10px"}),
        html.Th("mean",     style={"textAlign": "right", "paddingRight": "10px"}),
        html.Th("std",      style={"textAlign": "right", "paddingRight": "10px"}),
        html.Th("min",      style={"textAlign": "right", "paddingRight": "10px"}),
        html.Th("n_obs",    style={"textAlign": "right", "paddingRight": "10px"}),
        html.Th("cost gate",style={"textAlign": "left",  "paddingRight": "10px"}),
        html.Th("parameters", style={"textAlign": "left"}),
    ])
    body_rows = [header]
    for t in top_trials:
        cg = t.get("cost_gate") or {}
        is_best = best_trial_number is not None and t.get("number") == best_trial_number
        marker = "\u2605" if is_best else ""      # star marks the best trial
        body_rows.append(html.Tr([
            html.Td(f"{marker}#{t.get('number', '?')}",
                    style={"paddingRight": "10px",
                           "color": _GREEN if is_best else _GREY,
                           "fontWeight": "600" if is_best else "400"}),
            html.Td(_num(t.get("mean_sharpe"), 4, "-"),
                    style={"textAlign": "right", "paddingRight": "10px"}),
            html.Td(_num(t.get("std_sharpe"), 4, "-"),
                    style={"textAlign": "right", "paddingRight": "10px"}),
            html.Td(_num(t.get("min_sharpe"), 4, "-"),
                    style={"textAlign": "right", "paddingRight": "10px"}),
            html.Td(str(t.get("n_obs", "-")),
                    style={"textAlign": "right", "paddingRight": "10px"}),
            html.Td(str(cg.get("verdict", "-")),
                    style={"color": _GREY, "paddingRight": "10px"}),
            html.Td(_short_params(t.get("params") or {}),
                    style={"fontFamily": "monospace", "fontSize": "11px"}),
        ]))
    table = html.Table(body_rows,
                       style={"fontSize": "12px", "borderCollapse": "collapse",
                              "marginTop": "6px"})
    # The summary text says what the expander contains BEFORE the user opens
    # it - critical for a research tool where the operator scans many pages
    # and opens drill-downs only on the studies that look interesting.
    n_shown = len(top_trials)
    return html.Details([
        html.Summary(
            f"Per-trial drill-down (top {n_shown} by mean Sharpe, star = best)",
            style={"color": _GREY, "fontSize": "12px",
                   "cursor": "pointer", "marginTop": "10px"}),
        html.P(
            "Per-trial aggregate stats from the journal. The journal stores "
            "the mean / std / min Sharpe and the cost-gate verdict per trial; "
            "the per-fold breakdown itself is not written to disk. If you need "
            "the per-fold list, re-run with a smaller trial count and a "
            "verbose objective.",
            style={"color": _GREY, "fontSize": "11px", "marginTop": "6px"}),
        table,
    ])


def _short_params(params: dict) -> str:
    """Format a parameter dict for the drill-down row.

    Long values are truncated so the row fits the page. A 6-parameter study
    fits on one line; a 20-parameter one wraps. The fallback is a short
    ``k=value`` join.
    """
    parts = []
    for k in list(params.keys())[:6]:           # cap the visible keyset
        v = params[k]
        s = str(v)
        if len(s) > 8:
            s = s[:7] + "\u2026"
        parts.append(f"{k}={s}")
    if len(params) > 6:
        parts.append(f"\u2026+{len(params) - 6}")
    return "  ".join(parts)


def _recommendation_panel(snap: dict) -> html.Div:
    rec = snap.get("recommended") or {}
    base = snap.get("baseline") or {}
    v = snap.get("verdict") or {}
    status = v.get("verdict")

    children = [html.H3("Recommended parameters")]
    if not rec:
        children.append(html.P(
            "None. The verdict does not justify a change to the current "
            "configuration.",
            style={"color": _RED, "fontSize": "13px"}))
        return html.Div(children, className="panel")

    if status not in ("ESTABLISHED", "SUGGESTIVE"):
        children.append(html.P(
            f"Shown for information only - the verdict is {status}, so this "
            "parameter set is NOT recommended for the live configuration.",
            style={"color": _AMBER, "fontSize": "12px"}))

    rows = [html.Tr([html.Th("Parameter"), html.Th("Recommended"),
                     html.Th("Current default")])]
    for k, val in rec.items():
        cur = base.get(k, "-")
        changed = val != cur
        rows.append(html.Tr([
            html.Td(str(k)),
            html.Td(str(val), style={"color": _GREEN if changed else _GREY,
                                     "fontWeight": "600" if changed else "400"}),
            html.Td(str(cur), style={"color": _GREY}),
        ]))
    children.append(html.Table(rows, style={"fontSize": "12px",
                                            "borderCollapse": "collapse"}))
    children.append(html.P(
        "Applying these values to the live configuration is a separate, "
        "deliberate human action. This page never writes to production.",
        style={"color": _GREY, "fontSize": "11px", "marginTop": "10px"}))
    return html.Div(children, className="panel")


def body(study_name: str | None = None):
    """Page content. Pure function of the current snapshot - no callbacks.

    ``study_name`` is the optional override: when the user picks a different
    study from the input header at the top of the page, the URL search carries
    that name through to this function. If ``None``, the snapshot adapter
    picks the newest study on disk (existing behaviour).
    """
    snap = _data.optimizer_snapshot(name=study_name) if study_name else _data.optimizer_snapshot()
    v = snap.get("verdict") or {}
    imp = snap.get("importance") or {}
    wf = snap.get("workflow") or {}
    status = v.get("verdict") or "NO STUDY"
    colour = _VERDICT_COLOUR.get(status, _GREY)
    filters = _data.optimizer_available_filters() or {}

    children = [
        html.H2("Strategy Optimizer - parameter search"),
        # --- AUDIT FINDING D15 (2026-10-01). AI Insight header + control bar.
        # Phase 5 of the optimizer audit chain. The AI Insight header is a
        # read-only summary card that shows the entire optimizer setting
        # (mode, symbol, strategy, time period, every parameter, and cache
        # status). The control bar below it has the interactive controls
        # (mode toggle, dropdowns, date pickers, Download + Run buttons).
        # Run is disabled in Auto-Live mode.
        #
        # Both new regions are ADDED at the very top. The rest of the body
        # is unchanged: the study picker, KPI row, verdict panel, the
        # three-step visual workflow (importance / slice / parallel), the
        # recommendation table, the honest footer, and the provenance
        # panel are all preserved exactly as the previous audits shipped
        # them. The page is still a pure function of the journal.
        _opt_hdr._ai_insight_header(snap),
        _opt_hdr._control_bar(snap),
        # --- AUDIT FINDING D11 (2026-10-01). User input header.
        # Three read-only inputs that pick which existing study the page
        # displays. The page never writes to disk: the inputs only steer
        # the next ``optimizer_snapshot(name=...)`` call. The URL search
        # string is the single source of truth, so the page is bookmarkable
        # and the selection survives a refresh.
        _input_header(snap, filters, study_name),
        html.P(
            "Read-only. Every number is exactly what the optimizer recorded in "
            "its study journal - nothing is recomputed here. This engine places "
            "no orders and writes no live configuration.",
            style={"color": _GREY, "fontSize": "12px"}),
    ]

    # --- KPI row ----------------------------------------------------------- #
    children.append(html.Div([
        _kpi("Study", str(snap.get("name") or "none")),
        _kpi("Verdict", status, colour),
        _kpi("Trials (ok / run)",
             f"{snap.get('n_ok', 0)} / {snap.get('n_trials', 0)}"),
        _kpi("Best mean Sharpe", _num(v.get("best_mean_sharpe"))),
        _kpi("Probability real", _num((v.get("deflation") or {}).get("prob_real"))),
        _kpi("Improvement",
             _num(v.get("improvement_vs_baseline"), 4, "-"),
             _GREEN if (v.get("improvement_vs_baseline") or 0) > 0 else _GREY),
    ], className="kpi-row"))

    # --- no study at all --------------------------------------------------- #
    if not snap.get("ok"):
        msgs = (snap.get("errors") or []) + (snap.get("warnings") or [])
        children.append(html.Div([
            html.H3("No study to display"),
            html.P("Run a search to populate this page. From the repo root:"),
            html.Pre(
                "python Scripts\\optimize_strategy.py --space synthetic_demo "
                "--strategy synthetic --trials 150 --workflow",
                className="log-pre"),
            html.P("Then reload this page.", style={"color": _GREY,
                                                    "fontSize": "12px"}),
            *[html.P(m, style={"color": _AMBER, "fontSize": "12px"})
              for m in msgs],
        ], className="panel"))
        return children

    # --- verdict ----------------------------------------------------------- #
    children.append(_verdict_panel(snap))

    # --- STEP 1: importance ------------------------------------------------ #
    step1 = [
        html.H3("Step 1 - Which parameters are driving the ship?"),
        html.P("Variance of the trial score explained by each parameter. "
               "Read this FIRST: it decides which parameters Steps 2 and 3 "
               "show at all.",
               style={"color": _GREY, "fontSize": "12px"}),
    ]
    if wf.get("provisional"):
        step1.append(html.P(
            f"UNDER-BUDGET: only {imp.get('n_trials_used')} scoreable trials. "
            "Importance from a thin study is provisional - treat the ranking "
            "below as a hint, not a finding.",
            style={"color": _AMBER, "fontSize": "12px"}))
    try:
        from blue_engine.optimizer.plots import plot_param_importances
        step1.append(_figure_block(
            plot_param_importances(imp, k=len(snap.get("top_k") or []) or None),
            "Parameter importances"))
    except Exception as exc:  # noqa: BLE001
        step1.append(html.P(f"Not rendered: {exc}",
                            style={"color": _GREY, "fontSize": "12px"}))
    children.append(html.Div(step1, className="panel"))

    # --- STEP 2: slices ---------------------------------------------------- #
    step2 = [
        html.H3("Step 2 - Each important parameter's individual sweet spot"),
        html.P("The shaded band is the PLATEAU: the widest region within 5% of "
               "the best bin. Ship the middle of a plateau, never a spike - a "
               "spike is noise-fit and will not survive live.",
               style={"color": _GREY, "fontSize": "12px"}),
    ]
    try:
        from blue_engine.optimizer.plots import plot_slice
        # AUDIT FINDING D4 (2026-10-01). ``plot_slice`` returns a single
        # Plotly figure with stacked subplots, one per parameter; its
        # height grows linearly with the number of slices (~280 px each).
        # A 10-parameter study can produce a ~2,900 px-tall figure. Wrap
        # the slice panel in a scroll container so it cannot push the
        # recommendation panel and the honest-footer below the fold.
        # The cap (900 px) is the rough height of four slices; if the
        # study has more, the user scrolls inside the panel rather than
        # the page. Parallel-coordinates has a fixed height by Plotly's
        # default and is left alone.
        step2.append(html.Div(
            _figure_block(plot_slice(wf.get("step2_slice") or {}),
                          "Parameter slices"),
            style={"maxHeight": "900px", "overflowY": "auto",
                   "paddingRight": "8px"},
        ))
    except Exception as exc:  # noqa: BLE001
        step2.append(html.P(f"Not rendered: {exc}",
                            style={"color": _GREY, "fontSize": "12px"}))
    children.append(html.Div(step2, className="panel"))

    # --- STEP 3: parallel coordinates -------------------------------------- #
    step3 = [
        html.H3("Step 3 - How the important parameters weave together"),
        html.P("One line per top-scoring trial, axes normalised to the FULL "
               "searched range so you can see where the good region sits "
               "inside the space you actually searched. Bright lines scored "
               "highest.",
               style={"color": _GREY, "fontSize": "12px"}),
    ]
    try:
        from blue_engine.optimizer.plots import plot_parallel_coordinate
        step3.append(_figure_block(
            plot_parallel_coordinate(wf.get("step3_parallel") or {}),
            "Parallel coordinates"))
    except Exception as exc:  # noqa: BLE001
        step3.append(html.P(f"Not rendered: {exc}",
                            style={"color": _GREY, "fontSize": "12px"}))
    children.append(html.Div(step3, className="panel"))

    # --- recommendation ---------------------------------------------------- #
    children.append(_recommendation_panel(snap))

    # --- how to read this honestly ----------------------------------------- #
    children.append(html.Div([
        html.H3("How to read this honestly"),
        html.Ul([
            html.Li("A peak is not a plateau. Step 2 exists to separate them. "
                    "If a parameter's slice is a spike, that setting is "
                    "noise-fit and must not ship."),
            html.Li("Importance is measured on the trials that were RUN, not on "
                    "the market. A parameter can rank low simply because it was "
                    "never varied enough within the budget."),
            html.Li("The deflated Sharpe is the number to quote. The raw "
                    "best-trial Sharpe is the maximum of N draws and is biased "
                    "upward by construction."),
            html.Li("A study that did not establish an edge gets no "
                    "recommendation - the verdict says so before any table is "
                    "shown."),
        ], style={"fontSize": "12px", "color": _GREY}),
    ], className="panel"))

    # --- provenance -------------------------------------------------------- #
    rep = snap.get("report") or {}
    st = rep.get("study") or {}
    if st:
        children.append(html.Div([
            html.H3("Provenance"),
            html.Table([
                html.Tr([html.Td("space", style={"color": _GREY,
                                                 "paddingRight": "14px"}),
                         html.Td(str(st.get("space")))]),
                html.Tr([html.Td("strategy", style={"color": _GREY,
                                                    "paddingRight": "14px"}),
                         html.Td(str(st.get("strategy")))]),
                html.Tr([html.Td("backend", style={"color": _GREY,
                                                   "paddingRight": "14px"}),
                         html.Td(str(st.get("backend")))]),
                html.Tr([html.Td("seed", style={"color": _GREY,
                                                "paddingRight": "14px"}),
                         html.Td(str(st.get("seed")))]),
                html.Tr([html.Td("wall seconds", style={"color": _GREY,
                                                        "paddingRight": "14px"}),
                         html.Td(str(st.get("wall_seconds")))]),
                html.Tr([html.Td("data class", style={"color": _GREY,
                                                      "paddingRight": "14px"}),
                         html.Td(str(rep.get("data_class")))]),
            ], style={"fontSize": "12px", "borderCollapse": "collapse"}),
        ], className="panel"))

    return children# --------------------------------------------------------------------------- #
# Page layout
# --------------------------------------------------------------------------- #


def layout(**_kwargs):
    """Full page: the refreshable body plus its polling Interval.

    60s, not 15s like the ML page. A study is a *measurement* that only changes
    when a human runs the CLI - and re-rendering it is not free: the body
    rebuilds three Plotly figures, and the snapshot replays the whole trial
    journal. Polling faster would re-derive identical numbers on every tick.

    AUDIT FINDING D11 (2026-10-01). A ``dcc.Location`` was added so the
    read-only study picker in the header can write its choice to the URL
    search string. The URL is the single source of truth: bookmarking a
    page also bookmarks the chosen study, and refreshing the browser
    restores it. The location component never triggers a write to disk.
    """
    return html.Div(
        [
            dcc.Location(id="optimizer-url", refresh=False),
            html.Div(id="optimizer-refresh-out", children=body()),
            dcc.Interval(id="optimizer-refresh", interval=60000, n_intervals=0),
        ],
        className="page",
    )


def register_callbacks(app):
    """Wire the read-only study picker.

    AUDIT FINDING D11 (2026-10-01). The optimizer page has four
    dropdowns in the input header (Study, Strategy, Symbol, Time period).
    Only the Study dropdown drives the page; the other three are
    display filters that the operator reads but that do not change the
    snapshot. The Study dropdown writes its value into the URL search
    (``?study=<name>``) so the page is bookmarkable and so the existing
    60s polling callback re-reads the new study on the next tick without
    any extra wiring.

    The callback never mutates state on disk. It only updates the
    ``dcc.Location``'s search string, which the body reads on the next
    render. Dash treats the URL as client-side state, so the read-only
    guarantee of the page is preserved.
    """
    from dash import Input, Output, State

    @app.callback(
        Output("optimizer-url", "search"),
        Input("optimizer-study-pick", "value"),
        State("optimizer-url", "search"),
        prevent_initial_call=True,
    )
    def _sync_study_to_url(study_value: str | None, current_search: str | None):
        """Push the chosen study into the URL. The body reads it from there.

        We use ``prevent_initial_call`` so the page renders with the
        default (newest) study on first load and the URL is only
        rewritten when the operator actually picks something. This keeps
        the "first page" experience identical to before the header
        existed.
        """
        if not study_value:
            return current_search or ""
        # Preserve any other query parameters (strategy / symbol / period
        # for future expansion) by editing only the ``study`` key.
        from urllib.parse import urlencode, parse_qsl
        params = dict(parse_qsl((current_search or "").lstrip("?")))
        params["study"] = study_value
        return "?" + urlencode(params)

    @app.callback(
        Output("optimizer-refresh-out", "children"),
        Input("optimizer-url", "search"),
        Input("optimizer-refresh", "n_intervals"),
    )
    def _render_body(search: str | None, _n_intervals: int) -> list:
        """Render the body using the URL's ``study`` query parameter.

        This callback REPLACES the default ``m.body()`` refresh callback
        registered by ``dash_app._register_refresh_callbacks`` because
        Dash fires the first-declared callback per output. The behaviour
        is identical to the default except that it also reads
        ``?study=...`` from the URL. The page is still a pure function
        of the journal - we just let the URL choose which journal to
        read.
        """
        from urllib.parse import parse_qsl
        params = dict(parse_qsl((search or "").lstrip("?")))
        study = params.get("study") or None
        return body(study_name=study)