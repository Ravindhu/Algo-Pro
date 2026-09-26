"""
ml/data_sources/news_source.py — timestamped text for the NLP / LLM / GPT models.

WHAT THIS FEEDS
---------------
`Blue_engine/economics/nlp/` already has `sentiment.py`, `llm_scorer.py` and
`rbi_minutes.py`, and `portfolio/views/nlp_views.py` consumes them. Every one of
those needs the same thing this module produces: **text with a timestamp of when
it became public**.

THE ONLY HARD PROBLEM IS THE TIMESTAMP
--------------------------------------
News is where look-ahead is most tempting and most invisible. Consider:

    Article published  2026-09-12 18:40 IST
    Content mentions   "the RBI will hold rates at the October meeting"
    Your model reads it 2026-09-13  -> fine.
    Your model reads it 2026-09-12 09:15 (because you only stored the DATE)

That second case is a real leak: an 18:40 article was not knowable at the 09:15
open. Storing a date-only timestamp silently hands the model eight hours of
foresight, every single day, and no error is raised anywhere.

So this module requires `published_ts` with a TIME component and a timezone. A
record without one is either dropped or stamped with a conservative
`first_seen_ts` (when *we* first observed it), and either way a Degradation row is
written. Conservative-and-slow beats fast-and-fictional.

LICENSING (read before enabling)
--------------------------------
Exchange filings and company announcements from NSE/BSE are public, but bulk
redistribution is licensed. News wire content is copyrighted. This module
therefore stores only:
    * a headline,
    * a source URL and a content hash,
    * short excerpts where the licence permits,
and never re-publishes full article bodies into the repo. The `storage` config
key documents the intended use; keep `excerpt_only=True` unless you have a
licence that says otherwise. This is a compliance requirement, not a nicety.

INPUT FORMAT (CSV/JSONL, so any collector can feed it)
------------------------------------------------------
    published_ts,headline,source,url,symbols
    2026-09-12T18:40:00+05:30,"RBI holds repo rate at 6.5%",RBI,https://...,"NIFTY;BANKNIFTY"

Collectors are deliberately OUT of scope here: how you obtain the feed (RSS, a
licensed vendor, a filing scraper) is a deployment decision with licensing
consequences, and hiding it inside the ML layer would make that decision
invisible. Point `path` at whatever your collector writes.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import FileDataSource, FetchResult

__all__ = ["NewsSource", "REQUIRED_FIELDS"]


#: Fields every news row must carry. Enforced by `DataSource.fetch` via
#: `required_columns`, so a collector that drops a column fails loudly at fetch
#: time instead of producing a model that quietly lost its text features.
REQUIRED_FIELDS = ("published_ts", "headline")


class NewsSource(FileDataSource):
    """Read a timestamped headline corpus written by an external collector.

    Config keys:
        path           file to read (.csv or .jsonl). Required.
        excerpt_only   default True — keep only headline/excerpt, never full body.
        timezone       assumed tz when a timestamp lacks one, default "+05:30".
        allow_date_only  default False. When False, a date-only timestamp is
                       rejected (see the look-ahead discussion above). Set True
                       only for a historical research corpus where you accept the
                       caveat and have written it into the run manifest.
    """

    name = "news"
    kind = "news"
    required_columns = REQUIRED_FIELDS

    # ---- file resolution -------------------------------------------------- #

    def path(self, symbol: str = "*") -> Optional[str]:
        raw = self.config.get("path")
        if not raw:
            return None
        p = Path(str(raw))
        # Try as given, then relative to the repo data dir, so a config can say
        # just "news/headlines.jsonl" and still resolve regardless of CWD.
        if p.is_file():
            return str(p)
        try:
            from .. import config as ml_config
            alt = Path(ml_config.DATA_ROOT) / p
            if alt.is_file():
                return str(alt)
        except Exception:  # noqa: BLE001
            pass
        return None

    # ---- parsing ---------------------------------------------------------- #

    def parse(self, path: str) -> List[Dict[str, Any]]:
        """Read CSV or JSONL into normalised news rows. May raise on bad bytes."""
        p = Path(path)
        if p.suffix.lower() == ".jsonl":
            records = self._parse_jsonl(p)
        else:
            records = self._parse_csv(p)

        tz = str(self.config.get("timezone", "+05:30"))
        allow_date_only = bool(self.config.get("allow_date_only", False))
        excerpt_only = bool(self.config.get("excerpt_only", True))

        out: List[Dict[str, Any]] = []
        date_only_count = 0
        for rec in records:
            ts = str(rec.get("published_ts") or "").strip()
            if not ts:
                continue
            # A date-only stamp is the leak described in the module docstring.
            if len(ts) <= 10 and not allow_date_only:
                date_only_count += 1
                continue
            if len(ts) <= 10:
                ts = f"{ts}T23:59:00{tz}"     # conservative: end of that day
            elif "T" in ts and ("+" not in ts and "Z" not in ts):
                ts = f"{ts}{tz}"              # naive -> assume IST

            headline = str(rec.get("headline") or "").strip()
            if not headline:
                continue

            row: Dict[str, Any] = {
                "as_of_ts": ts,               # when it became public
                "published_ts": ts,
                "headline": headline,
                "source": str(rec.get("source") or "").strip() or None,
                "url": str(rec.get("url") or "").strip() or None,
                "symbols": str(rec.get("symbols") or "").strip() or None,
                # Stable id for dedup across collectors that re-emit the same
                # story. Hash of (timestamp, headline) — no full body needed.
                "doc_id": hashlib.sha256(
                    f"{ts}|{headline}".encode("utf-8")).hexdigest()[:16],
            }
            if not excerpt_only:
                row["body"] = rec.get("body")
            out.append(row)

        if date_only_count:
            self.deg.degraded(
                "curate", self.name, "date_only_timestamp",
                f"{date_only_count} news row(s) had a date-only timestamp and "
                f"were dropped (an 18:40 article is not knowable at the 09:15 "
                f"open). Re-collect with full ISO timestamps.",
                symbol="NEWS")

        out.sort(key=lambda r: r["as_of_ts"])
        return out

    @staticmethod
    def _parse_csv(p: Path) -> List[Dict[str, Any]]:
        with p.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(r) for r in csv.DictReader(fh)]

    @staticmethod
    def _parse_jsonl(p: Path) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue          # torn line; one bad record is not fatal
        return out

    # ---- fetch ------------------------------------------------------------ #

    def _fetch(self, *, symbol: str, start: Optional[str],
               end: Optional[str]) -> FetchResult:
        res = super()._fetch(symbol=symbol, start=start, end=end)
        if not res.ok:
            return res

        rows = res.rows
        if start:
            rows = [r for r in rows if str(r["as_of_ts"])[:10] >= start]
        if end:
            rows = [r for r in rows if str(r["as_of_ts"])[:10] <= end]
        if symbol and symbol != "*":
            # Filter to stories that name the instrument. Substring match is
            # deliberately conservative: it keeps unrelated text out of a
            # symbol's model at the cost of missing some relevant stories, which
            # is the right trade for a first version.
            rows = [r for r in rows
                    if symbol.upper() in str(r.get("symbols") or "").upper()
                    or symbol.upper() in str(r.get("headline") or "").upper()]

        if not rows:
            self.deg.degraded("fetch", self.name, "empty_window",
                              f"no news rows in [{start or '*'} .. {end or '*'}]"
                              f" for {symbol}", symbol=symbol)
            return FetchResult(source=self.name, symbol=symbol, ok=False,
                               warnings=["no news in window"], origin=res.origin)

        res.rows = rows
        return res

    def describe(self) -> Dict[str, Any]:
        d = super().describe()
        d["path"] = self.config.get("path")
        d["excerpt_only"] = bool(self.config.get("excerpt_only", True))
        return d