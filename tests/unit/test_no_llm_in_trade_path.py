"""X-9: no LLM client may be imported on the trade path (P2-7).

Flow stage: Risk gate, Execution, Sizing and portfolio, Ledger
Status: TEST

Rule (AGENTS.md, Operational boundaries 1): no import of an LLM client from
risk/, execution/, portfolio/, or monitor/ledger.py. Frontline and advice code
may use an LLM. The trade path must stay deterministic.

The check reads import statements with ``ast``. Comments and docstrings that
mention LLMs do not count.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "quant"

TRADE_PATH_DIRS = ["risk", "execution", "portfolio"]
TRADE_PATH_FILES = ["monitor/ledger.py"]

# A dotted module path containing any of these segments is an LLM import.
LLM_SEGMENTS = {"llm", "llms"}
# Third-party LLM client packages, matched on the top-level name.
LLM_CLIENT_PACKAGES = {"openai", "anthropic", "ollama", "openrouter", "litellm"}


def _trade_path_files():
    files = []
    for d in TRADE_PATH_DIRS:
        files += [p for p in (SRC / d).rglob("*.py")]
    files += [SRC / f for f in TRADE_PATH_FILES]
    return sorted(files)


def llm_imports(source: str) -> list:
    """Return the LLM-related module names imported by this source text."""
    found = []

    def bad(name: str) -> bool:
        parts = name.split(".")
        return any(p in LLM_SEGMENTS for p in parts) or parts[0] in LLM_CLIENT_PACKAGES

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if bad(a.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if bad(module):
                found.append("." * node.level + module)
            else:
                # "from quant.core import llm" names the llm module as an imported name.
                found += [f"{module}.{a.name}" for a in node.names if a.name in LLM_SEGMENTS]
        elif isinstance(node, ast.Call):
            # importlib.import_module("...llm...") and __import__("...") with a literal name.
            func = node.func
            is_dyn = (isinstance(func, ast.Name) and func.id == "__import__") or (
                isinstance(func, ast.Attribute) and func.attr == "import_module")
            if is_dyn and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) and bad(node.args[0].value):
                found.append(node.args[0].value)
    return found


def test_trade_path_files_exist():
    # Guards against the scan silently covering nothing after a rename.
    files = _trade_path_files()
    assert len(files) >= 20, f"only {len(files)} trade-path files found"


def test_no_llm_import_on_trade_path():
    offenders = {}
    for path in _trade_path_files():
        found = llm_imports(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(ROOT))] = found
    assert not offenders, f"LLM imports on the trade path: {offenders}"


# --- Transitive check (static, no imports run) -------------------------------------

BLUE_DIR = ROOT / "Blue_engine"


def _blue_file(name: str):
    parts = name.split(".")
    if parts[0] == "blue_engine":
        parts = parts[1:]
    base = BLUE_DIR.joinpath(*parts)
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    return base.with_suffix(".py") if base.with_suffix(".py").exists() else None


def _resolve(node: ast.AST, pkg_parts: list) -> list:
    """Absolute module names one import statement pulls in, relative imports resolved."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    base = node.module or ""
    if node.level:
        keep = pkg_parts[: len(pkg_parts) - (node.level - 1)]
        base = ".".join(keep + ([base] if base else []))
    return [base] + [f"{base}.{a.name}" for a in node.names]


def _blue_reach(start_names: list) -> set:
    """Blue modules loaded when the start modules are imported.

    Python also runs every parent package's __init__.py, so parents count too.
    """
    seen, stack = set(), list(start_names)
    while stack:
        name = stack.pop()
        if name in seen or not name.startswith("blue_engine"):
            continue
        seen.add(name)
        parts = name.split(".")
        stack += [".".join(parts[:k]) for k in range(2, len(parts))]
        path = _blue_file(name)
        if path is None:
            continue
        pkg = name.split(".") if path.name == "__init__.py" else name.split(".")[:-1]
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                stack += _resolve(node, pkg)
    return seen


def _bridge_targets() -> list:
    """Blue modules the trade path loads through quant.core.blue_bridge.load("...")."""
    targets = []
    for path in (SRC).rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "load" \
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                targets.append(f"blue_engine.{node.args[0].value}")
    return targets


@pytest.mark.xfail(strict=True, reason=(
    "Known gap X-9b: Blue package __init__ files load the LLM client. "
    "blue_engine/core/__init__.py imports frontline_agent, which imports core.llm; "
    "blue_engine/portfolio/__init__.py imports views.llm_note. Fix in Blue, then remove this marker."))
def test_no_llm_reached_transitively_from_trade_path():
    targets = _bridge_targets()
    assert targets, "no bridge targets found"
    reached = _blue_reach(targets)
    llm = sorted(m for m in reached if "llm" in m.split("."))
    assert not llm, f"trade path loads LLM modules: {llm}"


def test_scanner_catches_planted_violations():
    # The scanner must flag each kind of LLM import, or the test above proves nothing.
    samples = {
        "from quant.core.llm import tokens": ["quant.core.llm"],
        "import blue_engine.core.llm.tokens": ["blue_engine.core.llm.tokens"],
        "from ..core.llm import tokens": ["..core.llm"],
        "from quant.core import llm": ["quant.core.llm"],
        "import openai": ["openai"],
        "from anthropic import Anthropic": ["anthropic"],
        "import importlib\nimportlib.import_module('x.llm.scorer')": ["x.llm.scorer"],
    }
    for source, expected in samples.items():
        assert llm_imports(source) == expected, source


def test_scanner_ignores_comments_docstrings_and_safe_imports():
    source = '"""This module is LLM-free. It mentions openai and anthropic."""\n' \
             "# uses llm later\n" \
             "from quant.core.types import TradeIntent\n" \
             "import pandas as pd\n"
    assert llm_imports(source) == []
