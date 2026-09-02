"""Tests for the agent-profiles feature.

Covers:
  - Built-in profiles are always listed
  - User profiles round-trip via save → list → get
  - User profile with same name as built-in overrides it at runtime
  - delete_user_profile only removes user files, never built-ins
  - ``tools_allowed`` is normalised to ``list[str] | None`` on every load path
  - The wizard chat ``_resolve_profile_overrides`` helper appends the profile
    persona and surfaces provider/model overrides
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_home(monkeypatch, tmp_path: Path) -> None:
    """Default-resolved lookups read tmp_path, never the developer's $NVH_HOME."""
    monkeypatch.setenv("NVH_HOME", str(tmp_path))


def test_built_in_profiles_always_present(tmp_path: Path) -> None:
    from nvh.integrations.wizard.profiles import (
        BUILT_IN_PROFILES,
        list_profiles,
    )

    profiles = list_profiles(home_dir=tmp_path)
    names = {p.name for p in profiles}
    for built_in in BUILT_IN_PROFILES:
        assert built_in.name in names


def test_save_then_get_round_trip(tmp_path: Path) -> None:
    from nvh.integrations.wizard.profiles import (
        AgentProfile,
        get_profile,
        save_user_profile,
    )

    profile = AgentProfile(
        name="custom",
        title="Custom",
        description="My local profile",
        system_prompt="Be concise.",
        provider="ollama",
        model="ollama/qwen2.5:7b",
        temperature=0.3,
    )
    path = save_user_profile(profile, home_dir=tmp_path)
    assert Path(path).exists()

    fetched = get_profile("custom", home_dir=tmp_path)
    assert fetched is not None
    assert fetched.title == "Custom"
    assert fetched.system_prompt == "Be concise."
    assert fetched.provider == "ollama"
    # Saved profiles are always built_in=False even if the caller passed True.
    assert fetched.built_in is False


def test_user_profile_overrides_built_in(tmp_path: Path) -> None:
    """Saving with the same name as a built-in replaces it at runtime."""
    from nvh.integrations.wizard.profiles import (
        AgentProfile,
        get_profile,
        save_user_profile,
    )

    save_user_profile(
        AgentProfile(
            name="coder",
            title="My Coder",
            description="overridden",
            system_prompt="Pirate mode engaged.",
        ),
        home_dir=tmp_path,
    )
    coder = get_profile("coder", home_dir=tmp_path)
    assert coder is not None
    assert coder.title == "My Coder"
    assert "Pirate" in coder.system_prompt


def test_delete_user_profile_only_removes_user_files(tmp_path: Path) -> None:
    from nvh.integrations.wizard.profiles import (
        AgentProfile,
        delete_user_profile,
        list_profiles,
        save_user_profile,
    )

    save_user_profile(
        AgentProfile(name="scratch", title="Scratch", description="", system_prompt=""),
        home_dir=tmp_path,
    )
    assert delete_user_profile("scratch", home_dir=tmp_path) is True
    # Deleting again returns False, not raises.
    assert delete_user_profile("scratch", home_dir=tmp_path) is False
    # Built-ins still listed.
    names = {p.name for p in list_profiles(home_dir=tmp_path)}
    assert "coder" in names


def test_resolve_profile_overrides_no_op_for_default_wizard() -> None:
    """None and the explicit ``wizard`` pin both mean the general persona:
    no addon, no provider/model pin, no ceiling."""
    from nvh.integrations.wizard.chat import ProfileOverrides, _resolve_profile_overrides

    base = "BASE PROMPT"
    for name in (None, "wizard"):
        prof = _resolve_profile_overrides(name, None)
        assert prof == ProfileOverrides()
        assert prof.apply_to_prompt(base) == base
        assert prof.provider is None and prof.model is None and prof.cost_ceiling_usd is None


