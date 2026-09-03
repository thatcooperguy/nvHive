"""Tests for the Wizard tool registry + safety classes."""

from __future__ import annotations

from typing import Any

import pytest

from nvh.integrations.wizard.tools import (
    WizardTool,
    WizardToolRegistry,
    default_registry,
    format_summary,
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
    """Typos can't sneak past — only auto, confirm and privileged are allowed."""
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
    # ``privileged`` (2026-09, the Spark concierge's sudo tier) registers fine.
    reg.register(WizardTool(
        name="priv", description="privileged", safety_class="privileged", parameters={}, handler=_stub_handler,
    ))
    assert reg.get("priv").safety_class == "privileged"


def test_list_tools_orders_auto_confirm_privileged() -> None:
    """An explicit class order — not the classes' spelling — decides the catalogue."""
    reg = WizardToolRegistry()
    reg.register(WizardTool(name="a_priv", description="", safety_class="privileged", parameters={}, handler=_stub_handler))
    reg.register(_make_confirm_tool())
    reg.register(_make_auto_tool())
    assert [t.safety_class for t in reg.list_tools()] == ["auto", "confirm", "privileged"]


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
async def test_execute_confirm_tool_missing_required_argument_still_returns_card() -> None:
    """A model call that forgot a REQUIRED argument must yield the confirm
    card (with ``?`` in the summary), not a KeyError that the HTTP layer
    turns into a 500 on /v1/wizard/tools/execute."""
    reg = WizardToolRegistry()
    reg.register(_make_confirm_tool())
    result = await reg.execute("stub_confirm", arguments={})
    assert result["ok"] is False
    assert result["needs_confirmation"] is True
    assert result["summary"] == "About to act on ?."
    # No arguments at all is the same story.
    result = await reg.execute("stub_confirm")
    assert result["needs_confirmation"] is True
    assert result["summary"] == "About to act on ?."
    assert result["arguments"] == {}


def test_format_summary_never_raises() -> None:
    assert format_summary("Act on {target}.", {"target": "groq"}) == "Act on groq."
    assert format_summary("Act on {target}.", {}) == "Act on ?."
    assert format_summary("Act on {target}.", None) == "Act on ?."
    assert format_summary("{domain}.{service}", {"domain": "light"}) == "light.?"
    # Positional / attribute / index placeholders cannot be satisfied by a
    # name mapping: the raw template comes back rather than an exception.
    assert format_summary("{0} and {a.b}", {}) == "{0} and {a.b}"
    assert format_summary("{a[0]}", {"a": 3}) == "{a[0]}"
    assert format_summary("{n:d}", {"n": "not-an-int"}) == "{n:d}"
    assert format_summary("", {"x": 1}) == ""


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
    assert {"system_settings_get", "system_settings_plan", "system_settings_apply",
            "apt_install", "snap_install", "service_enable"} <= names
    # The Spark playbooks (2026-09-03): catalogue, dry run, privileged install.
    assert {"playbook_list", "playbook_plan", "playbook_install"} <= names


def test_default_registry_safety_class_distribution() -> None:
    """save_provider_key is the canonical confirm-class tool; the system-settings
    apply / installs are the privileged ones; the rest run auto."""
    reg = default_registry()
    by_name = {t.name: t for t in reg.list_tools()}
    assert by_name["refresh_models"].safety_class == "auto"
    assert by_name["repair_workspace"].safety_class == "auto"
    assert by_name["validate_provider_key"].safety_class == "auto"
    assert by_name["save_provider_key"].safety_class == "confirm"
    assert by_name["system_settings_get"].safety_class == "auto"
    assert by_name["system_settings_plan"].safety_class == "auto"
    assert by_name["playbook_list"].safety_class == "auto"
    assert by_name["playbook_plan"].safety_class == "auto"
    for name in ("system_settings_apply", "apt_install", "snap_install", "service_enable", "playbook_install"):
        assert by_name[name].safety_class == "privileged", name
        assert by_name[name].planner is not None, name
    assert {t.safety_class for t in reg.list_tools()} == {"auto", "confirm", "privileged"}


def test_default_registry_public_dicts_omit_handler() -> None:
    reg = default_registry()
    for tool in reg.list_tools():
        pub = tool.as_public_dict()
        assert "handler" not in pub
        assert "planner" not in pub
        assert "name" in pub
        assert "safety_class" in pub
        assert "parameters" in pub
        assert pub["enabled"] is True  # kill switch unset in this suite


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
