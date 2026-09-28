"""
ml_models.py - Page: ML Models (the offline learning layer's scoreboard).

WHAT THIS PAGE ANSWERS, IN THE ORDER AN OPERATOR ASKS IT
--------------------------------------------------------
   1. Is there a model at all, and is it *allowed* to trade?   -> KPI cards + status pill
   2. Which candidate won, and by how much?                    -> candidate table
   3. Is the edge real, or did we just search hard?            -> deflated Sharpe column
   4. Does it survive Indian transaction costs?                -> cost-gate column
   5. Is the edge consistent across time, or a coin flip?      -> per-fold table

WHY THIS PAGE EXISTS AT ALL
---------------------------
The training pipeline already computes every one of those numbers, but it
writes them into a signed manifest under ``data/ml_artifacts/``. Without a
page, the only way to read them is to open a JSON file — which means in
practice nobody checks them, and an unvalidated model eventually gets trusted
by accident. Showing the numbers is the control.

READ-ONLY, AND THAT MATTERS
---------------------------
Nothing here trains, promotes or deletes anything. A UI poll must never be
able to change the state of a model: an operator leaving this page open for an
afternoon must be exactly as safe as one who never opened it. Promotion
(draft -> validated) stays a deliberate, signed action outside the browser.

THE COLOUR RULES (used consistently, and they are the honest ones)
------------------------------------------------------------------
    green   the number clears its bar
    amber   clear-ish but not established (needs more data / fewer trials)
    red     fails the bar — do not trade this
    grey    NOT COMPUTABLE (we could not measure it)

Grey is a first-class state on purpose. "We could not measure it" is NOT the
same as "OK", and rendering a missing number as green would be the exact
false-comfort this page exists to prevent.
"""
from __future__ import annotations

import time

import dash
from dash import dcc, html

from blue_engine.dashboard.pages import _data

dash.register_page(__name__, path="/ml", name="ML Models", order=7)

# --- palette (mirrors the rest of the terminal) ---------------------------- #
_GREEN = "#22c55e"
_AMBER = "#f59e0b"
_RED = "#ef4444"
_GREY = "#94a3b8"

#: The deflated-Sharpe probability bar. Below this the result is not
#: established: it could be the best of a lucky search. Kept as a named
#: constant because it is a *decision threshold*, not a display preference —
#: see ml/evaluation/metrics.py:deflated_sharpe.
_PROB_REAL_BAR = 0.95

#: Minimum parameter count for a model to be a model (a fold-level fit needs
#: enough rows to train on at all). Mirrors training min_train's spirit.
_MIN_TRAIN = 20


def _pill(text: str, ok: bool | None, *, warn: bool = False) -> html.Span:
    """A status pill. ``ok=None`` renders grey = not computable (not 'OK')."""
    if ok is None:
        bg, fg = "#334155", _GREY
    elif ok:
        bg, fg = "#14532d", "#dcfce7"
    elif warn:
        bg, fg = "#78350f", "#fef3c7"
    else:
        bg, fg = "#7f1d1d", "#fee2e2"
    return html.Span(text, className="pill", style={"background": bg, "color": fg})


def _num(value, digits: int = 4, *, pct: bool = False, default: str = "-") -> str:
    """Format a metric, rendering None as an explicit dash (never as 0.0)."""
    if value is None:
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if pct:
        return f"{f * 100:.1f}%"
    return f"{f:.{digits}f}"


def _skill_colour(mean, std) -> str:
    """Colour a skill cell by the SAME rule the trainer's verdict uses.

    This mirrors ``fold_summary``'s verdict logic on purpose: if the page and
    the manifest disagreed about whether an edge is real, the page would be
    worse than useless.
    """
    if mean is None:
        return _GREY
    if mean <= 0:
        return _RED
    if std is not None and std > mean:
        return _AMBER          # unstable: dispersion swamps the signal
    return _GREEN


def _prob_colour(p) -> str:
    """Deflated-Sharpe verdict colour."""
    if p is None:
        return _GREY
    if p >= _PROB_REAL_BAR:
        return _GREEN
    if p >= 0.80:
        return _AMBER
    return _RED


def _cost_colour(survives) -> str:
    """Cost-gate colour. None = the gate could not run (grey, not green)."""
    if survives is None:
        return _GREY
    return _GREEN if survives else _RED


def _kpi(label: str, value: str, colour: str = "#e2e8f0") -> html.Div:
    return html.Div(
        [
            html.Div(label, className="kpi-label"),
            html.Div(value, className="kpi-value", style={"color": colour}),
        ],
        className="kpi-card",
    )


# --------------------------------------------------------------------------- #
# panels
# --------------------------------------------------------------------------- #


