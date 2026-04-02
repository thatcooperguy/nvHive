"""NVHive — Multi-LLM Orchestration Platform."""

__version__ = "0.2.0"

# SDK exports for Python usage
from nvh.sdk import (
    ask,
    ask_sync,
    convene,
    convene_sync,
    poll,
    poll_sync,
    quick,
    quick_sync,
    safe,
    safe_sync,
)

__all__ = [
    "ask", "convene", "poll", "safe", "quick",
    "ask_sync", "convene_sync", "poll_sync", "safe_sync", "quick_sync",
    "__version__",
]
