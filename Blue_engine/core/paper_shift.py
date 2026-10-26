"""
paper_shift.py - measure the agent line before anybody trusts it with capital.

WHY THIS FILE EXISTS
====================
The next-priority item was:

    "Run a paper shift with stage_gate.enabled=true and
     agent.self_correction.tools=true against a real agent_inputs payload, and
     measure: attempts per stage, clamp rate, and whether the gate's default
     requirements are too strict for a cold-start cycle."

Measuring that by hand, once, proves nothing. This module makes it repeatable:

* it runs the pipeline in **paper mode only** (``dry_run`` forced True),
* over N cycles, against the same context the live loop would see,
* with the agent layer on (self-correction + read-only tools) and the data gate
  on, unless told otherwise,
* and returns a **measurement**, not a verdict: attempts, clamp rate, gate
  blocks per stage, JSON write success, and wall-clock cost.

WHAT IT REFUSES TO DO
---------------------
* It will not run with ``dry_run=False``. There is no flag for it. A paper
  harness that can be pointed at real money is not a paper harness.
* It does not touch the broker, the writer sandbox beyond ``data/``, or any
  config. It reads the config it is given.
* It never reports a rate it could not measure: ``None`` means "no data", never
  zero.

WHAT THE NUMBERS MEAN
---------------------
``gate_blocks``       per stage: how many cycles the station was starved.
``clamp_rate``        fraction of accepted answers the safe rails tightened.
``mean_attempts``     mean correction attempts per *advised* stage (1.0 = the
                      first answer was accepted).
``idle_stages``       stages where no agent opinion was produced at all.
``write_failures``    skill JSON writes that were refused (should be 0).
``cycles_allowed``    cycles where the line reached risk without a gate stop.

USAGE
-----
::

    from blue_engine.core.paper_shift import run_paper_shift

    result = run_paper_shift(cycles=5, agent_inputs={"economic": "..."})
    result["metrics"]["mean_attempts"], result["metrics"]["clamp_rate"]

CLI::

    python -m blue_engine.core.paper_shift --cycles 5 --json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

__all__ = [
    "DEFAULT_CONFIG",
    "load_base_config",
    "research_config",
    "run_paper_shift",
    "main",
]

#: The default master config, relative to the repo root.
DEFAULT_CONFIG = (Path(__file__).resolve().parents[1]
                  / "input" / "macro_master.json")

#: Stages the prompt-model agent advises (mirrors the pipeline's loop).
ADVISED_STAGES = ("economic", "market", "screener", "valuation",
                  "backtest", "optimization", "portfolio", "risk")


def load_base_config(path: Optional[Any] = None) -> Dict[str, Any]:
    """Read the master config. Raises ``FileNotFoundError`` if absent."""
    target = Path(path) if path else DEFAULT_CONFIG
    with target.open("r", encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError(f"{target} is not a JSON object")
    return config


def research_config(base: Mapping[str, Any], *, gate: bool = True,
                    tools: bool = True, attempts: int = 2,
                    on_block: str = "halt_analysis",
                    offline: bool = False,
                    probe_infrastructure: bool = False) -> Dict[str, Any]:
    """Return a COPY of ``base`` configured for a research shift.

    Paper mode is forced, the agent layer is switched on, and the LLM chain is
    constrained to a single backend so a measurement is attributable:

    * ``dry_run = True`` - always, no argument can change it;
    * ``agent.enabled = True``;
    * ``agent.self_correction = {enabled, max_attempts, tools}``;
    * ``stage_gate = {enabled, on_block}``;
    * ``agent.timeout_seconds`` capped at 20 s so a hung local model cannot
      stall the measurement.

    ``offline=True`` disables every LLM backend. The agent layer is still wired
    (so the code path, the gate and the telemetry are exercised), but the answer
    is an explicit ``disabled`` failure instead of a network round-trip. That
    keeps a measurement run deterministic and fast; use ``offline=False`` only
    when you actually want model output in the numbers.

    ``probe_infrastructure=False`` (the default) points the dashboard/OpenAlgo
    health probes at a closed port so the infrastructure stage returns fast.
    Profiled cost of leaving it on: 3 x 1 s of socket timeouts per cycle, which
    dominates a short measurement and has nothing to do with the agent layer.
    Set it True when the measurement IS about infrastructure health.
    """
    config = json.loads(json.dumps(base))          # deep copy, JSON-safe
    config["dry_run"] = True
    config["gate_on_infrastructure"] = False       # this harness measures the
                                                   # AGENT layer, not infra
    engines = config.setdefault("engines", {})
    agent = engines.setdefault("agent", {})
    agent["enabled"] = True
    agent["timeout_seconds"] = min(float(agent.get("timeout_seconds", 20) or 20), 20)
    agent["self_correction"] = {
        "enabled": True,
        "max_attempts": max(1, min(int(attempts), 3)),
        "tools": bool(tools),
    }
    # Read-only evidence roots the toolbelt may read (core/data_tools.py).
    agent.setdefault("tools", {})
    agent["tools"].setdefault("read_roots", ["data", "output"])
    engines["stage_gate"] = {"enabled": bool(gate), "on_block": on_block}
    if offline:
        llm = agent.setdefault("llm", {})
        llm.setdefault("ollama", {})["enabled"] = False
        llm.setdefault("openrouter", {})["enabled"] = False
    if not probe_infrastructure:
        # Point every health probe at port 9 (discard). The stage still runs and
        # still reports honestly; it just does not spend 3 x 1 s per cycle on
        # connect timeouts. Keys mirror InfrastructureEngine.__init__:
        # openalgo_port / dash_port / ws_port.
        infra = engines.setdefault("infrastructure", {})
        for key in ("openalgo_port", "dash_port", "ws_port"):
            infra[key] = 9
        infra.setdefault("openalgo_host", "127.0.0.1")
    return config


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def _rate(numerator: int, denominator: int) -> Optional[float]:
    """A rate, or None when there is nothing to divide by. Never 0/0."""
    return None if denominator <= 0 else round(numerator / denominator, 4)


def collect_metrics(cycles: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-cycle reports into the measurement the SOP asks for."""
    advised_present = 0
    attempted_total = 0
    clamped_total = 0
    corrected_total = 0
    write_failures = 0
    gate_blocks: Dict[str, int] = {}
    idle: Dict[str, int] = {}
    worked: Dict[str, int] = {}
    allowed = 0
    stopped_at: Dict[str, int] = {}
    durations: List[float] = []
    unavailable = 0
    rejected = 0
    tool_calls = 0

    for report in cycles:
        if not isinstance(report, Mapping):
            continue
        advice = report.get("agent_advice") or {}
        corrections = report.get("self_correction") or {}
        writes = report.get("skill_writes") or {}
        shift = report.get("shift") or {}

        telemetry = report.get("agent_telemetry") or {}
        for stage in ADVISED_STAGES:
            outcome = advice.get(stage)
            if isinstance(outcome, Mapping):
                advised_present += 1
                attempted_total += 1
                if any("clamped" in str(w)
                       for w in (outcome.get("warnings") or [])):
                    clamped_total += 1
                trace = corrections.get(stage) or {}
                corrected_total += int(trace.get("corrections") or 0)
                row = telemetry.get(stage) or {}
                kind = str(row.get("warning_kind") or "")
                if kind in ("llm_unavailable", "llm_failed", "quota"):
                    unavailable += 1
                elif kind:
                    rejected += 1
                tool_calls += int(row.get("tools_used") or 0)
        for write in writes.values():
            if isinstance(write, Mapping) and not write.get("ok"):
                write_failures += 1
        for stage in (report.get("gated") or []):
            gate_blocks[str(stage)] = gate_blocks.get(str(stage), 0) + 1

        for row in shift.get("stations") or []:
            state, stage = row.get("state"), str(row.get("stage"))
            if state == "idle":
                idle[stage] = idle.get(stage, 0) + 1
            elif state == "worked":
                worked[stage] = worked.get(stage, 0) + 1

        if report.get("ready_to_trade") is not False or not report.get("gated"):
            allowed += 1
        if shift.get("stopped_at"):
            key = str(shift["stopped_at"])
            stopped_at[key] = stopped_at.get(key, 0) + 1
        if isinstance(report.get("duration_seconds"), (int, float)):
            durations.append(float(report["duration_seconds"]))

    runs = len(cycles)
    return {
        "cycles": runs,
        "cycles_allowed": allowed,
        "advised_stage_runs": advised_present,
        # Mean attempts per advised stage: 1.0 = first answer always accepted.
        "mean_attempts": (round(1 + corrected_total / advised_present, 3)
                          if advised_present else None),
        "corrections_total": corrected_total,
        # "The chain is unavailable" is NOT "the model answered badly". Keeping
        # them apart is what makes the number actionable.
        "llm_unavailable_runs": unavailable,
        "schema_rejected_runs": rejected,
        "tool_calls": tool_calls,
        "clamp_rate": _rate(clamped_total, advised_present),
        "write_failures": write_failures,
        "gate_blocks": dict(sorted(gate_blocks.items())),
        "stopped_at": dict(sorted(stopped_at.items())),
        "idle_stages": dict(sorted(idle.items())),
        "worked_stages": dict(sorted(worked.items())),
        "mean_duration_seconds": (round(statistics.fmean(durations), 3)
                                  if durations else None),
        "notes": _interpret(gate_blocks, advised_present, allowed, runs,
                            unavailable=unavailable, rejected=rejected),
    }


