"""Tests for the web-search subpackage.

HTTP calls are mocked — no test depends on an external network. We verify:

  - Backend selection follows env precedence (SearXNG > Brave > DDG)
  - The shared SearchResult shape comes back from each backend
  - DDG HTML parsing extracts title/url/snippet and unwraps proxy redirects
  - The client returns {ok: False, error} instead of raising on backend errors
  - The Wizard registry exposes ``web_search`` as auto-class
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.integrations.web_search import (
    SearchError,
    SearchResult,
    active_backend,
    web_search,
)


def test_active_backend_defaults_to_duckduckgo(monkeypatch) -> None:
    monkeypatch.delenv("NVH_SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert active_backend() == "duckduckgo"


def test_active_backend_picks_searxng_when_set(monkeypatch) -> None:
    monkeypatch.setenv("NVH_SEARXNG_URL", "https://searx.example.com")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    assert active_backend() == "searxng"


def test_active_backend_picks_brave_when_only_brave_set(monkeypatch) -> None:
    monkeypatch.delenv("NVH_SEARXNG_URL", raising=False)
    monkeypatch.setenv("BRAVE_API_KEY", "brv_abc")
    assert active_backend() == "brave"


def test_active_backend_searxng_wins_over_brave(monkeypatch) -> None:
    """SearXNG is user-hosted and key-free — should outrank Brave when both set."""
    monkeypatch.setenv("NVH_SEARXNG_URL", "https://searx.example.com")
    monkeypatch.setenv("BRAVE_API_KEY", "brv_abc")
    assert active_backend() == "searxng"


@pytest.mark.asyncio
async def test_web_search_returns_error_envelope_on_empty_query() -> None:
    result = await web_search("   ")
    assert result["ok"] is False
    assert "empty" in result["error"].lower()


@pytest.mark.asyncio
async def test_web_search_dispatches_to_ddg_by_default(monkeypatch) -> None:
    monkeypatch.delenv("NVH_SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    hits = [
        SearchResult(title="nvHive docs", url="https://nvhive.dev", snippet="Multi-LLM."),
    ]
    with patch(
        "nvh.integrations.web_search.backends.duckduckgo_search",
        new=AsyncMock(return_value=hits),
    ):
        result = await web_search("nvhive")

    assert result["ok"] is True
    assert result["backend"] == "duckduckgo"
    assert result["results"][0]["url"] == "https://nvhive.dev"


@pytest.mark.asyncio
async def test_web_search_dispatches_to_searxng_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("NVH_SEARXNG_URL", "https://searx.example.com")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    hits = [SearchResult(title="t", url="https://x", snippet="s")]
    with patch(
        "nvh.integrations.web_search.backends.searxng_search",
        new=AsyncMock(return_value=hits),
    ):
        result = await web_search("query", top_k=3)

    assert result["ok"] is True
    assert result["backend"] == "searxng"
    assert len(result["results"]) == 1


@pytest.mark.asyncio
async def test_web_search_returns_in_band_error_when_backend_raises(monkeypatch) -> None:
    """SearchError should land in {ok: False, error} — not propagate up."""
    monkeypatch.delenv("NVH_SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    with patch(
        "nvh.integrations.web_search.backends.duckduckgo_search",
        new=AsyncMock(side_effect=SearchError("DDG layout changed")),
    ):
        result = await web_search("anything")

    assert result["ok"] is False
    assert "DDG layout changed" in result["error"]
    assert result["backend"] == "duckduckgo"


@pytest.mark.asyncio
async def test_web_search_caps_top_k_at_20(monkeypatch) -> None:
    monkeypatch.delenv("NVH_SEARXNG_URL", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    captured: dict[str, int] = {}

    async def fake(query: str, *, top_k: int = 5, timeout: float = 10.0):
        captured["top_k"] = top_k
        return [SearchResult(title="t", url="https://x", snippet="s")]

    with patch("nvh.integrations.web_search.backends.duckduckgo_search", new=fake):
        await web_search("query", top_k=999)

    assert captured["top_k"] == 20


# ────────────────────────────────────────────────────────────────────────────
# DDG HTML parsing — we own a regex scrape, so we own a parsing test.
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duckduckgo_parses_results_and_unwraps_redirects() -> None:
    """The DDG scrape must extract title/url/snippet and unwrap the
    /l/?uddg= proxy redirect that DDG sometimes wraps destination URLs in."""
    from nvh.integrations.web_search.backends import duckduckgo_search

    html_body = (
        '<div class="result__body">'
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fnvhive.dev%2F">nvHive home</a>'
        '<a class="result__snippet" href="#">Multi-LLM platform &amp; rootless GPU</a>'
        "</div>"
        '<div class="result__body">'
        '<a class="result__a" href="https://example.com/docs">Docs</a>'
        '<a class="result__snippet" href="#">A bunch of docs</a>'
        "</div>"
    )

    fake_resp = MagicMock()
    fake_resp.text = html_body
    fake_resp.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=fake_resp)

    with patch("nvh.integrations.web_search.backends.httpx.AsyncClient", return_value=fake_client):
        results = await duckduckgo_search("nvhive", top_k=2)

    assert len(results) == 2
    assert results[0].title == "nvHive home"
    assert results[0].url == "https://nvhive.dev/"
    assert "Multi-LLM" in results[0].snippet
    assert results[1].url == "https://example.com/docs"


@pytest.mark.asyncio
async def test_duckduckgo_raises_searcherror_when_no_results_parsed() -> None:
    from nvh.integrations.web_search.backends import duckduckgo_search

    fake_resp = MagicMock()
    fake_resp.text = "<html>nothing matches the regex here</html>"
    fake_resp.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    fake_client.post = AsyncMock(return_value=fake_resp)

    with (
        patch("nvh.integrations.web_search.backends.httpx.AsyncClient", return_value=fake_client),
        pytest.raises(SearchError),
    ):
        await duckduckgo_search("anything", top_k=5)


@pytest.mark.asyncio
async def test_searxng_requires_env_var(monkeypatch) -> None:
    from nvh.integrations.web_search.backends import searxng_search

    monkeypatch.delenv("NVH_SEARXNG_URL", raising=False)
    with pytest.raises(SearchError):
        await searxng_search("anything")


@pytest.mark.asyncio
async def test_brave_requires_api_key(monkeypatch) -> None:
    from nvh.integrations.web_search.backends import brave_search

    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(SearchError):
        await brave_search("anything")


def test_wizard_registry_includes_web_search() -> None:
    from nvh.integrations.wizard.tools import default_registry

    registry = default_registry()
    tool = registry.get("web_search")
    assert tool is not None
    assert tool.safety_class == "auto"
    # Schema advertises the two params
    assert "query" in tool.parameters
    assert "top_k" in tool.parameters
