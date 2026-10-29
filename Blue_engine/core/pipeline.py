"""
pipeline.py - the master pipeline that chains the analytic and safety engines.

    Economic -> Market -> (Screener -> Valuation) -> Portfolio -> Alpha
       -> Risk -> POV -> Infrastructure

Each stage reads the outputs of the stages before it. The pipeline never
raises: a stage failure is recorded in that stage's :class:`EngineResult` and
(unless it is a hard gate) the pipeline continues with what it has.

Safety model
------------
* ``dry_run`` (default True) runs every analytic engine and builds the POV
  config, but does NOT submit orders. Live execution requires an explicit
  ``dry_run=False`` AND an OpenAlgo SDK client in the context.
* The Infrastructure engine's ``ready_to_trade`` can be configured to veto the
  POV stage (``gate_on_infrastructure=True``).
* No engine imports OpenAlgo internals. The SDK client is injected by the
  caller as ``context['client']``.

Usage::

    from blue_engine.core.pipeline import MacroPipeline
    pipe = MacroPipeline.from_config({"dry_run": True, ...})
    report = pipe.run(context={"macro": {...}, "market": {...}, ...})
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..alpha import AlphaEngine
from .base import EngineResult, EngineStage
from ..economics import EconomicEngine
from ..infrastructure import InfrastructureEngine
from ..market import MarketEngine
from ..portfolio import PortfolioEngine
from ..pov import PovEngine
from ..screener import ScreenerEngine
from ..valuation_engine import ValuationEngine
from ..sectors import SectorEngine
from .agent_memory import AgentMemory
from .agent_team import AgentTeam
from .data_tools import DataToolbox
from .frontline_agent import FrontlineAgent
from .risk_engine import RiskEngine
from .self_correction import SelfCorrectionLoop
from .skill_writer import SkillWriter
from .stage_gate import StageGate
from .stage_skills import AGENT_STAGES, load_skills

logger = logging.getLogger(__name__)

# Default config lives under blue_engine/input/ (sibling of this core package).
DEFAULT_CONFIG_PATH = (Path(__file__).resolve().parent.parent
                       / "input" / "macro_master.json")


class MacroPipeline:
    """Ordered analytic chain with optional fundamental stages and risk veto."""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.config: Dict[str, Any] = dict(config or {})
        self._log_fn = log
        self.dry_run: bool = bool(self.config.get("dry_run", True))
        self.gate_on_infrastructure: bool = bool(
            self.config.get("gate_on_infrastructure", True))

        engines_cfg = self.config.get("engines") or {}
        self.frontline_agent = FrontlineAgent(engines_cfg.get("agent", {}))
        # ---- agentic frontline layer (data/gate/JSON only, never execution) -- #
        # Skills describe WHAT each stage may conclude; the gate decides whether
        # there is enough evidence to advance; the writer publishes the answer
        # as JSON. None of the three can place, size or cancel an order.
        self.skills = load_skills(self.config, stages=AGENT_STAGES)
        self.stage_gate = StageGate.from_config(self.config)
        agent_io = engines_cfg.get("agent_io") or {}
        self.skill_writer = SkillWriter(
            agent_io.get("sandbox"), audit=bool(agent_io.get("audit", True)))
        #: Self-correction / toolbelt config. The toolbox itself is built AFTER
        #: the engines exist (it reuses their injected providers), below.
        agent_cfg = engines_cfg.get("agent") or {}
        self._tools_cfg = dict(agent_cfg.get("tools") or {})
        #: The self-correction loop config: attempts, and whether tools are on.
        self.self_correction = dict(agent_cfg.get("self_correction") or {})
        self.tools_enabled = bool(self.self_correction.get("tools", True))
        #: A stopped analytic chain still runs risk + infrastructure + POV-block
        #: logic so the operator sees a complete, honestly-blocked report.
        self._post_gate_stages = ("risk", "infrastructure", "pov")
        self.engines = [
            EconomicEngine(engines_cfg.get("economic", {}),
                           provider=self._provider("economic"), log=log),
            MarketEngine(engines_cfg.get("market", {}),
                         provider=self._provider("market"), log=log),
        ]
        sector_cfg = engines_cfg.get("sector") or {}
        if bool(sector_cfg.get("pipeline_enabled", False)):
            self.engines.append(SectorEngine(
                sector_cfg, provider=self._provider("sector"), log=log))
        screener_cfg = engines_cfg.get("screener") or {}
        if bool(screener_cfg.get("pipeline_enabled", False)):
            self.engines.append(ScreenerEngine(
                screener_cfg, provider=self._provider("screener"), log=log))
        valuation_cfg = engines_cfg.get("valuation") or {}
        if bool(valuation_cfg.get("pipeline_enabled", False)):
            self.engines.append(ValuationEngine(
                valuation_cfg, provider=self._provider("valuation"), log=log))
        self.engines.extend([
            PortfolioEngine(engines_cfg.get("portfolio", {}),
                            provider=self._provider("portfolio"), log=log),
            AlphaEngine(engines_cfg.get("alpha", {}),
                        provider=self._provider("alpha"), log=log),
            RiskEngine(engines_cfg.get("risk", {}),
                       provider=self._provider("risk"), log=log),
            PovEngine(engines_cfg.get("pov", {}),
                      provider=self._provider("pov"), log=log),
            InfrastructureEngine(engines_cfg.get("infrastructure", {}),
                                 provider=self._provider("infrastructure"),
                                 log=log),
        ])
        # Deterministic order by stage.
        self.engines.sort(key=lambda e: e.stage.value)

        #: Read-only evidence the agent may fetch for itself: the SAME providers
        #: the engines were injected with (never new ones), plus JSON roots.
        self.data_toolbox = DataToolbox(
            read_roots=[*self._tools_cfg.get("read_roots", []), "."],
            providers={e.name: e.provider for e in self.engines if e.provider},
            catalog_paths=self._tools_cfg.get("catalog_paths") or [])
        #: The three memories (working / calibration / mistakes). JSON under
        #: the SAME sandbox as the skill writer, so an agent has one writable
        #: place. Disabled by config: memory is context, not a requirement.
        mem_cfg = dict(agent_cfg.get("memory") or {})
        self.memory_enabled = bool(mem_cfg.get("enabled", True))
        self.memory = (AgentMemory.load(self.skill_writer.sandbox)
                       if self.memory_enabled else None)
        #: Tracks which upstream results changed, for the working memory.
        self._last_evidence: Dict[str, Any] = {}
        #: The roster: which skill is posted at which station, in line order.
        #: Recording only - it reads the finished report, it never runs engines.
        self.agent_team = AgentTeam(self.skills)

    # ---- agentic advice -------------------------------------------------- #
    def _advise_with_correction(self, stage: str, text: str,
                                ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Run one stage skill through the bounded self-correction loop.

        Two independent things make an answer unacceptable:

        * the schema/validator rejected it, or
        * the DATA GATE says the stage still lacks evidence.

        Only those two produce a correction prompt. A clamped answer is a
        success - the safe rails already bound it, so re-asking would just
        spend a call.
        """
        loop = SelfCorrectionLoop(
            self.frontline_agent,
            max_attempts=int(self.self_correction.get("max_attempts", 2)),
            stage_gate=self.stage_gate if self.stage_gate.enabled else None,
            context=lambda: ctx, gate_stage=stage)
        tools = (self.data_toolbox.tools()
                 if (self.tools_enabled
                     and self.self_correction.get("enabled", True)) else None)
        memory_block = (self.memory.prompt_block(stage)
                        if self.memory is not None else "")
        outcome = loop.run(stage, text, tools=tools, memory_block=memory_block)
        if not outcome.get("ok") and self.self_correction.get("enabled", True):
            outcome["warning"] = (f"self-correction exhausted after "
                                 f"{len(outcome.get('attempts') or [])} attempt(s): "
                                 f"{outcome.get('method')}")
        self._learn_from_outcome(stage, outcome)
        return outcome

    # ---- memory ---------------------------------------------------------- #
    def _learn_from_outcome(self, stage: str, outcome: Dict[str, Any]) -> None:
        """Update all three memories from one finished station. Never raises.

        What is learned is deliberately narrow:

        * **mistake** - the deterministic judge's failure kind + detail, so the
          next prompt carries the exact error. Only a JUDGEMENT failure is
          recorded: ``llm_unavailable`` is an infrastructure fact, not a mistake
          the model made, and teaching a model to avoid it would be wrong.
        * **calibration** - the proposed and operative values, and whether the
          rails clamped them.
        * **working** - what this station produced and which evidence it holds.

        A memory failure must never affect the trade path, so everything is
        guarded and the error is recorded as a warning, not raised.
        """
        if self.memory is None:
            return
        try:
            kind = _warning_kind(outcome)
            ok = bool(outcome.get("ok"))
            if not ok and kind in ("schema_rejected", "other"):
                self.memory.mistakes.record(
                    stage, kind=outcome.get("failure_kind") or kind,
                    detail=outcome.get("failure_detail")
                    or [str(outcome.get("warning") or "")],
                    fix=self._fix_hint(outcome))
                self.memory.mark("mistakes")

            advice = outcome.get("advice") if ok else None
            if isinstance(advice, dict):
                clamped = bool(outcome.get("clamped")) or any(
                    "clamped" in str(w) for w in (outcome.get("warnings") or []))
                self.memory.calibration.observe(
                    stage, proposed=advice, operative=advice,
                    clamped=clamped, method=str(outcome.get("method") or ""))
                self.memory.mark("calibration")
                self.memory.working.record_station(
                    stage, produced=advice,
                    evidence=[str(r) for r in (advice.get("data_requests") or [])],
                    state="worked" if not clamped else "worked_clamped")

            self.memory.working.record_station(
                stage, produced=None if ok else {"failed": kind},
                state="worked" if ok else "failed")
            self.memory.mark("working")
        except Exception as exc:  # noqa: BLE001 - memory must never break a run
            logger.warning("agent memory update failed for %s: %r", stage, exc)

    @staticmethod
    def _fix_hint(outcome: Dict[str, Any]) -> str:
        """A one-line fix suggestion stored with the mistake, from real detail."""
        detail = outcome.get("failure_detail") or []
        if not detail:
            return ""
        first = str(detail[0])
        if "must be one of" in first or "must be between" in first:
            return "answer inside the stated allowed range"
        if "absent" in first or "unexpected keys" in first:
            return "return exactly the schema keys, nothing more"
        if "not a single JSON object" in first:
            return "reply with one JSON object only"
        return ""

    def save_memory(self) -> Dict[str, Any]:
        """Persist dirty memories. Safe to call at the end of any run."""
        if self.memory is None:
            return {}
        try:
            return self.memory.save()
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent memory save failed: %r", exc)
            return {"error": repr(exc)}
    def _stage_position(self, name: str) -> int:
        """Index of ``name`` in the deterministic engine order (-1 if absent).

        Used by the data gate so "halt at stage N" skips only stages N+1.. and
        never erases the stages that already produced usable evidence.
        """
        for index, engine in enumerate(self.engines):
            if engine.name == name:
                return index
        return -1
    # ---- provider resolution ------------------------------------------ #
    def _provider(self, key: str) -> Optional[Callable[[], Dict[str, Any]]]:
        """Resolve a provider callable from config.

        Config supplies dicts inline; a *callable* provider would be injected
        programmatically. We support ``{"provider": "module:function"}`` for
        a dotted path so a user can wire a live data adapter without editing
        engine code.
        """
        engines_cfg = self.config.get("engines") or {}
        block = engines_cfg.get(key) or {}
        ref = block.get("provider_ref")
        if not ref or ":" not in ref:
            return None
        module_name, func_name = ref.split(":", 1)
        try:
            import importlib
            module = importlib.import_module(module_name)
            return getattr(module, func_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not resolve provider_ref %s: %s", ref, exc)
            return None

    # ---- constructors -------------------------------------------------- #
    @classmethod
    def from_config(cls, config: Dict[str, Any],
                    log: Optional[Callable[[str], None]] = None) -> "MacroPipeline":
        return cls(config=config, log=log)

    @classmethod
    def from_config_file(cls, path: Optional[str] = None,
                         log: Optional[Callable[[str], None]] = None) -> "MacroPipeline":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        if not p.is_file():
            raise FileNotFoundError(f"macro config not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            return cls(config=json.load(fh), log=log)

    # ---- execution ----------------------------------------------------- #
    def run(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run every engine in stage order and return a combined report."""
        ctx: Dict[str, Any] = dict(context or {})
        ctx.setdefault("dry_run", self.dry_run)

        report: Dict[str, Any] = {
            "dry_run": self.dry_run,
            "stages": {},
            "order": [e.name for e in self.engines],
            "warnings": [],
            "agent_advice": {},
            "data_requests": {},
            "stage_gate": {},
            "gated": [],
            "skill_writes": {},
            "self_correction": {},
            "agent_telemetry": {},
            "agent_memory": {},
            "shift": {},
            "ready_to_trade": None,
        }

        agent_inputs = ctx.get("agent_inputs")
        if self.memory is not None:
            # One working-memory cycle per run. Bounded to the last few cycles.
            self.memory.working.begin_cycle(_dt.datetime.now().strftime("%H%M%S"),
                                            product="one risk-checked trading decision")
        if self.frontline_agent.enabled and isinstance(agent_inputs, dict):
            # One shared agentic budget for the whole run: the per-call limit
            # applies to the pipeline, not to each of the 8 engines.
            begin = getattr(self.frontline_agent.client, "begin_session", None)
            if callable(begin):
                begin()
            try:
                for name in (
                        "economic", "market", "screener", "valuation",
                        "backtest", "optimization", "portfolio", "risk"):
                    raw_input = agent_inputs.get(name)
                    text = raw_input.get("text") if isinstance(raw_input, dict) else raw_input
                    if not isinstance(text, str) or not text.strip():
                        continue
                    # Self-correction loop: the model answers, a DETERMINISTIC
                    # judge diagnoses, and only an actual failure is re-asked -
                    # bounded by max_attempts and the shared call budget.
                    outcome = self._advise_with_correction(name, text, ctx)
                    report["agent_advice"][name] = outcome
                    if outcome.get("attempts"):
                        report["self_correction"][name] = {
                            "corrections": outcome.get("corrections", 0),
                            "attempts": outcome["attempts"],
                        }
                    # One measurement row per station. This is what the paper
                    # harness aggregates: attempts, clamp rate, tool use.
                    report["agent_telemetry"][name] = {
                        "ok": bool(outcome.get("ok")),
                        "method": outcome.get("method"),
                        "attempts": len(outcome.get("attempts") or []),
                        "corrections": int(outcome.get("corrections") or 0),
                        # Distinct from ``ok``: an answer can be schema-valid
                        # and still fail the DATA gate. A harness must not read
                        # "the agent failed" when the truth is "the feed was
                        # missing", so both are recorded.
                        "warning_kind": _warning_kind(outcome),
                        "clamped": any("clamped" in str(w) for w in
                                       (outcome.get("warnings") or [])),
                        "tools_used": len(outcome.get("tool_trace") or []),
                        "json_written": bool(
                            (report["skill_writes"].get(name) or {}).get("ok")),
                    }
                    if outcome.get("ok"):
                        if not isinstance(ctx.get("agent_advice"), dict):
                            ctx["agent_advice"] = {}
                        ctx["agent_advice"][name] = outcome["advice"]
                        # The skill publishes its answer as JSON in the sandbox.
                        # This is the ONLY file an agent may write: no code, no
                        # config, no order state (see core/skill_writer.py).
                        skill = self.skills.get(name)
                        if skill is not None:
                            write = self.skill_writer.write_stage(
                                skill.stage, outcome["advice"],
                                meta={"skill_id": skill.skill_id,
                                      "method": outcome.get("method")})
                            report["skill_writes"][name] = write
                            if not write.get("ok"):
                                report["warnings"].append(
                                    f"skill {name} JSON write refused: "
                                    f"{write.get('reason')}")
                        # An agent that lacks evidence names it here; the data
                        # layer supplies it on the next cycle. This never
                        # changes an order - it only fills a gap.
                        needs = _data_requests(outcome["advice"])
                        if needs:
                            report["data_requests"][name] = needs
                    else:
                        report["warnings"].append(
                            f"frontline agent {name}: "
                            f"{outcome.get('warning', outcome.get('method'))}")
            finally:
                end = getattr(self.frontline_agent.client, "end_session", None)
                if callable(end):
                    end()

        # ---- data-sufficiency gate (read-only, logic-only) ----------------- #
        # "Only data-satisfied logic code moves to the next stage."
        #
        # Two modes, one meaning:
        #
        #   halt_analysis -> the FIRST starved stage stops execution of every
        #                    later analytic stage; earlier stages still run so
        #                    the report shows how far the evidence got.
        #   warn          -> every verdict is recorded and the chain runs to
        #                    completion unchanged.
        #
        # The verdict is evaluated BEFORE a stage runs; whether the stage itself
        # executes is decided by ``gate_execution`` inside the loop below.
        chain_halted_at: Optional[str] = None
        halted_position = -1
        gate_execution = self.stage_gate.on_block == "halt_analysis"
        for engine in self.engines:
            verdict = self.stage_gate.check(engine.name, ctx)
            report["stage_gate"][engine.name] = verdict.to_dict()
            if verdict.satisfied or report["gated"]:
                continue
            chain_halted_at = engine.name
            halted_position = self._stage_position(engine.name)
            report["gated"].append(engine.name)
            report["ready_to_trade"] = False
            if gate_execution:
                report["warnings"].append(
                    f"analysis halted at {engine.name}: {verdict.reason}")
            else:
                report["warnings"].append(
                    f"insufficient data at {engine.name} (warn mode): "
                    f"{verdict.reason}")

        # Preflight: Infrastructure is the *last* analytic stage but its health
        # verdict gates the POV *execution*. We therefore evaluate it first for
        # its gate signal, then again in natural order for the report, so the
        # ordering stays top-down and the report is complete.
        infra_engine = next(
            (e for e in self.engines if e.stage is EngineStage.INFRASTRUCTURE), None)
        infrastructure_ok = True
        risk_veto_active = False
        if self.gate_on_infrastructure and infra_engine is not None:
            pre = infra_engine.run(ctx)
            infrastructure_ok = bool(pre.get("ready_to_trade", False))
            ctx["infrastructure"] = _as_ctx(pre)
            report["ready_to_trade"] = pre.get("ready_to_trade")
            report["warnings"].extend(pre.warnings)

        for engine in self.engines:
            # Halted analytic chain: record the stop honestly instead of running
            # logic on evidence we already know is insufficient. Risk /
            # infrastructure / POV still run (see _post_gate_stages). In "warn"
            # mode nothing is skipped - the chain is only annotated.
            # Skip only stages strictly AFTER the halt: the starved stage and
            # everything before it still run, so the report shows how far the
            # evidence actually got instead of erasing successful work.
            skip = (gate_execution
                    and chain_halted_at is not None
                    and self._stage_position(engine.name) > halted_position
                    and engine.name not in self._post_gate_stages)
            if skip:
                report["stages"][engine.name] = {
                    "engine": engine.name,
                    "ok": False,
                    "data": {"gated": True,
                             "halted_at": chain_halted_at,
                             "reason": "insufficient data at "
                                       f"{chain_halted_at}"},
                    "warnings": [f"skipped: data gate halted at "
                                 f"{chain_halted_at}"],
                    "error": None,
                }
                continue

            # Skip the POV execution when infrastructure is not ready and the
            # gate is enabled. The analytic POV config is still built so the
            # operator can inspect it.
            if (engine.stage is EngineStage.POV
                    and (risk_veto_active or (
                        self.gate_on_infrastructure and not infrastructure_ok))):
                report["stages"][engine.name] = {
                    "engine": engine.name,
                    "ok": True,
                    "data": {"slice": {"status": "blocked",
                                       "reason": ("risk veto active"
                                                  if risk_veto_active else
                                                  "infrastructure not ready")}},
                    "warnings": [
                        ("POV blocked by risk gate" if risk_veto_active else
                         "POV blocked: infrastructure gate failed")],
                    "error": None,
                }
                report["warnings"].append(
                    "POV blocked by risk veto" if risk_veto_active else
                    "POV blocked by infrastructure gate")
                continue

            result = engine.run(ctx)
            ctx[engine.name] = _as_ctx(result)
            stage_result = dict(result.__dict__)
            if engine.name in report["agent_advice"]:
                stage_result["data"] = dict(result.data)
                stage_result["data"]["agent_advice"] = \
                    report["agent_advice"][engine.name]
            if engine.name in report["skill_writes"]:
                # Where the skill's JSON answer landed, so the dashboard can
                # link the card to the file without reading the sandbox.
                stage_result.setdefault("data", {})
                stage_result["data"]["skill_json"] = \
                    report["skill_writes"][engine.name].get("path")
            report["stages"][engine.name] = stage_result
            report["warnings"].extend(result.warnings)
            if engine.name == "risk":
                risk_veto_active = (
                    not result.ok or bool(result.get("risk_veto_active", True)))
                if risk_veto_active:
                    report["ready_to_trade"] = False
                    report["warnings"].append(
                        "POV blocked by risk veto or failed risk assessment")

        report["alpha_selected"] = (
            report["stages"].get("alpha", {}).get("data", {}).get("selected"))
        # The factory's work-in-progress record for this run. One row per
        # station in line order: who worked, with what evidence, what it
        # produced, where the JSON went, and what is still missing.
        if self.memory is not None:
            # Persist what the stations learned this cycle, then report a
            # summary so the dashboard can show memory state without reading
            # the sandbox.
            self.save_memory()
            report["agent_memory"] = self.memory.summary()
        report["shift"] = self.agent_team.run_shift(report).to_dict()
        # Hand the ledger to the dashboard (in-process mirror + one JSON file).
        # Best-effort: a dashboard handoff must never fail a trading run.
        try:
            from ..dashboard.state import publish_shift

            published = publish_shift(report["shift"])
            if not published.get("ok"):
                report["warnings"].append(
                    f"shift not published to the dashboard: "
                    f"{published.get('reason')}")
        except Exception as exc:  # noqa: BLE001 - never break a run for a view
            report["warnings"].append(f"shift publish skipped: {exc!r}")
        # LLM token/cost telemetry for this run. Pure read of the governor's
        # counters - the numbers were already collected during the calls, so
        # this adds no compute. Best-effort, exactly like the shift handoff.
        try:
            from ..dashboard.state import publish_token_usage

            tokens = self.frontline_agent.client.status().get("tokens")
            if isinstance(tokens, dict):
                report["llm_tokens"] = tokens
                published = publish_token_usage(tokens)
                if not published.get("ok"):
                    report["warnings"].append(
                        f"token usage not published to the dashboard: "
                        f"{published.get('reason')}")
        except Exception as exc:  # noqa: BLE001 - never break a run for a view
            report["warnings"].append(f"token publish skipped: {exc!r}")
        return report


def _warning_kind(outcome: Any) -> str:
    """Classify why an agent outcome was not usable. ``""`` when it was.

    Kept separate from ``ok`` because the paper-shift metrics must be able to
    distinguish "the model/the chain is unavailable" from "the model answered
    and the answer was rejected". Conflating them produced a false negative in
    the first baseline measurement.
    """
    if not isinstance(outcome, dict):
        return "unknown"
    if outcome.get("ok"):
        return ""
    warning = str(outcome.get("warning") or "")
    method = str(outcome.get("method") or "")
    if "no LLM backend available" in warning or method == "disabled":
        return "llm_unavailable"
    if method in ("quota",):
        return "quota"
    if method in ("llm_failed", "error"):
        return "llm_failed"
    if "schema" in warning or method == "invalid_response":
        return "schema_rejected"
    return "other"


def _data_requests(advice: Any) -> List[str]:
    """Extract a skill's ``data_requests`` from its validated payload.

    Two shapes are accepted, because both already exist in the LLM layer:

    * ``{"data_requests": ["cpi_history", ...]}`` - explicit list
    * ``{"data_requests": {"needs": [...], "reason": "..."}}`` - the
      ``DATA_REQUESTS_SCHEMA_KEYS`` shape

    Returns a clean ``list[str]`` (max 20 entries). Never raises: this sits on
    the trading path and a malformed request is not a reason to halt a cycle.
    """
    if not isinstance(advice, dict):
        return []
    raw = advice.get("data_requests")
    needs: List[str] = []
    if isinstance(raw, dict):
        raw = raw.get("needs")
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple)):
        for item in raw[:20]:
            if not isinstance(item, str):
                continue          # a non-string "need" is not a data name
            text = item.strip()
            if text:
                needs.append(text)
    return needs


def _as_ctx(result: EngineResult) -> Dict[str, Any]:
    """Adapt an EngineResult to the context sub-dict engines expect."""
    return {"ok": result.ok, "data": result.data, "warnings": result.warnings,
            "error": result.error}


def run_pipeline(config: Optional[Dict[str, Any]] = None,
                 context: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Convenience wrapper: build a pipeline from config and run it."""
    return MacroPipeline(config=config, log=log).run(context=context)


__all__ = ["MacroPipeline", "run_pipeline", "DEFAULT_CONFIG_PATH"]