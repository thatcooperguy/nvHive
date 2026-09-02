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
