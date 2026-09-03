"""Tests for AgentProfile enforcement on the Wizard chat path.

Covers the profile fields ``wizard_chat`` / ``wizard_chat_stream`` enforce,
and the concierge wave's binding rules (docs/proposals/SPARK_CONCIERGE_2026-09.md):

  - ``tools_allowed`` filters the tool catalog in the system prompt and gates
    ``_run_auto_tool`` (disallowed tools are refused, never executed, never
    counted as an executed tool, and dropped from ``confirm_required`` /
    ``tool_calls``). A specialist the *concierge* chose keeps the general
    Wizard's read-only core auto tools (``diagnose``, ``refresh_models``,
    ``rag_ask_vault`` — never ``web_search`` / ``repair_workspace``); a
    ``strict-tools`` profile (the core ``vault-rag``) and an explicit pin
    keep their whitelist exactly.
  - A profile's provider/model pin is advisory: honoured when the provider is
    *available* — registered and, for Ollama, answering a cached ``/api/tags``
    probe (``setup_from_config`` registers Ollama from config whether or not
    the daemon runs, so ``registry.has`` alone is not enough) — (a provider
    pinned without a model gets *its own* model from a provider-constrained
    route, never the router's provider-specific pick), otherwise the router's
    decision stands and the routing reason says so — except ``local-only``
    profiles: pinned, they decline deterministically instead of running on a
    cloud provider; chosen by the concierge, the turn is demoted to the
    general Wizard and ``profile_reason`` says why. Refusal and demotion note
    say *which* problem it is — "not running" (start it) vs "not configured"
    (enable it) — from the probe's own verdict.
  - The probe is async (``httpx.AsyncClient``; the blocking ``httpx.get`` is
    never called and other coroutines keep running while it waits), a
    negative answer is cached only :data:`LOCAL_PROBE_NEGATIVE_TTL_S` (a
    positive one :data:`LOCAL_PROBE_TTL_S`), and the cache is dropped when
    ``refresh_models`` / ``repair_workspace`` actually run or a completion on
    the local provider fails.
  - The router's local-first pick of a *registered but down* Ollama never
    serves a turn: both paths re-route to the best registered cloud provider
    with its own model and ``routing_reason`` records
    ``local provider unreachable, using <provider>``; with nothing else
    registered the decision stands and the turn falls back as before.
  - ``temperature`` / ``max_tokens`` reach ``provider.complete`` and
    ``provider.stream``; ``None`` inherits the engine default.
  - Auto-class calls the loop did not run (``max_iterations=1``, cost ceiling)
    are reported in ``deferred_tool_calls`` with a reason, never in
    ``confirm_required``; refusals are emitted as ``tool_result`` events.
  - The streaming path emits ``confirm_required`` once, before ``done`` — or
    before ``error`` when a later iteration fails.
  - The streaming ``done`` event carries the cost reported on the final
    ``StreamChunk`` and enforces ``max_cost_usd_per_turn``.
  - The registry and the profile catalog are built once per turn; history
    (with ``used_profile``) reaches the concierge unchanged and attribution
    is persisted.
  - Deterministic fallbacks (no engine, LLM error, stream error) are
    attributed to no specialist in both the envelope and the persisted
    wizard-meta, carry ``fallback_reason``, and keep the tools an earlier
    iteration executed; the stream's ``error`` event has the same shape.
    ``profile_reason`` is independent of attribution: the concierge's reason
    (a demotion note included) travels on every path; ``None`` only for an
    explicit pin, where no selection ran.

Engines / providers are the same MagicMock doubles used by
tests/test_wizard_chat_stream.py and tests/test_wizard_iteration_cap.py. The
tool registry is a real ``WizardToolRegistry`` with counting stub handlers,
swapped in via ``default_registry`` so nothing touches the workspace. Every
test is hermetic: ``NVH_HOME`` points at ``tmp_path``, turns whose routing
matters pin a saved profile, and the local-daemon probe is a double
(``local_probe``) so nothing touches the network.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from nvh.integrations.wizard.tools import WizardTool, WizardToolRegistry

_EMPTY = {
    "gpu": {"detected": False},
    "storage": {"available": False},
    "providers": [],
    "ollama_models": [],
    "recent_jobs": [],
    "receipts": {},
    "vault": {},
}

_WEB = 'TOOL_CALL: {"name": "web_search", "arguments": {"query": "gpus"}}'
_REFRESH = 'TOOL_CALL: {"name": "refresh_models", "arguments": {}}'
_SAVE_KEY = 'TOOL_CALL: {"name": "save_provider_key", "arguments": {"provider": "groq", "api_key": "gsk_x"}}'
_DIAGNOSE = 'TOOL_CALL: {"name": "diagnose", "arguments": {}}'
_APPLY = 'TOOL_CALL: {"name": "system_settings_apply", "arguments": {"setting": "enable_ssh"}}'
_FULL_CATALOG = ["diagnose", "refresh_models", "save_provider_key", "system_settings_apply", "web_search"]

_LOCAL_ONLY_REASON = "profile_local_only_provider_unavailable"


# ───────────────────────────────────────────────────────────────────────────
# Doubles
# ───────────────────────────────────────────────────────────────────────────


class _Counter:
    """Async tool handler that records every invocation."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(args)
        return {"ran": True}


@pytest.fixture()
def fake_registry(monkeypatch) -> tuple[WizardToolRegistry, dict[str, _Counter]]:
    """Three auto tools (two of them core: diagnose, refresh_models), one
    confirm tool and one privileged tool, all with counting handlers."""
    counters = {
        name: _Counter()
        for name in ("web_search", "refresh_models", "diagnose", "save_provider_key", "system_settings_apply")
    }
    reg = WizardToolRegistry()
    reg.register(WizardTool(
        name="web_search", description="Search the web.", safety_class="auto",
        parameters={"query": {"type": "string"}}, handler=counters["web_search"],
    ))
    reg.register(WizardTool(
        name="refresh_models", description="Refresh the model list.", safety_class="auto",
        parameters={}, handler=counters["refresh_models"],
    ))
    reg.register(WizardTool(
        name="diagnose", description="Read-only workspace diagnostics.", safety_class="auto",
        parameters={}, handler=counters["diagnose"],
    ))
    reg.register(WizardTool(
        name="save_provider_key", description="Persist an API key.", safety_class="confirm",
        parameters={"provider": {}, "api_key": {}}, handler=counters["save_provider_key"],
    ))
    async def _plan_stub(args: dict[str, Any]) -> dict[str, Any]:
        """The dry run the red card shows; runs nothing."""
        return {"ok": True, "setting": args.get("setting"), "commands": ["sudo systemctl enable --now ssh"], "sudo": True}

    reg.register(WizardTool(
        name="system_settings_apply", description="Apply a system setting (sudo).", safety_class="privileged",
        parameters={"setting": {}}, handler=counters["system_settings_apply"], planner=_plan_stub,
    ))
    monkeypatch.setattr("nvh.integrations.wizard.tools.default_registry", lambda: reg)
    return reg, counters


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path: Path) -> None:
    """No vault recall; the profile store and plugin dir resolve to tmp_path so
    no test reads the developer's real $NVH_HOME."""
    monkeypatch.setenv("NVH_WIZARD_AUTOFOLD_VAULT", "0")
    monkeypatch.setenv("NVH_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def local_probe(monkeypatch) -> AsyncMock:
    """The local daemon answers by default and no test hits the network.

    Yields the probe double (``chat._probe_local_provider``, a coroutine) so
    a test can make Ollama unreachable (``return_value = False``), erroring
    (``side_effect = ...``) or count probes. ``probe.real`` is the unpatched
    coroutine for the tests that exercise the actual HTTP probe against an
    in-process fake daemon. The reachability cache is empty before and after
    every test.

    Sits on top of ``tests/conftest.py``'s autouse ``_hermetic_local_probe``
    (set up before this module-level fixture, torn down after it): that
    double is already installed here, so the genuine coroutine is what *it*
    carries on ``.real``. This fixture's double is the one the tests see.
    """
    from nvh.integrations.wizard import chat as chat_mod

    chat_mod._reset_local_probe_cache()
    current = chat_mod._probe_local_provider
    probe = AsyncMock(return_value=True)
    probe.real = getattr(current, "real", current)
    monkeypatch.setattr(chat_mod, "_probe_local_provider", probe)
    yield probe
    chat_mod._reset_local_probe_cache()


def _engine_for(provider: MagicMock, *, registered: bool = True) -> MagicMock:
    decision = MagicMock()
    decision.provider = "ollama"
    decision.model = "ollama/x"
    decision.reason = "local-first"
    engine = MagicMock()
    engine.initialize = AsyncMock()
    engine._check_budget = AsyncMock()
    engine._log_query = AsyncMock()
    engine.router.route = MagicMock(return_value=decision)
    engine.registry.get = MagicMock(return_value=provider)
    engine.registry.has = MagicMock(return_value=registered)
    engine.config.defaults.temperature = 0.7
    engine.config.defaults.max_tokens = 256
    return engine


def _complete_engine(contents: list[str], *, registered: bool = True) -> MagicMock:
    """Engine whose ``provider.complete`` returns each content in turn."""
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    responses = [
        CompletionResponse(
            content=c, model="ollama/x", provider="ollama",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"), latency_ms=1, finish_reason=FinishReason.STOP,
        )
        for c in contents
    ]
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=responses)
    return _engine_for(provider, registered=registered)


def _stream_engine(iterations: list[Any], *, registered: bool = True) -> MagicMock:
    """Engine whose ``provider.stream`` yields one chunk list per iteration.

    Items are token strings (wrapped in ``MagicMock(delta=...)`` like the
    existing stream tests) or ready-made ``StreamChunk`` objects, so a test
    can attach usage/cost to the final chunk the way real providers do. An
    iteration given as an exception instance is raised when requested.
    """
    def _chunks(items: list[Any]):
        async def _gen():
            for item in items:
                yield MagicMock(delta=item) if isinstance(item, str) else item
        return _gen()

    calls = {"i": 0}

    def stream_fn(**kwargs):
        i = calls["i"]
        calls["i"] += 1
        item = iterations[i] if i < len(iterations) else [""]
        if isinstance(item, BaseException):
            raise item
        return _chunks(item)

    provider = MagicMock()
    provider.stream = MagicMock(side_effect=stream_fn)
    return _engine_for(provider, registered=registered)


