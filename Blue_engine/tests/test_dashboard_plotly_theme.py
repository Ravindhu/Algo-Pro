from __future__ import annotations

from blue_engine.dashboard.ui.plotly_theme import apply_plotly_theme


def test_plotly_theme_uses_transparent_dark_surface_and_muted_grid():
    import plotly.graph_objects as go

    figure = apply_plotly_theme(go.Figure())

    assert figure.layout.paper_bgcolor == "rgba(0,0,0,0)"
    assert figure.layout.plot_bgcolor == "rgba(0,0,0,0)"
    assert figure.layout.font.color == "#C1C2C5"
    assert figure.layout.template.layout.xaxis.gridcolor == "#2C2E33"
    assert figure.layout.template.layout.yaxis.gridcolor == "#2C2E33"
