"""QueryService — single-provider completion as a reusable domain operation.

Establishes the pattern for extracting HTTP-coupled business logic out of
``nvh/api/server.py``. The service:

  - Takes typed inputs (Pydantic model or plain dataclass), not a Request.
  - Calls into the engine and provider registry.
  - Raises domain exceptions on failure (``ProviderError``,
    ``BudgetExceededError``); the caller maps them to HTTPException.
  - Has no FastAPI / starlette imports, so it's reusable from the CLI,
    agent loops, background jobs, etc.

Streaming and multimodal-attachment paths still live in the route handler in
this PR — they touch private engine state (``_check_budget``, ``_log_query``)
and untangling them cleanly is its own change. The non-multimodal,
non-streaming path is migrated as a worked example.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nvh.core.engine import Engine
    from nvh.providers.base import CompletionResponse

logger = logging.getLogger(__name__)


class QueryService:
    """Execute a single-provider completion against the engine.

    The service is stateless — pass the engine in per-call rather than
    binding it at construction, so the same instance is safe to share
    across requests / threads.
    """

    async def execute(
        self,
        engine: Engine,
        *,
        prompt: str,
        provider: str | None,
        model: str | None,
        system_prompt: str | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> CompletionResponse:
        """Run a single, non-streaming, text-only completion.

        Raises:
            BudgetExceededError: When the engine's budget enforcement trips.
            ProviderError: When the selected provider rejects the request,
                is unconfigured, or fails mid-call.
        """
        return await engine.query(
            prompt=prompt,
            provider=provider,
            model=model,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