def _run_summary(runs) -> html.Div:
    """The provenance strip for the latest run: when, from what, at what commit."""
    if not runs:
        return html.Div("No training runs recorded yet.", className="empty")
    run = runs[0]
    created = run.get("created_at")
    when = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created))
            if created else "-")
    items = [
        ("Run", str(run.get("run_id") or "-")),
        ("Symbol", str(run.get("symbol") or "-")),
        ("Created", when),
        ("Git commit", str(run.get("git_commit") or "-")),
        ("ML version", str(run.get("ml_version") or "-")),
        ("Sources", ", ".join(run.get("sources") or []) or "-"),
        ("Rows / features", f"{run.get('n_rows')} / {run.get('n_features')}"),
        ("Horizon / folds", f"{run.get('horizon')}d / {run.get('n_splits')}"),
        ("Trials (n_trials)", str(run.get("n_trials"))),
        ("Artifact", str(run.get("short_address") or "-")),
    ]
    return html.Table(
        [html.Thead(html.Tr([html.Th("Field"), html.Th("Value")])),
         html.Tbody([html.Tr([html.Td(k, style={"fontWeight": 600}),
                              html.Td(v, className="mono")]) for k, v in items])],
        className="data-table",
    )


def _candidate_table(cands) -> html.Table | html.Div:
    """One row per candidate: skill, deflated Sharpe, cost gate, verdict.

    Column order follows the decision order, not the alphabetical order:
    a reader should be able to stop at the first red column.
    """
    if not cands:
        return html.Div("No candidates evaluated in this run.", className="empty")

    body = []
    for c in cands:
        mean, std = c.get("skill_mean"), c.get("skill_std")
        prob = c.get("prob_real")
        survives = c.get("survives_cost")
        body.append(
            html.Tr(
                [
                    html.Td(c.get("name", "-"), style={"fontWeight": 600}),
                    html.Td(c.get("family", "-")),
                    html.Td(f"{c.get('n_folds') or 0}"
                            + (f" ({c.get('n_errors')} err)" if c.get("n_errors") else "")),
                    # skill: mean ± std, coloured by the manifest's own rule
                    html.Td(
                        f"{_num(mean)} ± {_num(std)}",
                        className="mono",
                        style={"color": _skill_colour(mean, std)},
                    ),
                    html.Td(_num(c.get("skill_min")), className="mono",
                            style={"color": _skill_colour(c.get("skill_min"), None)}),
                    html.Td(_num(c.get("auc_mean")), className="mono"),
                    # deflated Sharpe: the correction for the search itself
                    html.Td(
                        _num(prob, pct=True),
                        className="mono",
                        style={"color": _prob_colour(prob)},
                    ),
                    html.Td(_num(c.get("deflated_sharpe"), digits=3), className="mono"),
                    html.Td(_num(c.get("n_trials"), digits=0), className="mono"),
                    # the Indian transaction-cost gate
                    html.Td(
                        f"{_num(c.get('net_bps'), digits=2)} bps",
                        className="mono",
                        style={"color": _cost_colour(survives)},
                    ),
                    html.Td(c.get("verdict", "-"),
                            style={"maxWidth": "420px", "whiteSpace": "normal"}),
                ]
            )
        )

    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Candidate"),
                        html.Th("Family"),
                        html.Th("Folds"),
                        html.Th("Skill (mean ± std)"),
                        html.Th("Worst fold"),
                        html.Th("AUC"),
                        html.Th("Deflated p"),
                        html.Th("Deflated Sharpe"),
                        html.Th("Trials"),
                        html.Th("Net edge"),
                        html.Th("Verdict"),
                    ]
                )
            ),
            html.Tbody(body),
        ],
        className="data-table",
    )


