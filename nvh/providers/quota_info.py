"""Provider quota information — rate limits, reset times, and upgrade links.

When a provider returns a rate limit error, this module provides
user-friendly context: what the limit is, when it resets, and how
to upgrade for higher limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


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


# Per-provider quota details
PROVIDER_QUOTAS: dict[str, QuotaInfo] = {
    "openai": QuotaInfo(
        provider="openai",
        tier="paid",
        limit_description="Pay-as-you-go with rate limits based on spending tier",
        reset_hint="Limits reset every minute. Higher spend = higher limits.",
        upgrade_url="https://platform.openai.com/settings/organization/billing",
        upgrade_hint="Add API credits at platform.openai.com/billing (min $5)",
    ),
    "anthropic": QuotaInfo(
        provider="anthropic",
        tier="paid",
        limit_description="Pay-as-you-go with tier-based rate limits",
        reset_hint="Limits reset every minute",
        upgrade_url="https://console.anthropic.com/settings/billing",
        upgrade_hint="Add API credits at console.anthropic.com",
    ),
    "groq": QuotaInfo(
        provider="groq",
        tier="free",
        limit_description="Free tier: 30 requests/min, 14,400 tokens/min",
        reset_hint="Resets every 60 seconds",
        upgrade_url="https://console.groq.com/settings/billing",
        upgrade_hint="Free tier is generous. Upgrade for higher throughput.",
    ),
    "google": QuotaInfo(
        provider="google",
        tier="free",
        limit_description="Free tier: 15 requests/min, 1M tokens/day",
        reset_hint="Per-minute limits reset every 60s. Daily limits reset at midnight PT.",
        upgrade_url="https://aistudio.google.com/apikey",
        upgrade_hint="Create a new API key in a fresh project, or enable billing.",
    ),
    "github": QuotaInfo(
        provider="github",
        tier="free",
        limit_description="Free for GitHub users: 50-150 requests/day",
        reset_hint="Daily limits reset at midnight UTC",
        upgrade_url="https://github.com/settings/tokens",
        upgrade_hint="Ensure token has 'models' permission. Free for all GitHub users.",
    ),
    "mistral": QuotaInfo(
        provider="mistral",
        tier="free",
        limit_description="Free Experiment plan: 2 requests/min",
        reset_hint="Resets every 60 seconds",
        upgrade_url="https://console.mistral.ai/billing",
        upgrade_hint="Upgrade to paid plan for higher limits",
    ),
    "cohere": QuotaInfo(
        provider="cohere",
        tier="free",
        limit_description="Trial tier with rate limits",
        reset_hint="Resets every minute",
        upgrade_url="https://dashboard.cohere.com/billing",
        upgrade_hint="Upgrade from trial for production limits",
    ),
    "deepseek": QuotaInfo(
        provider="deepseek",
        tier="paid",
        limit_description="Very cheap: $0.07/M input tokens",
        reset_hint="Limits reset every minute",
        upgrade_url="https://platform.deepseek.com/top_up",
        upgrade_hint="Add credits at platform.deepseek.com (very affordable)",
    ),
    "nvidia": QuotaInfo(
        provider="nvidia",
        tier="free",
        limit_description="1000+ free API credits, 40 requests/min",
        reset_hint="Credits don't expire. Rate limits reset every minute.",
        upgrade_url="https://build.nvidia.com/",
        upgrade_hint="Sign up for NVIDIA Developer Program for free credits",
    ),
    "cerebras": QuotaInfo(
        provider="cerebras",
        tier="free",
        limit_description="Free tier: 30 requests/min",
        reset_hint="Resets every 60 seconds",
        upgrade_url="https://cloud.cerebras.ai/",
        upgrade_hint="Free tier available with generous limits",
    ),
    "siliconflow": QuotaInfo(
        provider="siliconflow",
        tier="free",
        limit_description="Permanently free models at 1000 requests/min",
        reset_hint="Resets every 60 seconds",
        upgrade_url="https://cloud.siliconflow.cn/",
        upgrade_hint="Free models always available",
    ),
    "fireworks": QuotaInfo(
        provider="fireworks",
        tier="free",
        limit_description="Free tier available",
        reset_hint="Limits reset every minute",
        upgrade_url="https://fireworks.ai/account/billing",
        upgrade_hint="Free tier available on signup",
    ),
    "llm7": QuotaInfo(
        provider="llm7",
        tier="anonymous",
        limit_description="Anonymous: 30 requests/min. With token: 120 requests/min",
        reset_hint="Resets every 60 seconds",
        upgrade_url="https://llm7.io",
        upgrade_hint="Get a token for 4x higher limits (120 RPM)",
    ),
    "ollama": QuotaInfo(
        provider="ollama",
        tier="free",
        limit_description="Unlimited (local GPU)",
        reset_hint="No rate limits — runs on your hardware",
        upgrade_url="https://ollama.com/library",
        upgrade_hint="Pull more models: ollama pull nemotron-small",
    ),
}


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
    if info.upgrade_url:
        parts.append(f"  Upgrade: {info.upgrade_hint}")
        parts.append(f"  Link: {info.upgrade_url}")

    # What fallback was used?
    if fallback_provider:
        parts.append(f"  Routed to: {fallback_provider} (automatic fallback)")

    return "\n".join(parts)
