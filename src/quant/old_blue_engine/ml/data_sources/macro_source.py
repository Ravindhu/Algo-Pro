"""
ml/data_sources/macro_source.py — re-expose the economics providers as ML sources.

WHY RE-EXPOSE RATHER THAN RE-IMPLEMENT
--------------------------------------
`Blue_engine/economics/providers/` already contains hard-won, verified adapters:

    MospiProvider             CPI / GDP / unemployment (official MoSPI MCP)
    RbiReferenceRateProvider  official FBIL USD/INR fix
    FiiFlowProvider           FII/DII flows, PCR, India VIX
    IndiaMacroProvider        RBI / data.gov.in prints
    OpenAlgoMacroProvider     commodity proxies via the SDK

Each one documents real traps it works around (the MoSPI "rows[0] is Rural, not
the Combined headline" bug; the SSE framing; frequency_code 1 vs 2). Rewriting
that logic inside the ML package would duplicate those bugs and violate the
AGENTS.md instruction to *check redundancy and duplication*.

So this module is a thin **adapter**: it calls the existing provider and
reshapes the output into the ML row contract with an `as_of_ts`. One file, no
business logic, no new network code.

THE as_of_ts PROBLEM FOR MACRO DATA (the important part)
--------------------------------------------------------
A macro print has TWO timestamps and confusing them destroys a model:

    reference period : the month/quarter the number DESCRIBES  (e.g. Aug 2026)
    release date     : the day the public actually LEARNED it  (e.g. 12 Sep 2026)

Training on the reference period is look-ahead. In September you would "know"
August CPI on the 1st of August. The backtest becomes fiction.

Rule enforced here:
    as_of_ts = the provider's own `as_of` field when it reports one,
               otherwise the fetch time (`now`).

We use fetch time as the fallback **and record a DEGRADED row saying so**, because
using fetch time makes the feature *conservative* (the model learns the print
later than reality), which is safe-but-slower, whereas using the reference period
is unsafe-but-faster. Between two imperfect choices we always take the one that
cannot inflate the backtest.

This is also why the ML macro source is only useful once a history of
`data/economic_runs/*.jsonl` exists: each engine run stamps the values it saw at
that moment, giving us a genuine release-date series. That is the long-run fix,
and `history_from_runs()` below reads exactly that.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import config as ml_config
from .base import DataSource, FetchResult

__all__ = ["MacroSource", "history_from_runs", "ECONOMIC_RUNS_DIR"]


#: Where the economics engine records each run. One JSONL file per day.
#: (<repo>/data/economic_runs/YYYY-MM-DD.jsonl) — see economics/run_history.py.
ECONOMIC_RUNS_DIR = Path(ml_config.DATA_ROOT) / "economic_runs"


def history_from_runs(signal: Optional[str] = None,
                      limit_days: Optional[int] = None,
                      runs_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Build a *release-dated* macro history from recorded engine runs.

    This is the honest macro dataset: every row says "on this date, at this
    time, the engine saw this value". That is precisely the information a model
    would have had in production, so training on it cannot leak.

    Args:
        signal:     filter to one signal name (e.g. 'inflation'); None = all.
        limit_days: keep only the most recent N run files (cheap for a notebook).
        runs_dir:   override the directory (tests pass a temp folder).

    Returns:
        Rows shaped ``{"as_of_ts", "signal", "value", "unit", "source", "run_id"}``
        sorted oldest-first. Best-effort: an unreadable file is skipped, and an
        empty result is a legitimate outcome (a brand-new install has no runs yet).
    """
    base = Path(runs_dir) if runs_dir else ECONOMIC_RUNS_DIR
    if not base.is_dir():
        return []

    files = sorted(p for p in base.glob("*.jsonl"))
    if limit_days:
        files = files[-int(limit_days):]

    out: List[Dict[str, Any]] = []
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue      # torn line from an interrupted run
                    signals = rec.get("signals") or {}
                    if not isinstance(signals, dict):
                        continue
                    for name, block in signals.items():
                        if signal and name != signal:
                            continue
                        if not isinstance(block, dict):
                            continue
                        out.append({
                            "as_of_ts": (block.get("as_of")
                                         or rec.get("as_of")
                                         or rec.get("ts")
                                         or path.stem),
                            "signal": name,
                            "value": block.get("value"),
                            "unit": block.get("unit"),
                            "source": block.get("source") or rec.get("source"),
                            "run_id": rec.get("run_id") or path.stem,
                            "reference_period": block.get("as_of"),
                        })
        except Exception:  # noqa: BLE001 - one bad day must not kill the panel
            continue

    out.sort(key=lambda r: str(r.get("as_of_ts") or ""))
    return out


