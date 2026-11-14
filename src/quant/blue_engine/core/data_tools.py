"""
data_tools.py - the READ-ONLY toolbelt an agentic stage skill may call.

WHY THIS FILE EXISTS
====================
The operator's requirement:

    "frontline agent can collect data ... create database analysis database feed
     input in json file of the project"
    "agentic ai should help logic code to get enough data"

The agentic loop (``core/llm/router.py::AgenticLLM``) already supports up to
``max_rounds`` tool calls. It had no tools. This module supplies them - and only
read-only ones:

========================  ===============================================
tool                      what it reads
========================  ===============================================
``list_sources``          what evidence is on disk / wired in right now
``read_json``             one published JSON payload (relative path only)
``cycle_payload``         ``data/current_cycle_payload.json`` summary
``price_history``         OHLCV bars from the local SQLite price cache
``provider``              any injected read-only provider's ``fetch()``
``data_catalog``          the project's own data inventory
========================  ===============================================

WHAT "READ-ONLY" MEANS HERE
---------------------------
* Nothing writes. There is no write tool in this module, by design.
* ``read_json`` resolves relative to an allowlisted root, refuses traversal,
  refuses non-``.json`` files, and refuses anything larger than
  ``max_bytes`` (a 200 MB payload in an LLM prompt is a denial of service).
* ``provider`` calls a callable the pipeline already constructed - the same
  dependency-injected provider the engines use. It cannot create one.
* Every tool returns a JSON-safe dict and NEVER raises: an error is
  ``{"error": "..."}`` so the loop treats it as data.

The output of these tools is appended to the conversation as *untrusted data*
(see ``AgenticLLM.run``), and the final answer is still validated by
``frontline_agent._validate`` and clamped by ``_escalate``. A tool result can
therefore never widen a limit.

USAGE
-----
::

    from blue_engine.core.data_tools import build_data_tools

    tools = build_data_tools(providers={"economic": provider_obj},
                             read_roots=[Path("data"), Path("output")])
    outcome = agent.advise("economic", text, tools=tools)
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_READ_BYTES",
    "MAX_ROWS",
    "DataToolbox",
    "build_data_tools",
    "summarise_payload",
]

#: Hard cap on a single JSON file the agent may load into its context.
MAX_READ_BYTES = 512 * 1024

#: Hard cap on rows/keys returned by any single tool call.
MAX_ROWS = 200


def _json_safe(value: Any) -> Any:
    """Recursively coerce to JSON-safe data (NaN/inf -> None, Paths -> str)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in list(value.items())[:MAX_ROWS]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in list(value)[:MAX_ROWS]]
    return str(value)[:200]


def summarise_payload(payload: Any, *, depth: int = 1) -> Dict[str, Any]:
    """Shape summary instead of the payload itself.

    Why: a raw macro block can be hundreds of KB. The model usually needs to
    know *what is available*, not every number. Full values are one
    ``read_json`` call away when it actually needs them.
    """
    def walk(node: Any, level: int) -> Any:
        if level <= 0:
            return f"<{type(node).__name__} len={len(node)}>" \
                if isinstance(node, (Mapping, list, tuple)) else _json_safe(node)
        if isinstance(node, Mapping):
            return {str(k): walk(v, level - 1)
                    for k, v in list(node.items())[:MAX_ROWS]}
        if isinstance(node, (list, tuple)):
            return [walk(v, level - 1) for v in list(node)[:5]]
        return _json_safe(node)

    return walk(payload, depth) if isinstance(payload, (Mapping, list)) \
        else {"value": _json_safe(payload)}