def test_resolve_profile_overrides_appends_persona_and_routes(tmp_path: Path) -> None:
    """Resolving a real profile appends its system_prompt and carries its
    provider/model preferences for the router to honor."""
    from nvh.integrations.wizard.chat import _resolve_profile_overrides
    from nvh.integrations.wizard.profiles import AgentProfile, save_user_profile

    save_user_profile(
        AgentProfile(
            name="careful",
            title="Careful Reviewer",
            description="",
            system_prompt="Be skeptical and ask one clarifying question.",
            provider="ollama",
            model="ollama/qwen2.5-coder:7b",
        ),
        home_dir=tmp_path,
    )

    prof = _resolve_profile_overrides("careful", tmp_path)
    out = prof.apply_to_prompt("BASE")
    assert "BASE" in out
    assert "Careful Reviewer" in out
    assert "skeptical" in out
    assert prof.provider == "ollama"
    assert prof.model == "ollama/qwen2.5-coder:7b"
    assert prof.cost_ceiling_usd is None
    assert prof.local_only is False


def test_resolve_profile_overrides_unknown_name_is_safe() -> None:
    from nvh.integrations.wizard.chat import ProfileOverrides, _resolve_profile_overrides

    prof = _resolve_profile_overrides("this-profile-does-not-exist", None)
    assert prof == ProfileOverrides()
    assert prof.apply_to_prompt("BASE") == "BASE"


def test_tools_allowed_is_normalised_on_every_load_path(tmp_path: Path) -> None:
    """``tools_allowed`` reaches the chat layer as ``list[str] | None`` whether
    it came from hand-written YAML (a bare string), the library JSON, or code
    (a tuple) — so the whitelist check needs no special cases."""
    from nvh.integrations.wizard.chat import _resolve_profile_overrides
    from nvh.integrations.wizard.profiles import (
        AgentProfile,
        _library_profile,
        get_profile,
        normalise_tools_allowed,
    )

    pdir = tmp_path / "agent-profiles"
    pdir.mkdir()
    (pdir / "notes.yaml").write_text(
        "title: Notes\nsystem_prompt: ''\ntools_allowed: web_search\n", encoding="utf-8",
    )
    (pdir / "open.yaml").write_text("title: Open\ntools_allowed: null\n", encoding="utf-8")

    notes = get_profile("notes", home_dir=tmp_path)
    assert notes is not None and notes.tools_allowed == ["web_search"]
    open_ = get_profile("open", home_dir=tmp_path)
    assert open_ is not None and open_.tools_allowed is None

    assert _library_profile({"name": "x", "tools_allowed": "rag_ask"}).tools_allowed == ["rag_ask"]
    assert _library_profile({"name": "y"}).tools_allowed is None
    assert AgentProfile(
        name="t", title="t", description="", system_prompt="", tools_allowed=("a", "b"),
    ).tools_allowed == ["a", "b"]

    assert normalise_tools_allowed(None) is None
    assert normalise_tools_allowed("") == []
    assert normalise_tools_allowed(" web_search ") == ["web_search"]
    assert normalise_tools_allowed({"b", "a"}) == ["a", "b"] or normalise_tools_allowed({"b", "a"}) == ["b", "a"]
    with pytest.raises(TypeError):
        normalise_tools_allowed(3)

    # The chat layer just freezes the list — no str special case left.
    assert _resolve_profile_overrides("notes", tmp_path).tools_allowed == frozenset({"web_search"})
    assert _resolve_profile_overrides("open", tmp_path).tools_allowed is None


def test_prompt_template_round_trips_and_renders(tmp_path: Path) -> None:
    """``prompt_template`` (0.42, replaces ~/.council/templates) survives the
    YAML round trip and wraps the user's input; unknown placeholders stay
    visible instead of vanishing."""
    from nvh.integrations.wizard.profiles import AgentProfile, get_profile, save_user_profile

    save_user_profile(
        AgentProfile(
            name="reviewer", title="Reviewer", description="", system_prompt="Be terse.",
            prompt_template="Review for bugs in {{lang}}:\n{{input}}",
        ),
        home_dir=tmp_path,
    )
    profile = get_profile("reviewer", home_dir=tmp_path)
    assert profile is not None
    assert profile.render_prompt("print(1)", {"lang": "python"}) == (
        "Review for bugs in python:\nprint(1)"
    )
    assert "{{lang}}" in profile.render_prompt("x")
    # {{text}} / {{code}} are accepted as aliases of {{input}} (old template vocabulary).
    assert AgentProfile(
        name="t", title="t", description="", system_prompt="", prompt_template="<{{code}}>",
    ).render_prompt("y") == "<y>"
    # Profiles without a template are pass-through.
    coder = get_profile("coder", home_dir=tmp_path)
    assert coder is not None and coder.render_prompt("hi") == "hi"