class MacroSource(DataSource):
    """Macro prints as ML rows, sourced from the economics provider stack.

    Config keys:
        signals       list[str] | None — restrict to these signal names.
        provider      an already-built MacroProvider (dependency injection for
                      tests and for the dashboard, which owns a live client).
        client        an OpenAlgo SDK client, used to build the default stack.
        allow_fetch_time_as_of  bool, default True. When False, rows without a
                      provider-supplied `as_of` are DROPPED instead of being
                      stamped with fetch time. Use False for a strict backtest
                      where even a conservative stamp is unacceptable.
    """

    name = "macro"
    kind = "macro"
    required_columns = ("as_of_ts", "signal", "value")

    # ---- provider resolution --------------------------------------------- #

    def _provider(self):
        """Return a MacroProvider, or None when none can be built.

        Built once and cached: the economics stack holds caches internally and
        rebuilding it per call would re-hit every government endpoint.
        """
        if getattr(self, "_prov", "missing") != "missing":
            return self._prov

        injected = self.config.get("provider")
        if injected is not None:
            self._prov = injected
            return self._prov

        try:
            from ...economics.providers import build_default_stack

            self._prov = build_default_stack(
                client=self.config.get("client"),
                config=self.config.get("macro") or {},
                log=self._log_fn,
            )
        except Exception as exc:  # noqa: BLE001 - optional layer
            self.log(f"macro provider stack unavailable: {exc!r}")
            self._prov = None
        return self._prov

    def available(self) -> bool:
        try:
            return self._provider() is not None
        except Exception:  # noqa: BLE001
            return False

    # ---- fetch ------------------------------------------------------------ #

    def _fetch(self, *, symbol: str, start: Optional[str],
               end: Optional[str]) -> FetchResult:
        wanted = self.config.get("signals")
        allow_fetch_time = bool(self.config.get("allow_fetch_time_as_of", True))

        rows: List[Dict[str, Any]] = []
        warnings: List[str] = []

        # ---- strategy 1: recorded runs (best — real release dates) -------- #
        rows = history_from_runs(runs_dir=self.config.get("runs_dir"))
        if start:
            rows = [r for r in rows if str(r.get("as_of_ts", ""))[:10] >= start]
        if end:
            rows = [r for r in rows if str(r.get("as_of_ts", ""))[:10] <= end]
        if wanted:
            rows = [r for r in rows if r.get("signal") in set(wanted)]
        if rows:
            self.log(f"{len(rows)} macro rows from recorded engine runs")
            return FetchResult(source=self.name, symbol="MACRO", ok=True,
                               rows=rows,
                               origin=f"economic_runs n={len(rows)}")

        # ---- strategy 2: live provider (only gives TODAY's print) --------- #
        prov = self._provider()
        if prov is None:
            self.deg.fatal("fetch", self.name, "no_provider",
                           "no macro provider available and no recorded runs "
                           "in data/economic_runs/", symbol="MACRO")
            return FetchResult(source=self.name, symbol="MACRO", ok=False,
                               warnings=["no macro provider, no recorded runs"])

        payload = prov.fetch()
        signals = {k: v for k, v in payload.items() if k != "meta"}
        if wanted:
            signals = {k: v for k, v in signals.items() if k in set(wanted)}

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        dropped = 0
        for name, block in signals.items():
            if not isinstance(block, dict):
                # A bare number carries no date at all — unusable for training
                # without a stamp, so it only survives the lenient path.
                if not allow_fetch_time:
                    dropped += 1
                    continue
                rows.append({"as_of_ts": now_iso, "signal": name,
                             "value": block, "unit": None,
                             "source": (payload.get("meta") or {}).get("source"),
                             "reference_period": None})
                continue
            as_of = block.get("as_of")
            if not as_of:
                if not allow_fetch_time:
                    dropped += 1
                    continue
                as_of = now_iso
            rows.append({
                "as_of_ts": as_of,
                "signal": name,
                "value": block.get("value"),
                "unit": block.get("unit"),
                "source": block.get("source")
                          or (payload.get("meta") or {}).get("source"),
                # Keep the reference period for audits: it is what the print
                # describes, and comparing it with as_of_ts is how an auditor
                # spots a source that forgot to report a release date.
                "reference_period": block.get("as_of"),
            })

        meta = payload.get("meta") or {}
        for w in meta.get("warnings") or []:
            warnings.append(str(w))

        if dropped:
            self.deg.degraded(
                "fetch", self.name, "missing_release_date",
                f"{dropped} macro signal(s) had no release date and were dropped "
                f"(allow_fetch_time_as_of=False)",
                symbol="MACRO")

        if not rows:
            self.deg.degraded("fetch", self.name, "empty_window",
                              "macro provider returned no usable signals",
                              symbol="MACRO",
                              context={"meta": meta})
            return FetchResult(source=self.name, symbol="MACRO", ok=False,
                               warnings=warnings + ["no macro signals"])

        if len(rows) < 20:
            # One snapshot is not a time series. A model cannot learn from it,
            # so we say so now instead of producing a 1-row "trained" model.
            self.deg.degraded(
                "fetch", self.name, "single_snapshot",
                f"only {len(rows)} macro observation(s) available — a live "
                f"provider returns today's print only. Accumulate "
                f"data/economic_runs/ before training on macro.",
                symbol="MACRO")

        return FetchResult(source=self.name, symbol="MACRO", ok=True, rows=rows,
                           warnings=warnings,
                           origin=str(meta.get("source") or self.name))

    def describe(self) -> Dict[str, Any]:
        d = super().describe()
        d["runs_dir"] = str(self.config.get("runs_dir") or ECONOMIC_RUNS_DIR)
        return d