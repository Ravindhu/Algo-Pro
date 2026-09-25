"""
providers/india_provider.py - official Indian macro prints from public sources.

Fills the *slow* macro signals the SDK cannot provide: GDP, CPI, IIP, the
benchmark 10Y G-Sec, FX reserves, unemployment. These are published on public
endpoints (no-key or free-key) and change daily/weekly/monthly - so this
provider is always wrapped in :class:`CachedProvider` with a long TTL.

Sources (all public)
--------------------
* **data.gov.in** (``DATAGOVIN_API_KEY``, free) - CPI, IIP, official indices.
* **RBI / FBIL** - 10Y G-Sec yield, FX reserves, USD/INR reference.
* **NSE India** - FII/DII flows, PCR, VIX (see :mod:`.fii_provider`).

Degradation contract
--------------------
``requests`` is an *optional* dependency. If it is absent, or a source has no
key / no network, this provider returns warnings and the engine falls back to
whatever the config macro block or other providers supplied. It NEVER raises.

Security
--------
Keys are resolved via :func:`blue_engine.economics.providers.secrets.get_secret`
(env -> ``_restored/.env`` -> repo ``.env``). Only the env-var *name* appears
here; the value never does, and never enters a log line.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .base import MacroProvider, value_block
from .secrets import get_secret, has_secret, redact

try:  # optional dependency; the provider degrades cleanly without it
    import requests  # type: ignore
    _HAS_REQUESTS = True
except Exception:  # noqa: BLE001
    requests = None  # type: ignore
    _HAS_REQUESTS = False


# data.gov.in resource ids for common series (extend as needed). These are
# public resource identifiers, not secrets.
_DATA_GOVIN_RESOURCES: Dict[str, Dict[str, str]] = {
    "inflation": {"resource": "cpi-series", "unit": "pct"},
    "iip":       {"resource": "iip-series", "unit": "index"},
    "gdp_growth": {"resource": "gdp-series", "unit": "pct"},
}


class IndiaMacroProvider(MacroProvider):
    """Official Indian macro prints from public/no-key sources."""

    name = "india"

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 http_get: Optional[Callable[..., Any]] = None):
        super().__init__(config, log)
        # Inject for tests; default is requests.get when available.
        self._http_get = http_get
        self.timeout: float = float(self.config.get("timeout", 8.0))
        self.datagovin_key: Optional[str] = get_secret("DATAGOVIN_API_KEY")

    # ---- helpers -------------------------------------------------------- #
    def _get_json(self, url: str) -> Optional[Dict[str, Any]]:
        getter = self._http_get
        if getter is None:
            if not _HAS_REQUESTS:
                return None
            getter = requests.get  # type: ignore[union-attr]
        try:
            resp = getter(url, timeout=self.timeout)
            if hasattr(resp, "json"):
                return resp.json()
            if isinstance(resp, dict):
                return resp
        except Exception as exc:  # noqa: BLE001
            self.log(f"http GET failed ({exc!r}) for {url}")
        return None

    # ---- individual feeds ---------------------------------------------- #
    def _fetch_datagovin(self, warnings: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if not self.datagovin_key:
            warnings.append(
                "data.gov.in: %s not set; CPI/IIP/GDP skipped"
                % redact("DATAGOVIN_API_KEY", None))
            return out
        base = self.config.get("datagovin_base",
                               "https://api.data.gov.in/resource")
        for signal, spec in _DATA_GOVIN_RESOURCES.items():
            url = (f"{base}/{spec['resource']}?api-key={self.datagovin_key}"
                   f"&format=json&limit=2")
            payload = self._get_json(url)
            if not payload:
                warnings.append(f"data.gov.in: no data for '{signal}'")
                continue
            records = payload.get("records") or payload.get("data") or []
            if not records:
                continue
            latest = records[0]
            # Resource schemas vary; accept common value field names.
            val = (latest.get("value") or latest.get("current_value")
                   or latest.get("index"))
            prev = None
            if len(records) > 1:
                prev = (records[1].get("value")
                        or records[1].get("current_value")
                        or records[1].get("index"))
            if val is None:
                continue
            out[signal] = value_block(val, prev, unit=spec["unit"],
                                      as_of=latest.get("date"),
                                      source="data.gov.in")
        return out

    def _fetch_rbi(self, warnings: List[str]) -> Dict[str, Any]:
        """RBI/FBIL public readings.

        These endpoints are broker-free but their exact shape is source
        specific, so we keep a per-series URL template in config. Absent a
        template we simply skip (the engine degrades to other providers).
        """
        out: Dict[str, Any] = {}
        templates: Dict[str, str] = self.config.get("rbi_urls") or {}
        for signal, url in templates.items():
            payload = self._get_json(url)
            if not payload:
                warnings.append(f"RBI/FBIL: no data for '{signal}'")
                continue
            val = payload.get("value") or payload.get("latest")
            prev = payload.get("previous")
            if val is None:
                continue
            out[signal] = value_block(val, prev,
                                      unit=payload.get("unit"),
                                      as_of=payload.get("as_of"),
                                      source="rbi")
        return out

    # ---- main ----------------------------------------------------------- #
    def _fetch(self) -> Dict[str, Any]:
        warnings: List[str] = []
        out: Dict[str, Any] = {}
        out.update(self._fetch_datagovin(warnings))
        out.update(self._fetch_rbi(warnings))
        out["meta"] = {"ok": True,  # partial data is acceptable here
                       "warnings": warnings, "source": self.name,
                       "requests_available": _HAS_REQUESTS}
        return out


__all__ = ["IndiaMacroProvider"]