def test_library_entries_keep_prompt_template_and_max_tokens() -> None:
    """The packaged Agent Library must round-trip every AgentProfile field it
    may carry, not just the persona ones."""
    from nvh.integrations.wizard.profiles import _library_profile

    profile = _library_profile({
        "name": "lib-reviewer", "title": "Lib Reviewer", "system_prompt": "Be terse.",
        "prompt_template": "Review:\n{{input}}", "max_tokens": 512, "category": "Coding",
    })
    assert profile.built_in is True
    assert profile.prompt_template == "Review:\n{{input}}"
    assert profile.max_tokens == 512
    assert profile.render_prompt("x") == "Review:\nx"
    assert _library_profile({"name": "bare"}).prompt_template == ""


def test_apply_prompt_template_wraps_user_message_only_for_real_profiles(tmp_path: Path) -> None:
    """The template is applied from the already-resolved profile (no second
    catalog load); the general Wizard and unknown names resolve to None."""
    from nvh.integrations.wizard.chat import _apply_prompt_template, _resolve_profile
    from nvh.integrations.wizard.profiles import AgentProfile, save_user_profile

    save_user_profile(
        AgentProfile(name="wrap", title="Wrap", description="", system_prompt="",
                     prompt_template="Q: {{input}}"),
        home_dir=tmp_path,
    )
    assert _apply_prompt_template("why?", _resolve_profile("wrap", tmp_path)) == "Q: why?"
    assert _apply_prompt_template("why?", None) == "why?"
    assert _resolve_profile("wizard", tmp_path) is None
    assert _resolve_profile("missing-profile", tmp_path) is None
    # A preloaded catalog is honoured over the store.
    from nvh.integrations.wizard.profiles import list_profiles

    catalog = list_profiles(home_dir=tmp_path)
    assert _resolve_profile("wrap", tmp_path, profiles=catalog).prompt_template == "Q: {{input}}"
    assert _resolve_profile("wrap", tmp_path, profiles=()) is None


@pytest.mark.asyncio
async def test_wizard_chat_accepts_profile_argument(monkeypatch, tmp_path: Path) -> None:
    """End-to-end: passing profile="coder" still runs a valid chat turn."""
    from decimal import Decimal
    from unittest.mock import AsyncMock, MagicMock, patch

    from nvh.integrations.wizard import chat as chat_mod
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")

    fake = CompletionResponse(
        content="ok",
        model="ollama/x",
        provider="ollama",
        usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
        cost_usd=Decimal("0"),
        latency_ms=10,
        finish_reason=FinishReason.STOP,
    )
    fake_provider = MagicMock()
    fake_provider.complete = AsyncMock(return_value=fake)
    fake_decision = MagicMock()
    fake_decision.provider = "ollama"
    fake_decision.model = "ollama/x"
    fake_engine = MagicMock()
    fake_engine.initialize = AsyncMock()
    fake_engine._check_budget = AsyncMock()
    fake_engine._log_query = AsyncMock()
    fake_engine.router.route = MagicMock(return_value=fake_decision)
    fake_engine.registry.get = MagicMock(return_value=fake_provider)
    fake_engine.config.defaults.temperature = 0.7
    fake_engine.config.defaults.max_tokens = 256

    snap = {
        "gpu": {"detected": False}, "storage": {"available": False},
        "providers": [], "ollama_models": [], "recent_jobs": [],
        "receipts": {}, "vault": {},
    }

    with (
        patch("nvh.api.server.get_engine", return_value=fake_engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=snap),
    ):
        result = await chat_mod.wizard_chat(
            "review this code", profile="coder", home_dir=tmp_path,
        )

    assert result["mode"] == "llm"
