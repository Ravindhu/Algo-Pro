"""
inputs.py - the config-only control set (option A).

THE CONTRACT (read this before adding a control)
------------------------------------------------
Every control here writes a *parameter*. A parameter is a number the engine
reads on its next cycle. That is all.

    control  ->  POV_CONFIG_PATH (blue_engine/input/master.json)  ->  engine
    control  -/->  OpenAlgo  ->  broker

There is deliberately NO code path in this module that places, modifies or
cancels an order, and nothing here imports ``openalgo``. That is what keeps
``dashboard/pages/__init__.py``'s promise -- "All pages are read-only. Nothing
here places an order" -- true even though the user can now type freely.

If a future requirement is to place orders from the dashboard, that is a NEW
module (``pages/order_entry.py``) which must call
``blue_engine.core.risk_gate.check_intent`` before OpenAlgo. It must not be
added here: mixing parameter writes with order emission in one module is how a
UI bug becomes a trading incident.

WHAT "ENGINE RE-READS THEM" MEANS IN PRACTICE
---------------------------------------------
The write is a read-modify-write of the JSON config file, performed atomically
(temp file + os.replace) so a crash mid-write cannot leave a truncated config.
The running engine polls the same file (``POV_CONFIG_PATH``), so a change made
here takes effect on its next cycle. We do not poke engine memory directly:
that would make the dashboard and the config disagree, and the config is the
record of what the system was told to do.

VALIDATION
----------
Every control declares bounds (min/max) and the writer clamps to them. A
clamped value is reported back to the operator rather than silently accepted,
because a silently-ignored risk limit is worse than a rejected one.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import dash_mantine_components as dmc
from dash import html
from dash_iconify import DashIconify

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Spec: what a control is
# ---------------------------------------------------------------------------


@dataclass
class ControlSpec:
    """One editable parameter.

    Attributes:
        key: the config key (``master.json`` top level).
        label: human label.
        group: which panel it belongs to.
        kind: "number" | "int" | "select" | "switch" | "range" | "text".
        minimum / maximum: clamp bounds for numeric kinds.
        step: numeric step.
        options: for "select".
        help: tooltip text.
        unit: suffix shown after the input (e.g. "%", "bps").
    """

    key: str
    label: str
    group: str
    kind: str = "number"
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    options: List[Dict[str, str]] = field(default_factory=list)
    help: str = ""
    unit: str = ""
    default: Any = None


#: The editable parameters. Bounds are deliberate and conservative -- these are
#: the values an operator may change from the UI, so they are the values a UI
#: bug could set. Nothing here can exceed a sane trading range.
CONTROL_SPECS: List[ControlSpec] = [
    # --- runtime ---------------------------------------------------------- #
    ControlSpec("mode", "Strategy mode", "runtime", "select",
                options=[{"label": "Short term", "value": "short_term"},
                         {"label": "Investing", "value": "investing"}],
                default="short_term",
                help="POV sizing style: short_term uses risk_pct, investing uses qty_per_fire."),
    ControlSpec("starting_cash", "Starting cash", "runtime", "number",
                minimum=1000, maximum=100_000_000, step=1000,
                unit="INR", default=100000,
                help="Virtual ledger capital for paper mode."),
    ControlSpec("quantity", "Quantity per fire", "runtime", "int",
                minimum=1, maximum=100_000, step=1, default=100,
                help="Shares per execution slice (investing mode)."),
    ControlSpec("touch_buffer_pct", "Touch buffer", "runtime", "number",
                minimum=0.0, maximum=5.0, step=0.01, unit="%", default=0.05,
                help="How close to the trigger counts as a touch."),
    ControlSpec("max_symbols", "Max symbols", "runtime", "int",
                minimum=1, maximum=200, step=1, default=25,
                help="Cap on the watched universe."),

    # --- POV band --------------------------------------------------------- #
    ControlSpec("vwap_filter_enabled", "VWAP band filter", "pov_band", "switch",
                default=True,
                help="Master switch for the buyer-initiated VWAP band."),
    ControlSpec("vwap_ceiling_pct", "Ceiling above VWAP", "pov_band", "number",
                minimum=0.0, maximum=20.0, step=0.05, unit="%", default=0.5,
                help="Skip chasing above VWAP + this percent."),
    ControlSpec("vwap_floor_pct", "Floor below VWAP", "pov_band", "number",
                minimum=0.0, maximum=20.0, step=0.05, unit="%", default=3.0,
                help="Passive-add band below VWAP - this percent."),
    ControlSpec("min_pov_pct", "POV rate min", "pov_band", "number",
                minimum=0.1, maximum=100.0, step=0.5, unit="%", default=5.0,
                help="Lower bound of the interpolated participation rate."),
    ControlSpec("max_pov_pct", "POV rate max", "pov_band", "number",
                minimum=0.1, maximum=100.0, step=0.5, unit="%", default=25.0,
                help="Upper bound of the interpolated participation rate."),

    # --- slippage --------------------------------------------------------- #
    ControlSpec("slippage_guard_enabled", "Slippage guard", "slippage", "switch",
                default=True,
                help="Halt slicing when realised slippage breaches the cap."),
    ControlSpec("max_slippage_bps", "Max slippage", "slippage", "number",
                minimum=1.0, maximum=500.0, step=1.0, unit="bps", default=50.0,
                help="Slippage budget per execution."),
    ControlSpec("slippage_penalty_factor", "Slippage penalty", "slippage", "number",
                minimum=0.0, maximum=5.0, step=0.05, default=0.5,
                help="Rate throttle applied when slippage rises."),
    ControlSpec("arrival_window_ticks", "Arrival window", "slippage", "int",
                minimum=1, maximum=200, step=1, default=10,
                help="Ticks used to measure implementation shortfall."),

    # --- risk ------------------------------------------------------------- #
    ControlSpec("stop_loss_price", "Stop loss price", "risk", "number",
                minimum=0.0, maximum=1_000_000.0, step=0.05, default=None,
                help="Absolute exit trigger. 0 disables."),
    ControlSpec("ceiling_price", "Ceiling price (LIMIT)", "risk", "number",
                minimum=0.0, maximum=1_000_000.0, step=0.05, default=None,
                help="Skip any chase above this price. 0 disables."),
    ControlSpec("upper_circuit", "Upper circuit", "risk", "number",
                minimum=0.0, maximum=1_000_000.0, step=0.05, default=None,
                help="Exchange price band, upper."),
    ControlSpec("lower_circuit", "Lower circuit", "risk", "number",
                minimum=0.0, maximum=1_000_000.0, step=0.05, default=None,
                help="Exchange price band, lower."),
    ControlSpec("quote_failure_threshold", "Quote failure threshold", "risk", "int",
                minimum=1, maximum=100, step=1, default=3,
                help="Pause the feed after N consecutive quote misses."),
    ControlSpec("cancellation_cool_off_seconds", "Cancel cool-off", "risk", "number",
                minimum=0.0, maximum=600.0, step=1.0, unit="s", default=30.0,
                help="Minimum wait before re-placing after a cancel."),
]

SPEC_BY_KEY: Dict[str, ControlSpec] = {s.key: s for s in CONTROL_SPECS}

#: Panel order + titles for the terminal page.
GROUP_TITLES: Dict[str, str] = {
    "runtime": "Runtime inputs",
    "pov_band": "POV band inputs",
    "slippage": "Slippage guard",
    "risk": "Risk limits",
}
GROUP_ICONS: Dict[str, str] = {
    "runtime": "mdi:cog-outline",
    "pov_band": "mdi:chart-bell-curve-cumulative",
    "slippage": "mdi:speedometer-slow",
    "risk": "mdi:shield-alert-outline",
}


# ---------------------------------------------------------------------------
# Reading the current values
# ---------------------------------------------------------------------------


def current_values() -> Dict[str, Any]:
    """Read the live config and project it onto the control keys.

    Returns a dict with one entry per spec (missing keys fall back to the
    spec default) plus ``_extra`` holding every other config key untouched, so
    a write never drops a parameter this UI does not know about.
    """
    from blue_engine.dashboard.pages import _data

    cfg = _data.load_config()
    flat = _data._pov_block(cfg) if isinstance(cfg, dict) else {}
    out: Dict[str, Any] = {}
    for spec in CONTROL_SPECS:
        if spec.key in flat and flat[spec.key] is not None:
            out[spec.key] = flat[spec.key]
        else:
            out[spec.key] = spec.default
    return out


def clamp(spec: ControlSpec, value: Any) -> tuple[Any, Optional[str]]:
    """Clamp a value to the spec bounds.

    Returns ``(coerced_value, warning_or_None)``. The warning is what the UI
    shows back; we never silently accept an out-of-range risk limit.
    """
    if spec.kind == "switch":
        return bool(value), None
    if spec.kind == "select":
        allowed = [o["value"] for o in spec.options]
        if value not in allowed:
            return (spec.default if spec.default is not None
                    else (allowed[0] if allowed else value)), \
                f"{spec.label}: {value!r} not allowed; kept previous."
    if spec.kind in ("number", "int"):
        try:
            num = float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return spec.default, f"{spec.label}: {value!r} is not a number."
        if num is None:
            return spec.default, None
        if spec.kind == "int":
            num = int(round(num))
        warn = None
        if spec.minimum is not None and num < spec.minimum:
            warn = f"{spec.label}: clamped up to {spec.minimum}."
            num = spec.minimum
        if spec.maximum is not None and num > spec.maximum:
            warn = f"{spec.label}: clamped down to {spec.maximum}."
            num = spec.maximum
        return num, warn
    return value, None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _target_block(existing: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    """Return the dict that actually supplies the POV values, and whether it
    is the nested ``pov`` block.

    WHY THIS EXISTS (bug found by test_dmc_migration.py, 2026-10-07)
    --------------------------------------------------------------
    ``_data._pov_block`` merges a NESTED ``pov`` dict OVER the top-level keys,
    so when ``master.json`` has both, the nested block wins for reads. Writing
    to the top level would then appear to succeed while the engine kept reading
    the nested value -- a silent no-op that looks like a working UI. That is the
    worst possible failure for a risk parameter.

    So the writer must target the same block the reader prefers: nested if
    present, else top level.
    """
    nested = existing.get("pov")
    if isinstance(nested, dict):
        return nested, True
    return existing, False


def write_values(updates: Dict[str, Any]) -> tuple[bool, List[str]]:
    """Merge ``updates`` into the config file atomically.

    Read-modify-write: we load the existing file, overlay only the keys this UI
    owns, and write the whole thing back via a temp file + ``os.replace``.

    ``os.replace`` is atomic on both Windows and POSIX, so an engine polling
    the file either sees the whole old config or the whole new one -- never a
    half-written JSON, which would make the engine fall back to defaults and
    silently change behaviour.

    Returns ``(ok, messages)``. Never raises: a dashboard write failing must
    report, not crash the page.
    """
    from blue_engine.dashboard.pages import _data

    msgs: List[str] = []
    clean: Dict[str, Any] = {}
    for key, value in (updates or {}).items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            continue  # ignore keys this UI does not own
        coerced, warn = clamp(spec, value)
        if warn:
            msgs.append(warn)
        clean[key] = coerced

    if not clean:
        return True, msgs

    path = Path(_data.config_path())
    try:
        existing: Dict[str, Any] = {}
        if path.is_file():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError) as exc:
                msgs.append(f"existing config unreadable ({exc}); writing fresh")

        block, nested = _target_block(existing)
        block.update(clean)
        if nested:
            existing["pov"] = block

        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" so we control line endings; an engine on any OS can read it.
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(existing, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_name, path)
        except Exception:
            # Clean up the temp file if the replace never happened.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        where = "pov block" if nested else "top level"
        msgs.append(f"Saved {len(clean)} parameter(s) to {path.name} ({where}).")
        return True, msgs
    except Exception as exc:  # noqa: BLE001 - report, never crash the page
        logger.warning("config write failed: %s", exc)
        msgs.append(f"WRITE FAILED: {exc}")
        return False, msgs


# ---------------------------------------------------------------------------
# The control widgets
# ---------------------------------------------------------------------------


def control(spec: ControlSpec, value: Any) -> Any:
    """One editable control, with its label, tooltip and unit."""
    cid = {"type": "cfg-input", "key": spec.key}

    if spec.kind == "switch":
        widget: Any = dmc.Switch(
            id=cid, checked=bool(value), size="sm", label=spec.label,
        )
    elif spec.kind == "select":
        widget = dmc.Select(
            id=cid, data=spec.options, value=value, size="sm",
            label=spec.label, searchable=False, allowDeselect=False,
        )
    else:
        widget = dmc.NumberInput(
            id=cid,
            value=value,
            label=spec.label,
            size="sm",
            min=spec.minimum,
            max=spec.maximum,
            step=spec.step,
            suffix=(" " + spec.unit) if spec.unit else None,
            clampBehavior="strict",   # mirrors write_values() clamping in the UI
            hideControls=False,
            allowDecimal=(spec.kind == "number"),
        )

    if spec.help:
        return dmc.Tooltip(
            label=spec.help, position="top-start", multiline=True,
            w=280, withArrow=True, openDelay=400,
            children=widget,
        )
    return widget


def controls_for_group(group: str, values: Optional[Dict[str, Any]] = None,
                       columns: int = 3) -> Any:
    """All controls in one group, laid out in a responsive grid."""
    vals = values if values is not None else current_values()
    specs = [s for s in CONTROL_SPECS if s.group == group]
    if not specs:
        return html.Div("No controls in this group.", className="empty")
    return dmc.SimpleGrid(
        cols={"base": 1, "sm": 2, "lg": columns},
        spacing="md",
        children=[control(s, vals.get(s.key)) for s in specs],
    )


def save_button() -> dmc.Group:
    """The explicit save row.

    Deliberately NOT auto-save-on-change: these are trading parameters, and a
    stray arrow-key in a number field must not silently change the risk budget.
    The operator commits on purpose.
    """
    return dmc.Group(
        [
            dmc.Button("Apply to config", id="cfg-apply", size="sm",
                       leftSection=DashIconify(icon="mdi:content-save-outline",
                                               width=16)),
            dmc.Button("Revert to file", id="cfg-revert", size="sm",
                       variant="default",
                       leftSection=DashIconify(icon="mdi:undo-variant", width=16)),
            dmc.Text("Changes are written to blue_engine/input/master.json. "
                     "Orders are never placed from this dashboard.",
                     size="xs", c="dimmed"),
        ],
        gap="sm",
        align="center",
    )


__all__ = [
    "CONTROL_SPECS",
    "ControlSpec",
    "GROUP_ICONS",
    "GROUP_TITLES",
    "SPEC_BY_KEY",
    "clamp",
    "control",
    "controls_for_group",
    "current_values",
    "save_button",
    "write_values",
]
