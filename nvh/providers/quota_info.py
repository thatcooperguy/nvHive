"""Provider quota information — rate limits, reset times, and upgrade links.

When a provider returns a rate limit error, this module provides
user-friendly context: what the limit is, when it resets, and how
to upgrade for higher limits.

The per-provider rows are derived from :data:`nvh.providers.specs.PROVIDER_FACTS`:
the limit text is the spec's ``free_info``, the reset and upgrade wording
comes from its tier unless the row carries its own ``reset_hint`` /
``upgrade_hint`` (Gemini's midnight-PT daily cap, NIM's non-expiring credits,
Ollama's ``nvh models pull``), and a provider with nowhere to upgrade
(Mock, Triton without a library URL) gets no upgrade sentence at all. A
provider's quota text therefore changes by editing its spec row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nvh.providers.specs import PROVIDER_FACTS, ProviderSpec


@dataclass
class QuotaInfo:
    """Human-readable quota status for a provider."""
    provider: str
    tier: str               # "free", "paid", "anonymous"
    limit_description: str  # "30 requests per minute"
    reset_hint: str         # "Resets every 60 seconds"
    upgrade_url: str        # Link to upgrade/add credits
    upgrade_hint: str       # "Add $5 credits to increase limits"
    retry_after_seconds: float | None = None


_PAID_LIMITS = "Pay-as-you-go with tier-based rate limits"

# The per-tier default sentences a spec row may override.
_RESET_BY_TIER = {
    "local": "No rate limits — runs on your hardware",
    "anonymous": "Resets every 60 seconds",
    "free": "Per-minute limits reset every 60 seconds",
    "paid": "Limits reset every minute; higher spend raises them",
}


def _upgrade_hint(spec: ProviderSpec, tier: str) -> str:
    """The row's own sentence, else the tier's — and nothing when there is no URL to point at."""
    if spec.upgrade_hint:
        return spec.upgrade_hint
    if not spec.upgrade_url:
        return ""
    if tier == "local":
        return f"Pull more models from {spec.upgrade_url}"
    if tier == "anonymous":
        return f"Get a free token at {spec.upgrade_url} for higher limits"
    if tier == "free":
        return f"Upgrade the plan at {spec.upgrade_url} for higher limits"
    return f"Add credits or raise the tier at {spec.upgrade_url}"


def _quota_for(spec: ProviderSpec) -> QuotaInfo:
    tier = "local" if spec.is_local else spec.quota_tier
    if tier == "local":
        limit = spec.free_info or "Unlimited (local)"
    elif tier == "anonymous":
        limit = spec.free_info
    elif tier == "free":
        limit = spec.free_info or "Free tier with rate limits"
    else:
        limit = f"{_PAID_LIMITS}; {spec.free_info}" if spec.free_info else _PAID_LIMITS
    return QuotaInfo(
        provider=spec.name,
        tier="free" if tier == "local" else tier,
        limit_description=limit,
        reset_hint=spec.reset_hint or _RESET_BY_TIER[tier],
        upgrade_url=spec.upgrade_url,
        upgrade_hint=_upgrade_hint(spec, tier),
    )


# Per-provider quota details, one row per spec (cloud adapters and bespoke).
PROVIDER_QUOTAS: dict[str, QuotaInfo] = {name: _quota_for(spec) for name, spec in PROVIDER_FACTS.items()}


def get_quota_info(provider: str) -> QuotaInfo:
    """Get quota details for a provider."""
    return PROVIDER_QUOTAS.get(provider, QuotaInfo(
        provider=provider,
        tier="unknown",
        limit_description="Rate limits apply",
        reset_hint="Wait and retry",
        upgrade_url="",
        upgrade_hint="Check provider documentation for rate limits",
    ))


def parse_retry_after(error_message: str) -> float | None:
    """Extract retry-after seconds from an error message."""
    # Look for "retry in X.Xs" or "retry after X seconds"
    patterns = [
        r"retry\s+(?:in|after)\s+([\d.]+)\s*s",
        r"retry_after['\"]?\s*[:=]\s*([\d.]+)",
        r"retryDelay['\"]?\s*[:=]\s*['\"]?([\d.]+)",
        r"please\s+retry\s+in\s+([\d.]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, error_message, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def format_rate_limit_message(
    provider: str,
    error_message: str,
    fallback_provider: str | None = None,
) -> str:
    """Format a user-friendly rate limit message.

    Returns a clear message explaining:
    1. What happened
    2. What the limits are
    3. When it resets
    4. How to upgrade (if applicable)
    5. What fallback was used (if any)
    """
    info = get_quota_info(provider)
    retry = parse_retry_after(error_message)

    parts = [f"Rate limited by {provider} ({info.tier} tier)"]

    # What are the limits?
    parts.append(f"  Limit: {info.limit_description}")

    # When does it reset?
    if retry:
        parts.append(f"  Resets in: {retry:.0f} seconds")
    else:
        parts.append(f"  Reset: {info.reset_hint}")

    # How to upgrade?
    if info.upgrade_hint:
        parts.append(f"  Upgrade: {info.upgrade_hint}")
    if info.upgrade_url:
        parts.append(f"  Link: {info.upgrade_url}")

    # What fallback was used?
    if fallback_provider:
        parts.append(f"  Routed to: {fallback_provider} (automatic fallback)")

    return "\n".join(parts)