def _interpret(gate_blocks: Mapping[str, int], advised: int,
               allowed: int, runs: int, *, unavailable: int = 0,
               rejected: int = 0) -> List[str]:
    """Plain-English observations. Facts only - no recommendations."""
    notes: List[str] = []
    if runs and allowed == 0:
        notes.append("No cycle reached risk: the data gate stops the line every "
                     "time. The gate's default requirements are stricter than "
                     "the context this harness supplies.")
    if unavailable and unavailable >= advised:
        notes.append("EVERY advised stage failed for a chain reason, not a "
                     "judgement reason: this run measured the GATE, not the "
                     "self-correction loop. Re-run with --live-llm and a "
                     "reachable Ollama model to measure attempts/clamp rate.")
    if not advised:
        notes.append("No agent opinion was produced: the LLM chain was "
                     "unavailable (offline host or no key), so this cycle "
                     "measured only the deterministic line.")
    if gate_blocks.get("economic"):
        notes.append("'economic' is the first station and was starved in "
                     f"{gate_blocks['economic']}/{runs} cycles: a missing "
                     "'macro' snapshot blocks the whole analytic chain by "
                     "design (halt_analysis).")
    return notes


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #
def run_paper_shift(*, cycles: int = 3,
                    agent_inputs: Optional[Mapping[str, Any]] = None,
                    context: Optional[Mapping[str, Any]] = None,
                    config: Optional[Mapping[str, Any]] = None,
                    config_path: Optional[Any] = None,
                    gate: bool = True, tools: bool = True,
                    attempts: int = 2, offline: bool = True,
                    probe_infrastructure: bool = False,
                    log: Optional[Any] = None) -> Dict[str, Any]:
    """Run N paper cycles and return the measurement. Never places an order."""
    from .pipeline import MacroPipeline

    base = dict(config) if config else load_base_config(config_path)
    research = research_config(base, gate=gate, tools=tools, attempts=attempts,
                               offline=offline,
                               probe_infrastructure=probe_infrastructure)
    if research.get("dry_run") is not True:  # defensive: cannot be turned off
        raise RuntimeError("paper_shift refuses to run with dry_run=False")

    runs = max(1, min(int(cycles), 50))
    reports: List[Dict[str, Any]] = []
    for index in range(runs):
        started = time.perf_counter()
        ctx: Dict[str, Any] = dict(context or {})
        if agent_inputs:
            ctx["agent_inputs"] = dict(agent_inputs)
        report = MacroPipeline(research, log=log).run(ctx)
        report["duration_seconds"] = round(time.perf_counter() - started, 3)
        report["cycle"] = index + 1
        reports.append({
            "cycle": report["cycle"],
            "duration_seconds": report["duration_seconds"],
            "ready_to_trade": report.get("ready_to_trade"),
            "gated": list(report.get("gated") or []),
            "agent_advice": report.get("agent_advice") or {},
            "self_correction": report.get("self_correction") or {},
            "skill_writes": report.get("skill_writes") or {},
            "agent_telemetry": report.get("agent_telemetry") or {},
            "shift": report.get("shift") or {},
        })

    metrics = collect_metrics(reports)
    return {
        "harness": "paper_shift",
        "dry_run": True,
        "gate_enabled": bool(gate),
        "tools_enabled": bool(tools),
        "offline": bool(offline),
        "probed_infrastructure": bool(probe_infrastructure),
        "max_attempts": research["engines"]["agent"]["self_correction"]["max_attempts"],
        "config_path": str(config_path or DEFAULT_CONFIG),
        "metrics": metrics,
        "cycles": reports,
    }


