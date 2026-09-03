"""Shared Ollama-daemon helpers — base URL, model listing, required-model resolution.

These are used by the REPL (interactive missing-model offer), `nvh status --deep`
(diagnostic check + --fix), the Engine, the Ollama adapter and the setup flow.
Keep this module lightweight: a single httpx import and pure helpers, no heavy
deps. Centralizing avoids the call sites drifting from each other.
"""

from __future__ import annotations

import ipaddress
import os
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from nvh.config.settings import HiveConfig

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
# Ollama's own OLLAMA_HOST is honoured after OLLAMA_BASE_URL; it may be a
# bare "host:port" or a bind address like 0.0.0.0.
_URL_ENV_VARS = ("OLLAMA_BASE_URL", "OLLAMA_HOST")
# Names that mean "this machine" but are not addresses a client should dial:
# ``localhost`` resolves IPv6-first on some hosts and stalls every probe, and
# the wildcard binds (``0.0.0.0`` / ``::``) are what a *daemon* listens on.
# ``::1`` is not here on purpose -- it is a literal, so there is nothing to
# resolve, and a daemon bound to IPv6 loopback only does not answer on
# 127.0.0.1; it is kept as ``[::1]`` and the adapter knows it is local.
_LOOPBACK_ALIASES = frozenset({"localhost", "0.0.0.0", "::", "[::]"})


def _bracket_bare_ipv6(raw: str) -> str:
    """``"::1"`` / ``"fd00::5/ollama"`` -> ``"[::1]"`` / ``"[fd00::5]/ollama"`` (a scheme-less literal has no port)."""
    hostport, slash, path = raw.partition("/")
    try:
        is_ipv6 = ipaddress.ip_address(hostport).version == 6
    except ValueError:
        is_ipv6 = False
    return f"[{hostport}]{slash}{path}" if is_ipv6 else raw


def ollama_base_url(value: str | None = None) -> str:
    """The daemon's base URL: ``value``, else the env override, else the default.

    Loopback is spelled ``127.0.0.1`` -- ``localhost`` resolves through IPv6
    first on some hosts and adds hundreds of ms to every probe, and the
    wildcard binds ``0.0.0.0`` / ``::`` are not dialable. An IPv6 literal is
    kept and re-bracketed (``http://[::1]:11434`` stays exactly that;
    ``urlsplit`` strips the brackets and the netloc needs them back, or the
    result is the unparsable ``http://::1:11434``), and a bare ``::1`` / ``fd00::5``
    gains its brackets before the default port is added.
    """
    raw = (value or "").strip()
    if not raw:
        for var in _URL_ENV_VARS:
            raw = os.environ.get(var, "").strip()
            if raw:
                break
    if not raw:
        return DEFAULT_OLLAMA_URL
    if "://" not in raw:
        raw = f"http://{_bracket_bare_ipv6(raw)}"
    parts = urlsplit(raw)
    host = parts.hostname or "127.0.0.1"
    if host in _LOOPBACK_ALIASES:
        host = "127.0.0.1"
    try:
        port: int | str | None = parts.port
    except ValueError:  # out-of-range ports are passed through, e.g. a deliberately dead :99999
        port = parts.netloc.rpartition(":")[2]
    if ":" in host:  # an IPv6 literal: ``hostname`` dropped the brackets the netloc needs
        host = f"[{host}]"
    netloc = f"{host}:{port or 11434}"
    return urlunsplit((parts.scheme or "http", netloc, parts.path.rstrip("/"), "", ""))


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


def probe_installed_models(base_url: str | None = None, timeout: float = 2.0) -> list[str] | None:
    """Installed Ollama model tags, or None when the daemon is unreachable."""
    try:
        import httpx

        resp = httpx.get(f"{ollama_base_url(base_url)}/api/tags", timeout=timeout)
        if resp.status_code != 200:
            return None
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        return None


async def probe_installed_models_async(
    base_url: str | None = None, timeout: float = 2.0,
) -> list[str] | None:
    """Async twin of :func:`probe_installed_models` for callers on an event loop.

    Same contract — installed tags, or ``None`` when the daemon is unreachable
    or answers anything but 200 — but the wait happens in ``httpx.AsyncClient``
    so a coroutine (the API server's Wizard chat) never blocks the loop for
    up to ``timeout`` the way the blocking ``httpx.get`` would. Never raises.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{ollama_base_url(base_url)}/api/tags")
        if resp.status_code != 200:
            return None
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        return None


def list_installed_models(base_url: str | None = None) -> list[str]:
    """Return the list of installed Ollama model tags, or [] on any failure.

    Short timeout — this is used on hot paths (REPL startup banner).
    """
    return probe_installed_models(base_url) or []


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
