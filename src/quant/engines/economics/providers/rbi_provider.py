"""
providers/rbi_provider.py - USD/INR from the FBIL reference rate (via RBI).

Why this provider exists
------------------------
USD/INR feeds the economic engine's ``currency`` signal, and until now the only
source was ``OpenAlgoMacroProvider``, which asks the broker for ``USDINR`` on
the CDS (currency) segment. Many Indian retail brokers - mstock included - do
not carry CDS data at all, so that lookup fails and the engine silently fell
back to a hardcoded value in ``macro_master.json``. That fallback sat at 84.5
while the real rate was ~95.

The reference rate is the right answer for a *macro regime* signal anyway:

  * it is a single, official, end-of-day number, not a live tick - which suits
    a signal whose inputs are otherwise monthly/quarterly prints,
  * it is published by FBIL (Financial Benchmarks India Pvt Ltd) and
    republished by RBI on its homepage, so it is a trusted source with no key,
    no cost, and no broker dependency,
  * using it removes the CDS entitlement requirement entirely.

Source
------
  https://www.rbi.org.in/  ->  "Current Rates" ->  "Reference Rate"
  The table is labelled "(Source : FBIL)" and reads e.g.

      INR / 1 USD : 95.8918
      (As at 1.00pm of September 25, 2026)

FBIL publishes once per day on business days and RBI normally reflects it the
same evening, so a 6-12h cache TTL is appropriate and kind to their servers.

Failure handling
----------------
Network/parse failure -> warnings + no ``currency`` key, which lets the
CompositeProvider fall through to the next source rather than inventing a
number. Trading logic must never consume a guessed FX rate.

Payload note
------------
``currency`` carries unit ``usd_inr`` but its ``value`` is the *reference
rate* (FBIL/Reuter's cut-off), NOT a spot price. The two differ by a few paise
and can diverge more in stressed markets. ``source`` records which one it is,
and the Dashboard shows that string verbatim, so nobody has to guess.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Callable, Dict, Optional

from .base import MacroProvider, value_block

try:  # optional dependency
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:  # noqa: BLE001
    requests = None  # type: ignore
    _HAS_REQUESTS = False


#: "INR / 1 USD" header row, then the value cell. Tolerant of whitespace and of
#: the tags RBI's ASP.NET template emits between the two.
_INR_USD_RE = re.compile(
    r"INR\s*/\s*1\s*USD\s*</th>\s*<td[^>]*>\s*:\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE | re.DOTALL,
)

#: "(As at 1.00pm of September 25, 2026)" - the observation stamp.
_AS_AT_RE = re.compile(r"As at\s+([0-9:.apm\s]+?of\s+[A-Za-z]+\s+\d{1,2},?\s*\d{4})",
                       re.IGNORECASE)

#: Distinguish the reference-rate table from the other FX tables RBI shows
#: (card rates / travel card rates), which are NOT the reference rate.
_FBIL_RE = re.compile(r"FBIL", re.IGNORECASE)

#: Sanity band for USD/INR. A parse that lands outside this is a bug, not a
#: market move, and is rejected rather than published.
_MIN_PLAUSIBLE = 30.0
_MAX_PLAUSIBLE = 300.0


class RbiReferenceRateProvider(MacroProvider):
    """USD/INR from RBI's republished FBIL reference rate (no key required)."""

    name = "rbi_fbil"

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 http_get: Optional[Callable[..., Any]] = None):
        super().__init__(config, log)
        self._http_get = http_get
        self.timeout: float = float(self.config.get("timeout", 20.0))
        self.url: str = self.config.get("url") or "https://www.rbi.org.in/"
        #: Optional override so a test can supply canned HTML without network.
        self._html_override: Optional[str] = self.config.get("html")
        self._headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        }

    # ---- transport ------------------------------------------------------- #
    def _get_html(self) -> Optional[str]:
        if self._html_override is not None:
            return self._html_override

        getter = self._http_get
        if getter is None:
            if not _HAS_REQUESTS:
                self.log("requests not installed and no http_get injected")
                return None
            getter = requests.get  # type: ignore[union-attr]
        try:
            resp = getter(self.url, timeout=self.timeout, headers=self._headers)
            if hasattr(resp, "text"):
                return resp.text
            if hasattr(resp, "read"):          # urllib-style response
                raw = resp.read()
                return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            return str(resp)
        except Exception as exc:  # noqa: BLE001
            self.log(f"GET failed ({exc!r}) for {self.url}")
            return None

    # ---- parsing --------------------------------------------------------- #
    @staticmethod
    def parse_reference_rate(html: str) -> Optional[Dict[str, Any]]:
        """Extract (rate, as_at) from the RBI homepage HTML.

        Pure and static so it can be unit-tested against saved markup without
        touching the network - which matters because this is a screen-scrape
        and the template *will* change eventually.
        """
        if not html:
            return None

        # Guard against parsing the wrong FX table: only trust a page that
        # mentions FBIL, since the reference rate is FBIL's and the other
        # tables on the same page are not.
        if not _FBIL_RE.search(html):
            return None

        m = _INR_USD_RE.search(html)
        if not m:
            return None

        try:
            rate = float(m.group(1))
        except (TypeError, ValueError):
            return None

        if not (_MIN_PLAUSIBLE <= rate <= _MAX_PLAUSIBLE):
            return None

        as_at = None
        a = _AS_AT_RE.search(html)
        if a:
            as_at = " ".join(a.group(1).split())

        return {"rate": rate, "as_at": as_at}

    # ---- provider contract ----------------------------------------------- #
    def _fetch(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        warnings: list = []

        html = self._get_html()
        if html is None:
            warnings.append("rbi: no HTML fetched")
            out["meta"] = {"ok": False, "warnings": warnings, "source": self.name}
            return out

        parsed = self.parse_reference_rate(html)
        if parsed is None:
            warnings.append(
                "rbi: reference rate not found in page "
                "(template may have changed, or FBIL marker missing)"
            )
            out["meta"] = {"ok": False, "warnings": warnings, "source": self.name}
            return out

        rate = parsed["rate"]
        as_at = parsed["as_at"] or date.today().isoformat()

        # `previous` is intentionally omitted: RBI shows only the latest fix.
        # The engine's delevel/surprise views handle a missing previous by
        # falling back to the level view (see scoring.py), so nothing is
        # invented here. Recording a fabricated previous would corrupt the
        # change/surprise maths, which is worse than having no change view.
        out["currency"] = value_block(
            rate, previous=None, unit="usd_inr",
            as_of=as_at,
            source=f"FBIL reference rate (via RBI): {as_at}",
        )

        out["meta"] = {
            "ok": True,
            "stale": False,
            "warnings": warnings,
            "source": self.name,
            "as_at": as_at,
        }
        return out


__all__ = ["RbiReferenceRateProvider"]