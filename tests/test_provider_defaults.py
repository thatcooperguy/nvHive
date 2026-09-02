"""Shipped provider defaults: settings template vs. the server's copy.

The 0.41.1 hotfix replaced every retired model ID and removed the GitHub
Models provider (service retired 2026-07-30). Two hard-coded copies of the
defaults exist — nvh.config.settings.generate_default_config and
nvh.api.server._PROVIDER_DEFAULT_CONFIG — so these tests pin the new IDs
and keep the copies from drifting apart again.
"""

from __future__ import annotations

import pytest
import yaml

import nvh.api.server as server_module
from nvh.cli.setup import RETIRED_MODEL_RENAMES
from nvh.config.settings import generate_default_config

# Every ID the migrate table retires, plus the GitHub Models fallback that
# left with its provider (no rename target — the provider is gone).
RETIRED_MODEL_IDS = {
    old for table in RETIRED_MODEL_RENAMES.values() for old in table
} | {"meta-llama-3.1-8b-instruct"}

EXPECTED_DEFAULTS = {
    "openai": ("gpt-5.6-terra", "gpt-5.6-luna"),
    "anthropic": ("claude-sonnet-5", "claude-haiku-4-5-20251001"),
    "google": ("gemini/gemini-3.7-flash", "gemini/gemini-3.5-flash-lite"),
    "groq": ("groq/openai/gpt-oss-120b", "groq/openai/gpt-oss-20b"),
    "grok": ("xai/grok-4.6", "xai/grok-4.3"),
    "mistral": ("mistral/mistral-large-latest", "mistral/mistral-small-latest"),
    "cohere": ("command-a-03-2025", "command-r-08-2024"),
    "deepseek": ("deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"),
    "perplexity": ("perplexity/sonar-pro", "perplexity/sonar"),
    "together": ("together_ai/openai/gpt-oss-120b", "together_ai/openai/gpt-oss-20b"),
    "fireworks": (
        "fireworks_ai/accounts/fireworks/models/gpt-oss-120b",
        "fireworks_ai/accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
    ),
    "openrouter": ("openrouter/openai/gpt-oss-120b", "openrouter/openai/gpt-oss-20b"),
    "cerebras": ("cerebras/gpt-oss-120b", "cerebras/gpt-oss-120b"),
    "sambanova": ("sambanova/Meta-Llama-3.3-70B-Instruct", "sambanova/gpt-oss-120b"),
    "huggingface": ("huggingface/openai/gpt-oss-120b", "huggingface/openai/gpt-oss-20b"),
    "ai21": ("ai21_chat/jamba-large-1.7", "ai21_chat/jamba-mini-2"),
    "nvidia": ("nvidia_nim/meta/llama-3.3-70b-instruct", "nvidia_nim/meta/llama-3.1-8b-instruct"),
    "siliconflow": ("Qwen/Qwen2.5-7B-Instruct", ""),
    "llm7": ("gpt-oss", "minimax-m2.7"),
    "ollama": ("ollama/gemma3:4b", ""),
    "mock": ("mock/default", "mock/fast"),
}


def _template_advisors() -> dict[str, dict]:
    return yaml.safe_load(generate_default_config())["advisors"]


def test_template_default_and_fallback_models() -> None:
    advisors = _template_advisors()
    actual = {
        name: (block.get("default_model", ""), block.get("fallback_model", ""))
        for name, block in advisors.items()
        if name in EXPECTED_DEFAULTS
    }
    assert actual == EXPECTED_DEFAULTS


def test_template_has_no_retired_models_or_github() -> None:
    advisors = _template_advisors()
    assert "github" not in advisors
    assert "GITHUB_TOKEN" not in generate_default_config()
    for name, block in advisors.items():
        for field in ("default_model", "fallback_model"):
            assert block.get(field, "") not in RETIRED_MODEL_IDS, f"{name}.{field}"


def test_llm7_default_is_a_served_free_tier_model() -> None:
    llm7 = _template_advisors()["llm7"]
    assert llm7["enabled"] is True
    assert llm7["default_model"] == "gpt-oss"


def test_server_defaults_match_settings_template() -> None:
    advisors = _template_advisors()
    server_defaults = server_module._PROVIDER_DEFAULT_CONFIG
    assert "github" not in server_defaults
    for name, defaults in server_defaults.items():
        template = advisors[name]
        for field, value in defaults.items():
            assert template.get(field) == value, (
                f"{name}.{field}: server={value!r} template={template.get(field)!r}"
            )


def test_server_provider_maps_dropped_github() -> None:
    for mapping in (
        server_module._PROVIDER_ENV_VAR_MAP,
        server_module._PROVIDER_KEY_URLS,
        server_module._PROVIDER_DOC_URLS,
        server_module._PROVIDER_LOGO_SLUGS,
        server_module._PROVIDER_DEFAULT_CONFIG,
    ):
        assert "github" not in mapping
    assert "github" not in server_module._ALLOWED_PROVIDERS
    accepted = server_module.SaveKeyRequest(provider="groq", api_key="gsk_0123456789")
    assert accepted.provider == "groq"


def test_save_key_rejects_github() -> None:
    with pytest.raises(ValueError, match="Unknown provider 'github'"):
        server_module.SaveKeyRequest(provider="github", api_key="ghp_0123456789")
