"""Free tier auto-configuration.

On first run (or when no advisors are configured), automatically enables
advisors with free tiers so users can start using NVHive immediately
without any API key setup.

The ladder — which providers count as free and the order they are tried —
is derived from the ``free_tier`` / ``free_tier_rank`` facts in
:mod:`nvh.providers.specs` (:func:`nvh.providers.specs.free_tier_ladder`);
docs/PROVIDERS.md renders the same list. Ollama (local) comes first, then
the keyed free tiers; the anonymous LLM7 tier needs no key at all.

The goal: `nvh "What is machine learning?"` should work on first run
with zero configuration if Ollama is available locally.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from nvh.providers.specs import PROVIDER_SPECS, ProviderSpec, free_tier_ladder
from nvh.utils.ollama import ollama_base_url

logger = logging.getLogger(__name__)


@dataclass
class FreeTierAdvisor:
    name: str
    env_var: str           # primary env var to check ("" when no key is taken)
    check_fn: str          # "env", "anonymous" (works without a key) or "ollama" (daemon probe)
    priority: int          # lower = preferred (used first)
    daily_limit: str       # human-readable limit description

    @property
    def alt_env_vars(self) -> list[str]:
        """Alternative env var names, from the provider spec table."""
        spec = PROVIDER_SPECS.get(self.name)
        return list(spec.env_keys) if spec else []


def _advisor(spec: ProviderSpec) -> FreeTierAdvisor:
    if spec.name == "ollama":
        check_fn = "ollama"
    elif spec.anonymous_key:
        check_fn = "anonymous"
    else:
        check_fn = "env"
    return FreeTierAdvisor(
        name=spec.name,
        env_var=spec.key_env or "",
        check_fn=check_fn,
        priority=spec.free_tier_rank,
        daily_limit=spec.free_info,
    )


# Rank order from the spec table: Ollama first, then the keyed free tiers.
FREE_TIER_ADVISORS: list[FreeTierAdvisor] = [_advisor(spec) for spec in free_tier_ladder()]


def _env_key(advisor: FreeTierAdvisor) -> str:
    """The advisor's key from its primary or any alternative env var, else ""."""
    key = os.environ.get(advisor.env_var, "") if advisor.env_var else ""
    if not key:
        for alt in advisor.alt_env_vars:
            key = os.environ.get(alt, "")
            if key:
                break
    return key


def detect_available_free_advisors() -> list[FreeTierAdvisor]:
    """Check which free-tier advisors are available right now.

    Returns advisors sorted by priority (best first).
    """
    available = []

    for advisor in FREE_TIER_ADVISORS:
        if advisor.check_fn == "ollama":
            # Check if Ollama is running locally
            try:
                import httpx
                resp = httpx.get(f"{ollama_base_url()}/api/tags", timeout=2)
                if resp.status_code == 200:
                    available.append(advisor)
            except Exception:
                pass

        elif advisor.check_fn == "anonymous":
            # Works without any key (LLM7's anonymous tier)
            available.append(advisor)

        elif advisor.check_fn == "env":
            # Check if API key is set in environment
            key = _env_key(advisor)
            if not key:
                # Check keyring
                try:
                    import keyring
                    key = keyring.get_password("nvhive", f"{advisor.name}_api_key") or ""
                except Exception:
                    pass
            if key:
                available.append(advisor)

    available.sort(key=lambda a: a.priority)
    return available


def get_best_free_advisor() -> str | None:
    """Get the name of the best available free-tier advisor.

    Returns None if nothing is available.
    """
    available = detect_available_free_advisors()
    return available[0].name if available else None


def auto_configure_free_tiers(config_dict: dict) -> dict:
    """Auto-enable free tier advisors in a config dict.

    Called during first-run config generation to enable
    any advisors that have keys already in the environment.
    """
    advisors = config_dict.get("advisors", config_dict.get("providers", {}))

    for free_advisor in FREE_TIER_ADVISORS:
        if free_advisor.name not in advisors:
            continue
        if free_advisor.check_fn in ("ollama", "anonymous"):
            # No key needed: always enable the local daemon and the anonymous tier
            advisors[free_advisor.name]["enabled"] = True
        elif free_advisor.check_fn == "env" and _env_key(free_advisor):
            advisors[free_advisor.name]["enabled"] = True
            logger.info(f"Auto-enabled {free_advisor.name} (API key found in environment)")

    return config_dict


def format_free_tier_status() -> str:
    """Format a human-readable status of free tier availability."""
    available = detect_available_free_advisors()
    if not available:
        return "No free advisors available. Run `nvh ollama` to set up local AI."

    lines = ["Available free advisors:"]
    for a in available:
        lines.append(f"  {a.name}: {a.daily_limit}")
    return "\n".join(lines)
