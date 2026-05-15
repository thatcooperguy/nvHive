"""Tests for the Wizard tool registry + safety classes."""

from __future__ import annotations

from typing import Any

import pytest

from nvh.integrations.wizard.tools import (
    WizardTool,
    WizardToolRegistry,
    default_registry,
)

# ───────────────────────────────────────────────────────────────────────────
# Stub handlers — fast, no I/O.
# ───────────────────────────────────────────────────────────────────────────


async def _stub_handler(args: dict[str, Any]) -> dict[str, Any]:
    return {"args": args, "ran": True}


async def _stub_raises(args: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("intentional test failure")


def _make_auto_tool() -> WizardTool:
    return WizardTool(
        name="stub_auto",
        description="Stub auto tool",
        safety_class="auto",
        parameters={},
        handler=_stub_handler,
    )


def _make_confirm_tool() -> WizardTool:
    return WizardTool(
        name="stub_confirm",
        description="Stub confirm tool",
        safety_class="confirm",
        parameters={"target": {"type": "string", "required": True}},
        handler=_stub_handler,
        summary_template="About to act on {target}.",
    )


# ───────────────────────────────────────────────────────────────────────────
# Safety class enforcement
# ───────────────────────────────────────────────────────────────────────────


def test_register_rejects_never_class() -> None:
    """``never`` tools must never reach the registry — they're admin paths."""
    reg = WizardToolRegistry()
    bad = WizardTool(
        name="forbidden",
        description="should never register",
        safety_class="never",
        parameters={},
        handler=_stub_handler,
    )
    with pytest.raises(ValueError, match="never"):
        reg.register(bad)


def test_register_rejects_unknown_safety_class() -> None:
    """Typos can't sneak past — only auto + confirm are allowed."""
    reg = WizardToolRegistry()
    bad = WizardTool(
        name="weird",
        description="weird",
        safety_class="kinda-maybe",  # typo
        parameters={},
        handler=_stub_handler,
    )
    with pytest.raises(ValueError, match="safety_class"):
        reg.register(bad)


def test_register_overwrite_warns_but_succeeds(caplog: pytest.LogCaptureFixture) -> None:
    """Re-registering a name is allowed but warns so it's auditable in logs."""
    import logging

    reg = WizardToolRegistry()
    reg.register(_make_auto_tool())
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    module_logger = logging.getLogger("nvh.integrations.wizard.tools")
    handler = _Capture(level=logging.WARNING)
    module_logger.addHandler(handler)
    try:
        reg.register(_make_auto_tool())
    finally:
        module_logger.removeHandler(handler)
    assert any("Overwriting" in r.getMessage() for r in captured)


# ───────────────────────────────────────────────────────────────────────────
# Execution semantics
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_auto_tool_runs_without_confirmation() -> None:
    reg = WizardToolRegistry()
    reg.register(_make_auto_tool())
    result = await reg.execute("stub_auto", arguments={"x": 1}, confirmed=False)
    assert result["ok"] is True
    assert result["safety_class"] == "auto"
    assert result["result"]["ran"] is True


@pytest.mark.asyncio
async def test_execute_confirm_tool_requires_confirmation_flag() -> None:
    """confirm-class tools must NOT run without confirmed=True; instead
    they return a structured needs_confirmation payload."""
    reg = WizardToolRegistry()
    reg.register(_make_confirm_tool())
    result = await reg.execute("stub_confirm", arguments={"target": "groq"})
    assert result["ok"] is False
    assert result["needs_confirmation"] is True
    assert result["tool"]["name"] == "stub_confirm"
    assert result["arguments"]["target"] == "groq"
    # Summary template gets formatted with the args.
    assert "groq" in result["summary"]


@pytest.mark.asyncio
async def test_execute_confirm_tool_runs_when_confirmed() -> None:
    reg = WizardToolRegistry()
    reg.register(_make_confirm_tool())
    result = await reg.execute("stub_confirm", arguments={"target": "groq"}, confirmed=True)
    assert result["ok"] is True
    assert result["result"]["args"]["target"] == "groq"


@pytest.mark.asyncio
async def test_execute_unknown_tool_returns_error() -> None:
    reg = WizardToolRegistry()
    result = await reg.execute("nope")
    assert result["ok"] is False
    assert "Unknown" in result["error"]


@pytest.mark.asyncio
async def test_execute_handler_exception_is_caught_and_reported() -> None:
    reg = WizardToolRegistry()
    reg.register(WizardTool(
        name="raiser",
        description="raises",
        safety_class="auto",
        parameters={},
        handler=_stub_raises,
    ))
    result = await reg.execute("raiser")
    assert result["ok"] is False
    assert "intentional test failure" in result["error"]
    assert result["tool"] == "raiser"


# ───────────────────────────────────────────────────────────────────────────
# Default registry shape
# ───────────────────────────────────────────────────────────────────────────


def test_default_registry_contains_expected_tools() -> None:
    reg = default_registry()
    names = {t.name for t in reg.list_tools()}
    assert "refresh_models" in names
    assert "repair_workspace" in names
    assert "validate_provider_key" in names
    assert "save_provider_key" in names


def test_default_registry_safety_class_distribution() -> None:
    """save_provider_key is the canonical confirm-class tool; the rest run auto."""
    reg = default_registry()
    by_name = {t.name: t for t in reg.list_tools()}
    assert by_name["refresh_models"].safety_class == "auto"
    assert by_name["repair_workspace"].safety_class == "auto"
    assert by_name["validate_provider_key"].safety_class == "auto"
    assert by_name["save_provider_key"].safety_class == "confirm"


def test_default_registry_public_dicts_omit_handler() -> None:
    reg = default_registry()
    for tool in reg.list_tools():
        pub = tool.as_public_dict()
        assert "handler" not in pub
        assert "name" in pub
        assert "safety_class" in pub
        assert "parameters" in pub


@pytest.mark.asyncio
async def test_save_provider_key_requires_confirmation() -> None:
    """Stock save_provider_key is confirm-class and must refuse without ack."""
    reg = default_registry()
    result = await reg.execute(
        "save_provider_key",
        arguments={"provider": "openai", "api_key": "sk-test"},
        confirmed=False,
    )
    assert result["ok"] is False
    assert result["needs_confirmation"] is True
