"""
stage_skills.py - one SMALL skill per pipeline stage, with ZERO advisory caps.

WHY THIS FILE EXISTS
====================
``core/frontline_agent.py`` is one generic agent keyed by a string. The operator
asked for the opposite shape:

    "implement skill for small per-stage skills without any cap"
    "only data satisfied logic code move to next stage"
    "it should not edit code base only json entry"

So this module gives every stage its own *skill*: an id, the exact output shape
the stage accepts, the evidence it needs to be runnable, a purpose-built prompt,
and the JSON sandbox filename its answer is published to.

WHAT "NO CAP" MEANS HERE - AND WHAT IT DOES NOT MEAN
---------------------------------------------------
A *skill* here carries **no caps on judgement**. Inside its published schema a
skill may:

  * veto for any reason (``risk``),
  * blacklist any ticker for any reason (``screener``),
  * ask for MORE data — the ``data_requests`` block lets it name the exact
    evidence it is missing instead of guessing,
  * set the regime label, the exploration bounds, the valuation ceiling.

What a skill may **never** do is decide *magnitudes that protect capital*. That
clamp is not part of a skill at all — it lives in ``frontline_agent._CAPS`` /
``_escalate`` and in ``RiskEngine`` / ``RiskGate`` / ``pov``. Those modules do
not import this one, so a skill cannot widen what the deterministic rail allows.
The distinction matters: removing the clamp would let a prompt-injected
headline clear a live risk veto, which violates the project's own
"Capital Protection -> ... -> Strategy Performance" hierarchy.

WHAT A SKILL IS MADE OF
-----------------------
====================  =======================================================
field                 meaning
====================  =======================================================
``stage``             pipeline stage name (keys match ``EngineStage`` names)
``skill_id``          ``frontline.<stage>`` - the id used in logs/audit
``schema``            the output contract (``_SCHEMAS``-compatible tuples)
``data_needs``        evidence the stage needs; also the stage-gate defaults
``purpose``           one sentence for the prompt and the SKILL.md
``filename``          JSON file inside the write sandbox
``writes``            always ``"json_only"`` - code writes are impossible
``failless``          True: a skill failure never blocks the logic path
====================  =======================================================

USAGE
-----
::

    from blue_engine.core.stage_skills import load_skills, skills_document

    skills = load_skills(config)                 # config-driven
    skill = skills["risk"]
    skill.schema                                 # {key: (kind, ...)}
    skill.describe()                             # JSON-safe

    doc = skills_document()                      # for the dashboard/AI header

Resolution order for one stage: an explicit ``engines.<stage>.skill`` block in
the config wins; otherwise the built-in default below applies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

__all__ = [
    "AGENT_STAGES",
    "PIPELINE_SKILL_STAGES",
    "DATA_REQUESTS_SCHEMA_KEYS",
    "StageSkill",
    "DEFAULT_SKILLS",
    "load_skills",
    "skills_document",
]

#: Every stage a skill may be attached to, in pipeline guidance order. This is
#: the same list ``core/pipeline.py`` iterates for advice, plus ``sector`` and
#: ``alpha`` which have their own engines.
AGENT_STAGES: Tuple[str, ...] = (
    "economic", "market", "screener", "valuation",
    "sector", "portfolio", "alpha", "backtest", "optimization", "risk",
)

#: Stages the prompt-model agent (``frontline_agent._SCHEMAS``) currently keys.
PIPELINE_SKILL_STAGES: Tuple[str, ...] = (
    "economic", "market", "screener", "valuation",
    "backtest", "optimization", "portfolio", "risk",
)

#: Optional keys a skill answer may carry to request more evidence. These are
#: NOT part of the stage's decision schema and never reach the engine: the
#: pipeline folds them into ``report["data_requests"]`` so the data layer (and
#: the dashboard) can supply them on the next cycle. This is the mechanism
#: behind "agentic ai should help logic code to get enough data".
DATA_REQUESTS_SCHEMA_KEYS: Dict[str, Any] = {
    "needs": "array",
    "reason": "text",
}


@dataclass(frozen=True)
class StageSkill:
    """One small, independent stage skill. Immutable by design."""

    stage: str
    skill_id: str
    purpose: str
    schema: Dict[str, Any]
    data_needs: Tuple[Dict[str, Any], ...] = ()
    filename: str = ""
    writes: str = "json_only"
    failless: bool = True
    source: str = "default"

    # ---- introspection -------------------------------------------------- #
    def schema_keys(self) -> List[str]:
        return list(self.schema.keys())

    def accepts_data_requests(self) -> bool:
        return True
    def answer_schema(self, data_requests: bool = True) -> Dict[str, Any]:
        """The schema a model answer must satisfy.

        The skill's own keys are EXACT (no more, no less) plus one OPTIONAL,
        append-only ``data_requests`` block. Optional is the important part:
        an answer that names no missing evidence is complete, so a healthy
        skill never needs a second round-trip just to say "nothing missing".
        The block has its own validator (``_validate_data_requests`` in
        ``frontline_agent``) because it is not part of the stage's decision
        schema and never reaches an engine.
        """
        out = dict(self.schema)
        if data_requests:
            out["data_requests"] = ("data_requests",)
        return out

    def describe(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "skill_id": self.skill_id,
            "purpose": self.purpose,
            "filename": self.filename or f"{self.stage}.json",
            "writes": self.writes,
            "failless": self.failless,
            "source": self.source,
            "schema": {k: list(v) if isinstance(v, (tuple, list)) else v
                       for k, v in self.schema.items()},
            "data_needs": [dict(d) for d in self.data_needs],
            "accepts_data_requests": True,
        }

    # ---- prompt --------------------------------------------------------- #
    def instruction(self) -> Dict[str, Any]:
        """The exact contract handed to the model. Mirrors ``_instruction``.

        Kept here (not in ``frontline_agent``) so the skill - not the transport -
        owns its prompt. The agent validates the answer against ``schema`` and
        never invents a key that is absent from it.
        """
        properties: Dict[str, Any] = {}
        for key, rule in self.schema.items():
            kind = rule[0]
            if kind == "enum":
                properties[key] = {"enum": list(rule[1])}
            elif kind == "bool":
                properties[key] = {"type": "boolean"}
            elif kind in ("number", "integer"):
                properties[key] = {"type": "number" if kind == "number" else "integer",
                                   "minimum": rule[1], "maximum": rule[2]}
            elif kind == "text":
                properties[key] = {"type": "string", "max_words": rule[1]}
            elif kind in ("ticker", "date"):
                properties[key] = {"type": "string"}
            elif kind in ("tickers", "reasons"):
                properties[key] = {"type": "array"}
            else:  # unknown kind - surfaced honestly rather than guessed
                properties[key] = {"type": "unknown", "kind": kind}
        return {
            "role": f"FRONTLINE {self.stage.upper()} STAGE SKILL",
            "purpose": self.purpose,
            "must_return_json_with_exactly": sorted(properties),
            "properties": properties,
            "optional_data_requests": {
                "needs": "list of evidence you are missing",
                "reason": "why you need it",
            },
            "rules": [
                "Answer with DATA only. You cannot run, edit or write code.",
                "If evidence is missing, say so via data_requests - never guess.",
                "Your answer is an INPUT to deterministic logic; it is not a trade.",
            ],
        }


# --------------------------------------------------------------------------- #
# Built-in per-stage skills (zero advisory caps)
# --------------------------------------------------------------------------- #
def _skill(stage: str, purpose: str, schema: Dict[str, Any],
           data_needs: Iterable[Dict[str, Any]] = ()) -> StageSkill:
    return StageSkill(
        stage=stage,
        skill_id=f"frontline.{stage}",
        purpose=purpose,
        schema=dict(schema),
        data_needs=tuple(dict(d) for d in data_needs),
        filename=f"{stage}.json",
        source="default",
    )


DEFAULT_SKILLS: Dict[str, StageSkill] = {
    "economic": _skill(
        "economic",
        "Read policy, inflation and growth context into a macro regime that "
        "scales the equity allocation.",
        {
            "macro_regime": ("enum", ("Stagflationary", "Expansionary",
                                      "Recessionary", "Normal")),
            "equity_regime_multiplier": ("number", 0.25, 1.5),
            "interest_rate_bias": ("enum", ("Hawkish", "Dovish", "Neutral")),
            "rationale": ("text", 15),
        },
        data_needs=(
            {"path": "macro", "check": "present",
             "note": "policy rate, CPI, growth inputs"},
            {"path": "macro.policy_rate", "check": "range",
             "min_value": -5.0, "max_value": 30.0,
             "require_freshness": True,
             "note": "RBI repo rate must be present and fresh"},
        ),
    ),
    "market": _skill(
        "market",
        "Classify the traded market's volatility state and the lookback the "
        "technical layer should use.",
        {
            "market_volatility_state": ("enum", ("Low", "Elevated", "High_Panic")),
            "technical_lookback_days": ("enum", (10, 14, 20, 50)),
            "short_term_bias": ("enum", ("Bullish", "Bearish", "Sideways")),
            "rationale": ("text", 15),
        },
        data_needs=(
            {"path": "market", "check": "present",
             "note": "index quote and volatility inputs"},
        ),
    ),
    "screener": _skill(
        "screener",
        "Exclude symbols that must not be traded today, with a stated reason "
        "for every exclusion.",
        {
            "asset_blacklist": ("tickers",),
            "blacklist_reasons": ("reasons", 10),
        },
        data_needs=(),
    ),
    "valuation": _skill(
        "valuation",
        "Bound the growth assumption and the margin safety haircut for one "
        "company; refuse to valuate when the financials are incomplete.",
        {
            "target_ticker": ("ticker",),
            "growth_rate_ceiling": ("number", 0.0, 0.05),
            "margin_safety_adjustment": ("number", 0.0, 1.0),
            "rationale": ("text", 15),
        },
        data_needs=(),
    ),
    "sector": _skill(
        "sector",
        "Rank sectors as INVEST_MORE or WAIT_AND_SEE from relative-strength "
        "matrices plus qualitative context.",
        {
            # The sector engine owns its own validator
            # (sectors/sector_engine.py::_validate_advice); the skill only
            # declares the shape so the dashboard can render it.
            "allocations": ("mapping",),
            "rationale": ("text", 40),
        },
        data_needs=(),
    ),
    "portfolio": _skill(
        "portfolio",
        "Translate the macro/regime picture into portfolio constraints: sector "
        "ceiling, cash buffer and whether rebalancing is appropriate.",
        {
            "max_tech_sector_exposure": ("number", 0.0, 1.0),
            "min_cash_buffer_pct": ("number", 0.0, 1.0),
            "rebalance_allowed": ("bool",),
            "rationale": ("text", 15),
        },
        data_needs=(
            {"path": "alpha.data.selected", "check": "present",
             "note": "at least one selected signal is required"},
        ),
    ),
    "alpha": _skill(
        "alpha",
        "Flag signals whose setup looks inconsistent with the current regime "
        "so the deterministic scoring can weight them.",
        {
            "regime_conflicts": ("tickers",),
            "rationale": ("text", 15),
        },
        data_needs=(),
    ),
    "backtest": _skill(
        "backtest",
        "Decide the historical window and the slippage assumption that makes a "
        "backtest honest; name the flaw when one is visible.",
        {
            "historical_start_date": ("date",),
            "slippage_penalty_pct": ("number", 0.0, 0.05),
            "flaw_detected": ("text", 15),
        },
        data_needs=(
            {"path": "backtest.data.periods", "check": "min_count", "value": 1,
             "note": "a backtest needs at least one completed period"},
        ),
    ),
    "optimization": _skill(
        "optimization",
        "Bound the search space for the optimiser and pick the permitted "
        "search method.",
        {
            "min_exploration_rsi": ("integer", 0, 100),
            "max_exploration_rsi": ("integer", 0, 100),
            "optimization_method": ("enum", ("bounded_scipy", "genetic_restricted")),
            "rationale": ("text", 15),
        },
        data_needs=(),
    ),
    "risk": _skill(
        "risk",
        "The last advisory step before execution. May veto on any grounds and "
        "may only ever make the picture more cautious.",
        {
            "risk_veto_active": ("bool",),
            "max_allowed_slippage_pct": ("number", 0.0, 0.05),
            "force_trailing_stop_pct": ("number", 0.1, 20.0),
            "veto_reason": ("text", 15),
        },
        data_needs=(
            {"path": "portfolio.data", "check": "present",
             "note": "risk sizing needs the portfolio engine's output"},
        ),
    ),
}


# --------------------------------------------------------------------------- #
# Loading / overriding
# --------------------------------------------------------------------------- #
def _coerce_data_needs(raw: Any, where: str) -> Tuple[Dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{where}.data_needs must be a list")
    out: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or not entry.get("path"):
            raise ValueError(f"{where}.data_needs entries need a 'path'")
        out.append(dict(entry))
    return tuple(out)


def _build_skill(stage: str, block: Mapping[str, Any],
                 base: Optional[StageSkill]) -> StageSkill:
    """Merge a config ``skill`` block over the built-in for ``stage``."""
    where = f"engines.{stage}.skill"
    if not isinstance(block, Mapping):
        raise ValueError(f"{where} must be a mapping")

    default = base or StageSkill(
        stage=stage, skill_id=f"frontline.{stage}",
        purpose=str(block.get("purpose") or f"{stage} stage skill"),
        schema={}, filename=f"{stage}.json", source="config")

    if "enabled" in block and not bool(block["enabled"]):
        # A disabled skill still exists - it just reports itself as such - so a
        # caller never has to special-case a missing key.
        return StageSkill(
            stage=stage, skill_id=default.skill_id,
            purpose=default.purpose, schema={},
            data_needs=(), filename=default.filename,
            writes="json_only", failless=default.failless, source="disabled")

    schema = block.get("schema")
    if schema is None:
        schema = default.schema
    if not isinstance(schema, Mapping):
        raise ValueError(f"{where}.schema must be a mapping")
    # Normalise so ``_validate`` in frontline_agent can consume it unchanged:
    # a string kind becomes a 1-tuple, a list becomes a tuple.
    clean: Dict[str, Any] = {}
    for key, rule in schema.items():
        if isinstance(rule, str):
            clean[str(key)] = (rule,)
        elif isinstance(rule, (list, tuple)):
            clean[str(key)] = tuple(rule)
        else:
            raise ValueError(f"{where}.schema[{key!r}] must be a list or string")

    needs = (_coerce_data_needs(block["data_needs"], where)
             if "data_needs" in block else default.data_needs)
    filename = str(block.get("filename") or default.filename or f"{stage}.json")
    if not filename.lower().endswith(".json"):
        raise ValueError(f"{where}.filename must end in .json")

    return StageSkill(
        stage=stage,
        skill_id=str(block.get("skill_id") or default.skill_id),
        purpose=str(block.get("purpose") or default.purpose),
        schema=clean,
        data_needs=needs,
        filename=filename,
        writes="json_only",
        failless=bool(block.get("failless", default.failless)),
        source="config" if base is None else "config_override",
    )


def load_skills(config: Optional[Mapping[str, Any]] = None,
                *, stages: Optional[Iterable[str]] = None,
                include_disabled: bool = True) -> Dict[str, StageSkill]:
    """Return ``{stage: StageSkill}``, config overriding the built-ins.

    ``config`` is the whole pipeline config; the ``skill`` block is read from
    ``engines.<stage>.skill``. Unknown stages in config are ignored (they are
    not pipeline stages), and a stage with no config block keeps its default.
    """
    engines = (config or {}).get("engines") if isinstance(config, Mapping) else None
    engines = engines if isinstance(engines, Mapping) else {}
    wanted = [str(s).strip().lower() for s in (stages or AGENT_STAGES)]
    out: Dict[str, StageSkill] = {}
    for stage in wanted:
        base = DEFAULT_SKILLS.get(stage)
        stage_block = engines.get(stage)
        block = (stage_block.get("skill")
                 if isinstance(stage_block, Mapping) else None)
        skill = _build_skill(stage, block, base) if block else base
        if skill is None:
            continue
        if skill.source == "disabled" and not include_disabled:
            continue
        out[stage] = skill
    return out


def skills_document(skills: Optional[Mapping[str, StageSkill]] = None
                    ) -> Dict[str, Any]:
    """JSON-safe snapshot for the dashboard / AI Insight header / audit."""
    chosen = dict(skills or DEFAULT_SKILLS)
    return {
        "count": len(chosen),
        "stages": [chosen[k].describe() for k in sorted(chosen)],
        "writes": "json_only",
        "caps": "none in the skill; capital clamps live in risk_engine/risk_gate"
                "/pov and in frontline_agent._CAPS",
    }
