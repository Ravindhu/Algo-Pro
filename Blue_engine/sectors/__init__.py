"""Sector-relative-strength analysis and advisory allocation constraints."""

from .sector_engine import SectorAdvisor, SectorEngine
from .sector_hm_score import SectorHMScorer, score_sectors
__all__ = ["SectorAdvisor", "SectorEngine", "SectorHMScorer",
           "score_sectors"]