@contextmanager
def _patched(engine: MagicMock):
    with (
        patch("nvh.api.server.get_engine", return_value=engine),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        yield


def _save_profile(tmp_path: Path, name: str, **fields: Any) -> str:
    from nvh.integrations.wizard.profiles import AgentProfile, save_user_profile

    fields.setdefault("system_prompt", "")
    save_user_profile(
        AgentProfile(name=name, title=name.title(), description="", **fields),
        home_dir=tmp_path,
    )
    return name


def _route_auto_to(monkeypatch, name: str, reason: str = "matched 'broken'") -> dict[str, Any]:
    """Make the concierge pick ``name`` for every auto turn; pins pass through.

    Returns the dict the fake fills with the keyword arguments it received.
    """
    from nvh.integrations.wizard.concierge import SpecialistChoice

    seen: dict[str, Any] = {}

    def fake_resolve(requested, question, **kw):
        seen.update(kw)
        if requested is not None and requested.strip().lower() not in ("", "auto"):
            return requested, None
        return name, SpecialistChoice(name, f"{name}: {reason}", 0.9)

    monkeypatch.setattr("nvh.integrations.wizard.concierge.resolve_auto_profile", fake_resolve)
    return seen


def _catalog_spy(monkeypatch) -> list[list[str]]:
    """Record the tool names handed to build_system_prompt per turn."""
    from nvh.integrations.wizard import personality

    real_build = personality.build_system_prompt
    seen: list[list[str]] = []

    def spy(context, tools=None, **kw):
        seen.append(sorted(t["name"] for t in (tools or [])))
        return real_build(context, tools=tools, **kw)

    monkeypatch.setattr("nvh.integrations.wizard.personality.build_system_prompt", spy)
    return seen


def _wizard_meta(content: str) -> dict[str, Any]:
    m = re.search(r"<!-- wizard-meta: (\{.*\}) -->", content, re.DOTALL)
    assert m, content
    return json.loads(m.group(1))


async def _run(engine: MagicMock, *, stream: bool, **kw: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Run one turn on either path; returns ``(result_or_done, stream_events)``."""
    from nvh.integrations.wizard import chat as chat_mod

    with _patched(engine):
        if stream:
            events = [e async for e in chat_mod.wizard_chat_stream(**kw)]
            done = events[-1] if events and events[-1]["type"] == "done" else None
            return done, events
        return await chat_mod.wizard_chat(**kw), []


# ───────────────────────────────────────────────────────────────────────────
# Profile resolution helpers
# ───────────────────────────────────────────────────────────────────────────


def test_resolve_profile_overrides_exposes_fields_and_defaults(tmp_path: Path) -> None:
    from nvh.integrations.wizard.chat import ProfileOverrides, _resolve_profile_overrides

    name = _save_profile(
        tmp_path, "tuned", temperature=0.11, max_tokens=77, tools_allowed=["web_search"],
        provider="groq", model="groq/x", max_cost_usd_per_turn=0.5,
    )

    prof = _resolve_profile_overrides(name, tmp_path)
    assert prof.profile_name == name
    assert prof.temperature == 0.11
    assert prof.max_tokens == 77
    assert prof.tools_allowed == frozenset({"web_search"})
    assert (prof.provider, prof.model, prof.cost_ceiling_usd) == ("groq", "groq/x", 0.5)
    assert prof.local_only is False
    assert prof.apply_to_prompt("BASE") == "BASE"  # empty system_prompt → nothing appended

    # Default / unknown profiles: no overrides, no whitelist.
    for missing in (None, "wizard", "no-such-profile"):
        assert _resolve_profile_overrides(missing, tmp_path) == ProfileOverrides()


def test_resolve_profile_overrides_uses_a_preloaded_catalog(tmp_path: Path) -> None:
    """The chat paths load the catalog once and pass it in; the store is not
    re-read for the lookup."""
    from nvh.integrations.wizard import profiles as profiles_mod
    from nvh.integrations.wizard.chat import _resolve_profile_overrides

    name = _save_profile(tmp_path, "tuned", temperature=0.11)
    catalog = profiles_mod.list_profiles(home_dir=tmp_path)

    with patch(
        "nvh.integrations.wizard.profiles.list_profiles",
        side_effect=AssertionError("profile store re-read"),
    ):
        assert _resolve_profile_overrides(name, tmp_path, profiles=catalog).temperature == 0.11
        assert _resolve_profile_overrides("absent", tmp_path, profiles=catalog).profile_name is None


def test_with_core_tools_widens_a_whitelist_and_leaves_none_alone() -> None:
    from nvh.integrations.wizard.chat import WIZARD_CORE_AUTO_TOOLS, ProfileOverrides

    # Read-only / diagnostic only: the union must never re-enable a tool a
    # restriction-defined persona exists to forbid.
    assert WIZARD_CORE_AUTO_TOOLS == frozenset({"diagnose", "refresh_models", "rag_ask_vault"})
    assert not WIZARD_CORE_AUTO_TOOLS & {"web_search", "repair_workspace", "save_provider_key", "rag_ingest"}
    narrow = ProfileOverrides(profile_name="p", tools_allowed=frozenset({"rag_ask"}))
    assert narrow.with_core_tools().tools_allowed == frozenset({"rag_ask"}) | WIZARD_CORE_AUTO_TOOLS
    assert narrow.tools_allowed == frozenset({"rag_ask"})  # immutable original
    assert ProfileOverrides().with_core_tools().tools_allowed is None
    # strict-tools disables the union entirely.
    strict = ProfileOverrides(profile_name="p", tools_allowed=frozenset({"rag_ask"}), strict_tools=True)
    assert strict.with_core_tools() is strict
    assert strict.with_core_tools().tools_allowed == frozenset({"rag_ask"})


def test_strict_tools_tag_is_read_from_the_profile(tmp_path: Path) -> None:
    """The core vault-rag built-in ships with the tag; a saved profile can set it."""
    from nvh.integrations.wizard.chat import STRICT_TOOLS_TAG, _resolve_profile_overrides

    assert STRICT_TOOLS_TAG == "strict-tools"
    vault = _resolve_profile_overrides("vault-rag", tmp_path)
    assert vault.strict_tools is True and vault.tools_allowed == frozenset({"rag_ask_vault"})
    assert vault.with_core_tools().tools_allowed == frozenset({"rag_ask_vault"})
    name = _save_profile(tmp_path, "locked", tools_allowed=["web_search"], tags=["strict-tools"])
    assert _resolve_profile_overrides(name, tmp_path).strict_tools is True
    assert _resolve_profile_overrides("coder", tmp_path).strict_tools is False


# ───────────────────────────────────────────────────────────────────────────
# tools_allowed — catalog filtering + execution gate
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whitelist_filters_prompt_tool_catalog(tmp_path: Path, fake_registry, monkeypatch) -> None:
    """Only whitelisted tools reach build_system_prompt; the general Wizard
    still sees the whole registry."""
    from nvh.integrations.wizard import chat as chat_mod

    name = _save_profile(tmp_path, "notes-only", tools_allowed=["refresh_models"])
    seen = _catalog_spy(monkeypatch)

    engine = _complete_engine(["ok", "ok"])
    with _patched(engine):
        await chat_mod.wizard_chat("hi", profile=name, home_dir=tmp_path)
        await chat_mod.wizard_chat("hi", profile="wizard", home_dir=tmp_path)

    assert seen[0] == ["refresh_models"]
    assert seen[1] == _FULL_CATALOG
    # The rendered prompt handed to the provider lists only the whitelisted tool.
    first_prompt = engine.registry.get().complete.call_args_list[0].kwargs["system_prompt"]
    assert "• refresh_models [auto]" in first_prompt
    assert "• web_search [auto]" not in first_prompt
    assert "• save_provider_key [confirm]" not in first_prompt


@pytest.mark.asyncio
async def test_run_auto_tool_refuses_disallowed_tool_and_never_executes(fake_registry) -> None:
    from nvh.integrations.wizard.chat import _run_auto_tool

    _reg, counters = fake_registry

    refused = await _run_auto_tool(
        "web_search", {"query": "x"},
        tools_allowed=frozenset({"refresh_models"}), profile_name="notes-only",
    )
    assert refused == {
        "ok": False,
        "error": "tool 'web_search' is not allowed for profile 'notes-only'",
        "not_allowed": True,
    }
    assert counters["web_search"].calls == []

    allowed = await _run_auto_tool(
        "refresh_models", {}, tools_allowed=frozenset({"refresh_models"}), profile_name="notes-only",
    )
    assert allowed["ok"] is True
    assert counters["refresh_models"].calls == [{}]

    # No whitelist (default Wizard) keeps the all-tools behaviour.
    assert (await _run_auto_tool("web_search", {"query": "y"}))["ok"] is True
    assert counters["web_search"].calls == [{"query": "y"}]

    # Unknown-tool handling is untouched, whitelist or not.
    assert (await _run_auto_tool("nope", {}, tools_allowed=frozenset(), profile_name="p")) == {
        "ok": False, "error": "unknown tool: nope",
    }

    # A forbidden confirm-class tool is a refusal, not a "confirm me" deferral.
    forbidden = await _run_auto_tool(
        "save_provider_key", {"provider": "groq"},
        tools_allowed=frozenset({"web_search"}), profile_name="search-only",
    )
    assert forbidden["not_allowed"] is True and "deferred_to_user" not in forbidden
    assert counters["save_provider_key"].calls == []


@pytest.mark.asyncio
async def test_run_auto_tool_uses_the_turn_registry_without_rebuilding(fake_registry, monkeypatch) -> None:
    """``registry=`` is the turn's registry; the ~140 ms default_registry()
    build happens only when a caller omits it."""
    from nvh.integrations.wizard.chat import _run_auto_tool

    reg, counters = fake_registry

    def boom():
        raise AssertionError("registry rebuilt")

    monkeypatch.setattr("nvh.integrations.wizard.tools.default_registry", boom)

    assert (await _run_auto_tool("refresh_models", {}, registry=reg))["ok"] is True
    assert counters["refresh_models"].calls == [{}]
    # Omitting it still tries to build one — and reports honestly when that fails.
    assert await _run_auto_tool("refresh_models", {}) == {"ok": False, "error": "tool registry unavailable"}
    assert counters["refresh_models"].calls == [{}]


@pytest.mark.asyncio
async def test_chat_refuses_disallowed_auto_tool_and_stops_iterating(tmp_path: Path, fake_registry) -> None:
    """A refusal is recorded in tool_results and never counts as an executed
    tool: the loop does not pay for another completion to react to it."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    name = _save_profile(tmp_path, "vault-only", tools_allowed=["rag_ask_vault"])
    engine = _complete_engine([f"Searching.\n{_WEB}", "never reached"])

    with _patched(engine):
        result = await chat_mod.wizard_chat("latest gpus?", profile=name, home_dir=tmp_path)

    assert result["iterations"] == 1
    assert engine.registry.get().complete.await_count == 1
    assert result["answer"] == "Searching."
    assert result["tool_calls"] == [] and result["deferred_tool_calls"] == []
    assert [r["name"] for r in result["tool_results"]] == ["web_search"]
    assert result["tool_results"][0]["result"]["not_allowed"] is True
    assert counters["web_search"].calls == []


@pytest.mark.asyncio
async def test_chat_max_iterations_one_splits_confirm_deferred_and_refused(
    tmp_path: Path, fake_registry,
) -> None:
    """Unexecuted calls land in three buckets: confirm-class → tool_calls,
    auto-class → deferred_tool_calls (with the reason), outside the
    whitelist → a refusal in tool_results. Nothing runs."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    name = _save_profile(tmp_path, "refresh-and-save", tools_allowed=["refresh_models", "save_provider_key"])
    engine = _complete_engine([f"Doing three things.\n{_WEB}\n{_REFRESH}\n{_SAVE_KEY}"])

    with _patched(engine):
        result = await chat_mod.wizard_chat(
            "go", profile=name, home_dir=tmp_path, max_iterations=1,
        )

    assert result["iterations"] == 1
    assert [c["name"] for c in result["tool_calls"]] == ["save_provider_key"]
    assert result["deferred_tool_calls"] == [
        {"name": "refresh_models", "arguments": {}, "reason": chat_mod.DEFER_MAX_ITERATIONS},
    ]
    assert [r["name"] for r in result["tool_results"]] == ["web_search"]
    assert result["tool_results"][0]["result"]["not_allowed"] is True
    assert all(c.calls == [] for c in counters.values())


@pytest.mark.asyncio
async def test_run_auto_tool_defers_a_privileged_call_and_split_buckets_it_as_confirm(fake_registry) -> None:
    """``privileged`` is not ``auto``: ``_run_auto_tool`` hands it to the UI
    (``confirmed=True`` is only ever passed for auto tools) and
    ``_split_by_safety_class`` puts it in the confirm bucket, never the
    deferred-auto one."""
    from nvh.integrations.wizard.chat import _run_auto_tool, _split_by_safety_class

    reg, counters = fake_registry
    deferred = await _run_auto_tool("system_settings_apply", {"setting": "enable_ssh"}, registry=reg)
    assert deferred["deferred_to_user"] is True and deferred["safety_class"] == "privileged"
    assert deferred["ok"] is False
    assert counters["system_settings_apply"].calls == []

    call = {"name": "system_settings_apply", "arguments": {"setting": "enable_ssh"}}
    confirm, auto = _split_by_safety_class([call, {"name": "refresh_models", "arguments": {}}], reg)
    assert confirm == [call]
    assert auto == [{"name": "refresh_models", "arguments": {}}]

    # The whitelist still beats the safety class: a forbidden privileged tool
    # is a refusal, not a red card.
    forbidden = await _run_auto_tool(
        "system_settings_apply", {"setting": "enable_ssh"},
        tools_allowed=frozenset({"refresh_models"}), profile_name="notes-only", registry=reg,
    )
    assert forbidden["not_allowed"] is True and "deferred_to_user" not in forbidden
    assert counters["system_settings_apply"].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("max_iterations", [None, 1])
async def test_privileged_call_lands_in_tool_calls_never_in_deferred_or_executed(
    tmp_path: Path, fake_registry, stream: bool, max_iterations: int | None,
) -> None:
    """On both paths, with the loop free to follow up or capped at one
    iteration, a privileged TOOL_CALL is surfaced in ``tool_calls`` (the
    confirm-card bucket), never in ``deferred_tool_calls`` and never run."""
    _reg, counters = fake_registry
    if stream:
        engine = _stream_engine([["Enabling SSH.\n", _APPLY + "\n"], ["never reached"]])
    else:
        engine = _complete_engine([f"Enabling SSH.\n{_APPLY}", "never reached"])

    result, events = await _run(
        engine, stream=stream, question="enable ssh", profile="wizard", home_dir=tmp_path,
        max_iterations=max_iterations,
    )

    assert result is not None
    assert len(result["tool_calls"]) == 1
    surfaced = result["tool_calls"][0]
    assert surfaced["name"] == "system_settings_apply" and surfaced["arguments"] == {"setting": "enable_ssh"}
    # The red card's payload rides on the surfaced call — the registry's dry
    # run and the token the confirmed execute must bring back — so the UI
    # never reads a plan off the model's arguments. None of it reaches the model.
    assert surfaced["privileged"] is True
    assert surfaced["plan"]["commands"] == ["sudo systemctl enable --now ssh"]
    assert isinstance(surfaced["approval_token"], str) and "." in surfaced["approval_token"]
    assert isinstance(surfaced["approval_expires_at"], int)
    assert result["deferred_tool_calls"] == []
    assert result["tool_results"] == []
    assert result["iterations"] == 1
    assert all(c.calls == [] for c in counters.values())
    if stream:
        confirm_events = [e for e in events if e["type"] == "confirm_required"]
        assert len(confirm_events) == 1
        assert confirm_events[0]["tool_calls"] == result["tool_calls"]
        assert [e["type"] for e in events if e["type"] == "tool_result"] == []


@pytest.mark.asyncio
async def test_stream_refuses_disallowed_auto_tool(tmp_path: Path, fake_registry) -> None:
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    name = _save_profile(tmp_path, "refresh-only", tools_allowed=["refresh_models"])
    engine = _stream_engine([["Searching.\n", _WEB + "\n"], ["never reached"]])

    with _patched(engine):
        events = [e async for e in chat_mod.wizard_chat_stream("gpus?", profile=name, home_dir=tmp_path)]

    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["name"] == "web_search"
    assert tool_results[0]["result"]["not_allowed"] is True
    assert counters["web_search"].calls == []
    done = events[-1]
    assert done["type"] == "done"
    assert done["iterations"] == 1  # a refusal never earns another completion
    assert done["tool_calls"] == [] and done["deferred_tool_calls"] == []
    assert not [e for e in events if e["type"] == "confirm_required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_refused_confirm_class_call_is_recorded_not_confirmed_not_iterated(
    tmp_path: Path, fake_registry, stream: bool,
) -> None:
    """A confirm-class tool outside the whitelist: no confirm card, one
    refusal in tool_results (a tool_result event on the stream), loop stops."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    name = _save_profile(tmp_path, "search-only", tools_allowed=["web_search"])
    engine = (
        _stream_engine([["I can save that.\n", _SAVE_KEY + "\n"], ["never reached"]])
        if stream else _complete_engine([f"I can save that.\n{_SAVE_KEY}", "never reached"])
    )

    result, events = await _run(engine, stream=stream, question="save my key", profile=name, home_dir=tmp_path)

    assert result is not None
    assert result["iterations"] == 1
    assert result["tool_calls"] == [] and result["deferred_tool_calls"] == []
    assert [r["name"] for r in result["tool_results"]] == ["save_provider_key"]
    assert result["tool_results"][0]["result"]["not_allowed"] is True
    assert counters["save_provider_key"].calls == []
    if stream:
        types = [e["type"] for e in events]
        assert "confirm_required" not in types
        refusals = [e for e in events if e["type"] == "tool_result"]
        assert [(e["name"], e["result"]["not_allowed"]) for e in refusals] == [("save_provider_key", True)]
    else:
        assert chat_mod.WIZARD_FOLLOWUP_MAX_ITER > 1  # the loop *could* have continued


# ───────────────────────────────────────────────────────────────────────────
# Concierge routing — core tools stay when the concierge chose
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_routed_specialist_keeps_core_tools_while_a_pin_stays_strict(
    tmp_path: Path, fake_registry, monkeypatch,
) -> None:
    """Hidden routing must never remove the ability to look at the box: a
    specialist the concierge picked binds its whitelist ∪ the read-only core
    auto tools. The same profile pinned by the user keeps its strict whitelist."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    name = _save_profile(tmp_path, "medic", tools_allowed=["rag_ask"])
    seen = _catalog_spy(monkeypatch)
    _route_auto_to(monkeypatch, name)

    engine = _complete_engine([f"Refreshing.\n{_REFRESH}", "Found it.", f"Refreshing.\n{_REFRESH}"])
    with _patched(engine):
        routed = await chat_mod.wizard_chat("gpus?", home_dir=tmp_path)  # profile=None → concierge
        pinned = await chat_mod.wizard_chat("gpus?", profile=name, home_dir=tmp_path)

    # Concierge-chosen: catalog = whitelist ∪ core tools present in the registry.
    assert seen[0] == ["diagnose", "refresh_models"]
    assert routed["used_profile"] == name
    assert routed["profile_reason"] == f"{name}: matched 'broken'"
    assert routed["iterations"] == 2 and routed["answer"] == "Found it."
    assert counters["refresh_models"].calls == [{}]

    # Pinned: strict — rag_ask isn't in this registry, so the catalog is empty
    # and refresh_models is refused rather than run.
    assert seen[1] == []
    assert pinned["used_profile"] == name and pinned["profile_reason"] is None
    assert pinned["iterations"] == 1
    assert pinned["tool_results"][0]["result"]["not_allowed"] is True
    assert counters["refresh_models"].calls == [{}]  # unchanged


@pytest.mark.asyncio
async def test_core_tool_union_is_read_only_and_never_re_enables_web_search(
    tmp_path: Path, fake_registry, monkeypatch,
) -> None:
    """The union widens a hidden specialist with diagnostics only: a
    concierge-routed profile whose whitelist omits web_search still cannot
    call it."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    name = _save_profile(tmp_path, "notes-only", tools_allowed=["rag_ask_vault"])
    seen = _catalog_spy(monkeypatch)
    _route_auto_to(monkeypatch, name)
    engine = _complete_engine([f"Searching.\n{_WEB}", "never reached"])

    with _patched(engine):
        result = await chat_mod.wizard_chat("latest gpus?", home_dir=tmp_path)

    assert seen[0] == ["diagnose", "refresh_models"]  # web_search is not core
    assert result["used_profile"] == name
    assert result["iterations"] == 1
    assert [(r["name"], r["result"]["not_allowed"]) for r in result["tool_results"]] == [("web_search", True)]
    assert counters["web_search"].calls == []


@pytest.mark.asyncio
async def test_strict_tools_vault_rag_auto_routed_keeps_its_whitelist_exactly(
    tmp_path: Path, fake_registry, monkeypatch,
) -> None:
    """The core vault-rag built-in carries ``strict-tools``: its prompt says
    'never call web_search', so concierge routing adds nothing — not even the
    read-only core tools — and web_search stays refused."""
    from nvh.integrations.wizard import chat as chat_mod
    from nvh.integrations.wizard.profiles import get_profile

    _reg, counters = fake_registry
    vault = get_profile("vault-rag", home_dir=tmp_path)
    assert vault is not None and "strict-tools" in vault.tags and vault.tools_allowed == ["rag_ask_vault"]
    seen = _catalog_spy(monkeypatch)
    _route_auto_to(monkeypatch, "vault-rag", reason="matched 'my notes'")
    engine = _complete_engine([f"Searching and refreshing.\n{_WEB}\n{_REFRESH}", "never reached"])

    with _patched(engine):
        result = await chat_mod.wizard_chat("what did I note about gpus?", home_dir=tmp_path)

    assert seen[0] == []  # rag_ask_vault is not in this registry and nothing was added
    assert result["used_profile"] == "vault-rag"
    assert result["profile_reason"] == "vault-rag: matched 'my notes'"
    assert result["iterations"] == 1
    assert [(r["name"], r["result"]["not_allowed"]) for r in result["tool_results"]] == [
        ("web_search", True), ("refresh_models", True),
    ]
    assert counters["web_search"].calls == [] and counters["refresh_models"].calls == []


@pytest.mark.asyncio
async def test_install_medic_auto_routed_can_diagnose(tmp_path: Path, fake_registry, monkeypatch) -> None:
    """A library specialist without the tag keeps the read-only core tools:
    install-medic routed by the concierge runs diagnose and reacts to it."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    seen = _catalog_spy(monkeypatch)
    _route_auto_to(monkeypatch, "install-medic")
    engine = _complete_engine([f"Checking.\n{_DIAGNOSE}", "All good."])

    with _patched(engine):
        result = await chat_mod.wizard_chat("pip install failed", home_dir=tmp_path)

    assert "diagnose" in seen[0] and "web_search" not in seen[0]
    assert result["used_profile"] == "install-medic"
    assert result["iterations"] == 2 and result["answer"] == "All good."
    assert [r["name"] for r in result["tool_results"]] == ["diagnose"]
    assert result["tool_results"][0]["result"]["ok"] is True
    assert counters["diagnose"].calls == [{}]


# ───────────────────────────────────────────────────────────────────────────
# Provider pin — advisory, except local-only
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_unregistered_provider_pin_keeps_router_decision(tmp_path: Path, fake_registry, stream: bool) -> None:
    """A library profile pinned to ollama on a box without Ollama must still
    answer: the router's pick stands and the reason records the fallback."""
    name = _save_profile(tmp_path, "ollama-pinned", provider="ollama", model="ollama/qwen2.5-coder:7b")
    engine = _stream_engine([["ok"]], registered=False) if stream else _complete_engine(["ok"], registered=False)
    engine.router.route.return_value.provider = "groq"
    engine.router.route.return_value.model = "groq/llama"

    result, _events = await _run(engine, stream=stream, question="review this", profile=name, home_dir=tmp_path)

    assert result is not None
    assert result["answer"] == "ok"
    assert (result["used_provider"], result["used_model"]) == ("groq", "groq/llama")
    assert result["routing_reason"].startswith("local-first; profile_provider_unavailable: 'ollama'")
    engine.registry.has.assert_called_once_with("ollama")
    assert engine.registry.get.call_args_list == [call("groq")]
    assert result["used_profile"] == name


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_registered_provider_pin_overrides_router(tmp_path: Path, fake_registry, stream: bool) -> None:
    name = _save_profile(tmp_path, "groq-pinned", provider="groq", model="groq/llama")
    engine = _stream_engine([["ok"]]) if stream else _complete_engine(["ok"])

    result, _events = await _run(engine, stream=stream, question="go", profile=name, home_dir=tmp_path)

    assert result is not None
    assert (result["used_provider"], result["used_model"]) == ("groq", "groq/llama")
    assert result["routing_reason"] == "local-first"
    assert engine.registry.get.call_args_list == [call("groq")]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("provider", ["ollama", ""])
async def test_local_only_profile_declines_instead_of_using_a_cloud_provider(
    tmp_path: Path, fake_registry, stream: bool, provider: str,
) -> None:
    """``local-only`` profiles never fall back to the router's cloud pick: the
    turn answers deterministically, no completion is made, and the answer is
    the specialist's own (not the offline setup helper's). An empty pin
    means the default local provider."""
    name = _save_profile(tmp_path, "local-notes", provider=provider, tags=["notes", "local-only"])
    engine = _stream_engine([["never"]], registered=False) if stream else _complete_engine(["never"], registered=False)
    engine.router.route.return_value.provider = "groq"

    with patch("nvh.integrations.wizard.setup_agent.setup_assistant_reply") as offline_helper:
        result, events = await _run(engine, stream=stream, question="what did I note?", profile=name, home_dir=tmp_path)

    offline_helper.assert_not_called()
    engine.registry.has.assert_called_once_with("ollama")
    engine.registry.get.assert_not_called()
    if stream:
        assert result is None
        assert [e["type"] for e in events] == ["error"]
        err = events[0]
        assert "needs a local model" in err["fallback"] and "Ollama is not configured" in err["fallback"]
        assert err["error"] == err["fallback"]
        assert err["fallback_reason"] == _LOCAL_ONLY_REASON
        assert err["used_profile"] == name
    else:
        assert result is not None
        assert result["mode"] == "deterministic"
        assert result["fallback_reason"] == _LOCAL_ONLY_REASON
        assert "needs a local model" in result["answer"] and "Ollama is not configured" in result["answer"]
        assert result["used_profile"] == name
        assert result["tool_calls"] == [] and result["deferred_tool_calls"] == [] and result["iterations"] == 0


@pytest.mark.asyncio
async def test_local_only_profile_runs_when_its_local_provider_is_registered(tmp_path: Path, fake_registry) -> None:
    name = _save_profile(tmp_path, "local-notes", tags=["local-only"])  # no pin → ollama implied
    engine = _complete_engine(["from ollama"])
    engine.router.route.return_value.provider = "groq"  # the router would have gone to the cloud

    result, _ = await _run(engine, stream=False, question="what did I note?", profile=name, home_dir=tmp_path)

    assert result is not None and result["mode"] == "llm"
    assert result["answer"] == "from ollama"
    assert result["used_provider"] == "ollama"
    engine.registry.has.assert_called_once_with("ollama")


# ───────────────────────────────────────────────────────────────────────────
# temperature / max_tokens
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_profile_temperature_and_max_tokens_reach_provider_complete(
    tmp_path: Path, fake_registry,
) -> None:
    from nvh.integrations.wizard import chat as chat_mod

    tuned = _save_profile(tmp_path, "tuned", temperature=0.11, max_tokens=77)
    temp_only = _save_profile(tmp_path, "temp-only", temperature=0.33)
    engine = _complete_engine(["ok", "ok", "ok"])

    with _patched(engine):
        await chat_mod.wizard_chat("a", profile=tuned, home_dir=tmp_path)
        await chat_mod.wizard_chat("b", profile=temp_only, home_dir=tmp_path)
        await chat_mod.wizard_chat("c", profile="wizard", home_dir=tmp_path)

    calls = engine.registry.get().complete.call_args_list
    assert (calls[0].kwargs["temperature"], calls[0].kwargs["max_tokens"]) == (0.11, 77)
    # Unset fields inherit the engine default, field by field.
    assert (calls[1].kwargs["temperature"], calls[1].kwargs["max_tokens"]) == (0.33, 256)
    assert (calls[2].kwargs["temperature"], calls[2].kwargs["max_tokens"]) == (0.7, 256)


@pytest.mark.asyncio
async def test_profile_temperature_and_max_tokens_reach_provider_stream(
    tmp_path: Path, fake_registry,
) -> None:
    from nvh.integrations.wizard import chat as chat_mod

    tuned = _save_profile(tmp_path, "tuned", temperature=0.11, max_tokens=77)
    engine = _stream_engine([["ok"], ["ok"]])

    with _patched(engine):
        _ = [e async for e in chat_mod.wizard_chat_stream("a", profile=tuned, home_dir=tmp_path)]
        _ = [e async for e in chat_mod.wizard_chat_stream("b", profile="wizard", home_dir=tmp_path)]

    calls = engine.registry.get().stream.call_args_list
    assert (calls[0].kwargs["temperature"], calls[0].kwargs["max_tokens"]) == (0.11, 77)
    assert (calls[1].kwargs["temperature"], calls[1].kwargs["max_tokens"]) == (0.7, 256)


# ───────────────────────────────────────────────────────────────────────────
# Streaming — confirm_required / deferred_tool_calls / refusals
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_confirm_required_precedes_done_with_max_iterations_one(tmp_path: Path, fake_registry) -> None:
    """Iteration 1 returns a confirm-class call and the loop is capped at 1:
    the UI must still get confirm_required, once, before done."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    plain = _save_profile(tmp_path, "plain")
    engine = _stream_engine([["I can save that key.\n", _SAVE_KEY + "\n"]])

    with _patched(engine):
        events = [
            e async for e in chat_mod.wizard_chat_stream(
                "save my key", profile=plain, home_dir=tmp_path, max_iterations=1,
            )
        ]

    types = [e["type"] for e in events]
    assert types.count("confirm_required") == 1
    assert types.index("confirm_required") < types.index("done")
    assert types[-1] == "done"
    confirm = next(e for e in events if e["type"] == "confirm_required")
    done = events[-1]
    assert [c["name"] for c in confirm["tool_calls"]] == ["save_provider_key"]
    assert done["tool_calls"] == confirm["tool_calls"]
    assert done["deferred_tool_calls"] == []
    assert done["iterations"] == 1
    assert done["used_profile"] == plain
    # Nothing ran server-side and no tool events fired.
    assert "tool_call" not in types and "tool_result" not in types
    assert counters["save_provider_key"].calls == []


@pytest.mark.asyncio
async def test_stream_confirm_required_survives_later_iteration_and_emits_once(tmp_path: Path, fake_registry) -> None:
    """A confirm-class call deferred on iteration 1 alongside an auto call is
    still in confirm_required / done.tool_calls after iteration 2 answers —
    and confirm_required is emitted exactly once."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    plain = _save_profile(tmp_path, "plain")
    engine = _stream_engine([
        ["Refreshing, then saving.\n", _REFRESH + "\n", _SAVE_KEY + "\n"],
        ["Refreshed. Confirm the key save when ready."],
    ])

    with _patched(engine):
        events = [e async for e in chat_mod.wizard_chat_stream("refresh and save", profile=plain, home_dir=tmp_path)]

    types = [e["type"] for e in events]
    assert types.count("confirm_required") == 1
    assert types.index("confirm_required") < types.index("done")
    done = events[-1]
    assert done["iterations"] == 2
    assert [c["name"] for c in done["tool_calls"]] == ["save_provider_key"]
    assert [r["name"] for r in done["tool_results"]] == ["refresh_models"]
    assert counters["refresh_models"].calls == [{}]
    assert counters["save_provider_key"].calls == []


@pytest.mark.asyncio
async def test_stream_emits_pending_confirm_before_error_from_a_later_iteration(
    tmp_path: Path, fake_registry,
) -> None:
    """Iteration 1 defers a confirm-class call and runs an auto tool; iteration
    2 blows up. The user must still get to decide on the deferred call."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    plain = _save_profile(tmp_path, "plain")
    engine = _stream_engine([
        ["Refreshing, then saving.\n", _REFRESH + "\n", _SAVE_KEY + "\n"],
        RuntimeError("provider went away"),
    ])

    with (
        _patched(engine),
        patch("nvh.integrations.wizard.setup_agent.setup_assistant_reply",
              return_value={"answer": "Offline fallback", "actions": []}),
    ):
        events = [e async for e in chat_mod.wizard_chat_stream("refresh and save", profile=plain, home_dir=tmp_path)]

    types = [e["type"] for e in events]
    assert "done" not in types and types[-1] == "error"
    assert types.count("confirm_required") == 1
    assert types.index("confirm_required") < types.index("error")
    confirm = next(e for e in events if e["type"] == "confirm_required")
    assert [c["name"] for c in confirm["tool_calls"]] == ["save_provider_key"]
    assert counters["refresh_models"].calls == [{}]
    assert counters["save_provider_key"].calls == []
    assert "provider went away" in events[-1]["error"]
    assert events[-1]["fallback"] == "Offline fallback"


@pytest.mark.asyncio
async def test_stream_max_iterations_one_defers_auto_calls_instead_of_confirming_them(
    tmp_path: Path, fake_registry,
) -> None:
    """'Depth 1 = no tools' must hold end to end: an auto-class call the loop
    did not run is reported as deferred (with the reason), never handed to
    the UI as something to confirm — the UI would auto-run it."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    plain = _save_profile(tmp_path, "plain")
    engine = _stream_engine([["Two things.\n", _REFRESH + "\n", _SAVE_KEY + "\n"]])

    with _patched(engine):
        events = [
            e async for e in chat_mod.wizard_chat_stream("go", profile=plain, home_dir=tmp_path, max_iterations=1)
        ]

    types = [e["type"] for e in events]
    assert "tool_call" not in types and "tool_result" not in types
    confirm = next(e for e in events if e["type"] == "confirm_required")
    assert [c["name"] for c in confirm["tool_calls"]] == ["save_provider_key"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["tool_calls"] == confirm["tool_calls"]
    assert done["deferred_tool_calls"] == [
        {"name": "refresh_models", "arguments": {}, "reason": chat_mod.DEFER_MAX_ITERATIONS},
    ]
    assert all(c.calls == [] for c in counters.values())


@pytest.mark.asyncio
async def test_stream_emits_refusals_of_deferred_calls_as_tool_result_events(
    tmp_path: Path, fake_registry,
) -> None:
    """With max_iterations=1 a call outside the whitelist is refused: the
    refusal shows up in the live trace, not only in done.tool_results."""
    from nvh.integrations.wizard import chat as chat_mod

    _reg, counters = fake_registry
    name = _save_profile(tmp_path, "refresh-only", tools_allowed=["refresh_models"])
    engine = _stream_engine([["Two things.\n", _WEB + "\n", _REFRESH + "\n"]])

    with _patched(engine):
        events = [
            e async for e in chat_mod.wizard_chat_stream("go", profile=name, home_dir=tmp_path, max_iterations=1)
        ]

    refusals = [e for e in events if e["type"] == "tool_result"]
    assert [(e["name"], e["result"]["not_allowed"]) for e in refusals] == [("web_search", True)]
    assert not [e for e in events if e["type"] == "confirm_required"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["tool_calls"] == []
    assert [r["name"] for r in done["tool_results"]] == ["web_search"]
    assert done["deferred_tool_calls"] == [
        {"name": "refresh_models", "arguments": {}, "reason": chat_mod.DEFER_MAX_ITERATIONS},
    ]
    assert all(c.calls == [] for c in counters.values())


# ───────────────────────────────────────────────────────────────────────────
# Streaming cost + ceiling
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_done_reports_cost_from_final_chunk_and_enforces_ceiling(
    tmp_path: Path, fake_registry,
) -> None:
    """Providers attach usage + cost to the final StreamChunk; the stream path
    must roll it up like complete() and stop the loop at the profile ceiling.
    The auto call it did not run is deferred with the ceiling as the reason."""
    from nvh.integrations.wizard import chat as chat_mod
    from nvh.providers.base import StreamChunk, Usage

    _reg, counters = fake_registry
    name = _save_profile(tmp_path, "capped", max_cost_usd_per_turn=0.05)
    final = StreamChunk(
        delta="", is_final=True,
        usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
        cost_usd=Decimal("0.10"),
    )
    engine = _stream_engine([["Working.\n", _REFRESH + "\n", final], ["never reached"]])

    with _patched(engine):
        events = [e async for e in chat_mod.wizard_chat_stream("go", profile=name, home_dir=tmp_path)]

    done = events[-1]
    assert done["type"] == "done"
    assert done["cost_usd"] == pytest.approx(0.10)
    assert done["input_tokens"] == 100 and done["output_tokens"] == 50
    assert done["cost_ceiling_usd"] == 0.05
    assert done["cost_ceiling_hit"] is True
    assert done["iterations"] == 1
    # Ceiling fired before the tool step: reported as deferred, not executed,
    # and not offered to the UI as a confirm card it would auto-run.
    assert done["tool_calls"] == []
    assert done["deferred_tool_calls"] == [
        {"name": "refresh_models", "arguments": {}, "reason": chat_mod.DEFER_COST_CEILING},
    ]
    assert not [e for e in events if e["type"] in ("tool_result", "confirm_required")]
    assert counters["refresh_models"].calls == []


@pytest.mark.asyncio
async def test_stream_done_cost_zero_when_provider_reports_no_meter(tmp_path: Path, fake_registry) -> None:
    """Chunks without usage/cost (or with non-numeric doubles) leave the meter
    at 0 and never trip a ceiling."""
    from nvh.integrations.wizard import chat as chat_mod

    engine = _stream_engine([["Hello ", "world"]])

    with _patched(engine):
        events = [e async for e in chat_mod.wizard_chat_stream("hi", profile="wizard", home_dir=tmp_path)]

    done = events[-1]
    assert done["type"] == "done"
    assert done["cost_usd"] == 0.0
    assert done["input_tokens"] == 0 and done["output_tokens"] == 0
    assert done["cost_ceiling_hit"] is False
    assert done["cost_ceiling_usd"] is None
    assert done["used_profile"] is None


# ───────────────────────────────────────────────────────────────────────────
# Per-turn cost + continuity
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_builds_registry_and_loads_profile_catalog_once(
    tmp_path: Path, fake_registry, monkeypatch,
) -> None:
    """One default_registry() and one list_profiles() per turn, however many
    tools run and whether the concierge or a pin picked the profile."""
    from nvh.integrations.wizard import chat as chat_mod
    from nvh.integrations.wizard import profiles as profiles_mod

    reg, counters = fake_registry
    registry_builds: list[int] = []
    monkeypatch.setattr(
        "nvh.integrations.wizard.tools.default_registry",
        lambda: registry_builds.append(1) or reg,
    )
    real_list = profiles_mod.list_profiles
    catalog_loads: list[Any] = []

    def counting_list(home_dir=None):
        catalog_loads.append(home_dir)
        return real_list(home_dir=home_dir)

    monkeypatch.setattr(profiles_mod, "list_profiles", counting_list)
    name = _save_profile(tmp_path, "plain", prompt_template="Q: {{input}}")

    engine = _complete_engine([f"One.\n{_WEB}\n{_REFRESH}", f"Two.\n{_REFRESH}", "Done.", "Auto done."])
    with _patched(engine):
        pinned = await chat_mod.wizard_chat("go", profile=name, home_dir=tmp_path)
        assert (len(registry_builds), len(catalog_loads)) == (1, 1)
        await chat_mod.wizard_chat("go", home_dir=tmp_path)  # concierge path, real selection
        assert (len(registry_builds), len(catalog_loads)) == (2, 2)

    assert pinned["iterations"] == 3 and pinned["answer"] == "Done."
    assert counters["web_search"].calls == [{"query": "gpus"}]
    assert counters["refresh_models"].calls == [{}, {}]
    # The prompt template came from the same resolved profile, no extra lookup.
    # (The loop appends to the one messages list, so look at the user turn,
    # not the last element.)
    first_messages = engine.registry.get().complete.call_args_list[0].kwargs["messages"]
    assert [m.content for m in first_messages if m.role == "user"] == ["Q: go"]


@pytest.mark.asyncio
async def test_history_used_profile_reaches_concierge_and_attribution_is_persisted(
    tmp_path: Path, fake_registry, monkeypatch,
) -> None:
    """Continuity: history entries carry ``used_profile`` for the concierge
    (passed through unchanged, never leaked into provider messages), and the
    persisted turn records used_profile / profile_reason for reload."""
    from nvh.integrations.wizard import chat as chat_mod

    name = _save_profile(tmp_path, "medic", tools_allowed=["rag_ask"])
    seen = _route_auto_to(monkeypatch, name, reason="continuing from the previous turn")
    add_message = AsyncMock()
    monkeypatch.setattr("nvh.storage.repository.add_message", add_message, raising=False)
    history = [
        {"role": "user", "content": "my driver is broken"},
        {"role": "assistant", "content": "Let's check.", "used_profile": name},
    ]
    engine = _complete_engine(["Next step."])

    with _patched(engine):
        result = await chat_mod.wizard_chat(
            "and then?", history=history, home_dir=tmp_path, conversation_id="conv-1",
        )

    assert seen["history"] is history
    assert seen["history"][-1]["used_profile"] == name
    assert seen["home_dir"] == tmp_path
    assert {p.name for p in seen["profiles"]} >= {name, "wizard", "coder"}  # preloaded catalog
    messages = engine.registry.get().complete.call_args_list[0].kwargs["messages"]
    assert [m.role for m in messages] == ["system", "user", "assistant", "user"]
    assert result["used_profile"] == name
    assert result["profile_reason"] == f"{name}: continuing from the previous turn"

    assert add_message.await_count == 2
    meta = _wizard_meta(add_message.await_args_list[1].kwargs["content"])
    assert meta["used_profile"] == name
    assert meta["profile_reason"] == result["profile_reason"]
    assert meta["tool_calls"] == [] and meta["deferred_tool_calls"] == [] and meta["iterations"] == 1


# ───────────────────────────────────────────────────────────────────────────
# local-only — concierge choice is demoted, an explicit pin refuses
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_concierge_chosen_local_only_specialist_falls_back_to_general_wizard(
    tmp_path: Path, fake_registry, monkeypatch, stream: bool,
) -> None:
    """An ordinary unpinned question routed to a local-only specialist on a
    box without Ollama is answered by the general Wizard for the turn — never
    a refusal telling the user to pick another agent they never picked."""
    name = _save_profile(
        tmp_path, "home", system_prompt="I AM THE HOME SPECIALIST", tags=["smart-home", "local-only"],
        tools_allowed=["home_assistant_status"],
    )
    seen = _catalog_spy(monkeypatch)
    _route_auto_to(monkeypatch, name, reason="matched 'lights'")
    engine = (
        _stream_engine([["from the cloud"]], registered=False)
        if stream else _complete_engine(["from the cloud"], registered=False)
    )
    engine.router.route.return_value.provider = "groq"
    engine.router.route.return_value.model = "groq/llama"

    with patch("nvh.integrations.wizard.setup_agent.setup_assistant_reply") as offline_helper:
        result, events = await _run(engine, stream=stream, question="turn off the lights", home_dir=tmp_path)

    offline_helper.assert_not_called()
    assert result is not None
    assert result["type" if stream else "mode"] == ("done" if stream else "llm")
    assert result["answer"] == "from the cloud"
    assert (result["used_provider"], result["used_model"]) == ("groq", "groq/llama")
    assert result["used_profile"] is None
    assert result["profile_reason"].startswith(
        "general Wizard: local-only specialist unavailable: Ollama not configured",
    )
    assert f"would have been {name}: matched 'lights'" in result["profile_reason"]
    # The general Wizard answered: full catalog, no specialist persona, no whitelist.
    assert seen[0] == _FULL_CATALOG
    provider = engine.registry.get()
    prompt = (provider.stream if stream else provider.complete).call_args.kwargs["system_prompt"]
    assert "I AM THE HOME SPECIALIST" not in prompt
    engine.registry.has.assert_called_once_with("ollama")
    if stream:
        assert "error" not in [e["type"] for e in events]


@pytest.mark.asyncio
async def test_pinned_local_only_specialist_still_refuses_when_ollama_is_down(
    tmp_path: Path, fake_registry, monkeypatch,
) -> None:
    """Same profile, same box: the user who pinned it asked for that
    specialist, so the deterministic refusal (with the /agent hint) stands;
    the concierge-routed turn beside it is answered by the general Wizard."""
    from nvh.integrations.wizard import chat as chat_mod

    name = _save_profile(tmp_path, "home", tags=["local-only"])
    _route_auto_to(monkeypatch, name)
    engine = _complete_engine(["answered"], registered=False)
    engine.router.route.return_value.provider = "groq"

    with _patched(engine):
        routed = await chat_mod.wizard_chat("turn off the lights", home_dir=tmp_path)
        pinned = await chat_mod.wizard_chat("turn off the lights", profile=name, home_dir=tmp_path)

    assert routed["mode"] == "llm" and routed["answer"] == "answered"
    assert routed["used_profile"] is None and "pick another agent" not in routed["answer"]
    assert pinned["mode"] == "deterministic"
    assert pinned["fallback_reason"] == _LOCAL_ONLY_REASON
    assert pinned["used_profile"] == name and pinned["profile_reason"] is None
    assert "needs a local model" in pinned["answer"] and "pick another agent with /agent" in pinned["answer"]
    assert engine.registry.get().complete.await_count == 1  # the pinned turn made no completion


@pytest.mark.asyncio
async def test_concierge_chosen_local_only_specialist_runs_when_ollama_is_registered(
    tmp_path: Path, fake_registry, monkeypatch,
) -> None:
    """No demotion when the local provider is there: the specialist answers on
    Ollama (with its core-tool union) even though the router went to the cloud."""
    from nvh.integrations.wizard import chat as chat_mod

    name = _save_profile(tmp_path, "home", tags=["local-only"], tools_allowed=["home_assistant_status"])
    seen = _catalog_spy(monkeypatch)
    _route_auto_to(monkeypatch, name, reason="matched 'lights'")
    engine = _complete_engine(["from ollama"])
    engine.router.route.return_value.provider = "groq"

    with _patched(engine):
        result = await chat_mod.wizard_chat("turn off the lights", home_dir=tmp_path)

    assert result["mode"] == "llm" and result["answer"] == "from ollama"
    assert result["used_provider"] == "ollama"
    assert result["used_profile"] == name and result["profile_reason"] == f"{name}: matched 'lights'"
    assert seen[0] == ["diagnose", "refresh_models"]  # whitelist ∪ core, filtered by the registry


# ───────────────────────────────────────────────────────────────────────────
# Provider pin — the model follows the pinned provider
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_provider_pin_without_model_re_resolves_the_model_for_that_provider(
    tmp_path: Path, fake_registry, stream: bool,
) -> None:
    """The router's model belongs to the router's provider. A profile that
    pins only a provider gets that provider's own model from a
    provider-constrained route — ``ollama/x`` is never sent to groq."""
    name = _save_profile(tmp_path, "groq-only", provider="groq")  # no model pin
    engine = _stream_engine([["ok"]]) if stream else _complete_engine(["ok"])
    router_pick = engine.router.route.return_value  # ollama / ollama/x / "local-first"
    constrained = MagicMock(provider="groq", model="groq/default", reason="User override: --provider groq")

    def route(query, provider_override=None, **kw):
        return constrained if provider_override else router_pick

    engine.router.route = MagicMock(side_effect=route)

    result, _events = await _run(engine, stream=stream, question="go", profile=name, home_dir=tmp_path)

    assert result is not None
    assert (result["used_provider"], result["used_model"]) == ("groq", "groq/default")
    assert result["routing_reason"] == "local-first"  # the router's decision, re-pinned
    assert engine.router.route.call_args_list == [call(query="go"), call(query="go", provider_override="groq")]
    provider = engine.registry.get()
    sent = (provider.stream if stream else provider.complete).call_args.kwargs["model"]
    assert sent == "groq/default"


@pytest.mark.asyncio
async def test_provider_pin_keeps_the_router_model_when_the_router_already_chose_it(
    tmp_path: Path, fake_registry,
) -> None:
    """Same provider → the router's task-aware model pick is valid and stands;
    no second route."""
    name = _save_profile(tmp_path, "ollama-only", provider="ollama")
    engine = _complete_engine(["ok"])  # router: ollama / ollama/x

    result, _ = await _run(engine, stream=False, question="go", profile=name, home_dir=tmp_path)

    assert result is not None
    assert (result["used_provider"], result["used_model"]) == ("ollama", "ollama/x")
    engine.router.route.assert_called_once_with(query="go")
    assert engine.registry.get().complete.call_args.kwargs["model"] == "ollama/x"


@pytest.mark.asyncio
async def test_provider_pin_model_lookup_failure_lets_the_provider_apply_its_default(
    tmp_path: Path, fake_registry,
) -> None:
    """If the constrained route blows up the model is left empty (→ ``None``
    to the provider) rather than borrowing the router's provider-specific id."""
    name = _save_profile(tmp_path, "groq-only", provider="groq")
    engine = _complete_engine(["ok"])
    router_pick = engine.router.route.return_value

    def route(query, provider_override=None, **kw):
        if provider_override:
            raise RuntimeError("no config for groq")
        return router_pick

    engine.router.route = MagicMock(side_effect=route)

    result, _ = await _run(engine, stream=False, question="go", profile=name, home_dir=tmp_path)

    assert result is not None and result["mode"] == "llm"
    assert (result["used_provider"], result["used_model"]) == ("groq", "")
    assert engine.registry.get().complete.call_args.kwargs["model"] is None


# ───────────────────────────────────────────────────────────────────────────
# Deterministic fallbacks — attribution, carried tool state, error shape
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deterministic_fallbacks_attribute_no_specialist_in_envelope_and_meta(
    tmp_path: Path, fake_registry, monkeypatch,
) -> None:
    """No engine / LLM exception / stream exception: the offline helper is not
    the specialist, so ``used_profile`` is None in the live response *and* in
    the persisted wizard-meta — they must never disagree. The concierge's
    ``profile_reason`` is independent of that attribution and travels with
    the two concierge-routed turns; the explicit pin has none."""
    from nvh.integrations.wizard import chat as chat_mod

    name = _save_profile(tmp_path, "medic", tools_allowed=["rag_ask"])
    _route_auto_to(monkeypatch, name)
    add_message = AsyncMock()
    monkeypatch.setattr("nvh.storage.repository.add_message", add_message, raising=False)
    offline = patch(
        "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
        return_value={"answer": "Offline answer", "actions": []},
    )

    # 1. No engine (concierge-routed).
    with (
        offline,
        patch("nvh.api.server.get_engine", return_value=None),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        no_engine = await chat_mod.wizard_chat("broken", home_dir=tmp_path, conversation_id="c1")
    # 2. The LLM raises (explicit pin).
    boom = _complete_engine([])
    boom.registry.get().complete = AsyncMock(side_effect=RuntimeError("provider down"))
    with offline, _patched(boom):
        llm_failed = await chat_mod.wizard_chat("broken", profile=name, home_dir=tmp_path, conversation_id="c2")
    # 3. The stream raises (concierge-routed).
    with offline, _patched(_stream_engine([RuntimeError("stream down")])):
        events = [
            e async for e in chat_mod.wizard_chat_stream("broken", home_dir=tmp_path, conversation_id="c3")
        ]

    routed_reason = f"{name}: matched 'broken'"
    for result in (no_engine, llm_failed):
        assert result["mode"] == "deterministic" and result["answer"] == "Offline answer"
        assert result["used_profile"] is None
    assert no_engine["fallback_reason"] == "engine not initialized"
    assert no_engine["profile_reason"] == routed_reason  # concierge ran: its reason travels
    assert llm_failed["fallback_reason"] == "provider down"
    assert llm_failed["profile_reason"] is None  # explicit pin: no selection ran
    err = events[-1]
    assert err["type"] == "error" and err["fallback"] == "Offline answer"
    assert err["fallback_reason"] == "stream down"
    assert err["used_profile"] is None and err["profile_reason"] == routed_reason

    metas = [_wizard_meta(c.kwargs["content"]) for c in add_message.await_args_list[1::2]]
    assert len(metas) == 3
    for meta in metas:
        assert meta["used_profile"] is None
        assert meta["mode"] == "deterministic"
    assert [m["profile_reason"] for m in metas] == [routed_reason, None, routed_reason]
    assert [m["fallback_reason"] for m in metas] == ["engine not initialized", "provider down", "stream down"]
    assert "streamed" not in metas[1] and metas[2]["streamed"] is True


@pytest.mark.asyncio
async def test_pinned_local_only_refusal_is_attributed_to_the_specialist_in_envelope_and_meta(
    tmp_path: Path, fake_registry, monkeypatch,
) -> None:
    """The one deterministic answer a specialist owns: it declined itself."""
    from nvh.integrations.wizard import chat as chat_mod

    name = _save_profile(tmp_path, "local-notes", tags=["local-only"])
    add_message = AsyncMock()
    monkeypatch.setattr("nvh.storage.repository.add_message", add_message, raising=False)
    engine = _complete_engine(["never"], registered=False)
    engine.router.route.return_value.provider = "groq"

    with _patched(engine):
        result = await chat_mod.wizard_chat("notes?", profile=name, home_dir=tmp_path, conversation_id="c")

    meta = _wizard_meta(add_message.await_args_list[1].kwargs["content"])
    assert result["used_profile"] == meta["used_profile"] == name
    assert result["profile_reason"] is None and meta["profile_reason"] is None
    assert result["fallback_reason"] == meta["fallback_reason"] == _LOCAL_ONLY_REASON
    assert meta["mode"] == "deterministic" and meta["iterations"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_fallback_after_a_failed_later_iteration_keeps_executed_tools_and_iterations(
    tmp_path: Path, fake_registry, monkeypatch, stream: bool,
) -> None:
    """Iteration 1 ran refresh_models and deferred a confirm-class call;
    iteration 2 blew up. The deterministic envelope, the stream's events and
    the persisted meta all still carry that work — nothing executed is lost."""
    from nvh.integrations.wizard import chat as chat_mod
    from nvh.providers.base import CompletionResponse, FinishReason, Usage

    _reg, counters = fake_registry
    plain = _save_profile(tmp_path, "plain")
    add_message = AsyncMock()
    monkeypatch.setattr("nvh.storage.repository.add_message", add_message, raising=False)
    if stream:
        engine = _stream_engine([
            ["Refreshing, then saving.\n", _REFRESH + "\n", _SAVE_KEY + "\n"],
            RuntimeError("provider went away"),
        ])
    else:
        first = CompletionResponse(
            content=f"Refreshing, then saving.\n{_REFRESH}\n{_SAVE_KEY}", model="ollama/x", provider="ollama",
            usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"), latency_ms=1, finish_reason=FinishReason.STOP,
        )
        provider = MagicMock()
        provider.complete = AsyncMock(side_effect=[first, RuntimeError("provider went away")])
        engine = _engine_for(provider)

    with (
        _patched(engine),
        patch(
            "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
            return_value={"answer": "Offline fallback", "actions": []},
        ),
    ):
        if stream:
            events = [
                e async for e in chat_mod.wizard_chat_stream(
                    "refresh and save", profile=plain, home_dir=tmp_path, conversation_id="c",
                )
            ]
            result = events[-1]
            assert result["type"] == "error" and result["fallback"] == "Offline fallback"
            types = [e["type"] for e in events]
            assert types.count("confirm_required") == 1 and types.index("confirm_required") < types.index("error")
            assert [e["name"] for e in events if e["type"] == "tool_result"] == ["refresh_models"]
        else:
            result = await chat_mod.wizard_chat(
                "refresh and save", profile=plain, home_dir=tmp_path, conversation_id="c",
            )
            assert result["mode"] == "deterministic" and result["answer"] == "Offline fallback"
            assert result["iterations"] == 2  # two round-trips attempted; the second failed
            assert [r["name"] for r in result["tool_results"]] == ["refresh_models"]
            assert result["tool_results"][0]["result"]["ok"] is True
            assert [c["name"] for c in result["tool_calls"]] == ["save_provider_key"]
            assert result["deferred_tool_calls"] == []

    assert result["fallback_reason"] == "provider went away"
    assert result["used_profile"] is None and result["profile_reason"] is None
    assert counters["refresh_models"].calls == [{}] and counters["save_provider_key"].calls == []

    meta = _wizard_meta(add_message.await_args_list[1].kwargs["content"])
    assert meta["used_profile"] is None and meta["mode"] == "deterministic"
    assert meta["fallback_reason"] == "provider went away"
    assert meta["iterations"] == 2
    assert [r["name"] for r in meta["tool_results"]] == ["refresh_models"]
    assert [c["name"] for c in meta["tool_calls"]] == ["save_provider_key"]
    assert meta["deferred_tool_calls"] == []
    assert meta.get("streamed", False) is stream


@pytest.mark.asyncio
async def test_stream_error_events_carry_the_same_fallback_shape_as_the_envelope(
    tmp_path: Path, fake_registry,
) -> None:
    """Every stream fallback — no engine, LLM error, pinned local-only refusal
    — emits ``{type, error, fallback, fallback_reason, used_profile,
    profile_reason}``; the non-stream envelope carries the same
    ``fallback_reason`` values."""
    from nvh.integrations.wizard import chat as chat_mod

    local = _save_profile(tmp_path, "local-notes", tags=["local-only"])
    offline = patch(
        "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
        return_value={"answer": "Offline answer", "actions": []},
    )
    keys = {"type", "error", "fallback", "fallback_reason", "used_profile", "profile_reason"}

    with (
        offline,
        patch("nvh.api.server.get_engine", return_value=None),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        no_engine = [e async for e in chat_mod.wizard_chat_stream("hi", home_dir=tmp_path)]
        no_engine_env = await chat_mod.wizard_chat("hi", home_dir=tmp_path)
    with offline, _patched(_stream_engine([RuntimeError("stream down")])):
        failed = [e async for e in chat_mod.wizard_chat_stream("hi", home_dir=tmp_path)]
    refusing = _stream_engine([["never"]], registered=False)
    refusing.router.route.return_value.provider = "groq"
    with offline, _patched(refusing):
        refused = [e async for e in chat_mod.wizard_chat_stream("hi", profile=local, home_dir=tmp_path)]

    assert [e["type"] for e in no_engine] == ["error"]
    assert [e["type"] for e in failed] == ["iteration", "error"]
    assert [e["type"] for e in refused] == ["error"]
    for err in (no_engine[0], failed[-1], refused[0]):
        assert set(err) == keys, err
    assert no_engine[0]["fallback_reason"] == "engine not initialized" == no_engine_env["fallback_reason"]
    assert no_engine[0]["used_profile"] is None and no_engine[0]["fallback"] == "Offline answer"
    assert failed[-1]["fallback_reason"] == "stream down" and failed[-1]["used_profile"] is None
    assert refused[0]["fallback_reason"] == _LOCAL_ONLY_REASON
    assert refused[0]["used_profile"] == local and refused[0]["profile_reason"] is None
    assert refused[0]["error"] == refused[0]["fallback"] and "needs a local model" in refused[0]["fallback"]


# ───────────────────────────────────────────────────────────────────────────
# Local provider availability — registered is not running
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_available_means_registered_and_for_ollama_reachable(local_probe: AsyncMock) -> None:
    """``ProviderRegistry.setup_from_config`` registers Ollama from config
    whether or not the daemon runs, so registration alone must not count.
    Cloud providers are registry membership only (no probe); a registry
    that raises is "not registered"; a probe that raises is "not running"."""
    from nvh.integrations.wizard import chat as chat_mod

    engine = MagicMock()
    engine.registry.has = MagicMock(side_effect=lambda n: n in {"ollama", "groq"})

    assert await chat_mod._provider_available(engine, "groq") is True
    assert await chat_mod._provider_available(engine, "nvidia") is False
    assert await chat_mod._provider_unavailability(engine, "nvidia") == "not registered"
    local_probe.assert_not_awaited()

    assert await chat_mod._provider_available(engine, "ollama") is True
    local_probe.assert_awaited_once()

    chat_mod._reset_local_probe_cache()
    local_probe.return_value = False
    assert await chat_mod._provider_unavailability(engine, "ollama") == "not running"

    chat_mod._reset_local_probe_cache()
    local_probe.side_effect = OSError("socket exploded")
    assert await chat_mod._provider_unavailability(engine, "ollama") == "not running"

    engine.registry.has = MagicMock(side_effect=RuntimeError("no registry"))
    assert await chat_mod._provider_available(engine, "groq") is False
    assert await chat_mod._provider_available(engine, "ollama") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_registered_but_unreachable_ollama_demotes_a_concierge_chosen_local_only_specialist(
    tmp_path: Path, fake_registry, monkeypatch, local_probe: AsyncMock, stream: bool,
) -> None:
    """Ollama in config (registered) but not running: the concierge-chosen
    local-only specialist is demoted exactly as when it is unregistered —
    the general Wizard answers on the router's provider, never a dead one."""
    from nvh.utils.ollama import ollama_base_url

    name = _save_profile(tmp_path, "home", tags=["local-only"], tools_allowed=["home_assistant_status"])
    _route_auto_to(monkeypatch, name, reason="matched 'lights'")
    local_probe.return_value = False
    engine = _stream_engine([["from the cloud"]]) if stream else _complete_engine(["from the cloud"])
    engine.router.route.return_value.provider = "groq"
    engine.router.route.return_value.model = "groq/llama"

    result, events = await _run(engine, stream=stream, question="turn off the lights", home_dir=tmp_path)

    assert result is not None and result["answer"] == "from the cloud"
    assert (result["used_provider"], result["used_model"]) == ("groq", "groq/llama")
    assert result["used_profile"] is None
    assert result["profile_reason"].startswith(
        "general Wizard: local-only specialist unavailable: Ollama not running",
    )
    assert f"would have been {name}: matched 'lights'" in result["profile_reason"]
    engine.registry.has.assert_called_once_with("ollama")
    local_probe.assert_called_once_with(ollama_base_url())  # no config base_url → env/default
    if stream:
        assert "error" not in [e["type"] for e in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_registered_but_unreachable_ollama_refuses_a_pinned_local_only_specialist(
    tmp_path: Path, fake_registry, local_probe: AsyncMock, stream: bool,
) -> None:
    """The user pinned it: same refusal as an unregistered Ollama, no completion."""
    name = _save_profile(tmp_path, "local-notes", tags=["local-only"])
    local_probe.return_value = False
    engine = _stream_engine([["never"]]) if stream else _complete_engine(["never"])
    engine.router.route.return_value.provider = "groq"

    with patch("nvh.integrations.wizard.setup_agent.setup_assistant_reply") as offline_helper:
        result, events = await _run(
            engine, stream=stream, question="what did I note?", profile=name, home_dir=tmp_path,
        )

    offline_helper.assert_not_called()
    engine.registry.has.assert_called_once_with("ollama")
    engine.registry.get.assert_not_called()
    local_probe.assert_called_once()
    envelope = events[0] if stream else result
    assert envelope["fallback_reason"] == _LOCAL_ONLY_REASON
    assert envelope["used_profile"] == name and envelope["profile_reason"] is None
    text = envelope["fallback" if stream else "answer"]
    assert "needs a local model" in text and "Ollama is not running" in text


@pytest.mark.asyncio
async def test_reachable_ollama_lets_the_local_only_specialist_run(
    tmp_path: Path, fake_registry, monkeypatch, local_probe: AsyncMock,
) -> None:
    """Registered *and* answering: the specialist runs on Ollama, and the
    demotion check and the pin share one probe."""
    name = _save_profile(tmp_path, "home", tags=["local-only"])
    _route_auto_to(monkeypatch, name, reason="matched 'lights'")
    engine = _complete_engine(["from ollama"])
    engine.router.route.return_value.provider = "groq"

    result, _ = await _run(engine, stream=False, question="turn off the lights", home_dir=tmp_path)

    assert result is not None and result["mode"] == "llm"
    assert result["used_provider"] == "ollama" and result["answer"] == "from ollama"
    assert result["used_profile"] == name and result["profile_reason"] == f"{name}: matched 'lights'"
    local_probe.assert_called_once()


@pytest.mark.asyncio
async def test_local_probe_is_cached_per_base_url_within_the_ttl(
    tmp_path: Path, fake_registry, monkeypatch, local_probe: AsyncMock,
) -> None:
    """Two turns inside the TTL cost one probe; an expired entry is re-probed."""
    from nvh.integrations.wizard import chat as chat_mod

    name = _save_profile(tmp_path, "home", tags=["local-only"])
    _route_auto_to(monkeypatch, name)
    engine = _complete_engine(["one", "two", "three"])

    with _patched(engine):
        first = await chat_mod.wizard_chat("turn off the lights", home_dir=tmp_path)
        second = await chat_mod.wizard_chat("and the fan", home_dir=tmp_path)
    assert (first["used_profile"], second["used_profile"]) == (name, name)
    assert local_probe.call_count == 1

    url = local_probe.call_args.args[0]
    stamp, ok = chat_mod._LOCAL_PROBE_CACHE[url]
    assert ok is True
    chat_mod._LOCAL_PROBE_CACHE[url] = (stamp - chat_mod.LOCAL_PROBE_TTL_S - 1, ok)  # expire it
    with _patched(engine):
        third = await chat_mod.wizard_chat("and the lamp", home_dir=tmp_path)
    assert third["used_profile"] == name
    assert local_probe.call_count == 2


@pytest.mark.asyncio
async def test_local_probe_targets_the_engine_configured_base_url(
    tmp_path: Path, fake_registry, monkeypatch, local_probe: AsyncMock,
) -> None:
    """The daemon probed is the one the engine would talk to
    (``providers.ollama.base_url``), normalised like the adapter does."""
    from types import SimpleNamespace

    name = _save_profile(tmp_path, "home", tags=["local-only"])
    _route_auto_to(monkeypatch, name)
    engine = _complete_engine(["ok"])
    engine.config.providers = {"ollama": SimpleNamespace(base_url="http://spark.local:11434/")}

    await _run(engine, stream=False, question="turn off the lights", home_dir=tmp_path)

    local_probe.assert_called_once_with("http://spark.local:11434")


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True])
async def test_local_probe_exception_counts_as_unavailable(
    tmp_path: Path, fake_registry, monkeypatch, local_probe: AsyncMock, pinned: bool,
) -> None:
    """A probe that raises never reaches the chat: demotion (auto) / refusal (pin)."""
    name = _save_profile(tmp_path, "home", tags=["local-only"])
    _route_auto_to(monkeypatch, name)
    local_probe.side_effect = OSError("socket exploded")
    engine = _complete_engine(["from the cloud"])
    engine.router.route.return_value.provider = "groq"

    result, _ = await _run(
        engine, stream=False, question="turn off the lights",
        profile=name if pinned else None, home_dir=tmp_path,
    )

    assert result is not None
    if pinned:
        assert result["mode"] == "deterministic" and result["fallback_reason"] == _LOCAL_ONLY_REASON
        assert result["used_profile"] == name
    else:
        assert result["mode"] == "llm" and result["used_provider"] == "groq"
        assert result["used_profile"] is None
        assert result["profile_reason"].startswith("general Wizard: local-only specialist unavailable")
    local_probe.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_registered_but_unreachable_ollama_pin_falls_back_like_an_unregistered_one(
    tmp_path: Path, fake_registry, local_probe: AsyncMock, stream: bool,
) -> None:
    """A library profile pinned to ollama on a box where Ollama is configured
    but down must still answer: the router's pick stands and the reason says
    ``profile_provider_unavailable`` — like an unregistered pin, worded honestly."""
    name = _save_profile(tmp_path, "ollama-pinned", provider="ollama", model="ollama/qwen2.5-coder:7b")
    local_probe.return_value = False
    engine = _stream_engine([["ok"]]) if stream else _complete_engine(["ok"])
    engine.router.route.return_value.provider = "groq"
    engine.router.route.return_value.model = "groq/llama"

    result, _events = await _run(engine, stream=stream, question="review this", profile=name, home_dir=tmp_path)

    assert result is not None and result["answer"] == "ok"
    assert (result["used_provider"], result["used_model"]) == ("groq", "groq/llama")
    assert result["routing_reason"] == (
        "local-first; profile_provider_unavailable: 'ollama' is not running, using the router's groq"
    )
    assert engine.registry.get.call_args_list == [call("groq")]
    assert result["used_profile"] == name
    local_probe.assert_called_once()


@pytest.mark.asyncio
async def test_cloud_provider_pin_never_probes_the_network(
    tmp_path: Path, fake_registry, local_probe: AsyncMock,
) -> None:
    name = _save_profile(tmp_path, "groq-pinned", provider="groq", model="groq/llama")
    engine = _complete_engine(["ok"])

    result, _ = await _run(engine, stream=False, question="go", profile=name, home_dir=tmp_path)

    assert result is not None and result["used_provider"] == "groq"
    local_probe.assert_not_called()


# ───────────────────────────────────────────────────────────────────────────
# profile_reason travels with deterministic fallbacks
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_demoted_turn_keeps_its_profile_reason_when_the_llm_fails(
    tmp_path: Path, fake_registry, monkeypatch, stream: bool,
) -> None:
    """Concierge demotion (``used_profile`` None) followed by an LLM failure:
    the demotion reason still reaches the envelope / stream ``error`` event
    and the persisted meta — attribution and reason are independent."""
    name = _save_profile(tmp_path, "home", tags=["local-only"])
    _route_auto_to(monkeypatch, name, reason="matched 'lights'")
    add_message = AsyncMock()
    monkeypatch.setattr("nvh.storage.repository.add_message", add_message, raising=False)
    if stream:
        engine = _stream_engine([RuntimeError("stream down")], registered=False)
    else:
        engine = _complete_engine([], registered=False)
        engine.registry.get().complete = AsyncMock(side_effect=RuntimeError("provider down"))
    engine.router.route.return_value.provider = "groq"
    offline = patch(
        "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
        return_value={"answer": "Offline answer", "actions": []},
    )

    with offline:
        result, events = await _run(
            engine, stream=stream, question="turn off the lights", home_dir=tmp_path, conversation_id="c",
        )

    envelope = events[-1] if stream else result
    assert envelope["type" if stream else "mode"] == ("error" if stream else "deterministic")
    assert envelope["fallback_reason"] == ("stream down" if stream else "provider down")
    assert envelope["used_profile"] is None
    assert envelope["profile_reason"].startswith(
        "general Wizard: local-only specialist unavailable: Ollama not configured",
    )
    assert f"would have been {name}: matched 'lights'" in envelope["profile_reason"]
    meta = _wizard_meta(add_message.await_args_list[1].kwargs["content"])
    assert meta["mode"] == "deterministic" and meta["used_profile"] is None
    assert meta["profile_reason"] == envelope["profile_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_general_wizard_turn_without_an_engine_carries_the_concierge_reason(
    tmp_path: Path, fake_registry, monkeypatch, stream: bool,
) -> None:
    """A plain auto turn the (real) concierge left to the general Wizard,
    answered offline: envelope / error event and meta carry the short human
    reason, so the UI tooltip is never blank for a routed turn."""
    from nvh.integrations.wizard import chat as chat_mod
    from nvh.integrations.wizard.concierge import GENERAL_NO_MATCH_REASON

    add_message = AsyncMock()
    monkeypatch.setattr("nvh.storage.repository.add_message", add_message, raising=False)
    offline = patch(
        "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
        return_value={"answer": "Offline answer", "actions": []},
    )

    with (
        offline,
        patch("nvh.api.server.get_engine", return_value=None),
        patch("nvh.integrations.wizard.context.wizard_context", return_value=_EMPTY),
    ):
        if stream:
            events = [
                e async for e in chat_mod.wizard_chat_stream("tell me a joke", home_dir=tmp_path, conversation_id="c")
            ]
            envelope = events[-1]
            assert envelope["type"] == "error"
        else:
            envelope = await chat_mod.wizard_chat("tell me a joke", home_dir=tmp_path, conversation_id="c")
            assert envelope["mode"] == "deterministic"

    assert envelope["fallback_reason"] == "engine not initialized"
    assert envelope["used_profile"] is None
    assert envelope["profile_reason"].startswith(GENERAL_NO_MATCH_REASON)
    meta = _wizard_meta(add_message.await_args_list[1].kwargs["content"])
    assert meta["used_profile"] is None and meta["profile_reason"] == envelope["profile_reason"]


# ───────────────────────────────────────────────────────────────────────────
# Local probe — async, never a blocking call on the event loop
# ───────────────────────────────────────────────────────────────────────────


def _fake_daemon(monkeypatch, handler) -> dict[str, Any]:
    """Point every ``httpx.AsyncClient`` at an in-process fake Ollama and make
    the blocking ``httpx.get`` an error: the probe must never call it.

    Returns a dict collecting the ``timeout`` each client was built with.
    """
    import httpx

    real_client = httpx.AsyncClient
    seen: dict[str, Any] = {"timeouts": []}

    def client(*args: Any, **kwargs: Any) -> Any:
        seen["timeouts"].append(kwargs.get("timeout"))
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    def blocking(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("blocking httpx.get on the event loop")

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(httpx, "get", blocking)
    return seen


@pytest.mark.asyncio
async def test_local_probe_is_an_async_get_of_api_tags_and_never_calls_httpx_get(
    monkeypatch, local_probe: AsyncMock,
) -> None:
    """The real probe: one ``GET {base_url}/api/tags`` through
    ``httpx.AsyncClient`` with ``LOCAL_PROBE_TIMEOUT_S``; 200 = up, any other
    status or a connection error = down; never raises."""
    import httpx

    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setattr(chat_mod, "_probe_local_provider", local_probe.real)
    outcome: dict[str, Any] = {"status": 200}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if outcome["status"] == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(outcome["status"], json={"models": [{"name": "llama3:latest"}]})

    seen = _fake_daemon(monkeypatch, handler)
    url = "http://127.0.0.1:11434"

    assert await chat_mod._local_provider_reachable(url) is True
    assert str(requests[-1].url) == f"{url}/api/tags"
    assert seen["timeouts"] == [chat_mod.LOCAL_PROBE_TIMEOUT_S]

    chat_mod._reset_local_probe_cache()
    outcome["status"] = 503
    assert await chat_mod._local_provider_reachable(url) is False

    chat_mod._reset_local_probe_cache()
    outcome["status"] = "refused"
    assert await chat_mod._local_provider_reachable(url) is False
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_local_probe_lets_other_coroutines_run_while_it_waits(
    monkeypatch, local_probe: AsyncMock,
) -> None:
    """A slow daemon must not freeze the process: while the probe waits, other
    tasks on the same loop keep running (the blocking ``httpx.get`` stalled
    every request in uvicorn for up to the timeout)."""
    import asyncio

    import httpx

    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setattr(chat_mod, "_probe_local_provider", local_probe.real)

    async def slow_daemon(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={"models": []})

    _fake_daemon(monkeypatch, slow_daemon)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    try:
        assert await chat_mod._local_provider_reachable("http://127.0.0.1:11434") is True
    finally:
        task.cancel()
    assert ticks >= 3, ticks


@pytest.mark.asyncio
async def test_prepare_turn_collects_workspace_state_off_the_event_loop(
    tmp_path: Path, fake_registry,
) -> None:
    """``wizard_context`` blocks by design (httpx.get of /api/tags, nvidia-smi,
    disk and provider probes); called inline on uvicorn's loop it froze every
    other request for the whole collection, on every turn. While a slow fake
    runs, other coroutines must keep ticking — and the attribute tests patch
    (``context.wizard_context``) must be the one that runs, off the loop's
    thread."""
    import asyncio
    import threading
    import time as _time

    from nvh.integrations.wizard import chat as chat_mod

    loop_thread = threading.current_thread()
    seen: dict[str, Any] = {}

    def slow_context(*, home_dir: Any = None) -> dict[str, Any]:
        seen["thread"] = threading.current_thread()
        seen["home_dir"] = home_dir
        _time.sleep(0.2)
        return dict(_EMPTY)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    try:
        with patch("nvh.integrations.wizard.context.wizard_context", side_effect=slow_context):
            turn = await chat_mod._prepare_turn(
                "hi", history=None, home_dir=tmp_path, enable_followup=False,
                profile="wizard", label="test", engine=None,
            )
    finally:
        task.cancel()

    assert turn.snapshot == _EMPTY
    assert seen["home_dir"] == tmp_path
    assert seen["thread"] is not loop_thread
    assert ticks >= 3, ticks


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_chat_turn_reaches_the_daemon_through_the_async_probe(
    tmp_path: Path, fake_registry, monkeypatch, local_probe: AsyncMock, stream: bool,
) -> None:
    """End to end on both paths with the real probe: the demotion check, the
    pin and the re-route all await the async client; the blocking twin is
    never touched."""
    import httpx

    from nvh.integrations.wizard import chat as chat_mod

    monkeypatch.setattr(chat_mod, "_probe_local_provider", local_probe.real)
    _fake_daemon(monkeypatch, lambda request: httpx.Response(200, json={"models": []}))
    name = _save_profile(tmp_path, "home", tags=["local-only"])
    _route_auto_to(monkeypatch, name, reason="matched 'lights'")
    engine = _stream_engine([["from ollama"]]) if stream else _complete_engine(["from ollama"])
    engine.router.route.return_value.provider = "groq"

    result, _events = await _run(engine, stream=stream, question="turn off the lights", home_dir=tmp_path)

    assert result is not None and result["answer"] == "from ollama"
    assert result["used_provider"] == "ollama" and result["used_profile"] == name


# ───────────────────────────────────────────────────────────────────────────
# Routing never uses a local provider the probe says is down
# ───────────────────────────────────────────────────────────────────────────


def _two_provider_engine(
    stream: bool,
    *,
    enabled: tuple[str, ...] = ("ollama", "groq"),
    default_provider: str = "",
    contents: list[str] | None = None,
) -> MagicMock:
    """Router → ollama (``local-first``: the broker's pick for a short query
    while ollama is registered); ``enabled`` is the registry; a
    provider-constrained route yields that provider's own model."""
    contents = contents if contents is not None else ["from the cloud"]
    engine = _stream_engine([[c] for c in contents]) if stream else _complete_engine(contents)
    engine.registry.list_enabled = MagicMock(return_value=list(enabled))
    engine.registry.has = MagicMock(side_effect=lambda n: n in enabled)
    engine.config.defaults.provider = default_provider

    def route(query: str, provider_override: str | None = None, **kw: Any) -> Any:
        # A fresh decision per call, like the real router: a re-route mutates
        # the decision it was handed and must not leak into the next turn.
        if provider_override:
            return MagicMock(
                provider=provider_override, model=f"{provider_override}/default",
                reason=f"User override: --provider {provider_override}",
            )
        return MagicMock(provider="ollama", model="ollama/x", reason="local-first")

    engine.router.route = MagicMock(side_effect=route)
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_router_pick_of_a_dead_ollama_is_rerouted_to_a_registered_cloud_provider(
    tmp_path: Path, fake_registry, local_probe: AsyncMock, stream: bool,
) -> None:
    """``RoutingEngine._try_local_first`` returns ollama for any short query
    while ollama is merely *registered*. When the probe says the daemon is
    down the turn must not go there: groq answers with its own model and the
    routing reason says so."""
    local_probe.return_value = False
    engine = _two_provider_engine(stream)

    result, events = await _run(engine, stream=stream, question="hi", profile="wizard", home_dir=tmp_path)

    assert result is not None and result["answer"] == "from the cloud"
    assert (result["used_provider"], result["used_model"]) == ("groq", "groq/default")
    assert result["routing_reason"] == "local-first; local provider unreachable, using groq"
    assert engine.registry.get.call_args_list == [call("groq")]
    assert engine.router.route.call_args_list == [call(query="hi"), call(query="hi", provider_override="groq")]
    local_probe.assert_awaited_once()
    if stream:
        assert "error" not in [e["type"] for e in events]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_router_pick_of_a_live_ollama_stands(
    tmp_path: Path, fake_registry, local_probe: AsyncMock, stream: bool,
) -> None:
    engine = _two_provider_engine(stream, contents=["from ollama"])

    result, _events = await _run(engine, stream=stream, question="hi", profile="wizard", home_dir=tmp_path)

    assert result is not None and result["answer"] == "from ollama"
    assert (result["used_provider"], result["used_model"]) == ("ollama", "ollama/x")
    assert result["routing_reason"] == "local-first"
    engine.router.route.assert_called_once_with(query="hi")
    assert engine.registry.get.call_args_list == [call("ollama")]
    local_probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_dead_ollama_with_nothing_else_registered_keeps_the_decision(
    tmp_path: Path, fake_registry, local_probe: AsyncMock,
) -> None:
    """No cloud provider to move to: the decision stands, the completion fails
    fast and the turn falls back deterministically — never a re-route to a
    provider that does not exist."""
    local_probe.return_value = False
    engine = _two_provider_engine(False, enabled=("ollama",))
    engine.registry.get.return_value.complete = AsyncMock(side_effect=RuntimeError("connection refused"))

    with patch(
        "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
        return_value={"answer": "Offline answer", "actions": []},
    ):
        result, _ = await _run(engine, stream=False, question="hi", profile="wizard", home_dir=tmp_path)

    assert result is not None and result["mode"] == "deterministic"
    assert result["fallback_reason"] == "connection refused" and result["answer"] == "Offline answer"
    assert engine.registry.get.call_args_list == [call("ollama")]
    engine.router.route.assert_called_once_with(query="hi")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_reroute_skips_a_cloud_provider_the_router_would_skip(
    tmp_path: Path, fake_registry, local_probe: AsyncMock, stream: bool,
) -> None:
    """The fallback pick honours the router's own health gate: the configured
    default (groq) has an open circuit breaker (health 0.0), so the turn
    moves to the next healthy registered provider, with its own model."""
    local_probe.return_value = False
    engine = _two_provider_engine(stream, enabled=("ollama", "groq", "nvidia"), default_provider="groq")
    health = {"ollama": 1.0, "groq": 0.0, "nvidia": 1.0}
    engine.router.rate_manager.get_health_score = MagicMock(side_effect=lambda p: health[p])

    result, events = await _run(engine, stream=stream, question="hi", profile="wizard", home_dir=tmp_path)

    assert result is not None and result["answer"] == "from the cloud"
    assert (result["used_provider"], result["used_model"]) == ("nvidia", "nvidia/default")
    assert result["routing_reason"] == "local-first; local provider unreachable, using nvidia"
    assert engine.registry.get.call_args_list == [call("nvidia")]
    assert engine.router.route.call_args_list == [call(query="hi"), call(query="hi", provider_override="nvidia")]
    if stream:
        assert "error" not in [e["type"] for e in events]


@pytest.mark.asyncio
async def test_dead_ollama_with_only_gated_alternatives_keeps_the_decision(
    tmp_path: Path, fake_registry, local_probe: AsyncMock,
) -> None:
    """Every other registered provider is one the router would skip: no
    re-route to a provider that cannot take the turn — the decision stands
    and the turn falls back deterministically, as with nothing registered."""
    local_probe.return_value = False
    engine = _two_provider_engine(False)
    engine.router.rate_manager.get_health_score = MagicMock(return_value=0.0)
    engine.registry.get.return_value.complete = AsyncMock(side_effect=RuntimeError("connection refused"))

    with patch(
        "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
        return_value={"answer": "Offline answer", "actions": []},
    ):
        result, _ = await _run(engine, stream=False, question="hi", profile="wizard", home_dir=tmp_path)

    assert result is not None and result["mode"] == "deterministic"
    assert result["fallback_reason"] == "connection refused" and result["answer"] == "Offline answer"
    assert engine.registry.get.call_args_list == [call("ollama")]
    engine.router.route.assert_called_once_with(query="hi")


def test_best_non_local_provider_prefers_the_configured_default_then_registry_order() -> None:
    from nvh.integrations.wizard import chat as chat_mod

    engine = MagicMock()
    engine.registry.list_enabled = MagicMock(return_value=["ollama", "groq", "nvidia"])
    engine.config.defaults.provider = ""
    assert chat_mod._best_non_local_provider(engine) == "groq"
    engine.config.defaults.provider = "nvidia"
    assert chat_mod._best_non_local_provider(engine) == "nvidia"
    engine.config.defaults.provider = "ollama"  # the default *is* the dead one
    assert chat_mod._best_non_local_provider(engine) == "groq"
    engine.registry.list_enabled = MagicMock(return_value=["ollama"])
    assert chat_mod._best_non_local_provider(engine) is None
    engine.registry.list_enabled = MagicMock(side_effect=RuntimeError("no registry"))
    assert chat_mod._best_non_local_provider(engine) is None


def test_best_non_local_provider_skips_what_the_router_would_skip() -> None:
    """Mirrors ``RoutingEngine.route``'s gates: health under 0.1 (``<``, so
    exactly 0.1 passes) and "no models available" (empty catalog *and* no
    configured default model the registry knows)."""
    from nvh.integrations.wizard import chat as chat_mod

    assert chat_mod.ROUTER_MIN_HEALTH == 0.1

    engine = MagicMock()
    engine.registry.list_enabled = MagicMock(return_value=["ollama", "groq", "nvidia", "openai"])
    engine.config.defaults.provider = "groq"
    health = {"ollama": 1.0, "groq": 0.0, "nvidia": 1.0, "openai": 0.1}
    engine.router.rate_manager.get_health_score = MagicMock(side_effect=lambda p: health[p])
    models: dict[str, list[Any]] = {"groq": [MagicMock()], "nvidia": [MagicMock()], "openai": [MagicMock()]}
    engine.registry.get_models_for_provider = MagicMock(side_effect=lambda p: models[p])

    # The configured default has an open breaker: the first healthy one wins.
    assert chat_mod._best_non_local_provider(engine) == "nvidia"
    assert "ollama" not in [c.args[0] for c in engine.router.rate_manager.get_health_score.call_args_list]
    # Exactly the threshold is not under it.
    health["nvidia"] = 0.05
    assert chat_mod._best_non_local_provider(engine) == "openai"

    # Healthy but nothing to run on: an empty catalog and no configured default model.
    health.update(groq=1.0, nvidia=1.0)
    models["groq"] = []
    engine.config.providers.get = MagicMock(return_value=None)
    assert chat_mod._best_non_local_provider(engine) == "nvidia"
    # ...a configured default model the registry knows counts as a model.
    pconf = MagicMock()
    pconf.default_model = "groq/llama"
    engine.config.providers.get = MagicMock(side_effect=lambda p: pconf if p == "groq" else None)
    engine.registry.get_model_info = MagicMock(return_value=MagicMock())
    assert chat_mod._best_non_local_provider(engine) == "groq"
    engine.registry.get_model_info = MagicMock(return_value=None)
    assert chat_mod._best_non_local_provider(engine) == "nvidia"
    pconf.default_model = ""
    assert chat_mod._best_non_local_provider(engine) == "nvidia"

    # Every alternative gated out: nothing to move to.
    health.update(nvidia=0.0, openai=0.0)
    assert chat_mod._best_non_local_provider(engine) is None

    # The engine's own rate manager serves when the router has none.
    health.update(groq=0.0, nvidia=1.0)
    models["groq"] = [MagicMock()]
    engine.router = None
    engine.rate_manager.get_health_score = MagicMock(side_effect=lambda p: health[p])
    assert chat_mod._best_non_local_provider(engine) == "nvidia"


def test_best_non_local_provider_without_health_data_keeps_the_heuristic() -> None:
    """No rate manager, a raising one, a non-numeric score or a registry that
    cannot list models: the gate is open and the plain default-then-registry
    order applies — never a skip on missing data."""
    from nvh.integrations.wizard import chat as chat_mod

    engine = MagicMock()  # get_health_score / get_models_for_provider return MagicMocks
    engine.registry.list_enabled = MagicMock(return_value=["ollama", "groq", "nvidia"])
    engine.config.defaults.provider = ""
    assert chat_mod._best_non_local_provider(engine) == "groq"

    engine.router.rate_manager.get_health_score = MagicMock(side_effect=RuntimeError("no breaker"))
    engine.rate_manager.get_health_score = MagicMock(side_effect=RuntimeError("no breaker"))
    assert chat_mod._best_non_local_provider(engine) == "groq"

    engine.router = None
    engine.rate_manager = None
    engine.registry.get_models_for_provider = MagicMock(side_effect=RuntimeError("no catalog"))
    assert chat_mod._best_non_local_provider(engine) == "groq"

    engine.registry.get_models_for_provider = MagicMock(return_value=[])
    engine.config.providers.get = MagicMock(side_effect=RuntimeError("no config"))
    assert chat_mod._best_non_local_provider(engine) == "groq"  # unresolvable default model: unknown, not "none"

    engine.config.defaults.provider = "nvidia"
    assert chat_mod._best_non_local_provider(engine) == "nvidia"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_demoted_local_only_turn_is_answered_on_a_live_provider_not_the_dead_one(
    tmp_path: Path, fake_registry, monkeypatch, local_probe: AsyncMock, stream: bool,
) -> None:
    """The demotion assumes the general Wizard answers; with the router still
    pointing at the dead daemon that was not true. One probe serves both the
    demotion and the re-route, and the general Wizard answers on groq."""
    name = _save_profile(tmp_path, "home", tags=["local-only"], tools_allowed=["home_assistant_status"])
    _route_auto_to(monkeypatch, name, reason="matched 'lights'")
    local_probe.return_value = False
    engine = _two_provider_engine(stream)

    result, _events = await _run(engine, stream=stream, question="turn off the lights", home_dir=tmp_path)

    assert result is not None and result["answer"] == "from the cloud"
    assert (result["used_provider"], result["used_model"]) == ("groq", "groq/default")
    assert result["routing_reason"] == "local-first; local provider unreachable, using groq"
    assert result["used_profile"] is None
    assert result["profile_reason"].startswith(
        "general Wizard: local-only specialist unavailable: Ollama not running",
    )
    engine.registry.has.assert_called_once_with("ollama")
    local_probe.assert_awaited_once()
    assert engine.registry.get.call_args_list == [call("groq")]


@pytest.mark.asyncio
async def test_library_profile_pinned_to_a_dead_ollama_is_rerouted_with_both_notes(
    tmp_path: Path, fake_registry, local_probe: AsyncMock,
) -> None:
    """A non-local-only profile pinned to ollama, the router also on ollama,
    daemon down: the pin note no longer claims to be "using the router's
    ollama", and the re-route moves the turn to groq."""
    name = _save_profile(tmp_path, "ollama-pinned", provider="ollama", model="ollama/qwen2.5-coder:7b")
    local_probe.return_value = False
    engine = _two_provider_engine(False)

    result, _ = await _run(engine, stream=False, question="review this", profile=name, home_dir=tmp_path)

    assert result is not None and result["mode"] == "llm"
    assert (result["used_provider"], result["used_model"]) == ("groq", "groq/default")
    assert result["routing_reason"] == (
        "local-first; profile_provider_unavailable: 'ollama' is not running; "
        "local provider unreachable, using groq"
    )
    assert result["used_profile"] == name
    local_probe.assert_awaited_once()  # the pin's probe is what the re-route reads


# ───────────────────────────────────────────────────────────────────────────
# Probe cache — short negative TTL, dropped on daemon-touching events
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_negative_probe_is_trusted_for_seconds_a_positive_one_for_the_full_ttl(
    local_probe: AsyncMock,
) -> None:
    """"Start it and ask again" has to work within seconds: a "down" answer
    expires after LOCAL_PROBE_NEGATIVE_TTL_S while "up" keeps LOCAL_PROBE_TTL_S."""
    from nvh.integrations.wizard import chat as chat_mod

    assert chat_mod.LOCAL_PROBE_NEGATIVE_TTL_S == 5.0 < chat_mod.LOCAL_PROBE_TTL_S == 30.0
    url = "http://127.0.0.1:11434"
    local_probe.return_value = False
    assert await chat_mod._local_provider_reachable(url) is False
    assert await chat_mod._local_provider_reachable(url) is False
    assert local_probe.await_count == 1  # inside the negative TTL: cached

    stamp, ok = chat_mod._LOCAL_PROBE_CACHE[url]
    assert ok is False
    # Older than the negative TTL but well inside the positive one: re-probed.
    chat_mod._LOCAL_PROBE_CACHE[url] = (stamp - chat_mod.LOCAL_PROBE_NEGATIVE_TTL_S - 0.5, False)
    local_probe.return_value = True
    assert await chat_mod._local_provider_reachable(url) is True
    assert local_probe.await_count == 2

    # The same age on a positive answer is still trusted.
    stamp, ok = chat_mod._LOCAL_PROBE_CACHE[url]
    assert ok is True
    chat_mod._LOCAL_PROBE_CACHE[url] = (stamp - chat_mod.LOCAL_PROBE_NEGATIVE_TTL_S - 0.5, True)
    assert await chat_mod._local_provider_reachable(url) is True
    assert local_probe.await_count == 2


@pytest.mark.asyncio
async def test_probe_cache_entry_is_stamped_when_the_probe_finishes(
    monkeypatch, local_probe: AsyncMock,
) -> None:
    """A probe that runs into LOCAL_PROBE_TIMEOUT_S must not enter the cache
    already that far into its TTL: the entry is stamped when the probe
    *finishes*, so a "down" answer is trusted for the whole negative TTL
    (stamped at the start it was 30% expired on arrival and the daemon got
    re-probed early)."""
    from types import SimpleNamespace

    from nvh.integrations.wizard import chat as chat_mod

    clock = {"now": 100.0}
    monkeypatch.setattr(chat_mod, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    async def probe_that_times_out(url: str) -> bool:
        clock["now"] += chat_mod.LOCAL_PROBE_TIMEOUT_S
        return False

    local_probe.side_effect = probe_that_times_out
    url = "http://127.0.0.1:11434"

    assert await chat_mod._local_provider_reachable(url) is False
    finished = 100.0 + chat_mod.LOCAL_PROBE_TIMEOUT_S
    assert chat_mod._LOCAL_PROBE_CACHE[url] == (finished, False)

    # Just inside the negative TTL measured from completion: still trusted.
    # Measured from the start the entry would already have expired here.
    clock["now"] = finished + chat_mod.LOCAL_PROBE_NEGATIVE_TTL_S - 0.1
    assert clock["now"] - 100.0 > chat_mod.LOCAL_PROBE_NEGATIVE_TTL_S
    assert await chat_mod._local_provider_reachable(url) is False
    assert local_probe.await_count == 1

    # Past the negative TTL from completion: re-probed, and re-stamped at the end.
    clock["now"] = finished + chat_mod.LOCAL_PROBE_NEGATIVE_TTL_S + 0.1
    assert await chat_mod._local_provider_reachable(url) is False
    assert local_probe.await_count == 2
    assert chat_mod._LOCAL_PROBE_CACHE[url] == (clock["now"], False)


@pytest.mark.asyncio
async def test_module_probe_fixture_wins_over_the_conftest_double_and_keeps_the_real_coroutine(
    local_probe: AsyncMock,
) -> None:
    """``tests/conftest.py`` installs a hermetic probe double for every suite;
    this module's fixture must be the one in place (it sets up later) and
    ``local_probe.real`` must still be the genuine coroutine, not the
    conftest double — the fake-daemon tests above depend on it."""
    import inspect

    from nvh.integrations.wizard import chat as chat_mod

    assert chat_mod._probe_local_provider is local_probe
    assert not isinstance(local_probe.real, AsyncMock)
    assert inspect.iscoroutinefunction(local_probe.real)
    assert (local_probe.real.__module__, local_probe.real.__name__) == (chat_mod.__name__, "_probe_local_provider")
    assert chat_mod._LOCAL_PROBE_CACHE == {}


_REPAIR = 'TOOL_CALL: {"name": "repair_workspace", "arguments": {}}'


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_call, tool", [(_REFRESH, "refresh_models"), (_REPAIR, "repair_workspace")])
async def test_running_a_daemon_touching_tool_forgets_the_probe(
    tmp_path: Path, fake_registry, local_probe: AsyncMock, tool_call: str, tool: str,
) -> None:
    """Turn 1: ollama down → groq answers and the model runs refresh_models /
    repair_workspace. Turn 2, seconds later: the negative answer is *not*
    reused — the daemon is probed again and, now up, answers itself."""
    from nvh.integrations.wizard import chat as chat_mod

    assert chat_mod.LOCAL_PROBE_INVALIDATING_TOOLS == frozenset({"refresh_models", "repair_workspace"})
    reg, counters = fake_registry
    if tool not in counters:
        counters[tool] = _Counter()
        reg.register(WizardTool(
            name=tool, description="Safe repairs.", safety_class="auto", parameters={}, handler=counters[tool],
        ))
    name = _save_profile(tmp_path, "fixer", tools_allowed=[tool])
    local_probe.return_value = False
    engine = _two_provider_engine(False, contents=[f"Refreshing.\n{tool_call}", "Done.", "from ollama"])

    with _patched(engine):
        first = await chat_mod.wizard_chat("fix ollama", profile=name, home_dir=tmp_path)
        assert chat_mod._LOCAL_PROBE_CACHE == {}  # the tool ran: nothing cached any more
        local_probe.return_value = True
        second = await chat_mod.wizard_chat("hi again", profile=name, home_dir=tmp_path)

    assert first["used_provider"] == "groq" and first["iterations"] == 2 and first["answer"] == "Done."
    assert counters[tool].calls == [{}]
    assert second["used_provider"] == "ollama" and second["answer"] == "from ollama"
    assert local_probe.await_count == 2


@pytest.mark.asyncio
async def test_a_refused_daemon_tool_does_not_forget_the_probe(
    tmp_path: Path, fake_registry, local_probe: AsyncMock,
) -> None:
    """Only a tool that actually ran can have changed the daemon: a whitelist
    refusal of refresh_models leaves the cached "down" in place."""
    from nvh.integrations.wizard import chat as chat_mod

    name = _save_profile(tmp_path, "vault-only", tools_allowed=["rag_ask_vault"])
    local_probe.return_value = False
    engine = _two_provider_engine(False, contents=[f"Refreshing.\n{_REFRESH}", "from the cloud again"])

    with _patched(engine):
        first = await chat_mod.wizard_chat("fix ollama", profile=name, home_dir=tmp_path)
        second = await chat_mod.wizard_chat("hi again", profile=name, home_dir=tmp_path)

    assert first["tool_results"][0]["result"]["not_allowed"] is True
    assert (first["used_provider"], second["used_provider"]) == ("groq", "groq")
    assert local_probe.await_count == 1  # still the cached "down"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_failed_completion_on_ollama_forgets_the_positive_probe(
    tmp_path: Path, fake_registry, local_probe: AsyncMock, stream: bool,
) -> None:
    """The daemon died after the probe said "up": the failure drops the cached
    answer so the next turn probes again and, still down, goes to groq."""
    from nvh.integrations.wizard import chat as chat_mod

    engine = _two_provider_engine(stream)
    if stream:
        engine.registry.get.return_value.stream = MagicMock(side_effect=RuntimeError("ollama went away"))
    else:
        engine.registry.get.return_value.complete = AsyncMock(side_effect=RuntimeError("ollama went away"))
    offline = patch(
        "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
        return_value={"answer": "Offline answer", "actions": []},
    )

    with offline:
        result, events = await _run(engine, stream=stream, question="hi", profile="wizard", home_dir=tmp_path)
    envelope = events[-1] if stream else result
    assert envelope["fallback_reason"] == "ollama went away"
    assert chat_mod._LOCAL_PROBE_CACHE == {}
    assert local_probe.await_count == 1

    local_probe.return_value = False
    second, _ = await _run(_two_provider_engine(False), stream=False, question="hi", profile="wizard", home_dir=tmp_path)
    assert second is not None and second["used_provider"] == "groq"
    assert local_probe.await_count == 2


@pytest.mark.asyncio
async def test_a_failed_completion_on_a_cloud_provider_keeps_the_probe(
    tmp_path: Path, fake_registry, local_probe: AsyncMock,
) -> None:
    """A cloud failure says nothing about the local daemon."""
    import time

    from nvh.integrations.wizard import chat as chat_mod
    from nvh.utils.ollama import ollama_base_url

    name = _save_profile(tmp_path, "groq-pinned", provider="groq", model="groq/llama")
    chat_mod._LOCAL_PROBE_CACHE[ollama_base_url()] = (time.monotonic(), True)
    engine = _complete_engine([])
    engine.registry.get.return_value.complete = AsyncMock(side_effect=RuntimeError("groq 500"))

    with patch(
        "nvh.integrations.wizard.setup_agent.setup_assistant_reply",
        return_value={"answer": "Offline answer", "actions": []},
    ):
        result, _ = await _run(engine, stream=False, question="hi", profile=name, home_dir=tmp_path)

    assert result is not None and result["fallback_reason"] == "groq 500"
    assert ollama_base_url() in chat_mod._LOCAL_PROBE_CACHE
    local_probe.assert_not_awaited()


# ───────────────────────────────────────────────────────────────────────────
# Wording — "not running" and "not configured" are different problems
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_pinned_local_only_refusal_names_the_actual_problem_and_its_fix(
    tmp_path: Path, fake_registry, local_probe: AsyncMock, stream: bool,
) -> None:
    """Telling someone to start a daemon they never enabled sends them the
    wrong way: the refusal is worded from the probe's own verdict."""
    name = _save_profile(tmp_path, "local-notes", tags=["local-only"])

    # Ollama not in the registry (not enabled in config): enable it.
    engine = _stream_engine([["never"]], registered=False) if stream else _complete_engine(["never"], registered=False)
    engine.router.route.return_value.provider = "groq"
    result, events = await _run(engine, stream=stream, question="notes?", profile=name, home_dir=tmp_path)
    text = events[0]["fallback"] if stream else result["answer"]
    assert text == (
        "Local-Notes needs a local model; Ollama is not configured. "
        "Enable it on the Providers page and ask again, or pick another agent with /agent."
    )
    local_probe.assert_not_awaited()

    # Registered but the daemon is down: start it.
    local_probe.return_value = False
    engine = _stream_engine([["never"]]) if stream else _complete_engine(["never"])
    engine.router.route.return_value.provider = "groq"
    result, events = await _run(engine, stream=stream, question="notes?", profile=name, home_dir=tmp_path)
    text = events[0]["fallback"] if stream else result["answer"]
    assert text == (
        "Local-Notes needs a local model; Ollama is not running. "
        "Start it and ask again, or pick another agent with /agent."
    )
    local_probe.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("registered, state", [(False, "not configured"), (True, "not running")])
async def test_demotion_note_says_whether_ollama_is_missing_or_down(
    tmp_path: Path, fake_registry, monkeypatch, local_probe: AsyncMock, registered: bool, state: str,
) -> None:
    name = _save_profile(tmp_path, "home", tags=["local-only"])
    _route_auto_to(monkeypatch, name, reason="matched 'lights'")
    local_probe.return_value = False  # only consulted when registered
    engine = _complete_engine(["from the cloud"], registered=registered)
    engine.router.route.return_value.provider = "groq"

    result, _ = await _run(engine, stream=False, question="turn off the lights", home_dir=tmp_path)

    assert result is not None and result["used_profile"] is None
    assert result["profile_reason"] == (
        f"general Wizard: local-only specialist unavailable: Ollama {state}; "
        f"would have been {name}: matched 'lights'"
    )
    assert local_probe.await_count == (1 if registered else 0)
