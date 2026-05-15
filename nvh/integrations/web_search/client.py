"""Web search client — dispatches to a backend chosen by env, returns hits.

We keep the public API minimal: ``web_search(query, top_k)`` returns
``{ok, backend, results: [{title, url, snippet}]}`` or
``{ok: False, error}``. The Wizard tool wrapper, the HTTP endpoint, and
any future CLI all share this shape.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

SEARXNG_URL_ENV = "NVH_SEARXNG_URL"
BRAVE_KEY_ENV = "BRAVE_API_KEY"

DEFAULT_TOP_K = 5
MAX_TOP_K = 20


class SearchError(RuntimeError):
    """Raised when no configured backend produced results."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def active_backend() -> str:
    """Return the name of the backend ``web_search`` will dispatch to.

    Useful for the WebUI to surface "you're using SearXNG at <url>" so
    users understand which path is live and how to swap it.
    """
    if os.environ.get(SEARXNG_URL_ENV, "").strip():
        return "searxng"
    if os.environ.get(BRAVE_KEY_ENV, "").strip():
        return "brave"
    return "duckduckgo"


async def web_search(
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Run a single web search and return up to ``top_k`` hits.

    Errors are returned in-band ({ok: False, error: ...}) rather than
    raised, so the Wizard's follow-up loop sees a structured failure it
    can summarize for the user instead of a 500.
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "Empty query."}
    top_k = max(1, min(MAX_TOP_K, int(top_k)))

    backend = active_backend()
    try:
        if backend == "searxng":
            from nvh.integrations.web_search.backends import searxng_search

            results = await searxng_search(query, top_k=top_k, timeout=timeout)
        elif backend == "brave":
            from nvh.integrations.web_search.backends import brave_search

            results = await brave_search(query, top_k=top_k, timeout=timeout)
        else:
            from nvh.integrations.web_search.backends import duckduckgo_search

            results = await duckduckgo_search(query, top_k=top_k, timeout=timeout)
    except SearchError as exc:
        return {"ok": False, "backend": backend, "error": str(exc)}
    except Exception as exc:
        logger.warning("web_search backend=%s raised: %s", backend, exc)
        return {"ok": False, "backend": backend, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "backend": backend,
        "query": query,
        "results": [r.to_dict() for r in results[:top_k]],
    }
