# -*- coding: utf-8 -*-
"""Research-only data providers kept separate from daily market fetchers."""

from src.research.providers.official_disclosure import OfficialDisclosureProvider
from src.research.providers.tushare_research import TushareResearchProvider

__all__ = ["OfficialDisclosureProvider", "TushareResearchProvider"]
