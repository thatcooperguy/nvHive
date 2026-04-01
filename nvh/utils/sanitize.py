"""Log sanitization to prevent API key exposure in logs, error messages, and output."""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Known API key patterns (provider-specific, ordered most-specific first)
# ---------------------------------------------------------------------------

KEY_PATTERNS: list[re.Pattern[str]] = [
    # Anthropic admin key (must come before sk-ant- to avoid partial match)
    re.compile(r'sk-ant-admin[a-zA-Z0-9_-]{20,}'),
    # Anthropic
    re.compile(r'sk-ant-[a-zA-Z0-9_-]{20,}'),
    # OpenAI (project keys: sk-proj-..., service keys: sk-svc-..., standard: sk-...)
    re.compile(r'sk-[a-zA-Z0-9_-]{20,}'),
    # Google / Firebase
    re.compile(r'AIza[a-zA-Z0-9_-]{35}'),
    # Groq
    re.compile(r'gsk_[a-zA-Z0-9_-]{20,}'),
    # xAI / Grok
    re.compile(r'xai-[a-zA-Z0-9_-]{20,}'),
    # DeepSeek
    re.compile(r'dsk-[a-zA-Z0-9_-]{20,}'),
    # Mistral (keys are prefixed with a known segment in practice; match common format)
    re.compile(r'[Mm]istral[_-]?[a-zA-Z0-9_-]{20,}'),
    # Cohere v1/v2 API keys (alphanumeric, 40 chars, preceded by known field names)
    re.compile(r'(?:cohere[_-]?(?:api[_-]?)?key\s*[=:]\s*)[a-zA-Z0-9]{32,}', re.IGNORECASE),
    # Hive internal tokens
    re.compile(r'hive_[a-zA-Z0-9_-]{20,}'),
]

# NOTE: The generic long-key pattern (r'[a-zA-Z0-9]{32,}') is intentionally
# omitted from KEY_PATTERNS to avoid false positives on UUIDs, hashes, and
# other normal long strings.  If you need it, apply it only in contexts where
# you already know the value is a credential (e.g. a dedicated secrets scanner).

_REDACTED = "[REDACTED]"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sanitize(text: str) -> str:
    """Replace any detected API keys in *text* with ``[REDACTED]``.

    Only matches against the provider-specific patterns so that normal long
    strings (UUIDs, base64 blobs, etc.) are not incorrectly redacted.
    """
    for pattern in KEY_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def sanitize_dict(data: dict) -> dict:
    """Deep-sanitize a dictionary, redacting any values that look like API keys.

    Recurses into nested dicts and lists.  Keys are never modified.
    """
    result: dict = {}
    for k, v in data.items():
        if isinstance(v, str):
            result[k] = sanitize(v)
        elif isinstance(v, dict):
            result[k] = sanitize_dict(v)
        elif isinstance(v, list):
            result[k] = _sanitize_list(v)
        else:
            result[k] = v
    return result


def mask_key(key: str) -> str:
    """Return a partially-masked version of *key*: show first 4 and last 4 chars.

    Example::

        mask_key("sk-abcdefghijklmnopqrstuvwxyz1234")
        # "sk-a...1234"

    If the key is too short to mask meaningfully (< 9 chars), the whole value
    is replaced with ``[REDACTED]``.
    """
    if len(key) < 9:
        return _REDACTED
    return f"{key[:4]}...{key[-4:]}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sanitize_list(items: list) -> list:
    result = []
    for item in items:
        if isinstance(item, str):
            result.append(sanitize(item))
        elif isinstance(item, dict):
            result.append(sanitize_dict(item))
        elif isinstance(item, list):
            result.append(_sanitize_list(item))
        else:
            result.append(item)
    return result
