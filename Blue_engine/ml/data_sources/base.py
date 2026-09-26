"""
ml/data_sources/base.py — the DataSource contract every vendor adapter obeys.

THE ONE RULE THAT MATTERS
-------------------------
A source returns a table indexed by **knowledge time** (`as_of_ts`), not event
time. These are usually the same for a price bar (you know Friday's close on
Friday) but they are *wildly* different for fundamentals and macro prints:

    Event time   : 2026-06-30  (end of Q1 FY27)
    Knowledge    : 2026-08-14  (the day the company actually filed results)

If you stamp a fundamental with its event time and train on it, your model
"knows" Q1 earnings six weeks before they were public. The backtest becomes
spectacular and live trading loses. This is the #1 quant ML mistake and the
whole `as_of_ts` design exists to make it impossible to make accidentally.

CONTRACT
--------
    class MySource(DataSource):
        name = "my_source"
        def _fetch(self, symbol, start, end) -> list[dict] / DataFrame

    src = MySource(log=deg)
    res = src.fetch("INFY", "2015-01-01", "2026-09-26")
    res.ok          -> bool
    res.rows        -> list[dict], each with as_of_ts
    res.warnings    -> list[str]  (mirrored as Degradation rows)
    res.rows_hash   -> stable hash of the content (goes into the model manifest)

WHY IT MIRRORS THE EXISTING ENGINE PROVIDERS
--------------------------------------------
`blue_engine.portfolio.providers.base.PortfolioProvider` and
`blue_engine.economics.providers.base.MacroProvider` already established the
house style: **fetch never raises; failure is a soft result plus warnings.**
We deliberately keep the same shape (a `fetch()` wrapper around a `_fetch()` that
may raise) so a reader who knows one provider knows them all, and so the
dashboard can render any provider's health with the same component.

The difference: ML sources add `as_of_ts`, a content hash, and a schema
fingerprint, because ML artifacts must be reproducible months later.
"""

from __future__ import annotations

import abc
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import logger
from ..audit.degradation import DegradationLog


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #


@dataclass
class FetchResult:
    """What a source returns. A value object — no behaviour beyond helpers."""

    source: str
    symbol: str = "*"
    ok: bool = False
    rows: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    #: sha256 of the canonicalised rows. Recorded in the model manifest so we can
    #: prove months later exactly which data produced a model.
    rows_hash: Optional[str] = None
    #: Column name -> dtype string, hashed. Detects silent unit/column changes.
    schema_fingerprint: Optional[str] = None
    latency_ms: int = 0
    #: Where the bytes physically came from (URL, file path, SDK method). Used in
    #: incident reports: "which endpoint was failing at 09:20 that morning?"
    origin: Optional[str] = None

    @property
    def n(self) -> int:
        return len(self.rows)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source, "symbol": self.symbol, "ok": self.ok,
            "n": self.n, "warnings": list(self.warnings),
            "rows_hash": self.rows_hash,
            "schema_fingerprint": self.schema_fingerprint,
            "latency_ms": self.latency_ms, "origin": self.origin,
        }


# --------------------------------------------------------------------------- #
# Hashing helpers (used by the manifest — keep them stable and dependency-free)
# --------------------------------------------------------------------------- #


