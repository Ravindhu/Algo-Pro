"""
screener - the Screener engine (fundamental universe selection).

WHAT IT IS
----------
A Python port of the vendored npm package
``screener-india`` (https://github.com/sahiljani/screener-india) plus the
screening rules that turn raw fundamentals into a tradable universe.

This is the **fundamental half** of universe selection:

    Screener engine  ->  "WHICH names exist and pass the fundamental gates"
    Alpha engine     ->  "WHICH of those to buy/sell, and at what level"
    POV engine       ->  "HOW to slice the resulting order"

WHY IT IS VENDORED LIKE OPENALGO
--------------------------------
``vendor/screener-india/`` is a PRISTINE upstream clone (same rule as
``my-trading-system/openalgo/``): never hand-edited, pinned by
``vendor/SCREENER_INDIA_UPSTREAM_PIN``, refreshed only via
``_vendor_pin.py``. Everything we own lives in ``client/``, ``filters.py`` and
``screener_engine.py``. An upstream change can therefore never conflict with a
local edit, and the vendored TypeScript is provenance + a diff target, not a
runtime dependency (it is not imported and Node.js is not needed).

LAYOUT
------
    vendor/                PRISTINE screener-india clone + upstream pin
    client/base.py         transport primitives (cache, throttle, retry, envelope)
    client/parser.py       HTML -> Python (1:1 port of src/utils/parser.ts)
    client/numbers.py      screener.in display strings -> comparable floats
    client/screener_client.py   the ScreenerClient (port of src/client/*.ts)
    filters.py             Rule / Screener / the two requested rules
    screener_engine.py     ScreenerEngine(BaseEngine) - the engine entry point

THE REQUESTED SCREEN
--------------------
    1. market cap  >  200 Cr
    2. debt/equity <  1
Declared in ``blue_engine/input/screener_engine.json``; the code defaults match.
Sector exclusions (banking / finance / insurance) are ON by default because
debt/equity is not a meaningful solvency measure for a lender - see
``filters.py`` for the full argument.

SAFETY POSTURE
--------------
* READ-ONLY against screener.in. No order path, no OpenAlgo import, no broker.
* Bounded by design: request-rate throttle, page cap, symbol cap, disk cache.
* "Unknown" is a first-class screening outcome - a name whose debt cannot be
  measured FAILS the debt rule rather than passing it by default.
* Fundamental snapshots are labelled as such (``data_kind`` / ``as_of``) and are
  never presented as live prices. Live prices come from OpenAlgo ``quotes()``.
"""
from __future__ import annotations

from .filters import (
    FAIL,
    PASS,
    UNKNOWN,
    Rule,
    RuleResult,
    Screener,
    ScreenResult,
    default_rules,
)
from .screener_engine import ScreenerEngine
from .client.screener_client import ScreenerClient

__all__ = [
    "ScreenerEngine",
    "ScreenerClient",
    "Screener",
    "Rule",
    "RuleResult",
    "ScreenResult",
    "default_rules",
    "PASS",
    "FAIL",
    "UNKNOWN",
]