def _deflation_panel(cands) -> html.Div:
    """The 'did we just search hard?' panel, explained in-place.

    The number is meaningless without the trial count next to it, so the panel
    always prints both — an operator who reads only this panel still learns
    the right lesson.
    """
    if not cands:
        return html.Div("nothing to correct for — no candidates ran", className="empty")

    rows = []
    for c in cands:
        prob = c.get("prob_real")
        n_trials = c.get("n_trials")
        obs = c.get("deflated_sharpe")
        exp_max = c.get("expected_max_sharpe")
        if prob is None:
            note = ("not computable — the candidate produced no tradeable "
                    "returns in the test folds")
        elif prob >= _PROB_REAL_BAR:
            note = "established — the edge survives the correction for searching"
        elif prob >= 0.80:
            note = ("suggestive — needs more data or fewer trials before it "
                    "can be believed")
        else:
            note = ("NOT established — with this many trials, a result this "
                    "good is expected from luck alone")
        rows.append(
            html.Tr(
                [
                    html.Td(c.get("name", "-"), style={"fontWeight": 600}),
                    html.Td(str(n_trials if n_trials is not None else "-")),
                    html.Td(_num(obs, digits=4), className="mono"),
                    html.Td(_num(exp_max, digits=4), className="mono"),
                    html.Td(_num(prob, pct=True), className="mono",
                            style={"color": _prob_colour(prob)}),
                    html.Td(note, style={"whiteSpace": "normal"}),
                ]
            )
        )

    return html.Div(
        [
            html.Table(
                [
                    html.Thead(
                        html.Tr(
                            [
                                html.Th("Candidate"),
                                html.Th("Trials searched"),
                                html.Th("Observed Sharpe"),
                                html.Th("Expected max (luck)"),
                                html.Th("P(real)"),
                                html.Th("Reading"),
                            ]
                        )
                    ),
                    html.Tbody(rows),
                ],
                className="data-table",
            ),
            html.P(
                "Expected max is the Sharpe a random search of the same size "
                "would be expected to reach. If the observed number is not "
                "clearly above it, the result is selection luck. Trials must be "
                "the honest count — feature sets x models x horizons.",
                style={"color": _GREY, "fontSize": "12px", "marginTop": "8px"},
            ),
        ],
        className="panel",
    )


def _fold_table(cand) -> html.Table | html.Div:
    """Per-fold metrics — the dispersion that decides stability."""
    folds = cand.get("folds") or []
    if not folds:
        return html.Div("No folds recorded for this candidate.", className="empty")

    body = []
    for f in folds:
        if f.get("error"):
            body.append(
                html.Tr(
                    [
                        html.Td(str(f.get("fold"))),
                        html.Td("—", colSpan=8,
                                style={"color": _RED, "whiteSpace": "normal"}),
                        html.Td(str(f.get("error")), colSpan=2,
                                style={"color": _RED, "whiteSpace": "normal"}),
                    ]
                )
            )
            continue
        mean = f.get("skill")
        body.append(
            html.Tr(
                [
                    html.Td(str(f.get("fold"))),
                    html.Td(str(f.get("train_rows"))),
                    html.Td(str(f.get("test_rows"))),
                    html.Td(f.get("train_end", "-"), className="mono"),
                    html.Td(f.get("test_start", "-"), className="mono"),
                    html.Td(_num(f.get("accuracy")), className="mono"),
                    html.Td(_num(f.get("base_rate")), className="mono"),
                    html.Td(_num(mean), className="mono",
                            style={"color": _skill_colour(mean, None)}),
                    html.Td(_num(f.get("auc")), className="mono"),
                    html.Td(_num(f.get("sharpe"), digits=2), className="mono"),
                ]
            )
        )

    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Fold"),
                        html.Th("Train rows"),
                        html.Th("Test rows"),
                        html.Th("Train ends"),
                        html.Th("Test starts"),
                        html.Th("Accuracy"),
                        html.Th("Base rate"),
                        html.Th("Skill"),
                        html.Th("AUC"),
                        html.Th("Sharpe"),
                    ]
                )
            ),
            html.Tbody(body),
        ],
        className="data-table",
    )


def _errors_panel(errors) -> html.Div:
    """Unreadable artifacts, shown rather than swallowed."""
    if not errors:
        return html.Div("All artifacts readable.", style={"color": _GREY})
    return html.Div(
        [html.Div(e, className="mono", style={"color": _RED}) for e in errors]
    )


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #


