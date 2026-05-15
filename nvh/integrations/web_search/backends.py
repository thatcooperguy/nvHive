"""Web search backends — DuckDuckGo HTML, SearXNG JSON, Brave Search API.

The DDG path is a deliberately small HTML scrape: a 60-line regex over
``html.duckduckgo.com/html/`` output. We don't pull in a library because
this is the kind of thing that breaks every 12-18 months no matter what
wrapper you use, and a 60-line scrape is no harder to fix than a wrapped
one. SearXNG and Brave use proper JSON APIs.
"""

from __future__ import annotations

import html
import logging
import os
import re
import urllib.parse

import httpx

from nvh.integrations.web_search.client import (
    BRAVE_KEY_ENV,
    SEARXNG_URL_ENV,
    SearchError,
    SearchResult,
)

logger = logging.getLogger(__name__)

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_USER_AGENT = "Mozilla/5.0 (compatible; nvHive-Wizard/1.0)"

# DDG's HTML wraps each result in <div class="result__body">. The links
# are <a class="result__a"> with the destination URL, then a snippet
# <a class="result__snippet">.
_DDG_RESULT_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'.*?'
    r'<a[^>]*class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.DOTALL,
)

# DDG sometimes proxies the destination through /l/?uddg=<urlencoded>.
_DDG_REDIRECT_RE = re.compile(r"^//duckduckgo\.com/l/\?uddg=(?P<u>[^&]+)")
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", s)).strip()


def _unwrap_ddg_redirect(url: str) -> str:
    match = _DDG_REDIRECT_RE.match(url)
    if not match:
        return url
    try:
        return urllib.parse.unquote(match.group("u"))
    except Exception:
        return url


async def duckduckgo_search(
    query: str, *, top_k: int = 5, timeout: float = 10.0,
) -> list[SearchResult]:
    """Scrape DDG's HTML endpoint. Zero-config; fragile to layout changes."""
    headers = {"User-Agent": _USER_AGENT}
    data = {"q": query, "kl": "us-en"}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(DDG_HTML_URL, data=data, headers=headers)
            resp.raise_for_status()
            body = resp.text
    except httpx.HTTPError as exc:
        raise SearchError(f"DuckDuckGo request failed: {exc}") from exc

    results: list[SearchResult] = []
    for match in _DDG_RESULT_RE.finditer(body):
        url = _unwrap_ddg_redirect(match.group("url").strip())
        if url.startswith("//"):
            url = "https:" + url
        title = _strip_tags(match.group("title"))
        snippet = _strip_tags(match.group("snippet"))
        if not url or not title:
            continue
        results.append(SearchResult(title=title, url=url, snippet=snippet))
        if len(results) >= top_k:
            break

    if not results:
        # Empty result list = either DDG returned a 0-hit page (rare) or
        # the HTML layout changed. Surface a clear error either way.
        raise SearchError(
            "DuckDuckGo returned no results — the HTML layout may have changed. "
            "Set NVH_SEARXNG_URL or BRAVE_API_KEY for a more durable backend.",
        )
    return results


async def searxng_search(
    query: str, *, top_k: int = 5, timeout: float = 10.0,
) -> list[SearchResult]:
    """Query a self-hosted SearXNG instance via its JSON API."""
    base = os.environ.get(SEARXNG_URL_ENV, "").rstrip("/")
    if not base:
        raise SearchError(f"{SEARXNG_URL_ENV} is not set.")
    url = f"{base}/search"
    params = {"q": query, "format": "json", "safesearch": "1"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise SearchError(f"SearXNG request failed: {exc}") from exc

    items = data.get("results") or []
    results: list[SearchResult] = []
    for item in items:
        url_val = item.get("url") or ""
        title = (item.get("title") or "").strip()
        snippet = (item.get("content") or "").strip()
        if not url_val or not title:
            continue
        results.append(SearchResult(title=title, url=url_val, snippet=snippet))
        if len(results) >= top_k:
            break
    if not results:
        raise SearchError("SearXNG returned no results.")
    return results


async def brave_search(
    query: str, *, top_k: int = 5, timeout: float = 10.0,
) -> list[SearchResult]:
    """Query Brave Search's official API (requires BRAVE_API_KEY)."""
    key = os.environ.get(BRAVE_KEY_ENV, "").strip()
    if not key:
        raise SearchError(f"{BRAVE_KEY_ENV} is not set.")
    url = "https://api.search.brave.com/res/v1/web/search"
    params = {"q": query, "count": str(min(top_k, 20))}
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": key,
        "User-Agent": _USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise SearchError(f"Brave Search request failed: {exc}") from exc

    web = (data.get("web") or {}).get("results") or []
    results: list[SearchResult] = []
    for item in web:
        url_val = item.get("url") or ""
        title = (item.get("title") or "").strip()
        snippet = (item.get("description") or "").strip()
        if not url_val or not title:
            continue
        results.append(SearchResult(title=title, url=url_val, snippet=snippet))
        if len(results) >= top_k:
            break
    if not results:
        raise SearchError("Brave returned no results.")
    return results