def rows_hash(rows: Sequence[Dict[str, Any]]) -> str:
    """Stable sha256 over rows, independent of dict key order.

    Canonicalisation: sort keys, drop volatile bookkeeping fields, format floats
    with `repr` so 1.0 and 1.00 hash identically. Without this, re-fetching the
    same data would produce a different hash every time and the manifest would be
    useless as a reproducibility proof.
    """
    volatile = {"fetched_at", "ingested_at", "_raw"}
    h = hashlib.sha256()
    for r in rows:
        clean = {k: r[k] for k in sorted(r) if k not in volatile}
        h.update(json.dumps(clean, sort_keys=True, default=str).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def schema_fingerprint(rows: Sequence[Dict[str, Any]]) -> Optional[str]:
    """sha256 over the column names and their inferred types.

    Catches the silent unit/column change class of bug (see SchemaDriftError).
    Returns None for an empty table so callers must handle "unknown schema"
    rather than hashing an empty string and pretending it is a real fingerprint.
    """
    if not rows:
        return None
    first = rows[0]
    schema = {k: type(first[k]).__name__ for k in sorted(first)}
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


class DataSource(abc.ABC):
    """Abstract vendor adapter. Subclass and implement :meth:`_fetch`.

    Subclass checklist
    ------------------
    [ ] set a unique `name` (it becomes the cache filename and the audit label)
    [ ] set `kind` ('price' | 'fundamental' | 'macro' | 'flow' | 'news' | 'derivatives')
    [ ] implement `_fetch` returning rows that ALL carry `as_of_ts`
    [ ] declare `required_columns` so the quality gate can validate
    [ ] add a test pinning one known vendor value (proof the parsing is right)
    """

    name: str = "data_source"
    #: Coarse category. Lets the registry and the dashboard group sources without
    #: importing them, and lets the training config say "use all macro sources".
    kind: str = "unknown"
    #: Columns the quality gate will demand. Empty list = no assertion.
    required_columns: Sequence[str] = ()

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[DegradationLog] = None,
                 log_fn: Optional[Callable[[str], None]] = None):
        self.config: Dict[str, Any] = dict(config or {})
        self.deg: DegradationLog = log or DegradationLog()
        self._log_fn = log_fn

    # ---- logging ---------------------------------------------------------- #

    def log(self, message: str) -> None:
        text = f"[ml:{self.name}] {message}"
        if callable(self._log_fn):
            self._log_fn(text)
        else:
            logger.info(text)

    # ---- public API ------------------------------------------------------- #

    def fetch(self, symbol: str = "*", start: Optional[str] = None,
              end: Optional[str] = None) -> FetchResult:
        """Fetch a window. NEVER raises — failures come back as `ok=False`.

        `start`/`end` are ISO dates (YYYY-MM-DD) and inclusive. Implementations
        should treat a None start as "as far back as the source allows" and a None
        end as "up to now", because callers in a notebook rarely know the bounds.
        """
        started = time.time()
        try:
            result = self._fetch(symbol=symbol, start=start, end=end)
            if isinstance(result, FetchResult):
                res = result
            else:
                # Allow the friendly shortcut: _fetch returns a list of rows.
                res = FetchResult(source=self.name, symbol=symbol,
                                  ok=bool(result), rows=list(result or []))
        except Exception as exc:  # noqa: BLE001 - network/parse boundary
            self.log(f"fetch FAILED: {exc!r}")
            self.deg.degraded("fetch", self.name, "exception",
                              f"{type(exc).__name__}: {exc}",
                              symbol=symbol,
                              context={"start": start, "end": end})
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=[f"{type(exc).__name__}: {exc}"])

        res.source = res.source or self.name
        res.symbol = res.symbol or symbol
        res.latency_ms = int((time.time() - started) * 1000)

        if not res.ok or not res.rows:
            res.ok = False
            msg = (f"no rows for {symbol} [{start or '*'} .. {end or '*'}]")
            res.warnings.append(msg)
            self.deg.degraded("fetch", self.name, "empty_window", msg,
                              symbol=symbol, context={"start": start, "end": end})
            return res

        # Cheap structural validation here so every source gets it for free.
        missing = [c for c in self.required_columns
                   if c not in res.rows[0]]
        if missing:
            res.ok = False
            msg = f"missing required columns: {missing}"
            res.warnings.append(msg)
            self.deg.fatal("fetch", self.name, "schema_drift", msg,
                           symbol=symbol, context={"columns": sorted(res.rows[0])})
            return res

        res.rows_hash = res.rows_hash or rows_hash(res.rows)
        res.schema_fingerprint = (res.schema_fingerprint
                                  or schema_fingerprint(res.rows))
        for w in res.warnings:
            self.deg.degraded("fetch", self.name, "partial", w, symbol=symbol)
        return res

    @abc.abstractmethod
    def _fetch(self, *, symbol: str, start: Optional[str],
               end: Optional[str]) -> FetchResult | List[Dict[str, Any]]:
        """Do the real work. May raise; may do I/O. Must stamp `as_of_ts`."""
        raise NotImplementedError

    # ---- introspection ---------------------------------------------------- #

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind,
                "required_columns": list(self.required_columns),
                "config_keys": sorted(self.config.keys())}

    def available(self) -> bool:
        """True when this source can run *at all* (creds/libs present).

        Default is True. Override when the source needs an optional dependency
        (e.g. `yfinance`, a paid API key) so the registry can report
        "source present but disabled" instead of failing at fetch time.
        """
        return True


class FileDataSource(DataSource):
    """Base for sources that read a local file we already downloaded.

    Useful for two honest scenarios:
      1. A licensed dataset a human downloaded manually (NSE bulk files).
      2. A frozen snapshot committed for regression tests — so the test suite
         never depends on a live government website being up.

    Subclasses implement `path()` and `parse()`; this class handles the
    existence check, the audit row and the hashing.
    """

    kind = "file"

    @abc.abstractmethod
    def path(self, symbol: str = "*") -> Optional[str]:
        """Absolute path to the file, or None when it is not present."""
        raise NotImplementedError

    @abc.abstractmethod
    def parse(self, path: str) -> List[Dict[str, Any]]:
        """Read the file into rows. May raise on a corrupt file."""
        raise NotImplementedError

    def _fetch(self, *, symbol: str, start: Optional[str],
               end: Optional[str]) -> FetchResult:
        p = self.path(symbol)
        if not p:
            self.deg.fatal("fetch", self.name, "file_missing",
                           f"expected local file for {symbol} not found",
                           symbol=symbol)
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=["file missing"])
        rows = self.parse(p)
        return FetchResult(source=self.name, symbol=symbol, ok=bool(rows),
                           rows=rows, origin=p)


__all__ = [
    "DataSource", "FileDataSource", "FetchResult",
    "rows_hash", "schema_fingerprint",
]