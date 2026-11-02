"""
skill_writer.py - the ONLY write path an agentic stage skill is allowed to use.

WHY THIS FILE EXISTS
====================
The operator's rule for the frontline agent layer is explicit:

    "it should not edit code base, only json entry"
    "frontline agent can collect data ... feed input in json file of the project"

An LLM that can write files is an LLM that can write ``core/risk_engine.py``.
This module makes that impossible by construction: every agent-produced value
goes through :meth:`SkillWriter.write_stage` (or :meth:`SkillWriter.write_json`),
and both refuse anything outside a sandbox directory, anything that is not a
``.json`` file, and anything whose content is not plain JSON data.

WHAT IT GUARANTEES
------------------
1. **Extension allowlist.** Only ``*.json``. ``.py``, ``.ps1``, ``.bat``,
   ``.sh``, ``.dll``, ``.pth``, ``.toml``, ``.ini``, ``.env`` -> refused.
2. **Location allowlist.** The resolved path must sit inside the sandbox root
   (default ``<repo>/data/agent_stage``). ``..`` traversal, absolute paths and
   symlinks pointing outside are refused.
3. **Data-only content.** The payload must survive ``json.dumps`` with
   ``allow_nan=False``; NaN/Infinity are rejected, not silently coerced.
4. **Protected files.** ``input/macro_master.json`` and every config file is
   refused even if a caller widens the sandbox: an agent may *propose* config
   values, never persist them.
5. **Atomic writes.** tmp file + fsync + ``os.replace``, exactly like
   ``database/engine.py``'s cycle payload, so a reader never sees a partial
   file.
6. **Audit trail.** Every write (accepted or refused) is appended to
   ``agent_stage/_audit/writes.jsonl``. A refusal is a security event.

WHAT IT DOES NOT DO
-------------------
* It does not *decide* anything. Validation of the payload against a stage
  schema belongs to :mod:`blue_engine.core.stage_skills`.
* It is not imported by ``pov/``, ``risk_engine``, ``risk_gate`` or any other
  execution module. Execution stays logic-only; see
  ``tests/test_skill_writer.py::test_execution_modules_never_import_the_writer``.

USAGE
-----
::

    writer = SkillWriter()                       # default sandbox
    res = writer.write_stage("risk", advice, meta={"cycle": 42})
    # res == {"ok": True, "path": "...data/agent_stage/risk.json", ...}

    res = writer.write_json("../../core/risk_engine", {"x": 1})
    # res == {"ok": False, "reason": "'core/risk_engine' is not a .json file"}
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from ..runtime_paths import runtime_data_root

logger = logging.getLogger(__name__)

__all__ = [
    "WRITE_LOCK",
    "DEFAULT_SANDBOX_RELATIVE",
    "FORBIDDEN_EXTENSIONS",
    "SkillWriter",
    "AgentWriteRefused",
]

#: One process-wide lock: the dashboard and the runner both publish JSON, and a
#: lock is cheaper than reasoning about interleaved tmp files.
WRITE_LOCK = threading.Lock()

#: Sandbox location relative to the repository root. Same ``data/`` root that
#: already holds ``current_cycle_payload.json`` (database/engine.py).
DEFAULT_SANDBOX_RELATIVE = Path("data") / "agent_stage"

#: Anything an agent must never be able to drop on disk. Extension match is
#: case-insensitive and checked on the FINAL suffix.
FORBIDDEN_EXTENSIONS = frozenset({
    ".py", ".pyc", ".pyo", ".pyw", ".pyd", ".so", ".dll", ".exe", ".bat",
    ".cmd", ".ps1", ".psm1", ".sh", ".bash", ".vbs", ".js", ".mjs", ".cjs",
    ".jar", ".msi", ".pth", ".toml", ".ini", ".cfg", ".env", ".yaml", ".yml",
    ".sql", ".db", ".sqlite", ".parquet", ".pkl", ".pickle", ".joblib", ".pem",
    ".key", ".crt",
})

#: Basenames refused regardless of extension - configuration an agent may
#: propose but never persist (AGENTS.md: "Suggestions NEVER write configuration").
PROTECTED_BASENAMES = frozenset({
    "macro_master.json",
    "strategy.active.json",
    "pyproject.toml",
    "package.json",
})


class AgentWriteRefused(PermissionError):
    """Raised only by :meth:`SkillWriter.write_json` in strict mode."""


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _repo_root() -> Path:
    """Repository root, derived from this file (``<root>/blue_engine/core/``)."""
    return Path(__file__).resolve().parents[2]


def _is_relative_to(path: Path, root: Path) -> bool:
    """``Path.is_relative_to`` with a Python 3.8-safe fallback."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class SkillWriter:
    """Sandboxed JSON writer for agentic stage skills.

    Parameters
    ----------
    sandbox : Path, optional
        Root directory writes are confined to. Defaults to
        ``<repo>/data/agent_stage``. Created on first write, not at import.
    audit : bool, optional
        Append every write attempt to ``<sandbox>/_audit/writes.jsonl``.
        Default True - a refusal with no record is not auditable.
    protected_names : iterable of str, optional
        Extra basenames to refuse (added to :data:`PROTECTED_BASENAMES`).
    """

    def __init__(self, sandbox: Optional[Any] = None, *,
                 audit: bool = True,
                 protected_names: Optional[Iterable[str]] = None) -> None:
        root = Path(sandbox) if sandbox is not None else (
            runtime_data_root() / "agent_stage")
        # Resolve once: ``..`` in the sandbox itself must not leak the check.
        self.sandbox: Path = root.resolve()
        self.audit_enabled: bool = bool(audit)
        self.protected_names = PROTECTED_BASENAMES | frozenset(
            str(n) for n in (protected_names or ()))
        self._write_count = 0

    # ---- introspection -------------------------------------------------- #
    def describe(self) -> Dict[str, Any]:
        return {
            "sandbox": str(self.sandbox),
            "audit_enabled": self.audit_enabled,
            "writes": self._write_count,
            "protected_names": sorted(self.protected_names),
        }

    # ---- path safety ---------------------------------------------------- #
    def _resolve_target(self, name: str) -> Path:
        """Return the absolute target path or raise ``PermissionError``.

        All refusals are ``PermissionError`` subclasses so a caller cannot
        accidentally treat one as a retryable I/O error.
        """
        raw = str(name or "").strip()
        if not raw:
            raise PermissionError("agent write refused: empty file name")
        if "\x00" in raw:
            raise PermissionError("agent write refused: NUL byte in file name")

        candidate = Path(raw)
        if candidate.is_absolute() or (len(raw) > 1 and raw[1] == ":"):
            raise PermissionError(
                f"agent write refused: absolute path {raw!r} is not allowed")
        if candidate.suffix.lower() != ".json":
            raise PermissionError(
                f"agent write refused: {raw!r} is not a .json file")
        if candidate.suffix.lower() in FORBIDDEN_EXTENSIONS:  # defensive
            raise PermissionError(
                f"agent write refused: extension {candidate.suffix!r} is forbidden")
        if candidate.name.lower() in self.protected_names:
            raise PermissionError(
                f"agent write refused: {candidate.name!r} is a protected "
                f"configuration file")

        target = (self.sandbox / candidate).resolve()
        if not _is_relative_to(target, self.sandbox):
            raise PermissionError(
                f"agent write refused: {raw!r} escapes the sandbox "
                f"{self.sandbox}")
        if target.suffix.lower() != ".json":
            raise PermissionError(
                f"agent write refused: {raw!r} resolves to a non-.json file")
        if target.is_dir():
            raise PermissionError(
                f"agent write refused: {raw!r} is a directory")
        return target

    def assert_safe_target(self, name: str) -> Dict[str, Any]:
        """Return ``{"ok": bool, "path": str|None, "reason": str}`` - never raises."""
        try:
            return {"ok": True, "path": str(self._resolve_target(name)),
                    "reason": "allowed"}
        except PermissionError as exc:
            return {"ok": False, "path": None, "reason": str(exc)}

    # ---- serialisation -------------------------------------------------- #
    @staticmethod
    def _encode(payload: Any) -> str:
        """JSON-encode strictly. NaN/Infinity raise rather than degrade."""
        if not isinstance(payload, (Mapping, list)):
            raise TypeError(
                f"agent payload must be a mapping or a list, got "
                f"{type(payload).__name__}")
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2,
                              allow_nan=False, sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"agent payload is not JSON-serialisable: {exc}") from exc
        # ``default=str`` neutralises exotic objects (datetime, Path). Everything
        # else - NaN, Infinity, circular refs - still fails above, which is what
        # we want: a frontier model must not smuggle a non-finite number in.
        return text + "\n"

    # ---- writes --------------------------------------------------------- #
    def write_json(self, name: str, payload: Any, *, meta: Optional[Mapping[str, Any]] = None,
                   strict: bool = False) -> Dict[str, Any]:
        """Write ``payload`` to ``<sandbox>/<name>``.

        Returns a result dict (``ok``/``path``/``reason``/``bytes``/``written_at``)
        and never raises unless ``strict=True`` (then a refusal or a bad payload
        raises so a test/CLI fails loudly).
        """
        written_at = _utc_now_iso()
        try:
            target = self._resolve_target(name)
            encoded = self._encode(payload)
        except (PermissionError, TypeError, ValueError) as exc:
            result = {"ok": False, "path": None, "reason": str(exc),
                      "bytes": 0, "written_at": written_at}
            self._audit(name, False, str(exc), meta)
            if strict:
                raise AgentWriteRefused(str(exc)) if isinstance(
                    exc, PermissionError) else exc
            return result

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with WRITE_LOCK:
                fd, tmp_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".tmp",
                    dir=str(target.parent))
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                        stream.write(encoded)
                        stream.flush()
                        # fsync is unsupported on some Windows handles; a raised
                        # OSError from fsync must not lose the write that already
                        # succeeded, so it is downgraded to a debug note.
                        try:
                            os.fsync(stream.fileno())
                        except OSError as sync_exc:  # pragma: no cover - platform
                            logger.debug("skill_writer: fsync skipped: %r", sync_exc)
                    os.replace(tmp_name, target)
                finally:
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
        except OSError as exc:
            reason = f"agent write failed: {exc!r}"
            self._audit(name, False, reason, meta)
            if strict:
                raise
            return {"ok": False, "path": str(target), "reason": reason,
                    "bytes": 0, "written_at": written_at}

        self._write_count += 1
        self._audit(str(target), True, "written", meta)
        return {"ok": True, "path": str(target), "reason": "written",
                "bytes": len(encoded.encode("utf-8")), "written_at": written_at}

    def write_stage(self, stage: str, payload: Any, *,
                    meta: Optional[Mapping[str, Any]] = None,
                    strict: bool = False) -> Dict[str, Any]:
        """Write ``<sandbox>/<stage>.json`` - the canonical skill output file."""
        name = str(stage or "").strip().lower()
        if not name or not all(c.isalnum() or c in "_-" for c in name):
            result = {"ok": False, "path": None,
                      "reason": f"invalid stage name {stage!r}",
                      "bytes": 0, "written_at": _utc_now_iso()}
            self._audit(str(stage), False, result["reason"], meta)
            if strict:
                raise AgentWriteRefused(result["reason"])
            return result
        return self.write_json(f"{name}.json", payload, meta=meta, strict=strict)

    def read_stage(self, stage: str) -> Dict[str, Any]:
        """Read back ``<sandbox>/<stage>.json``. Never raises."""
        try:
            target = self._resolve_target(f"{str(stage).strip().lower()}.json")
            with target.open("r", encoding="utf-8") as stream:
                return {"ok": True, "payload": json.load(stream),
                        "path": str(target), "reason": "read"}
        except Exception as exc:  # noqa: BLE001 - readers must never raise
            return {"ok": False, "payload": None, "path": None,
                    "reason": f"{type(exc).__name__}: {exc}"}

    # ---- audit trail ---------------------------------------------------- #
    def _audit(self, name: str, ok: bool, reason: str,
               meta: Optional[Mapping[str, Any]]) -> None:
        if not self.audit_enabled:
            return
        line = {
            "ts": _utc_now_iso(),
            "target": str(name),
            "ok": bool(ok),
            "reason": str(reason),
            "meta": dict(meta or {}),
        }
        try:
            audit_dir = self.sandbox / "_audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            path = audit_dir / "writes.jsonl"
            with WRITE_LOCK:
                with path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(line, ensure_ascii=False,
                                            allow_nan=False, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 - auditing is best-effort
            logger.warning("skill_writer: audit append failed: %r", exc)
