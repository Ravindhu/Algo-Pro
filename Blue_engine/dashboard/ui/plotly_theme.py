"""Shared transparent Plotly theme for the DMC dashboard."""
from __future__ import annotations

from functools import lru_cache
from typing import Any

TRANSPARENT = "rgba(0,0,0,0)"
GRID = "#2C2E33"
FOREGROUND = "#C1C2C5"


@lru_cache(maxsize=1)
def dashboard_dark_template() -> Any:
    """Build and cache the dashboard's dark, transparent Plotly template."""
    import plotly.graph_objects as go
    import plotly.io as pio

    template = go.layout.Template(pio.templates["plotly_dark"])
    template.layout.update(
        paper_bgcolor=TRANSPARENT,
        plot_bgcolor=TRANSPARENT,
        font={"color": FOREGROUND},
        xaxis={"gridcolor": GRID, "zerolinecolor": GRID},
        yaxis={"gridcolor": GRID, "zerolinecolor": GRID},
        polar={
            "bgcolor": TRANSPARENT,
            "radialaxis": {"gridcolor": GRID, "color": FOREGROUND},
            "angularaxis": {"gridcolor": GRID, "color": FOREGROUND},
        },
    )
    return template


def apply_plotly_theme(figure: Any) -> Any:
    """Apply the shared template and transparent chart surfaces to a figure."""
    figure.update_layout(
        template=dashboard_dark_template(),
        paper_bgcolor=TRANSPARENT,
        plot_bgcolor=TRANSPARENT,
        font={"color": FOREGROUND},
    )
    return figure


__all__ = ["apply_plotly_theme", "dashboard_dark_template"]
