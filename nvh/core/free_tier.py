"""Free tier auto-configuration.

On first run (or when no advisors are configured), automatically enables
advisors with free tiers so users can start using NVHive immediately
without any API key setup.

Free tiers available:
- Ollama (local)  — unlimited, free, requires NVIDIA GPU
- GitHub Models   — 50-150 req/day free for all GitHub users
- Groq            — free tier: 30 req/min, 14.4K tokens/min
- Google Gemini   — free tier: 15 req/min
- Mistral         — Free Experiment plan: 2 RPM, 1B tokens/month
- Cohere          — trial API key on signup
- NVIDIA NIM      — 1000+ free API credits on signup
- SiliconFlow     — permanently free models at 1000 RPM
- LLM7            — anonymous 30 RPM, no signup required

The goal: `nvh "What is machine learning?"` should work on first run
with zero configuration if Ollama is available locally.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FreeTierAdvisor:
    name: str
    env_var: str           # primary env var to check
    alt_env_vars: list[str]  # alternative env var names
    check_fn: str          # "env" or "ollama" (special check)
    priority: int          # lower = preferred (used first)
    daily_limit: str       # human-readable limit description


FREE_TIER_ADVISORS = [
    FreeTierAdvisor(
        name="ollama",
        env_var="",
        alt_env_vars=[],
        check_fn="ollama",
        priority=1,
        daily_limit="Unlimited (local GPU)",
    ),
    FreeTierAdvisor(
        name="github",
        env_var="GITHUB_TOKEN",
        alt_env_vars=["HIVE_GITHUB_TOKEN"],
        check_fn="env",
        priority=2,
        daily_limit="Free for all GitHub users: 50-150 req/day, frontier models",
    ),
    FreeTierAdvisor(
        name="groq",
        env_var="GROQ_API_KEY",
        alt_env_vars=["HIVE_GROQ_API_KEY"],
        check_fn="env",
        priority=3,
        daily_limit="30 req/min, 14.4K tok/min",
    ),
    FreeTierAdvisor(
        name="google",
        env_var="GOOGLE_API_KEY",
        alt_env_vars=["HIVE_GOOGLE_API_KEY", "GEMINI_API_KEY"],
        check_fn="env",
        priority=4,
        daily_limit="15 req/min free",
    ),
    FreeTierAdvisor(
        name="mistral",
        env_var="MISTRAL_API_KEY",
        alt_env_vars=["HIVE_MISTRAL_API_KEY"],
        check_fn="env",
        priority=5,
        daily_limit="Free Experiment plan: 2 RPM, 1B tokens/month",
    ),
    FreeTierAdvisor(
        name="cohere",
        env_var="COHERE_API_KEY",
        alt_env_vars=["HIVE_COHERE_API_KEY", "CO_API_KEY"],
        check_fn="env",
        priority=6,
        daily_limit="Trial tier rate limits",
    ),
    FreeTierAdvisor(
        name="nvidia",
        env_var="NVIDIA_API_KEY",
        alt_env_vars=["NIM_API_KEY", "HIVE_NVIDIA_API_KEY"],
        check_fn="env",
        priority=7,
        daily_limit="1000+ free API credits, 40 RPM, NVIDIA Developer Program",
    ),
    FreeTierAdvisor(
        name="siliconflow",
        env_var="SILICONFLOW_API_KEY",
        alt_env_vars=["HIVE_SILICONFLOW_API_KEY"],
        check_fn="env",
        priority=8,
        daily_limit="Permanently free models at 1000 RPM",
    ),
    FreeTierAdvisor(
        name="llm7",
        env_var="LLM7_API_KEY",
        alt_env_vars=["HIVE_LLM7_API_KEY"],
        check_fn="llm7",
        priority=9,
        daily_limit="Anonymous access: 30 RPM, no signup. Token: 120 RPM",
    ),
    FreeTierAdvisor(
        name="fireworks",
        env_var="FIREWORKS_API_KEY",
        alt_env_vars=["HIVE_FIREWORKS_API_KEY"],
        check_fn="env",
        priority=10,
        daily_limit="Free tier available",
    ),
    FreeTierAdvisor(
        name="cerebras",
        env_var="CEREBRAS_API_KEY",
        alt_env_vars=["HIVE_CEREBRAS_API_KEY"],
        check_fn="env",
        priority=11,
        daily_limit="Free tier: 30 req/min",
    ),
    FreeTierAdvisor(
        name="sambanova",
        env_var="SAMBANOVA_API_KEY",
        alt_env_vars=["HIVE_SAMBANOVA_API_KEY"],
        check_fn="env",
        priority=12,
        daily_limit="Free tier available",
    ),
    FreeTierAdvisor(
        name="huggingface",
        env_var="HUGGINGFACE_API_KEY",
        alt_env_vars=["HF_TOKEN", "HIVE_HUGGINGFACE_API_KEY"],
        check_fn="env",
        priority=13,
        daily_limit="Free Inference API",
    ),
    FreeTierAdvisor(
        name="ai21",
        env_var="AI21_API_KEY",
        alt_env_vars=["HIVE_AI21_API_KEY"],
        check_fn="env",
        priority=14,
        daily_limit="Free tier available",
    ),
]


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
                resp = httpx.get("http://localhost:11434/api/tags", timeout=2)
                if resp.status_code == 200:
                    available.append(advisor)
            except Exception:
                pass

        elif advisor.check_fn == "llm7":
            # LLM7 works without any key (anonymous access)
            available.append(advisor)

        elif advisor.check_fn == "env":
            # Check if API key is set in environment
            key = os.environ.get(advisor.env_var, "")
            if not key:
                for alt in advisor.alt_env_vars:
                    key = os.environ.get(alt, "")
                    if key:
                        break
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

    # Always enable Ollama if it's in the config
    if "ollama" in advisors:
        advisors["ollama"]["enabled"] = True

    # Always enable LLM7 if it's in the config (no key needed)
    if "llm7" in advisors:
        advisors["llm7"]["enabled"] = True

    # Enable any advisor that has a key available
    for free_advisor in FREE_TIER_ADVISORS:
        if free_advisor.name in advisors and free_advisor.check_fn == "env":
            key = os.environ.get(free_advisor.env_var, "")
            if not key:
                for alt in free_advisor.alt_env_vars:
                    key = os.environ.get(alt, "")
                    if key:
                        break
            if key:
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
