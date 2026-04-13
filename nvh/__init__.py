"""NVHive — Multi-LLM Orchestration Platform."""

__version__ = "0.27.1"

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
