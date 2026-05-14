"""Tests for the QueryService scaffolding."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nvh.api.services import QueryService
from nvh.api.services.query_service import QueryService as DirectImport


def test_public_re_export() -> None:
    """QueryService is exported from nvh.api.services for stable import path."""
    assert QueryService is DirectImport


@pytest.mark.asyncio
async def test_execute_delegates_to_engine_query() -> None:
    engine = MagicMock()
    expected_response = MagicMock(name="CompletionResponse")
    engine.query = AsyncMock(return_value=expected_response)

    service = QueryService()
    result = await service.execute(
        engine,
        prompt="hello",
        provider="ollama",
        model="gemma3:4b",
        system_prompt="be brief",
        temperature=0.7,
        max_tokens=128,
    )

    assert result is expected_response
    engine.query.assert_awaited_once_with(
        prompt="hello",
        provider="ollama",
        model="gemma3:4b",
        system_prompt="be brief",
        temperature=0.7,
        max_tokens=128,
        stream=False,
    )


@pytest.mark.asyncio
async def test_execute_propagates_engine_errors() -> None:
    engine = MagicMock()
    engine.query = AsyncMock(side_effect=RuntimeError("provider down"))

    service = QueryService()
    with pytest.raises(RuntimeError, match="provider down"):
        await service.execute(
            engine,
            prompt="hello",
            provider=None,
            model=None,
            system_prompt=None,
            temperature=None,
            max_tokens=None,
        )
