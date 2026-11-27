"""Owner-entered symbols that are merged into every screen refresh (2026-11-27).

Plain English: the owner can type a symbol in the Screener stocks panel. It is saved here and
added to the candidate list on each refresh. It is NOT a bypass: the refresh still fetches a
live quote, checks the circuit band and the ASM/GSM flag, and the bot's own gates still run.

File: ``data/screened/manual_symbols.json`` = ``{"symbols": ["INFY", ...]}``.
Never raises on read. Writes are atomic (temp file + os.replace).

Flow stage: Liquidity screener (owner input merged into the screen refresh; no order path)
Status: PHASE 3 IMPLEMENTED (2026-11-27; read/write only the manual symbols file)
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_SYMBOLS = 50
SYMBOL_RE = re.compile(r"^[A-Z0-9&-]{1,20}$")  # NSE symbols and 6-digit BSE codes


def normalise(symbol: Any) -> Optional[str]:
    """Upper-cased, trimmed symbol, or None when it is not a valid symbol."""
    text = str(symbol or "").strip().upper()
    return text if SYMBOL_RE.match(text) else None


def read(path) -> List[str]:
    """Saved manual symbols, in order, de-duplicated. Missing or broken file gives []."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    raw = payload.get("symbols") if isinstance(payload, dict) else None
    out: List[str] = []
    for item in raw if isinstance(raw, list) else []:
        sym = normalise(item)
        if sym and sym not in out:
            out.append(sym)
    return out[:MAX_SYMBOLS]


def _write(path, symbols: List[str]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"symbols": symbols}, fh, indent=1)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add(path, symbol: Any) -> Tuple[bool, str, List[str]]:
    """Add one symbol. Returns (ok, reason, symbols_now)."""
    sym = normalise(symbol)
    current = read(path)
    if sym is None:
        return False, "not a valid symbol (letters, digits, & or -; up to 20)", current
    if sym in current:
        return True, "already in the manual list", current
    if len(current) >= MAX_SYMBOLS:
        return False, f"manual list is full ({MAX_SYMBOLS})", current
    updated = current + [sym]
    _write(path, updated)
    return True, "added", updated


def remove(path, symbol: Any) -> Tuple[bool, str, List[str]]:
    """Remove one symbol. Returns (ok, reason, symbols_now)."""
    sym = normalise(symbol)
    current = read(path)
    if sym is None or sym not in current:
        return False, "not in the manual list", current
    updated = [s for s in current if s != sym]
    _write(path, updated)
    return True, "removed", updated


def as_candidates(symbols: List[str]) -> List[Dict[str, Any]]:
    """Candidate rows in the same shape as a selected Screener.in row, flagged ``manual``."""
    return [{"symbol": s, "selected": True, "manual": True} for s in symbols]
