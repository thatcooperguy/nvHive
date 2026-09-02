"""Shared Ollama-daemon helpers — model listing, required-model resolution.

These are used by the REPL (interactive missing-model offer), `nvh status --deep`
(diagnostic check + --fix), and the setup flow. Keep this module lightweight:
a single httpx import and pure helpers, no heavy deps. Centralizing avoids
the three call sites drifting from each other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nvh.config.settings import HiveConfig


def strip_ollama_prefix(model: str) -> str:
    """Normalize a config-style Ollama model name to the tag Ollama stores.

    Config values commonly carry a ``ollama/`` prefix (a LiteLLM routing
    convention): ``ollama/llama3.2-vision`` -> ``llama3.2-vision``. Ollama's
    ``/api/tags`` endpoint returns the bare tag (sometimes with ``:latest``),
    so we strip the prefix before comparing.
    """
    if not model:
        return ""
    return model.removeprefix("ollama/")


def _tags_match(required: str, installed: str) -> bool:
    """Return True if an installed tag satisfies a required model name.

    Ollama reports tags like ``llama3.2-vision:latest``; configs often omit
    the ``:latest`` suffix. We accept a prefix match on the base tag so
    ``llama3.2-vision`` matches ``llama3.2-vision:latest`` but ``llama3.2``
    does NOT match ``llama3.2-vision`` (prevents over-matching).
    """
    required = required.strip()
    installed = installed.strip()
    if not required or not installed:
        return False
    if required == installed:
        return True
    # Split on ':' to compare base and tag separately
    req_base, _, req_tag = required.partition(":")
    inst_base, _, inst_tag = installed.partition(":")
    if req_base != inst_base:
        return False
    # Required has no explicit tag → any installed tag for that base matches.
    # Otherwise tags must match exactly.
    return req_tag == "" or req_tag == inst_tag


def list_installed_models(base_url: str = "http://localhost:11434") -> list[str]:
    """Return the list of installed Ollama model tags, or [] on any failure.

    Short timeout — this is used on hot paths (REPL startup banner).
    """
    try:
        import httpx
        resp = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=2)
        if resp.status_code != 200:
            return []
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        return []


def required_ollama_models(config: HiveConfig) -> list[str]:
    """Return the set of Ollama model tags that the config expects to exist.

    Collected from ``default_model`` + ``fallback_model`` across every
    enabled advisor whose type is ``ollama`` (or whose default_model starts
    with ``ollama/``). Tags are normalized via ``strip_ollama_prefix``.

    The result preserves first-seen order and deduplicates.
    """
    required: list[str] = []
    seen: set[str] = set()
    providers = getattr(config, "providers", None) or {}
    for _name, pconfig in providers.items():
        if not getattr(pconfig, "enabled", False):
            continue
        is_ollama = (
            getattr(pconfig, "type", "") == "ollama"
            or str(getattr(pconfig, "default_model", "")).startswith("ollama/")
        )
        if not is_ollama:
            continue
        for attr in ("default_model", "fallback_model"):
            raw = str(getattr(pconfig, attr, "") or "")
            tag = strip_ollama_prefix(raw)
            if tag and tag not in seen:
                seen.add(tag)
                required.append(tag)
    return required


def missing_models(
    required: list[str],
    installed: list[str],
) -> list[str]:
    """Return the subset of required tags that aren't present in installed."""
    return [r for r in required if not any(_tags_match(r, inst) for inst in installed)]
