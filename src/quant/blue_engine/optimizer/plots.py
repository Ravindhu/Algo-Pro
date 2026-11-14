"""
optimizer/plots.py - the three AGENTS.md workflow figures, as Plotly.

CONTRACT
--------
Every function takes the DATA artefact from ``analysis.py`` (not a study) and
returns a ``dict``: ``{"ok", "figure", "error"}``. Two rules that make this
file safe to call from both the CLI and the Dash page:

1. plotly is imported lazily inside each function. The tests (and a bare
   machine) run without it: the functions then return ``ok=False`` with the
   data intact, which is the dashboard's existing "grey is a first-class state"
   convention (see pages/ml_models.py).
2. An empty/failed input produces ``ok=False``, never a blank figure. A blank
   chart on a trading dashboard reads as "nothing happened"; ``ok=False`` with
   an error string reads as "this was not rendered" - the honest state.

THE THREE FIGURES, IN READING ORDER
-----------------------------------
    plot_param_importances()   Step 1 - ranked bar: what drives the ship
    plot_slice()               Step 2 - sweet spot per important parameter
    plot_parallel_coordinate() Step 3 - how they weave together, filtered
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

__all__ = ["plot_param_importances", "plot_slice",
           "plot_parallel_coordinate", "THEME"]


#: Palette mirrors the shared dark dashboard surfaces and financial colors.
THEME = {
    "paper": "rgba(0,0,0,0)",
    "panel": "rgba(0,0,0,0)",
    "grid": "#2C2E33",
    "fg": "#C1C2C5",
    "muted": "#909296",
    "accent": "#845EF7",
    "good": "#40C057",
    "bad": "#FA5252",
    "amber": "#FCC419",
}


def _blank(title: str, why: str) -> Dict[str, Any]:
    return {"ok": False, "figure": None, "error": why, "title": title}


def _layout(fig, title: str, x_title: str, y_title: str) -> None:
    """Shared dark-theme layout. In-place, cheap, and identical across figures."""
    fig.update_layout(
        title=title,
        paper_bgcolor=THEME["paper"],
        plot_bgcolor=THEME["panel"],
        font={"color": THEME["fg"], "size": 12},
        xaxis={"title": x_title, "gridcolor": THEME["grid"]},
        yaxis={"title": y_title, "gridcolor": THEME["grid"]},
        margin={"l": 60, "r": 20, "t": 50, "b": 50},
    )


# --------------------------------------------------------------------------- #
# Step 1 - importances
# --------------------------------------------------------------------------- #


def plot_param_importances(importance: Dict[str, Any],
                           *, k: Optional[int] = None) -> Dict[str, Any]:
    """Step 1: horizontal bar of variance-explained, most important on top.

    Bars above the top-k cut are accented; the rest are muted, which visually
    carries the workflow's "filter to the important ones" decision into the
    figure itself instead of requiring the reader to consult a list.
    """
    if not importance or not importance.get("ok"):
        return _blank("Parameter importances",
                      importance.get("reason", "no importance data"))
    imp: Dict[str, float] = importance.get("importance") or {}
    ranking: List[str] = importance.get("ranking") or sorted(
        imp, key=lambda n: imp[n], reverse=True)
    if not ranking:
        return _blank("Parameter importances", "no parameters ranked")
    k = int(k or len(ranking))
    names = list(reversed(ranking))               # horizontal bars read bottom-up
    vals = [imp.get(n, 0.0) for n in names]
    top_k = set(ranking[:k])

    try:
        import plotly.graph_objects as go
    except Exception as exc:  # noqa: BLE001
        return _blank("Parameter importances", f"plotly unavailable: {exc}")

    fig = go.Figure(go.Bar(
        x=vals, y=names, orientation="h",
        marker_color=[THEME["accent"] if n in top_k else THEME["muted"]
                      for n in names],
        hovertemplate="%{y}: %{x:.3f}<extra></extra>",
    ))
    _layout(fig, "Parameter importances (variance explained)",
            "share of score variance", "parameter")
    if importance.get("under_budget"):
        fig.update_layout(
            title="Parameter importances (UNDER-BUDGET - treat as provisional)")
    return {"ok": True, "figure": fig, "title": "Parameter importances"}


# --------------------------------------------------------------------------- #
# Step 2 - slices
# --------------------------------------------------------------------------- #


def plot_slice(slice_data: Dict[str, Any],
               names: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Step 2: one small-multiple row per parameter.

    Numeric parameters: median score vs binned value, with the plateau band
    shaded and the argmax marked. Categorical parameters: bar per category.
    Returns ONE figure containing all requested slices as stacked subplots,
    because a 2x2 grid of separate figures is worse to read than one column.
    """
    if not slice_data or not slice_data.get("ok"):
        return _blank("Parameter slices",
                      slice_data.get("reason", "no slice data"))
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception as exc:  # noqa: BLE001
        return _blank("Parameter slices", f"plotly unavailable: {exc}")

    slices = [s for s in (slice_data.get("slices") or [])
              if not names or s.get("name") in set(names)]
    if not slices:
        return _blank("Parameter slices", "no slices matched")

    fig = make_subplots(rows=len(slices), cols=1, shared_xaxes=False,
                        vertical_spacing=0.9 / max(1, len(slices)),
                        subplot_titles=[s.get("note", s["name"])
                                        for s in slices])
    for i, s in enumerate(slices, start=1):
        if s.get("kind") == "numeric":
            pts = s.get("points") or []
            xs = [p["mid"] for p in pts]
            ys = [p["median_score"] for p in pts]
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode="lines+markers",
                line={"color": THEME["accent"]},
                marker={"size": 6},
                name=s["name"],
                customdata=[[p["lo"], p["hi"], p["n"]] for p in pts],
                hovertemplate=("value %{x:.4g}<br>median score %{y:.3f}"
                               "<br>bin [%{customdata[0]:.4g}, "
                               "%{customdata[1]:.4g}], n=%{customdata[2]}"
                               "<extra></extra>")), row=i, col=1)
            # Plateau band: the honest "region you can trade" shading.
            pl = s.get("plateau")
            if pl:
                fig.add_vrect(x0=pl[0], x1=pl[1],
                              fillcolor=THEME["good"], opacity=0.10,
                              line_width=0, row=i, col=1)
            am = s.get("argmax")
            if am:
                fig.add_trace(go.Scatter(
                    x=[am["mid"]], y=[am["median_score"]], mode="markers",
                    marker={"color": THEME["good"], "size": 12,
                            "symbol": "diamond"},
                    name=f"{s['name']} argmax",
                    hovertemplate="peak: %{x:.4g}<extra></extra>"),
                    row=i, col=1)
        else:
            cats = s.get("categories") or []
            fig.add_trace(go.Bar(
                x=[str(c["value"]) for c in cats],
                y=[c["median_score"] for c in cats],
                marker_color=[THEME["good"] if c == (s.get("argmax") or {})
                              else THEME["muted"] for c in cats],
                name=s["name"],
                hovertemplate="%{x}: %{y:.3f} (n=%{customdata})<extra></extra>",
                customdata=[[c["n"]] for c in cats]), row=i, col=1)
        fig.update_yaxes(title_text="median score", row=i, col=1)
        fig.update_xaxes(title_text=s["name"], row=i, col=1)

    fig.update_layout(
        title="Parameter slices - individual sweet spots (shaded = plateau)",
        paper_bgcolor=THEME["paper"], plot_bgcolor=THEME["panel"],
        font={"color": THEME["fg"], "size": 12},
        showlegend=False, height=280 * len(slices) + 90,
        margin={"l": 60, "r": 20, "t": 60, "b": 40})
    return {"ok": True, "figure": fig, "title": "Parameter slices"}


