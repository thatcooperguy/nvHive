"""Tests for the Wizard tool registry + safety classes."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nvh.integrations.wizard.tools import (
    IMAGE_PROFILE_REFUSAL,
    WizardTool,
    WizardToolRegistry,
    default_registry,
    format_summary,
    parameters_from_json_schema,
    verify_approval,
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
    # The sandbox bridge (2026-09-03, phase 3): the agent's shell and run_code.
    assert {"shell", "run_code"} <= names
    # The vision bridge (phase 3): the allowlisted eyes the attached images are meant for.
    assert {"analyze_image", "read_text_from_image"} <= names


def test_default_registry_safety_class_distribution() -> None:
    """save_provider_key is the canonical confirm-class tool (run_code joins it:
    Docker-only, refused in-band without Docker); the system-settings apply /
    installs and the sandbox shell are the privileged ones; the rest run auto."""
    reg = default_registry()
    by_name = {t.name: t for t in reg.list_tools()}
    assert by_name["refresh_models"].safety_class == "auto"
    assert by_name["repair_workspace"].safety_class == "auto"
    assert by_name["validate_provider_key"].safety_class == "auto"
    assert by_name["save_provider_key"].safety_class == "confirm"
    assert by_name["run_code"].safety_class == "confirm"
    assert by_name["run_code"].planner is None
    for name in ("analyze_image", "read_text_from_image"):
        assert by_name[name].safety_class == "auto", name
        assert by_name[name].planner is None, name
    assert by_name["system_settings_get"].safety_class == "auto"
    assert by_name["system_settings_plan"].safety_class == "auto"
    assert by_name["playbook_list"].safety_class == "auto"
    assert by_name["playbook_plan"].safety_class == "auto"
    for name in ("system_settings_apply", "apt_install", "snap_install", "service_enable", "playbook_install", "shell"):
        assert by_name[name].safety_class == "privileged", name
        assert by_name[name].planner is not None, name
    assert {t.safety_class for t in reg.list_tools()} == {"auto", "confirm", "privileged"}


def test_parameters_from_json_schema_is_the_one_translation() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "description": "A"},
            "b": {"type": "integer", "default": 5},
            "c": None,
            "d": {"type": ["string", "null"], "description": "nullable"},
            "e": {"anyOf": [{"type": "null"}, {"type": "integer"}]},
            "f": {"oneOf": [{"type": "string"}, {"type": "null"}]},
            "g": {"type": "null"},
        },
        "required": ["a", "d"],
    }
    assert parameters_from_json_schema(schema, {"b": "B"}) == {
        "a": {"type": "string", "description": "A", "required": True},
        "b": {"type": "integer", "description": "B", "required": False},
        "c": {"type": "string", "description": "", "required": False},
        "d": {"type": "string", "description": "nullable", "required": True},
        "e": {"type": "integer", "description": "", "required": False},
        "f": {"type": "string", "description": "", "required": False},
        "g": {"type": "string", "description": "", "required": False},
    }
    assert parameters_from_json_schema({}) == {}
    assert parameters_from_json_schema(None) == {}


@pytest.mark.asyncio
async def test_a_plans_pinned_arguments_become_the_approved_call() -> None:
    """A planner's decisions ride on the card's ``arguments`` and are what the
    token signs: the model's bare call does not verify, the pinned one does,
    and the handler sees the pins."""
    seen: list[dict[str, Any]] = []

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        seen.append(dict(args))
        return {"ok": True, "applied": False}

    async def planner(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "commands": ["x"], "pinned_arguments": {"mode": "docker"}}

    reg = WizardToolRegistry()
    reg.register(WizardTool(
        name="stub_pinned", description="pins", safety_class="privileged", parameters={},
        handler=handler, planner=planner,
    ))
    card = await reg.execute("stub_pinned", arguments={"a": 1, "mode": "host"})
    assert card["needs_confirmation"] is True and card["plan"]["pinned_arguments"] == {"mode": "docker"}
    assert card["arguments"] == {"a": 1, "mode": "docker"}  # the pin wins over the caller's value
    token = card["approval_token"]
    assert verify_approval("stub_pinned", {"a": 1}, token) is False
    refused = await reg.execute("stub_pinned", arguments={"a": 1, "mode": "host"}, confirmed=True, approval_token=token)
    assert refused["approval_required"] is True and seen == []
    out = await reg.execute("stub_pinned", arguments=card["arguments"], confirmed=True, approval_token=token)
    assert out["ok"] is True and seen == [{"a": 1, "mode": "docker"}]
    # A plan without pins (or a malformed one) changes nothing about the card.
    async def bare_planner(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "commands": ["x"], "pinned_arguments": ["not", "a", "dict"]}

    reg.register(WizardTool(
        name="stub_bare", description="no pins", safety_class="privileged", parameters={},
        handler=handler, planner=bare_planner,
    ))
    card = await reg.execute("stub_bare", arguments={"a": 1})
    assert card["arguments"] == {"a": 1}
    assert verify_approval("stub_bare", {"a": 1}, card["approval_token"]) is True


def test_wizard_registry_is_the_one_registry_class_and_never_carries_desktop_hands() -> None:
    """D2: WizardToolRegistry is a thin subclass of the core ToolRegistry; the
    Wizard instance keeps its curated set and the desktop tools (mouse_*,
    keyboard_*, scroll) that the agent registry has are never registered."""
    from nvh.core.tools import Tool, ToolRegistry

    assert issubclass(WizardToolRegistry, ToolRegistry) and issubclass(WizardTool, Tool)
    reg = default_registry()
    names = {t.name for t in reg.list_tools()}
    desktop = {"mouse_move", "mouse_click", "keyboard_type", "keyboard_press", "scroll", "capture_screenshot"}
    assert not names & desktop
    agent_names = {t.name for t in ToolRegistry(include_system=True).list_tools()}
    assert desktop <= agent_names
    # Every Wizard tool reads its parameters back in the prompt/UI shape, derived from one JSON Schema.
    for tool in reg.list_tools():
        assert tool.parameters == tool.wizard_parameters
        assert tool.input_schema.get("type") == "object"
        assert tool.as_public_dict()["parameters"] == tool.parameters


def test_wizard_tool_declared_as_json_schema_reads_back_the_wizard_shape() -> None:
    tool = WizardTool(
        name="j", description="", safety_class="auto",
        parameters={"type": "object", "properties": {"a": {"type": ["string", "null"], "description": "A"}}, "required": ["a"]},
        handler=_stub_handler,
    )
    assert tool.parameters == {"a": {"type": "string", "description": "A", "required": True}}
    assert tool.as_openai_tool()["function"]["parameters"]["required"] == ["a"]


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


# ───────────────────────────────────────────────────────────────────────────
# 0.46 adds: ask_user, remember, generate_image
# ───────────────────────────────────────────────────────────────────────────


def _fake_imagegen(monkeypatch) -> dict[str, list[dict[str, Any]]]:
    """A stand-in ``nvh.integrations.imagegen`` that records the plan / render calls.

    A finite ceiling (or ``requested="local"``) plans the local backend — the
    hosted price is unknown — and no ceiling plans the hosted one. Every name
    here is made up: nothing in this fake is a catalog fact.
    """
    calls: dict[str, list[dict[str, Any]]] = {"plan": [], "render": []}

    class ImageGenerationUnavailable(RuntimeError):  # noqa: N818 — the C1 contract's name
        pass

    def _plan(cost_ceiling_usd: float | None, requested: str) -> dict[str, Any]:
        if cost_ceiling_usd is not None or requested == "local":
            return {
                "backend": "comfyui", "reason": "", "model": "fake-local-checkpoint", "cost_usd": 0.0,
                "cost_known": True, "host": "127.0.0.1", "port": 8188,
                "summary": "local ComfyUI on 127.0.0.1:8188 · $0 · uses GPU memory while it runs",
            }
        return {
            "backend": "nvidia", "reason": "", "model": "fake-hosted", "cost_usd": None, "cost_known": False,
            "host": None, "port": None,
            "summary": "NVIDIA-hosted (fake-hosted) · cost unknown · billed to your NVIDIA account",
        }

    def plan_image_backend(*, home_dir=None, cost_ceiling_usd=None, requested="auto"):
        calls["plan"].append({"cost_ceiling_usd": cost_ceiling_usd, "requested": requested})
        return _plan(cost_ceiling_usd, requested)

    async def render_image(
        prompt, *, home_dir=None, width=1024, height=1024, backend="auto", cost_ceiling_usd=None,
        scope="wizard", timeout=180.0,
    ):
        calls["render"].append({
            "prompt": prompt, "width": width, "height": height, "backend": backend,
            "cost_ceiling_usd": cost_ceiling_usd, "scope": scope,
        })
        plan = _plan(cost_ceiling_usd, backend)
        return SimpleNamespace(
            path=f"/outputs/images/{scope}/fake.png", suffix=".png", backend=plan["backend"], model=plan["model"],
            width=width, height=height, cost_usd=plan["cost_usd"], cost_known=plan["cost_known"], latency_ms=7,
            scope=scope, filename="fake.png", url=f"/v1/outputs/images/{scope}/fake.png",
        )

    module = types.ModuleType("nvh.integrations.imagegen")
    module.ImageGenerationUnavailable = ImageGenerationUnavailable  # type: ignore[attr-defined]
    module.plan_image_backend = plan_image_backend  # type: ignore[attr-defined]
    module.render_image = render_image  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "nvh.integrations.imagegen", module)
    return calls


def test_default_registry_has_the_three_0_46_tools_and_the_class_set_is_unchanged() -> None:
    """``ask_user`` and ``remember`` are confirm-class without a planner; ``generate_image`` is
    confirm-class *with* one and never shows ``profile`` to the model — the chat pins it."""
    reg = default_registry()
    by_name = {t.name: t for t in reg.list_tools()}
    assert by_name["ask_user"].safety_class == "confirm" and by_name["ask_user"].planner is None
    assert by_name["remember"].safety_class == "confirm" and by_name["remember"].planner is None
    assert by_name["generate_image"].safety_class == "confirm" and by_name["generate_image"].planner is not None
    assert by_name["ask_user"].description.startswith(
        "Ask the user one clarifying question; the answer arrives on the next turn.",
    )
    assert by_name["ask_user"].summary_template == "Question for you: {question}"
    assert by_name["remember"].summary_template == "Save to your vault: {title}"
    assert by_name["generate_image"].summary_template == "Generate an image: {prompt}"
    assert list(by_name["ask_user"].parameters) == ["question", "choices", "allow_free_text"]
    assert by_name["ask_user"].parameters["question"]["required"] is True
    assert list(by_name["remember"].parameters) == ["title", "body", "tags"]
    image = by_name["generate_image"]
    assert list(image.parameters) == ["prompt", "width", "height", "backend"]
    assert "profile" not in image.as_public_dict()["parameters"]
    assert "profile" not in image.as_openai_tool()["function"]["parameters"]["properties"]
    assert image.input_schema["properties"]["width"]["enum"] == [512, 768, 1024]
    assert image.input_schema["properties"]["backend"]["enum"] == ["auto", "local", "nvidia"]
    assert {t.safety_class for t in reg.list_tools()} == {"auto", "confirm", "privileged"}


@pytest.mark.asyncio
async def test_ask_user_card_has_no_plan_and_its_handler_runs_nothing() -> None:
    reg = default_registry()
    card = await reg.execute("ask_user", arguments={"question": "Which disk?"})
    assert card["needs_confirmation"] is True and card["summary"] == "Question for you: Which disk?"
    assert "plan" not in card and "approval_token" not in card
    out = await reg.execute("ask_user", arguments={"question": "  Which  disk? "}, confirmed=True)
    assert out["ok"] is True
    assert out["result"] == {
        "answered_in_chat": True,
        "question": "Which disk?",
        "note": "ask_user is answered in the chat; nothing runs on the server.",
    }


@pytest.mark.asyncio
async def test_remember_refuses_approval_tokens_privileged_output_and_secret_only_bodies(
    monkeypatch, tmp_path: Path,
) -> None:
    """Exact-literal guards: the red card's secret and the sudo note a privileged outcome carries
    never become a memory; a body that is nothing but a key is refused after redaction; the word
    sudo on its own is a legitimate memory."""
    from nvh.integrations.wizard.system_settings import NEEDS_TERMINAL_HINT

    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    monkeypatch.setattr("nvh.integrations.rag.vault_bridge.index_vault_note", AsyncMock(return_value=False))
    reg = default_registry()
    for body in ("the approval_token was abc.def", f"ssh fix: {NEEDS_TERMINAL_HINT}"):
        out = await reg.execute("remember", arguments={"title": "t", "body": body}, confirmed=True)
        assert out["result"]["saved"] is False and "approval token" in out["result"]["error"], body
    out = await reg.execute(
        "remember", arguments={"title": "my key", "body": "gsk_" + "a" * 40}, confirmed=True,
    )
    assert out["result"]["saved"] is False and "nothing to remember" in out["result"]["error"]
    assert not list(tmp_path.glob("vault/Wizard Memory/*.md"))
    out = await reg.execute(
        "remember", arguments={"title": "udev", "body": "ComfyUI needs sudo for the udev rule"}, confirmed=True,
    )
    assert out["result"]["saved"] is True and out["result"]["indexed"] is False
    # init_vault seeds a doc note under Wizard Memory too, so count the records this tool wrote.
    notes = [
        p for p in tmp_path.glob("vault/Wizard Memory/*.md")
        if "Source: wizard" in p.read_text(encoding="utf-8")
    ]
    assert len(notes) == 1 and "needs sudo for the udev rule" in notes[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_remember_writes_only_under_the_process_home_vault(monkeypatch, tmp_path: Path) -> None:
    """``{title, body, tags}`` only: a model-supplied ``home_dir`` and ``category`` are dropped, the
    note lands under ``$NVH_HOME/vault/Wizard Memory/`` and nothing is created elsewhere."""
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("NVH_HOME", str(home))
    indexed = AsyncMock(return_value=True)
    monkeypatch.setattr("nvh.integrations.rag.vault_bridge.index_vault_note", indexed)
    reg = default_registry()

    out = await reg.execute("remember", arguments={
        "title": "ComfyUI lives on the second NVMe",
        "body": "ComfyUI is installed under /mnt/nvme1/comfy; models are symlinked from there.",
        "tags": ["comfyui", "paths", "wizard"],
        "home_dir": str(elsewhere),
        "category": "Decisions",
    }, confirmed=True)

    result = out["result"]
    assert result["saved"] is True and result["indexed"] is True and result["redacted"] == 0
    assert result["category"] == "Wizard Memory" and result["title"] == "ComfyUI lives on the second NVMe"
    assert result["tags"] == ["wizard", "comfyui", "paths"]
    assert isinstance(result["id"], str) and len(result["id"]) == 32
    path = Path(result["path"])
    assert path.is_file() and path.parent == home / "vault" / "Wizard Memory"
    text = path.read_text(encoding="utf-8")
    assert "Schema: nvh-memory/1" in text and "Source: wizard" in text and "Category: Wizard Memory" in text
    assert not elsewhere.exists()
    # The new note is the only file in the vault that carries the memory — nothing under Decisions/.
    hits = [p for p in (home / "vault").rglob("*.md") if "second NVMe" in p.read_text(encoding="utf-8")]
    assert hits == [path]
    # Indexed in the process home: the path as written, no home_dir forwarded.
    assert indexed.await_args.args == (result["path"],)
    assert "home_dir" not in indexed.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile", [None, "", "   ", "no-such-profile", 42],
    ids=["missing", "empty", "blank", "unknown", "not-a-string"],
)
async def test_generate_image_refuses_a_call_without_a_pinned_profile(monkeypatch, profile: Any) -> None:
    """``profile`` is pinned by the chat, not chosen by the model: anything but the general sentinel or
    a resolvable profile is a refusal — never "no ceiling" — and the paid backend is never reached,
    neither by the handler nor by the planner."""
    calls = _fake_imagegen(monkeypatch)
    reg = default_registry()
    arguments: dict[str, Any] = {"prompt": "a hero image for the lab wiki"}
    if profile is not None:
        arguments["profile"] = profile
    out = await reg.execute("generate_image", arguments=arguments, confirmed=True)
    assert out["result"] == {"ok": False, "error": IMAGE_PROFILE_REFUSAL}
    plan = await reg.plan("generate_image", arguments)
    assert plan["ok"] is False and plan["error"] == IMAGE_PROFILE_REFUSAL
    assert plan["summary"] == IMAGE_PROFILE_REFUSAL and plan["commands"] == []
    assert calls == {"plan": [], "render": []}


@pytest.mark.asyncio
async def test_generate_image_takes_the_ceiling_from_the_pinned_profile(monkeypatch) -> None:
    """The general sentinel means no ceiling (hosted path, cost unknown); a profile with
    ``max_cost_usd_per_turn`` hands its ceiling to the backend plan, which then picks local."""
    from nvh.integrations.wizard.profiles import get_profile

    calls = _fake_imagegen(monkeypatch)
    reg = default_registry()

    card = await reg.execute("generate_image", arguments={"prompt": "hero", "profile": "wizard"})
    assert card["needs_confirmation"] is True and card["summary"] == "Generate an image: hero"
    assert card["plan"]["ok"] is True and card["plan"]["backend"] == "nvidia"
    assert card["plan"]["cost_known"] is False and card["plan"]["summary"].startswith("NVIDIA-hosted")
    assert "approval_token" not in card and "privileged" not in card and "pinned_arguments" not in card["plan"]
    assert calls["plan"] == [{"cost_ceiling_usd": None, "requested": "auto"}]

    out = await reg.execute(
        "generate_image", arguments={"prompt": "hero", "profile": "wizard", "width": 768}, confirmed=True,
    )
    result = out["result"]
    assert result["ok"] is True and result["backend"] == "nvidia" and result["model"] == "fake-hosted"
    assert result["cost_usd"] is None and result["cost_known"] is False and result["latency_ms"] == 7
    assert (result["width"], result["height"]) == (768, 1024)
    assert result["url"] == "/v1/outputs/images/wizard/fake.png" and result["path"].endswith("fake.png")
    assert result["note"] == f"saved to {result['path']}; nvHive does not prune this folder"
    assert calls["render"][-1]["cost_ceiling_usd"] is None and calls["render"][-1]["scope"] == "wizard"

    # The built-in researcher carries a per-turn ceiling: the plan and the render both see it.
    ceiling = get_profile("researcher").max_cost_usd_per_turn
    assert ceiling is not None and ceiling > 0
    plan = await reg.plan("generate_image", {"prompt": "hero", "profile": "researcher", "backend": "auto"})
    assert plan["backend"] == "comfyui" and plan["cost_known"] is True and plan["cost_usd"] == 0.0
    assert calls["plan"][-1] == {"cost_ceiling_usd": ceiling, "requested": "auto"}
    out = await reg.execute("generate_image", arguments={"prompt": "hero", "profile": "researcher"}, confirmed=True)
    assert out["result"]["backend"] == "comfyui" and calls["render"][-1]["cost_ceiling_usd"] == ceiling

    # Sizes are the three the schema lists; the model's spelling of the backend passes through.
    bad = await reg.execute(
        "generate_image", arguments={"prompt": "hero", "profile": "wizard", "width": 2048}, confirmed=True,
    )
    assert bad["result"]["ok"] is False and bad["result"]["error"] == "width must be one of 512, 768, 1024"
    out = await reg.execute(
        "generate_image", arguments={"prompt": "hero", "profile": "wizard", "backend": "local"}, confirmed=True,
    )
    assert out["result"]["backend"] == "comfyui" and calls["render"][-1]["backend"] == "local"


@pytest.mark.asyncio
async def test_generate_image_resolves_a_pinned_profile_in_the_pinned_home(monkeypatch, tmp_path) -> None:
    """A profile that exists only under the request's ``home_dir`` (``POST /v1/wizard/chat {home_dir}``):
    the chat pins ``profile_home`` beside ``profile`` and both the plan and the run resolve the ceiling
    there; without the pin the process home is read, where the profile is unknown (a refusal)."""
    from nvh.integrations.wizard.profiles import AgentProfile, save_user_profile
    from nvh.integrations.wizard.tools import _image_profile_ceiling, _plan_generate_image

    req_home = tmp_path / "mnt-vol"
    save_user_profile(AgentProfile(
        name="artist", title="Artist", description="", system_prompt="", max_cost_usd_per_turn=0.05,
    ), home_dir=req_home)
    assert _image_profile_ceiling("artist") == (False, None)
    assert _image_profile_ceiling("artist", home_dir=str(req_home)) == (True, 0.05)
    assert _image_profile_ceiling("artist", home_dir="") == (False, None)
    assert _image_profile_ceiling("wizard", home_dir=str(req_home)) == (True, None)

    calls = _fake_imagegen(monkeypatch)
    plan = await _plan_generate_image({"prompt": "hero", "profile": "artist", "profile_home": str(req_home)})
    assert plan["ok"] is True and plan["backend"] == "comfyui"
    assert calls["plan"] == [{"cost_ceiling_usd": 0.05, "requested": "auto"}]
    refused = await _plan_generate_image({"prompt": "hero", "profile": "artist"})
    assert refused["ok"] is False and refused["error"] == IMAGE_PROFILE_REFUSAL
    reg = default_registry()
    out = await reg.execute(
        "generate_image", arguments={"prompt": "hero", "profile": "artist", "profile_home": str(req_home)}, confirmed=True,
    )
    assert out["result"]["ok"] is True and calls["render"][-1]["cost_ceiling_usd"] == 0.05


@pytest.mark.asyncio
async def test_generate_image_reports_an_unavailable_backend_in_plain_words(monkeypatch) -> None:
    calls = _fake_imagegen(monkeypatch)
    module = sys.modules["nvh.integrations.imagegen"]

    async def unavailable(prompt, **kwargs):
        raise module.ImageGenerationUnavailable("No image backend: set NVAPI_KEY or start ComfyUI.")

    monkeypatch.setattr(module, "render_image", unavailable)
    reg = default_registry()
    out = await reg.execute("generate_image", arguments={"prompt": "hero", "profile": "wizard"}, confirmed=True)
    assert out["result"] == {"ok": False, "error": "No image backend: set NVAPI_KEY or start ComfyUI."}
    assert calls["render"] == []
