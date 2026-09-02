"""``ollama_base_url`` is the one place every Ollama probe gets its address."""

from __future__ import annotations

import pytest

from nvh.utils.ollama import (
    DEFAULT_OLLAMA_URL,
    list_installed_models,
    ollama_base_url,
    probe_installed_models,
)


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)


def test_default_is_loopback_by_ip(no_env):
    assert ollama_base_url() == DEFAULT_OLLAMA_URL == "http://127.0.0.1:11434"


def test_env_precedence_and_normalisation(no_env, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11435")
    assert ollama_base_url() == "http://127.0.0.1:11435"  # a bind address is not a target
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/")
    assert ollama_base_url() == "http://127.0.0.1:11434"  # localhost resolves IPv6-first and stalls
    monkeypatch.setenv("OLLAMA_BASE_URL", "https://gpu-box.example:8443/ollama")
    assert ollama_base_url() == "https://gpu-box.example:8443/ollama"


def test_explicit_value_wins(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://elsewhere:1")
    assert ollama_base_url("gpu-box") == "http://gpu-box:11434"
    assert ollama_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    # An out-of-range port is kept verbatim rather than rejected (tests use one as "down").
    assert ollama_base_url("http://localhost:99999") == "http://127.0.0.1:99999"


def test_probe_distinguishes_unreachable_from_empty():
    # Nothing listens on port 1; the probe says "unreachable", the list says "none".
    assert probe_installed_models("http://127.0.0.1:1", timeout=0.5) is None
    assert list_installed_models("http://127.0.0.1:1") == []


@pytest.mark.asyncio
async def test_async_probe_matches_the_sync_contract_without_blocking(monkeypatch):
    """``probe_installed_models_async`` is the event-loop twin: same URL, same
    verdicts (200 → tags, other status / connection error → None), never
    raises, and never touches the blocking ``httpx.get``."""
    import httpx

    from nvh.utils.ollama import probe_installed_models_async

    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("blocking httpx.get called"))
    outcome = {"status": 200}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if outcome["status"] == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(outcome["status"], json={"models": [{"name": "llama3:latest"}, {"name": "qwen:7b"}]})

    real_client = httpx.AsyncClient
    timeouts: list = []

    def client(*args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    assert await probe_installed_models_async("localhost:11434", timeout=0.7) == ["llama3:latest", "qwen:7b"]
    assert str(seen[-1].url) == "http://127.0.0.1:11434/api/tags"  # normalised like the sync probe
    assert timeouts == [0.7]
    outcome["status"] = 503
    assert await probe_installed_models_async() is None
    outcome["status"] = "refused"
    assert await probe_installed_models_async() is None


@pytest.mark.asyncio
async def test_async_probe_distinguishes_unreachable_from_empty():
    # Nothing listens on port 1: the async probe says "unreachable" too, without raising.
    from nvh.utils.ollama import probe_installed_models_async

    assert await probe_installed_models_async("http://127.0.0.1:1", timeout=0.5) is None