def _default_agent_inputs() -> Dict[str, str]:
    """A conservative, obviously-synthetic payload for a smoke shift.

    Deliberately plain: it exercises the schema, the gate and the correction
    loop without asking the model for a market opinion it cannot justify.
    """
    return {
        "economic": "CPI steady near the upper band; policy rate unchanged for "
                    "two meetings; growth tracking trend.",
        "market": "Index in a narrow range for three weeks; realised volatility "
                  "below its 20-day average; breadth flat.",
        "risk": "No corporate action flags. Liquidity normal. Treat this cycle "
                "as advisory only.",
    }


def main(argv: Optional[List[str]] = None) -> int:
    """CLI: run the shift and print either JSON or a short table."""
    parser = argparse.ArgumentParser(
        prog="python -m blue_engine.core.paper_shift",
        description="Paper shift: measure the agent line. Never trades.")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--config", default=None,
                        help="master config path (default: input/macro_master.json)")
    parser.add_argument("--no-gate", action="store_true",
                        help="disable the data gate (measures the agent alone)")
    parser.add_argument("--no-tools", action="store_true",
                        help="disable the read-only data toolbelt")
    parser.add_argument("--attempts", type=int, default=2,
                        help="max self-correction attempts (1..3)")
    parser.add_argument("--probe-infra", action="store_true",
                        help="really probe dashboard/OpenAlgo ports (adds ~3 s "
                             "per cycle; off by default)")
    parser.add_argument("--live-llm", action="store_true",
                        help="allow real LLM calls (default: offline, so the "
                             "measurement is fast and deterministic)")
    parser.add_argument("--json", action="store_true",
                        help="print the full measurement as JSON")
    parser.add_argument("--out", default=None,
                        help="also write the measurement to this JSON path")
    args = parser.parse_args(argv)

    result = run_paper_shift(cycles=args.cycles, config_path=args.config,
                             gate=not args.no_gate, tools=not args.no_tools,
                             attempts=args.attempts,
                             offline=not args.live_llm,
                             probe_infrastructure=args.probe_infra,
                             agent_inputs=_default_agent_inputs())
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8")

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    m = result["metrics"]
    print("PAPER SHIFT (dry_run=True - no orders can be placed)")
    print(f"  cycles              : {m['cycles']}")
    print(f"  gate / tools        : {result['gate_enabled']} / "
          f"{result['tools_enabled']}")
    print(f"  llm                 : {'offline (no model calls)' if result['offline'] else 'live'}")
    print(f"  cycles allowed      : {m['cycles_allowed']}")
    print(f"  advised stage runs  : {m['advised_stage_runs']}")
    print(f"  mean attempts       : {m['mean_attempts']}")
    print(f"  clamp rate          : {m['clamp_rate']}")
    print(f"  llm unavailable     : {m['llm_unavailable_runs']}")
    print(f"  schema rejected     : {m['schema_rejected_runs']}")
    print(f"  tool calls          : {m['tool_calls']}")
    print(f"  write failures      : {m['write_failures']}")
    print(f"  gate blocks         : {m['gate_blocks'] or '-'}")
    print(f"  stopped at          : {m['stopped_at'] or '-'}")
    print(f"  mean duration (s)   : {m['mean_duration_seconds']}")
    for note in m["notes"]:
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