def body():
    """Page content. Pure function of the current snapshot — no callbacks."""
    snap = _data.ml_training_snapshot()
    runs = snap.get("runs") or []
    latest = snap.get("latest")
    cands = snap.get("candidates") or []

    # --- KPI row ----------------------------------------------------------- #
    n_runs = snap.get("n_runs", 0)
    status = (latest or {}).get("status", "-")
    best = (latest or {}).get("best_candidate")
    n_validated = sum(1 for r in runs if r.get("status") == "validated")

    # The single most important number on the page: how good is the best
    # candidate, and is it allowed near capital?
    best_cand = next((c for c in cands if c.get("name") == best), None)
    best_skill = (best_cand or {}).get("skill_mean")
    best_skill_txt = _num(best_skill) if best_cand else "no edge found"
    best_colour = _skill_colour(
        best_skill, (best_cand or {}).get("skill_std")) if best_cand else _RED

    kpis = html.Div(
        [
            _kpi("Runs recorded", str(n_runs)),
            _kpi("Validated models", str(n_validated),
                 _GREEN if n_validated else _GREY),
            _kpi("Latest status",
                 str(status).upper(),
                 _GREEN if status == "validated" else _AMBER),
            _kpi("Best candidate", str(best) if best else "none", best_colour),
            _kpi("Best skill", best_skill_txt, best_colour),
        ],
        className="kpi-row",
    )

    children = [
        html.H2("ML Models — training scoreboard"),
        html.P(
            "Read-only. Numbers are exactly what the walk-forward runner "
            "recorded in the signed manifest — nothing is recomputed here. "
            "Grey means NOT COMPUTABLE, which is not the same as OK.",
            style={"color": _GREY, "fontSize": "12px"},
        ),
        kpis,
    ]

    if not runs:
        children.append(
            html.Div(
                [
                    html.H3("No artifacts yet"),
                    html.P(
                        "Train a model to populate this page. From the repo root:"
                    ),
                    html.Pre(
                        "cd Blue_engine\n"
                        "..\\openalgo\\.venv\\Scripts\\python.exe "
                        "-c \"from blue_engine.ml.training import run_training; "
                        "print(run_training('NIFTY'))\"",
                        className="log-pre",
                    ),
                    html.P(
                        "A run is saved as DRAFT until it passes the "
                        "walk-forward and cost gates. Promotion to VALIDATED is "
                        "a deliberate, signed action — never a side effect of a "
                        "page refresh.",
                        style={"color": _GREY, "fontSize": "12px"},
                    ),
                ],
                className="panel",
            )
        )
        return children

    # --- provenance -------------------------------------------------------- #
    children.append(
        html.Div([html.H3("Latest run — provenance"), _run_summary(runs)],
                 className="panel")
    )

    # --- candidate comparison --------------------------------------------- #
    children.append(
        html.Div([html.H3("Candidates — honest comparison"),
                  _candidate_table(cands)], className="panel")
    )

    # --- deflated Sharpe ---------------------------------------------------- #
    children.append(
        html.Div([html.H3("Deflated Sharpe — corrected for the search"),
                  _deflation_panel(cands)], className="panel")
    )

    # --- per-fold drill-down ------------------------------------------------ #
    if best_cand:
        children.append(
            html.Div(
                [
                    html.H3(f"Per-fold metrics — {best_cand.get('name')}"),
                    html.P(
                        "Skill is accuracy minus the majority-class base rate. "
                        "A column of positive skill with a small spread is an "
                        "edge; a wide spread around a small mean is a coin flip.",
                        style={"color": _GREY, "fontSize": "12px"},
                    ),
                    _fold_table(best_cand),
                ],
                className="panel",
            )
        )

    # --- all runs ----------------------------------------------------------- #
    if len(runs) > 1:
        rows = []
        for r in runs:
            created = r.get("created_at")
            when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(created))
                    if created else "-")
            rows.append(
                html.Tr(
                    [
                        html.Td(when, className="mono"),
                        html.Td(str(r.get("name")), style={"fontWeight": 600}),
                        html.Td(str(r.get("symbol"))),
                        html.Td(_pill(str(r.get("status")).upper(),
                                      r.get("status") == "validated",
                                      warn=r.get("status") == "draft")),
                        html.Td(str(r.get("best_candidate") or "none")),
                        html.Td(str(r.get("n_candidates"))),
                        html.Td(str(r.get("short_address")), className="mono"),
                    ]
                )
            )
        children.append(
            html.Div(
                [
                    html.H3(f"Run history ({len(runs)})"),
                    html.Table(
                        [
                            html.Thead(
                                html.Tr(
                                    [
                                        html.Th("Created"),
                                        html.Th("Name"),
                                        html.Th("Symbol"),
                                        html.Th("Status"),
                                        html.Th("Best"),
                                        html.Th("Candidates"),
                                        html.Th("Artifact"),
                                    ]
                                )
                            ),
                            html.Tbody(rows),
                        ],
                        className="data-table",
                    ),
                ],
                className="panel",
            )
        )

    # --- errors ------------------------------------------------------------- #
    if snap.get("errors"):
        children.append(
            html.Div([html.H3("Artifact errors"), _errors_panel(snap["errors"])],
                     className="panel")
        )

    return children


def layout(**_kwargs):
    # Full page: the refreshable body plus its polling Interval.
    #
    # 15s, not 5s: this page reads artifacts that change only when a human
    # fires a training run, so polling faster buys nothing and re-reads the
    # index on every tick.
    return html.Div(
        [
            html.Div(id="ml-refresh-out", children=body()),
            dcc.Interval(id="ml-refresh", interval=15000, n_intervals=0),
        ],
        className="page",
    )