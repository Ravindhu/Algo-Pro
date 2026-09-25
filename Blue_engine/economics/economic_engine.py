"""
economic_engine.py - the Economic engine (orchestrator).

Answers one question for the rest of the stack: *what is the macro regime, and
how should it tilt risk?* It aggregates macro signals into a single ``risk_on``
score in [-1, +1], a categorical regime label, a **posterior probability** when
a regime model is available, and a ``gross_exposure_multiplier`` consumed by the
Portfolio engine.

Signals (all optional; each contributes only when data is present):

    gdp_growth        higher growth            -> risk-on
    bond_return       higher yields            -> mixed (see note)
    debt              higher debt/gdp          -> risk-off
    employment        lower unemployment       -> risk-on
    inflation         higher CPI               -> risk-off (rate risk)
    climate           climate-stress index     -> risk-off (tail risk)
    tax               higher effective tax      -> risk-off (earnings drag)
    currency          depreciating local FX    -> mixed (see note)
    forex_reserves    rising reserves          -> risk-on (external stability)
    news_tone         positive news/RBI tone   -> risk-on (optional)

What changed from the baseline
------------------------------
The baseline scored each signal as ``tanh(level / scale)`` blended with hand-set
weights (weaknesses #1-#5 in ``ECONOMIC_ENGINE_PLAN.md``). This version keeps
that as the **level** path but adds, all optional and additive:

  * **surprise** view  - print vs expectation/AR(1)   (``features.surprise``)
  * **delevel** view   - z-score of change vs trend   (``features.delevel``)
  * **HMM posterior**  - P(RISK_ON) from a sticky HMM  (``models.hmm_regime``)
  * **NLP tone**       - central-bank/headline tone    (``nlp.*``)
  * probability-aware ``gross_exposure_multiplier``    (``regime.py``)

Every addition degrades to the baseline when its data or dependency is absent,
so behaviour is *identical* to before until the desk opts in via config.

Every input is expected as a ``{"value": ..., "previous": ...,
"unit": "pct"|"index"|...}`` dict, or a bare number. Each signal is normalised
to a signed score using a direction (+1 risk-on / -1 risk-off), then combined
with configurable weights.

This module is PURE: no network, no SDK, no DB. Feed it via the ``provider``
callable or pass a ``macro`` block in the context / config.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..core.base import BaseEngine, EngineResult, EngineStage
from ..core.results import clamp, safe_float
from . import regime as regime_mod
from .features import delevel as delevel_mod
from .features import surprise as surprise_mod
from .nlp import event_calendar, sentiment
from .scoring import DEFAULT_BLEND, score_signal


# ---------------------------------------------------------------------------
# Signal specification: direction + weight defaults
# ---------------------------------------------------------------------------
#  direction = +1 -> a HIGHER value is risk-ON.
#  direction = -1 -> a HIGHER value is risk-OFF.
#  For FX, "higher" means the USD/INR rate rises (local currency weakens);
#  we treat a *rising* rate as mildly risk-off for equities via imported
#  inflation, but it also helps exporters, so its weight is deliberately small.
DEFAULT_SIGNALS: Dict[str, Dict[str, Any]] = {
    "gdp_growth":     {"direction": +1, "weight": 1.5, "label": "GDP growth"},
    "bond_return":    {"direction": -1, "weight": 1.0, "label": "Bond yield"},
    "debt":           {"direction": -1, "weight": 1.0, "label": "Debt / GDP"},
    "employment":     {"direction": -1, "weight": 1.2, "label": "Unemployment"},
    "inflation":      {"direction": -1, "weight": 1.5, "label": "Inflation (CPI)"},
    "climate":        {"direction": -1, "weight": 0.5, "label": "Climate stress"},
    "tax":            {"direction": -1, "weight": 0.8, "label": "Effective tax"},
    "currency":       {"direction": -1, "weight": 0.5, "label": "USD/local FX"},
    "forex_reserves": {"direction": +1, "weight": 1.0, "label": "FX reserves"},
    # Optional, only scored when the data is present:
    "news_tone":      {"direction": +1, "weight": 0.5, "label": "News / RBI tone"},
}

# Regime cut-points now live in regime.py; re-exported here for backwards
# compatibility with anything importing REGIME_BANDS from this module.
REGIME_BANDS = regime_mod.REGIME_BANDS


def _extract_value(raw: Any) -> Optional[float]:
    """Read a numeric value from a number or a {'value': ...} block."""
    if isinstance(raw, dict):
        return safe_float(raw.get("value", raw.get("latest")))
    return safe_float(raw)


def _extract_previous(raw: Any) -> Optional[float]:
    if isinstance(raw, dict):
        return safe_float(raw.get("previous", raw.get("prior")))
    return None


class EconomicEngine(BaseEngine):
    """Aggregate macro signals into a single risk-on score + regime label."""

    name = "economic"
    stage = EngineStage.ECONOMIC

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 **kwargs: Any):
        super().__init__(config, provider, kwargs.get("log"))

        # Per-signal overrides (direction / weight) from config.
        self.signals: Dict[str, Dict[str, Any]] = {
            key: dict(spec) for key, spec in DEFAULT_SIGNALS.items()
        }
        for key, override in (self.config.get("signals") or {}).items():
            if key in self.signals and isinstance(override, dict):
                self.signals[key].update(override)

        # Feature/model toggles. All default to the *baseline* behaviour so an
        # unmodified config reproduces the original engine exactly.
        self.use_surprise: bool = bool(self.config.get("use_surprise", True))
        self.use_delevel: bool = bool(self.config.get("use_delevel", True))
        self.blend: Dict[str, float] = {
            **DEFAULT_BLEND, **(self.config.get("blend") or {})}

        # Regime model (HMM/cluster). Off by default; opt in with regime_model.
        self.regime_model: str = str(self.config.get("regime_model", "none"))
        self.model_weight: float = float(self.config.get("model_weight", 0.0))

        # NLP: always-on lexical tone; event calendar gives an uncertainty trim.
        self.use_nlp: bool = bool(self.config.get("use_nlp", True))
        self.event_trim: float = float(self.config.get("event_trim", 0.0))

        # Persist each run to data/economic_runs/ for the dashboard history.
        self.persist: bool = bool(self.config.get("persist", False))

    # ---- data acquisition ---------------------------------------------- #
    def _collect_macro(self, context: Dict[str, Any]) -> Dict[str, Any]:
        # Priority: context["macro"] > provider() > config["macro"].
        if isinstance(context.get("macro"), dict):
            return context["macro"]
        if callable(self.provider):
            try:
                fetched = self.provider()
                if isinstance(fetched, dict):
                    return fetched
            except Exception as exc:  # noqa: BLE001
                self.log(f"provider failed ({exc!r}); using config macro block")
        if isinstance(self.config.get("macro"), dict):
            return self.config["macro"]
        return {}

    # ---- NLP augmentation -------------------------------------------- #
    def _nlp_signals(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Attach a news_tone signal from text, if any is supplied.

        Text can arrive as ``context['headlines']`` (list) or
        ``context['rbi_text']`` (str). Absent text -> no signal (no warning;
        text is optional).
        """
        out: Dict[str, Any] = {}
        if not self.use_nlp:
            return out
        text = context.get("rbi_text")
        headlines = context.get("headlines") or []
        if text:
            tone = sentiment.score_text(str(text))
            out["news_tone"] = {"value": tone["score"], "source": "rbi_text"}
        elif headlines:
            tone = sentiment.score_headlines(list(headlines))
            out["news_tone"] = {"value": tone["score"],
                                "source": "headlines"}
        return out

    # ---- scoring (three-view blend) ----------------------------------- #
    def _score_signals(self, macro: Dict[str, Any],
                       warnings: List[str]):
        contributions: Dict[str, Dict[str, Any]] = {}
        weighted_sum = 0.0
        weight_total = 0.0

        # Precompute surprise / delevel views once for the whole block.
        z_deltas: Dict[str, Optional[float]] = {}
        z_surprises: Dict[str, Optional[float]] = {}
        surprise_methods: Dict[str, str] = {}
        if self.use_delevel:
            for k, v in delevel_mod.delevel_scores(macro).items():
                z_deltas[k] = v.get("z")
        if self.use_surprise:
            for k, v in surprise_mod.surprise_scores(macro).items():
                z_surprises[k] = v.get("z")
                surprise_methods[k] = v.get("method", "none")

        for key, spec in self.signals.items():
            raw = macro.get(key)
            value = _extract_value(raw)
            if value is None:
                warnings.append(f"signal '{key}' missing")
                continue

            direction = float(spec.get("direction", 0))
            weight = float(spec.get("weight", 0))
            scale = safe_float(spec.get("scale")) or self._default_scale(key)

            # Guard against double-counting: when surprise has only the
            # 'previous' reading to work from (method 'change'), its z is the
            # SAME number as the delevel view. Feeding both into the blend would
            # weight the change twice. We keep the surprise *value* for display
            # but exclude it from the score in that case.
            z_surprise_for_score = z_surprises.get(key)
            if surprise_methods.get(key) == "change":
                z_surprise_for_score = None

            # Blend level / delevel / surprise views (each saturating so an
            # extreme print cannot dominate the composite).
            scored = score_signal(
                direction=direction, level=value, scale=scale,
                z_delta=z_deltas.get(key),
                z_surprise=z_surprise_for_score,
                blend=spec.get("blend") or self.blend,
            )
            signed = scored["score"]
            views = dict(scored["views"])
            # For display: show the surprise view even when it was excluded from
            # scoring (method 'change' duplicating delevel), so the dashboard
            # column is never blank. We do NOT re-add it to the blend.
            if surprise_methods.get(key) == "change" and "surprise" not in views:
                z = z_surprises.get(key)
                if z is not None:
                    from .scoring import surprise_view
                    sv = surprise_view(z)
                    if sv is not None:
                        views["surprise"] = round(direction * sv, 4)

            contributions[key] = {
                "label": spec.get("label", key),
                "value": value,
                "previous": _extract_previous(raw),
                "direction": direction,
                "weight": weight,
                "score": signed,
                "views": views,
                "views_used": scored["views_used"],
                "surprise_method": surprise_methods.get(key, "none"),
            }
            weighted_sum += signed * weight
            weight_total += abs(weight)

        composite = (weighted_sum / weight_total) if weight_total > 0 else 0.0
        return round(clamp(composite, -1.0, 1.0), 4), contributions

    @staticmethod
    def _default_scale(key: str) -> float:
        # Sensible unit scales so a single reading lands mid-band.
        return {
            "gdp_growth": 5.0, "bond_return": 7.0, "debt": 90.0,
            "employment": 8.0, "inflation": 6.0, "climate": 100.0,
            "tax": 30.0, "currency": 85.0, "forex_reserves": 700.0,
            "news_tone": 1.0,
        }.get(key, 10.0)

    @staticmethod
    def _regime(score: float):
        # Kept for backwards compatibility; delegates to regime.band().
        return regime_mod.band(score)

    # ---- optional regime model (HMM / cluster) ------------------------ #
    def _regime_model(self, context: Dict[str, Any],
                      composite: float) -> Dict[str, Any]:
        """Run the configured regime model over the composite history.

        History is ``context['economic_history']`` (time-ordered composite
        scores). If short, the current score is appended so a single-sample
        history yields 'insufficient_data' rather than an error.
        """
        if self.regime_model in ("none", ""):
            return {"method": "none", "p_risk_on": None, "label": None}
        history = list(context.get("economic_history") or [])
        history.append(composite)
        try:
            if self.regime_model == "hmm":
                from .models import hmm_regime
                return hmm_regime.detect_regime(history)
            if self.regime_model == "cluster":
                from .models import cluster
                return cluster.cluster_states([[v] for v in history],
                                              score_column=0)
        except Exception as exc:  # noqa: BLE001 - a model must never halt us
            self.log(f"regime model '{self.regime_model}' failed: {exc!r}")
            return {"method": "error", "error": repr(exc),
                    "p_risk_on": None, "label": None}
        return {"method": "unknown", "p_risk_on": None, "label": None}

    # ---- main ---------------------------------------------------------- #
    def _run(self, context: Dict[str, Any]) -> EngineResult:
        warnings: List[str] = []
        macro = dict(self._collect_macro(context) or {})

        # Merge any NLP-derived signals into the macro block (non-destructive).
        for k, v in self._nlp_signals(context).items():
            macro.setdefault(k, v)

        if not macro:
            return EngineResult(
                engine=self.name, ok=True,
                data={"regime": "UNKNOWN", "risk_on_score": 0.0,
                      "signals": {}, "gross_exposure_multiplier": 1.0},
                warnings=["no macro data supplied; neutral by default"],
            )

        composite, contributions = self._score_signals(macro, warnings)
        regime, guidance = regime_mod.band(composite)

        # Optional sticky regime posterior (off unless regime_model is set).
        model = self._regime_model(context, composite)
        p_risk_on = model.get("p_risk_on")

        # Gross-exposure multiplier: score path, optionally blended with the
        # model posterior via ``model_weight`` (default 0 -> identical to
        # baseline). Never zero and never above the cap.
        mult_info = regime_mod.combine_multiplier(
            composite, p_risk_on, model_weight=self.model_weight)
        multiplier = mult_info["multiplier"]

        # Optional event-risk trim: scale gross down near known events (this is
        # *uncertainty*, not direction). Off unless event_trim > 0.
        event_info = None
        if self.event_trim > 0:
            event_info = event_calendar.upcoming_events(
                context.get("as_of"),
                within_days=int(self.config.get("event_window_days", 10)),
                fixed_events=self.config.get("fixed_events"),
                mpc_dates=self.config.get("mpc_dates"))
            trim = self.event_trim * float(event_info.get("event_risk", 0.0))
            multiplier = clamp(multiplier * (1.0 - trim),
                               regime_mod.MULTIPLIER_FLOOR,
                               regime_mod.MULTIPLIER_CAP)

        # Provenance from the provider stack, when present.
        meta = macro.get("meta") or {}

        self.log(f"regime={regime} risk_on_score={composite:+.2f} "
                 f"gross_mult={multiplier:.2f} "
                 f"p_risk_on={p_risk_on} model={model.get('method')}")

        result = EngineResult(
            engine=self.name, ok=True,
            data={
                "regime": regime,
                "guidance": guidance,
                "risk_on_score": composite,
                "gross_exposure_multiplier": round(multiplier, 4),
                "multiplier_detail": mult_info,
                "regime_model": model,
                "signals": contributions,
                "signals_available": len(contributions),
                "nlp": self._nlp_signals(context) or None,
                "event": event_info,
                "source_meta": meta or None,
            },
            warnings=warnings + list(meta.get("warnings") or []),
        )

        # Persistence hook. The engine core stays pure; when ``persist`` is on
        # we write the run to the JSONL history the dashboard reads. Off by
        # default so a raw engine call has zero side effects.
        if getattr(self, "persist", False):
            try:
                from . import run_history
                run_history.record(result.data)
            except Exception as exc:  # noqa: BLE001 - never abort a run
                self.log(f"run history persist failed: {exc!r}")
        return result


__all__ = ["EconomicEngine", "DEFAULT_SIGNALS", "REGIME_BANDS"]