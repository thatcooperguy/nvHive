"""NVHive — Multi-LLM Orchestration Platform."""

__version__ = "0.39.0"


# Silence LiteLLM's chatter as early as possible. Real-rig retest
# 2026-05-22: users saw "common_utils.py:24 No module named botocore"
# and "common_utils.py:979" errors scrolling past during install.sh —
# these come from litellm's bedrock/sagemaker plugins trying to
# `import boto3` at module load. Both are SOFT-FAILURES (litellm
# catches them and falls back to "no AWS"), but litellm logs the
# failures via Python's logging module BEFORE the suppression in
# nvh.cli.main:main() runs. Suppressing here means nvHive is silent
# the moment any nvh code is imported — including module-level
# imports in provider files that pull in litellm before main() runs.
#
# This is purely noise-suppression; functionality is unchanged.
def _suppress_litellm_noise() -> None:
    try:
        import logging

        import litellm

        litellm.suppress_debug_info = True
        logging.getLogger("LiteLLM").setLevel(logging.WARNING)
        logging.getLogger("LiteLLM Router").setLevel(logging.WARNING)
        logging.getLogger("LiteLLM Proxy").setLevel(logging.WARNING)
        # The bedrock/sagemaker plugin import-failure noise lives under
        # the package's own loggers. Suppress them too.
        for _name in ("litellm.llms.bedrock", "litellm.llms.sagemaker"):
            logging.getLogger(_name).setLevel(logging.ERROR)
    except Exception:
        # If litellm isn't importable yet (very-early-startup edge),
        # the suppression in nvh.cli.main:main() will catch it.
        pass


_suppress_litellm_noise()


# SDK exports for Python usage
from nvh.sdk import (
    # User-facing API
    ask,
    ask_sync,
    # Infrastructure API — for tool builders embedding nvHive
    complete,
    complete_sync,
    convene,
    convene_sync,
    health,
    health_sync,
    poll,
    poll_sync,
    quick,
    quick_sync,
    route,
    safe,
    safe_sync,
    stream,
)

__all__ = [
    # User-facing API
    "ask", "convene", "poll", "safe", "quick",
    "ask_sync", "convene_sync", "poll_sync", "safe_sync", "quick_sync",
    # Infrastructure API
    "complete", "complete_sync", "stream",
    "route", "health", "health_sync",
    "__version__",
]
