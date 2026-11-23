"""
sector_screener_map.py - NSE sector INDEX -> screener.in sector slugs.

WHY THIS EXISTS
---------------
Two different taxonomies described the same companies and nothing translated
between them:

  * ``VendorSectorProvider`` / ``SectorHMScorer`` speak in **NSE index** names:
    ``NIFTY_FMCG``, ``NIFTY_METAL``, ``NIFTY_IT``.
  * ``ScreenerClient.get_sector_data`` needs **screener.in slugs**:
    ``IN04/IN0401/IN040107/IN040107001`` (Diversified FMCG),
    ``IN08/IN0801/IN080101/IN080101001`` (Computers - Software & Consulting).

Without a translation a sector verdict could never filter a stock universe, so
the sector layer could only ever allocate to an index - which is exactly the
path the research showed is not tradeable.

THE RULE THIS SUPPORTS
----------------------
The measured filter is **RSI above its EMA, at ANY level**
(``sector_hm_score`` R3: 0.518 hit rate at 5d, 0.535 at 20d against 0.500 for
random). The RSI<=40 "discount" cap was measured as the part that HURT
(RSI<=40 alone: 0.454 / 0.446) and is therefore NOT a filter here. It remains
available as a display label.

Verified 2026-10-24 against ``ScreenerClient.list_sectors()`` (188 leaf
sectors). Every slug below was copied from that live response, not typed from
memory, because one wrong IN-code silently screens the wrong companies.

COVERAGE IS DELIBERATELY PARTIAL
--------------------------------
A sector index is a *weighted basket*; screener sectors are disjoint
sub-industries. The mapping is therefore many-to-one in places: NIFTY BANK
spans both public and private sector bank sub-industries. Sectors with no
honest equivalent (NIFTY MNC, NIFTY PSE - which cut across industries rather
than naming one) are mapped to ``[]`` and reported as unmapped, never guessed.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

__all__ = [
    "INDEX_TO_SCREENER_SECTORS",
    "UNMAPPED_SECTORS",
    "screener_sectors_for",
    "mapped_indices",
    "is_mapped",
]

#: NSE sector index stem -> screener.in leaf sector slugs.
INDEX_TO_SCREENER_SECTORS: Dict[str, List[str]] = {
    # ---------------------------------------------------------------- IT
    "NIFTY_IT": [
        "IN08/IN0801/IN080101/IN080101001",   # Computers - Software & Consulting
        "IN08/IN0801/IN080101/IN080101002",   # Software Products
        "IN08/IN0801/IN080102/IN080102001",   # IT Enabled Services
    ],
    # -------------------------------------------------------------- BANK
    # NIFTY BANK holds both PSU and private banks; the index does not split.
    "NIFTY_BANK": [
        "IN05/IN0501/IN050102/IN050102001",   # Public Sector Bank
        "IN05/IN0501/IN050102/IN050102002",   # Private Sector Bank
        "IN05/IN0501/IN050102/IN050102003",   # Other Bank
    ],
    "NIFTY_PSU_BANK": [
        "IN05/IN0501/IN050102/IN050102001",   # Public Sector Bank
    ],
    # ------------------------------------------------------- FIN SERVICES
    "NIFTY_FIN_SERVICE": [
        "IN05/IN0501/IN050101/IN050101001",   # Financial Institution
        "IN05/IN0501/IN050101/IN050101002",   # Housing Finance Company
        "IN05/IN0501/IN050101/IN050101004",   # NBFC
        "IN05/IN0501/IN050101/IN050101005",   # Other Financial Services
        "IN05/IN0501/IN050103/IN050103001",   # Asset Management Company
        "IN05/IN0501/IN050103/IN050103006",   # Stockbroking & Allied
    ],
    # ------------------------------------------------------------ PHARMA
    "NIFTY_PHARMA": [
        "IN06/IN0601/IN060101/IN060101001",   # Pharmaceuticals
        "IN06/IN0601/IN060101/IN060101002",   # Biotechnology
    ],
    # ------------------------------------------------------------ FMCG
    "NIFTY_FMCG": [
        "IN04/IN0401/IN040107/IN040107001",   # Diversified FMCG
        "IN04/IN0401/IN040105/IN040105001",   # Personal Care
        "IN04/IN0401/IN040106/IN040106001",   # Household Products
        "IN04/IN0401/IN040104/IN040104004",   # Packaged Foods
        "IN04/IN0401/IN040102/IN040102001",   # Breweries & Distilleries
        "IN04/IN0401/IN040103/IN040103001",   # Cigarettes & Tobacco
      ],
    # ------------------------------------------------------------ AUTO
    "NIFTY_AUTO": [
        "IN02/IN0201/IN020101/IN020101001",   # Passenger Cars & Utility Vehicles
        "IN02/IN0201/IN020101/IN020101002",   # 2/3 Wheelers
        "IN02/IN0201/IN020102/IN020102001",   # Auto Components & Equipments
        "IN02/IN0201/IN020102/IN020102006",   # Tyres & Rubber Products
        "IN07/IN0702/IN070202/IN070202001",   # Tractors
        "IN07/IN0702/IN070202/IN070202002",   # Commercial Vehicles
    ],
    # ------------------------------------------------------------ METAL
    "NIFTY_METAL": [
        "IN01/IN0103/IN010301/IN010301004",   # Iron & Steel
        "IN01/IN0103/IN010302/IN010302001",   # Aluminium
        "IN01/IN0103/IN010302/IN010302002",   # Copper
        "IN01/IN0103/IN010302/IN010302003",   # Zinc
        "IN01/IN0103/IN010303/IN010303001",   # Diversified Metals
    ],
    # ----------------------------------------------------------- ENERGY
    "NIFTY_ENERGY": [
        "IN03/IN0301/IN030103/IN030103001",   # Refineries & Marketing
        "IN03/IN0301/IN030102/IN030102001",   # Oil Exploration & Production
        "IN03/IN0301/IN030101/IN030101001",   # Gas Transmission/Marketing
        "IN11/IN1101/IN110101/IN110101005",   # Integrated Power Utilities
    ],
    # ------------------------------------------------------------- INFRA
    "NIFTY_INFRA": [
        "IN07/IN0701/IN070101/IN070101001",   # Civil Construction
        "IN07/IN0702/IN070203/IN070203001",   # Heavy Electrical Equipment
        "IN11/IN1101/IN110101/IN110101004",   # Power Generation
        "IN09/IN0901/IN090103/IN090103002",   # Port & Port services
    ],
    # ------------------------------------------------------------ REALTY
    "NIFTY_REALTY": [
        "IN02/IN0205/IN020501/IN020501001",   # Residential, Commercial Projects
        "IN02/IN0205/IN020501/IN020501002",   # Real Estate related services
        "IN02/IN0205/IN020501/IN020501003",   # REITs
    ],
    # ------------------------------------------------ CONSUMER DURABLES
    "NIFTY_CONSUMER_DURABLES": [
        "IN02/IN0202/IN020201/IN020201003",   # Consumer Electronics
        "IN02/IN0202/IN020201/IN020201007",   # Household Appliances
        "IN02/IN0202/IN020201/IN020201017",   # Footwear
        "IN02/IN0202/IN020201/IN020201018",   # Furniture, Home Furnishing
    ],
    # ------------------------------------------------ CHEMICALS (no index
    # data exists, but the screener side is mapped so a future data source
    # can use it immediately)
    "NIFTY_CHEMICALS": [
        "IN01/IN0101/IN010101/IN010101001",   # Commodity Chemicals
        "IN01/IN0101/IN010101/IN010101002",   # Specialty Chemicals
        "IN01/IN0101/IN010101/IN010101006",   # Petrochemicals
        "IN01/IN0101/IN010102/IN010102001",   # Fertilizers
        "IN01/IN0101/IN010102/IN010102002",   # Pesticides & Agrochemicals
    ],
}

#: Sector indices with no honest single-taxonomy equivalent. Recorded so a run
#: reports "cannot filter on this sector" instead of screening nothing and
#: looking successful.
UNMAPPED_SECTORS: Dict[str, str] = {
    "NIFTY_MNC": "cross-industry (MNC ownership), not a screener.in sector",
    "NIFTY_PSE": "cross-industry (public ownership), not a screener.in sector",
}


def screener_sectors_for(index: str) -> List[str]:
    """screener.in slugs for one NSE sector index. Empty if unmapped."""
    return list(INDEX_TO_SCREENER_SECTORS.get(str(index).upper(), []))


def mapped_indices() -> List[str]:
    return sorted(INDEX_TO_SCREENER_SECTORS)


def is_mapped(index: str) -> bool:
    return bool(screener_sectors_for(index))


def unmapped_reason(index: str) -> str:
    return UNMAPPED_SECTORS.get(str(index).upper(),
                                "no screener.in sector equivalent recorded")


def expand(indices: Sequence[str]) -> Dict[str, List[str]]:
    """{index: slugs} for the given indices, skipping unmapped ones."""
    return {i: screener_sectors_for(i) for i in indices
            if is_mapped(i)}
