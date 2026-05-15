"""Native web search for the Wizard — zero-config DDG, opt-in SearXNG/Brave.

Three backends, picked at call time by environment:

  - ``NVH_SEARXNG_URL`` set → SearXNG JSON API (self-hosted, best quality).
  - ``BRAVE_API_KEY`` set → Brave Search API (free tier ~2k/mo).
  - Otherwise → DuckDuckGo HTML scrape (zero-config, no key, fragile to
    DDG layout changes but works out of the box).

Order is deliberate: privacy-preserving + user-controlled paths first,
public scrape as the safe fallback. Backends share a single result
shape ``{title, url, snippet}`` so callers don't care which one ran.
"""

from __future__ import annotations

from nvh.integrations.web_search.client import (
    SearchError,
    SearchResult,
    active_backend,
    web_search,
)

__all__ = ["SearchError", "SearchResult", "active_backend", "web_search"]