# --------------------------------------------------------------------------- #
# Step 3 - parallel coordinates
# --------------------------------------------------------------------------- #


def plot_parallel_coordinate(parallel_data: Dict[str, Any]) -> Dict[str, Any]:
    """Step 3: one line per top trial across the important parameters.

    Axes are min-max normalised by the FULL searched range (computed in
    ``analysis.parallel_report``), so the chart shows where the good region
    sits *inside* the range searched - which is the question this figure
    answers. Axis tick labels carry the real values so normalisation does not
    hide the numbers.
    """
    if not parallel_data or not parallel_data.get("ok"):
        return _blank("Parallel coordinates",
                      parallel_data.get("reason", "no parallel data"))
    try:
        import plotly.graph_objects as go
    except Exception as exc:  # noqa: BLE001
        return _blank("Parallel coordinates", f"plotly unavailable: {exc}")

    axes = parallel_data.get("axes") or []
    lines = parallel_data.get("lines") or []
    if not axes or not lines:
        return _blank("Parallel coordinates", "no axes or lines to draw")

    dims = []
    for ax in axes:
        name = ax["name"]
        if ax["kind"] == "numeric":
            lo, hi = ax["lo"], ax["hi"]
            ticks = [lo, 0.5 * (lo + hi), hi]
            dims.append(dict(
                label=name, values=[l["coords"].get(name, 0.5) for l in lines],
                range=[0.0, 1.0],
                tickvals=[0.0, 0.5, 1.0],
                ticktext=[_fmt(t) for t in ticks]))
        else:
            order = ax.get("order") or []
            # Category positions on [0,1]; text labels preserved on the axis.
            dims.append(dict(
                label=name,
                values=[l["coords"].get(name, 0.5) for l in lines],
                range=[0.0, max(1.0, len(order) - 1)],
                tickvals=list(range(len(order))),
                ticktext=[str(o) for o in order]))

    # Colour encodes the trial score so "how they weave together at peak"
    # reads at a glance: bright = best, dark = merely good.
    scores = [float(l["score"]) for l in lines]
    lo_s, hi_s = min(scores), max(scores)
    span = (hi_s - lo_s) or 1.0
    colors = [int(255 * (s - lo_s) / span) for s in scores]

    fig = go.Figure(data=go.Parcoords(
        line={"color": colors,
              "colorscale": [[0, THEME["muted"]], [1, THEME["accent"]]],
              "showscale": True,
              "colorbar": {"title": "score"}},
        dimensions=dims))
    sr = parallel_data.get("score_range") or {}
    fig.update_layout(
        title=(f"Parallel coordinates - top {len(lines)} trials "
               f"(score {sr.get('cutoff', 0):.3f} to {sr.get('best', 0):.3f})"),
        paper_bgcolor=THEME["paper"], plot_bgcolor=THEME["panel"],
        font={"color": THEME["fg"], "size": 12},
        margin={"l": 80, "r": 80, "t": 60, "b": 40})
    return {"ok": True, "figure": fig, "title": "Parallel coordinates"}


def _fmt(v: float) -> str:
    """Compact axis tick labels (1e-3 scale params otherwise eat the axis)."""
    a = abs(v)
    if a == 0:
        return "0"
    if a >= 100 or a < 0.01:
        return f"{v:.3g}"
    return f"{v:.4g}"