class DataToolbox:
    """Builds the read-only tool dict handed to ``AgenticLLM``."""

    def __init__(self, *, read_roots: Optional[Iterable[Any]] = None,
                 providers: Optional[Mapping[str, Any]] = None,
                 catalog_paths: Optional[Iterable[Any]] = None) -> None:
        self.read_roots: List[Path] = [Path(r).resolve() for r in (read_roots or [])]
        self.providers: Dict[str, Any] = dict(providers or {})
        self.catalog_paths: List[Path] = [Path(p) for p in (catalog_paths or [])]

    # ---- helpers -------------------------------------------------------- #
    def _resolve(self, relative: str) -> Path:
        raw = str(relative or "").strip()
        if not raw:
            raise ValueError("empty path")
        if Path(raw).is_absolute() or (len(raw) > 1 and raw[1] == ":"):
            raise ValueError("absolute paths are not allowed")
        if Path(raw).suffix.lower() != ".json":
            raise ValueError("only .json payloads can be read")
        if not self.read_roots:
            raise ValueError("no read roots configured")
        for root in self.read_roots:
            target = (root / raw).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue                     # traversal -> try the next root
            if target.is_file():
                return target
        raise FileNotFoundError(f"{raw!r} not found under any read root")

    # ---- tools ---------------------------------------------------------- #
    def list_sources(self, _args: Dict[str, Any]) -> Dict[str, Any]:
        """List the JSON payloads available on disk and the wired providers."""
        files: List[Dict[str, Any]] = []
        for root in self.read_roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.json"))[:MAX_ROWS]:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                files.append({"path": str(path.relative_to(root)).replace("\\", "/"),
                              "root": str(root), "bytes": size})
        return {"json_files": files,
                "providers": sorted(self.providers),
                "value_hint": "call read_json with one of the listed paths"}

    def read_json(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read one allowlisted JSON payload (capped by ``MAX_READ_BYTES``)."""
        try:
            path = self._resolve(args.get("path"))
            size = path.stat().st_size
            if size > MAX_READ_BYTES:
                return {"error": f"payload is {size} bytes; limit is "
                                 f"{MAX_READ_BYTES}. Ask for a summary instead.",
                        "path": str(path), "bytes": size}
            with path.open("r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}",
                    "requested": str(args.get("path") or "")}
        if isinstance(payload, Mapping) and args.get("summary_only"):
            payload = summarise_payload(payload)
        return {"path": str(path), "bytes": size, "payload": _json_safe(payload)}

    def cycle_payload(self, _args: Dict[str, Any]) -> Dict[str, Any]:
        """Summary of ``data/current_cycle_payload.json`` if it exists."""
        for root in self.read_roots:
            candidate = root / "current_cycle_payload.json"
            if candidate.is_file():
                try:
                    with candidate.open("r", encoding="utf-8") as stream:
                        payload = json.load(stream)
                except Exception as exc:  # noqa: BLE001 - tools return data
                    return {"error": f"{type(exc).__name__}: {exc}"}
                return {"path": str(candidate),
                        "synced_at": payload.get("synced_at"),
                        "data_class": payload.get("data_class"),
                        "shape": summarise_payload(payload, depth=1)}
        return {"error": "current_cycle_payload.json not found",
                "hint": "run the database engine's write_cycle_payload first"}

    def price_history(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Read OHLCV bars for one symbol from the local price cache."""
        symbol = str(args.get("symbol") or "").strip().upper()
        if not symbol:
            return {"error": "symbol is required"}
        try:
            limit = max(1, min(int(args.get("limit") or 120), MAX_ROWS))
        except (TypeError, ValueError):
            limit = 120
        exchange = str(args.get("exchange") or "NSE").strip().upper()
        interval = str(args.get("interval") or "D").strip().upper()
        try:
            from ..portfolio.providers.price_cache import PriceCache
        except Exception:  # noqa: BLE001 - import layout may differ
            return {"error": "price cache module unavailable"}
        try:
            cache = getattr(self, "_price_cache", None)
            if cache is None:
                cache = PriceCache()
                self._price_cache = cache
            rows = cache.rows(symbol, exchange, interval, limit=limit)
            return {"symbol": symbol, "exchange": exchange,
                    "interval": interval, "rows": _json_safe(rows)[-limit:]}
        except Exception as exc:  # noqa: BLE001 - never raise into the loop
            return {"error": f"{type(exc).__name__}: {exc}", "symbol": symbol}

    def provider(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call one already-wired read-only provider's ``fetch()``."""
        name = str(args.get("name") or "").strip().lower()
        source = self.providers.get(name)
        if source is None:
            return {"error": f"unknown provider {name!r}",
                    "available": sorted(self.providers)}
        try:
            fetch: Callable[[], Any] = (source.fetch
                                        if hasattr(source, "fetch") else source)
            payload = fetch()
        except Exception as exc:  # noqa: BLE001 - tools return data
            return {"error": f"{type(exc).__name__}: {exc}", "provider": name}
        return {"provider": name,
                "shape": summarise_payload(payload, depth=1)}

    def data_catalog(self, _args: Dict[str, Any]) -> Dict[str, Any]:
        """Report which known project data artifacts exist right now."""
        found = []
        for path in self.catalog_paths:
            found.append({"path": str(path), "exists": path.exists(),
                          "bytes": path.stat().st_size if path.exists() else 0})
        return {"artifacts": found, "count": len(found)}

    # ---- assembly ------------------------------------------------------- #
    def tools(self) -> Dict[str, Callable[[Dict[str, Any]], Any]]:
        """The tool dict for ``AgenticLLM``. Read-only by construction."""
        return {
            "list_sources": self.list_sources,
            "read_json": self.read_json,
            "cycle_payload": self.cycle_payload,
            "price_history": self.price_history,
            "provider": self.provider,
            "data_catalog": self.data_catalog,
        }

    def describe(self) -> Dict[str, Any]:
        return {
            "read_roots": [str(r) for r in self.read_roots],
            "providers": sorted(self.providers),
            "tools": sorted(self.tools()),
            "max_read_bytes": MAX_READ_BYTES,
            "writable": False,
        }


def build_data_tools(*, read_roots: Optional[Iterable[Any]] = None,
                     providers: Optional[Mapping[str, Any]] = None,
                     catalog_paths: Optional[Iterable[Any]] = None
                     ) -> Dict[str, Callable[[Dict[str, Any]], Any]]:
    """Convenience: return the tool dict directly."""
    return DataToolbox(read_roots=read_roots, providers=providers,
                       catalog_paths=catalog_paths).tools()
