"""Supported Cloud market and independent GitHub discovery adapters."""

from .github_search import GitHubSearchFetcher
from .market import MarketFetcher

__all__ = ["GitHubSearchFetcher", "MarketFetcher"]
