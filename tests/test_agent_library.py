"""Contract + behavioral tests for the packaged Agent Library.

The library (nvh/catalog/agent-library.json, added 2026-08-05) ships 100
original in-house-authored agent profiles across ~38 categories, plus the
Smart Home pair, the Setup Concierge and the Model Sommelier added
2026-09-02 and the privileged pair — the Device Settings desk and the App
Installer — added 2026-09-03. These tests pin the catalog's integrity invariants and the
loader's ordering contract so a bad regeneration or a packaging slip fails
CI instead of shipping a broken /agents page.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from nvh.integrations.wizard.profiles import (
    BUILT_IN_PROFILES,
    _load_library_profiles,
    list_profiles,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "nvh" / "catalog" / "agent-library.json"

VALID_TOOLS = {
    "diagnose", "refresh_models", "repair_workspace", "validate_provider_key",
    "save_provider_key", "rag_ask", "rag_ask_vault", "rag_ingest",
    "web_search",
    "home_assistant_status", "home_assistant_entities", "home_assistant_state",
    "home_assistant_services", "home_assistant_call",
    # The privileged tier (proposal §3.4). ``system_settings_get`` / ``_plan``
    # are auto (read-only facts and a dry run); ``_apply``, ``apt_install``,
    # ``snap_install`` and ``service_enable`` render a red approval card.
    "system_settings_get", "system_settings_plan", "system_settings_apply",
    "apt_install", "snap_install", "service_enable",
    # The Spark playbooks (proposal §3.5). ``playbook_list`` / ``playbook_plan``
    # are auto (the catalogue and a compiled plan); ``playbook_install`` is
    # privileged and renders the red card.
    "playbook_list", "playbook_plan", "playbook_install",
}
# 100 original profiles (2026-08-05) + the two Smart Home profiles + the
# Setup Concierge + the Model Sommelier (all 2026-09-02) + Device Settings
# + the App Installer (both 2026-09-03).
LIBRARY_SIZE = 106
# Distinct categories; Setup is the newest and, for now, has one member.
# The sommelier, the settings desk and the app installer joined Ops (now
# eleven members) and added no category.
CATEGORY_COUNT = 40
OPS_COUNT = 11
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# The first-run guide (proposal §3.2). The concierge routes onboarding
# questions here; it must bind exactly the tools its four steps use, plus
# the two read-only playbook tools (2026-09-03) so the tour can propose an
# install and hand the run to the App Installer — never ``playbook_install``.
SETUP_CONCIERGE_TOOLS = [
    "diagnose", "refresh_models", "repair_workspace",
    "validate_provider_key", "save_provider_key", "rag_ask_vault",
    "playbook_list", "playbook_plan",
]
# The model sommelier (2026-09-02): reads the platform block, checks the
# shelf, recommends, and hands over the pull command without running it.
MODEL_SOMMELIER_TOOLS = ["refresh_models", "diagnose", "web_search", "rag_ask_vault"]
# The device settings desk (2026-09-03, proposal §3.4): the privileged tier's
# read / plan / apply triple, the three package-and-service actions, and
# diagnose. Order is the contract — the read comes first because the prompt
# must call it first.
DEVICE_SETTINGS_TOOLS = [
    "system_settings_get", "system_settings_plan", "system_settings_apply",
    "apt_install", "snap_install", "service_enable", "diagnose",
]
# The app installer (2026-09-03, proposal §3.5): the Spark playbooks' list /
# plan / install triple and diagnose. Order is the contract — the list comes
# first because the prompt must call it first.
APP_INSTALLER_TOOLS = ["playbook_list", "playbook_plan", "playbook_install", "diagnose"]
# The privileged tier's allowlist: which profile may bind which privileged
# tool. Strict on purpose — a stray library edit cannot hand ``apt_install``
# or ``playbook_install`` to a general-purpose persona, and the two desks do
# not borrow each other's tools.
PRIVILEGED_TOOL_OWNERS = {
    "device-settings": {
        "system_settings_get", "system_settings_plan", "system_settings_apply",
        "apt_install", "snap_install", "service_enable",
    },
    "app-installer": {"playbook_install"},
}
# Ops specialists the concierge routes trouble reports to: each must be able
# to run `diagnose`, the tool whose description says to run it when the user
# reports trouble.
OPS_TROUBLESHOOTERS = {
    "install-medic", "gpu-triage", "provider-keysmith",
    "latency-tuner", "model-sommelier", "model-librarian", "vram-planner",
    "device-settings", "app-installer",
}
# Profiles whose prompt reasons about the machine's memory: they must take
# the figure from the platform block, never hard-code one (the Spark ships
# in more than one memory size). Bandwidth figures ("273 GB/s") are fine.
PLATFORM_AWARE_PROFILES = {"setup-concierge", "model-sommelier"}
MEMORY_FIGURE_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:GB|GiB|TB|gigabytes?)\b(?!/s)", re.IGNORECASE)
# Profiles whose context is the user's home (occupancy, locks, cameras):
# pinned to the local provider and tagged so chat.py refuses cloud routing.
LOCAL_ONLY_PROFILES = {"home-assistant", "home-automation-planner"}


def _catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_catalog_formatting_is_canonical() -> None:
    """indent=1, ensure_ascii=False, no trailing newline — a regeneration
    that reformats the file produces a 1,700-line diff for nothing."""
    raw = CATALOG.read_text(encoding="utf-8")
    assert raw == json.dumps(json.loads(raw), indent=1, ensure_ascii=False)


def test_ops_troubleshooters_can_diagnose() -> None:
    by_name = {p["name"]: p for p in _catalog()["profiles"]}
    for name in sorted(OPS_TROUBLESHOOTERS):
        tools = by_name[name]["tools_allowed"]
        assert tools is not None and "diagnose" in tools, f"{name} cannot diagnose: {tools}"


def test_smart_home_profiles_are_local_only() -> None:
    by_name = {p["name"]: p for p in _catalog()["profiles"]}
    for name in sorted(LOCAL_ONLY_PROFILES):
        p = by_name[name]
        assert p["provider"] == "ollama", name
        assert p["model"] == "", f"{name}: leave the model to the router"
        assert "local-only" in p["tags"], name


def test_setup_concierge_profile_contract() -> None:
    profiles = _catalog()["profiles"]
    p = {x["name"]: x for x in profiles}["setup-concierge"]
    assert p["title"] == "Setup Concierge"
    assert p["category"] == "Setup"
    assert p["tools_allowed"] == SETUP_CONCIERGE_TOOLS
    assert p["provider"] == "" and p["model"] == "", "leave routing to the router"
    assert p["temperature"] == 0.3
    assert p["tags"] == ["setup", "first-run", "spark"]
    prompt = p["system_prompt"]
    # The five duties of the brief: platform-aware, one step at a time in
    # order, diagnose on trouble, honest about sudo, short with a next action.
    for phrase in (
        "platform", "DGX Spark", "unified pool", "MoE", "cloud desktop", "NVH_HOME",
        "ONE step at a time", "refresh_models", "apt upgrade", "validate_provider_key",
        "save_provider_key", "diagnose", "can_sudo", "in_sudo_group", "next single action",
        # (5) Spark playbooks (2026-09-03): propose from the catalogue, prefer
        # the rootless pack, show the plan, and hand the run to the App
        # Installer — the concierge itself never installs.
        "playbook_list", "playbook_plan", "rootless studio pack", "App Installer",
        "never install anything yourself", "`nvh playbook install <id>`",
    ):
        assert phrase in prompt, phrase
    assert "playbook_install" not in prompt and "playbook_install" not in p["tools_allowed"]
    # Setup is its own category with the concierge as its only member.
    categories = Counter(x["category"] for x in profiles)
    assert categories["Setup"] == 1
    assert len(categories) == CATEGORY_COUNT


def test_model_sommelier_profile_contract() -> None:
    profiles = _catalog()["profiles"]
    by_name = {x["name"]: x for x in profiles}
    p = by_name["model-sommelier"]
    assert p["title"] == "Model Sommelier"
    assert p["category"] == "Ops"
    assert "\n" not in p["description"] and len(p["description"]) < 260
    assert p["tools_allowed"] == MODEL_SOMMELIER_TOOLS
    assert p["provider"] == "" and p["model"] == "", "leave routing to the router"
    assert p["temperature"] == 0.2
    assert p["tags"] == ["models", "spark", "ops"]
    prompt = p["system_prompt"]
    # The brief: which model for THIS machine, from the platform block;
    # bandwidth-aware (MoE first on a unified pool); reads the shelf; at most
    # two picks with quant and context; two-sentence trade-off; ends with the
    # pull command and never pulls.
    for phrase in (
        "WHICH local model", "THIS machine", "platform block", "unified pool minus the OS reserve",
        "memory_available_gb", "MemAvailable", "never quote a memory figure", "273 GB/s",
        "dense 70B", "MoE", "Nemotron 3", "gpt-oss", "refresh_models", "at most two picks",
        "quantization", "context length", "two sentences", "`nvh models pull <tag>`",
        "do not pull the model yourself",
    ):
        assert phrase in prompt, phrase
    # Ops gained a member and no category was added; the shelf next door is
    # still the librarian's.
    categories = Counter(x["category"] for x in profiles)
    assert categories["Ops"] == OPS_COUNT
    assert len(categories) == CATEGORY_COUNT
    assert by_name["model-librarian"]["category"] == "Ops"


def test_device_settings_profile_contract() -> None:
    """The privileged tier's specialist (proposal §3.4 and §5 "Sudo reality").

    Its prompt is the safety contract the model is held to: read the state
    before planning, plan before applying, never ask for a password, and know
    the DGX OS traps that a generic Linux answer would walk into.
    """
    profiles = _catalog()["profiles"]
    by_name = {x["name"]: x for x in profiles}
    p = by_name["device-settings"]
    assert p["title"] == "Device Settings"
    assert p["category"] == "Ops"
    assert "\n" not in p["description"] and len(p["description"]) < 260
    assert p["tools_allowed"] == DEVICE_SETTINGS_TOOLS
    assert p["provider"] == "" and p["model"] == "", "leave routing to the router"
    assert p["temperature"] == 0.2
    # ``strict-tools`` keeps the whitelist exactly these seven when the
    # concierge routes the turn: without it chat.py unions the core auto
    # tools onto a concierge-chosen profile (refresh_models, rag_ask_vault).
    assert p["tags"] == ["ops", "spark", "privileged", "strict-tools"]
    prompt = p["system_prompt"]
    for phrase in (
        # (a) reads the platform block and is honest about what it can do.
        "platform block", "can_sudo", "in_sudo_group", "their own terminal",
        # (b) get, then plan, then apply behind the red card.
        "system_settings_get first", "system_settings_plan", "system_settings_apply",
        "undo", "red card",
        # (c) the DGX OS traps.
        "apt upgrade", "stranded the GPU driver", "validated update channel",
        "hold_nvidia_driver_packages", "GDM greeter", "headless",
        "disable_headless_suspend", "OOBE user", "docker group",
        "usermod -aG docker $USER", "tailscale0", "lock the user out",
        "Wi-Fi profiles are per user",
        # (d) never a password. (e) short, one change at a time.
        "Never ask for, accept, or repeat back a password", "no password parameter",
        "one change at a time", "single next action", "diagnose",
    ):
        assert phrase in prompt, phrase
    # It must not promise to run something it cannot: no bare "I will run
    # sudo" and no invented password prompt.
    assert "your password" not in prompt.lower()
    # Ops gained a member and no category was added.
    categories = Counter(x["category"] for x in profiles)
    assert categories["Ops"] == OPS_COUNT
    assert len(categories) == CATEGORY_COUNT


def test_app_installer_profile_contract() -> None:
    """The Spark playbooks' specialist (proposal §3.2 roster row and §3.5).

    Its prompt is the safety contract the model is held to: list before
    planning, plan before installing, prefer the rootless pack, one hand-off
    command when sudo needs a password, never a password, never a bare
    ``apt upgrade``, one install at a time.
    """
    profiles = _catalog()["profiles"]
    by_name = {x["name"]: x for x in profiles}
    p = by_name["app-installer"]
    assert p["title"] == "App Installer"
    assert p["category"] == "Ops"
    assert "\n" not in p["description"] and len(p["description"]) < 260
    assert p["tools_allowed"] == APP_INSTALLER_TOOLS
    assert p["provider"] == "" and p["model"] == "", "leave routing to the router"
    assert p["temperature"] == 0.2
    # ``strict-tools`` keeps the whitelist exactly these four when the
    # concierge routes the turn (see the device-settings contract).
    assert p["tags"] == ["ops", "spark", "privileged", "strict-tools"]
    prompt = p["system_prompt"]
    for phrase in (
        # (a) reads the platform block, lists first, prefers the rootless pack.
        "platform block", "can_sudo", "in_sudo_group", "playbook_list first",
        "rootless_alternative", "prefer the rootless studio pack", "NVH_HOME",
        # (b) the compiled plan — sudo, manual, time/disk, undo — before the
        # red card; never claims a run that has not happened.
        "playbook_plan", "which steps need sudo", "MANUAL steps", "estimated time and disk",
        "undo preview", "playbook_install", "red card", "never speak as though it has run",
        # The research policies: pipe-to-shell verbatim + flagged, docker
        # group re-login (never newgrp), tokens declared not stored, undo as
        # preview, Update Now never automated.
        "pipe-to-shell: unpinned", "log out and back in", "never newgrp",
        "HF_TOKEN", "NGC_API_KEY", "never stores them", "Uninstall commands are a preview",
        "Update Now",
        # (c) the one hand-off command, verbatim.
        "`nvh playbook install <id>`", "nvHive never sees it",
        # (d) never a password, never a bare apt upgrade.
        "Never ask for, accept, or repeat back a password", "no password parameter",
        "apt upgrade", "validated update channel",
        # (e) one at a time, short.
        "One install at a time", "short answers", "single next action", "diagnose",
    ):
        assert phrase in prompt, phrase
    assert "your password" not in prompt.lower()
    # No hand-typed memory figure: the platform block owns the numbers.
    assert MEMORY_FIGURE_RE.search(prompt) is None
    # Ops gained a member and no category was added.
    categories = Counter(x["category"] for x in profiles)
    assert categories["Ops"] == OPS_COUNT
    assert len(categories) == CATEGORY_COUNT


def test_privileged_tools_are_bound_only_by_their_desks() -> None:
    """The privileged tier is a strict allowlist (:data:`PRIVILEGED_TOOL_OWNERS`):
    ``system_settings_*`` and the package / service actions only on the
    settings desk, ``playbook_install`` only on the app installer, and
    neither desk borrows the other's. A stray library edit cannot hand a
    privileged tool to a general-purpose persona. A profile with no whitelist
    (``null``) is filtered by the registry instead."""
    privileged = set().union(*PRIVILEGED_TOOL_OWNERS.values())
    for p in _catalog()["profiles"]:
        tools = p.get("tools_allowed")
        if tools is None:
            continue
        overlap = privileged & set(tools)
        allowed = PRIVILEGED_TOOL_OWNERS.get(p["name"], set())
        assert overlap <= allowed, (p["name"], sorted(overlap - allowed))
    # ... and each owner does bind its own set, so the allowlist is not stale.
    by_name = {x["name"]: x for x in _catalog()["profiles"]}
    for owner, tools in PRIVILEGED_TOOL_OWNERS.items():
        assert tools <= set(by_name[owner]["tools_allowed"]), owner
    # The read-only playbook tools are auto: the concierge may hold them, the
    # privileged install it may not.
    assert {"playbook_list", "playbook_plan"} <= set(by_name["setup-concierge"]["tools_allowed"])
    assert "playbook_install" not in by_name["setup-concierge"]["tools_allowed"]


def test_platform_aware_prompts_quote_no_memory_figure() -> None:
    """Review 2026-09-02 (4): the concierge said '128 GB on the standard
    unit'. Memory comes from the platform block; only bandwidth may be a
    number."""
    by_name = {x["name"]: x for x in _catalog()["profiles"]}
    for name in sorted(PLATFORM_AWARE_PROFILES):
        prompt = by_name[name]["system_prompt"]
        hit = MEMORY_FIGURE_RE.search(prompt)
        assert hit is None, f"{name} hard-codes a memory figure: {hit.group(0)!r}"
        assert "128" not in prompt, name
        assert "memory_total_gb" in prompt, f"{name} must read the platform block"
    # The concierge names the pool the platform block reports, not a size.
    concierge = by_name["setup-concierge"]["system_prompt"]
    assert "unified pool shared with the OS" in concierge
    assert "reports as memory_total_gb" in concierge
    # The regex itself lets a bandwidth figure through and catches a size.
    assert MEMORY_FIGURE_RE.search("a 273 GB/s unified pool") is None
    assert MEMORY_FIGURE_RE.search("128 GB on the standard unit")


def test_catalog_has_exactly_the_pinned_profile_count() -> None:
    assert len(_catalog()["profiles"]) == LIBRARY_SIZE


def test_catalog_names_are_unique_kebab_and_collision_free() -> None:
    profiles = _catalog()["profiles"]
    names = [p["name"] for p in profiles]
    assert len(names) == len(set(names)), "duplicate profile names"
    builtin_names = {p.name for p in BUILT_IN_PROFILES}
    for name in names:
        assert NAME_RE.match(name), f"non-kebab name: {name!r}"
        assert name not in builtin_names, f"collides with core built-in: {name}"


def test_catalog_entries_are_well_formed() -> None:
    for p in _catalog()["profiles"]:
        assert p["title"].strip(), p["name"]
        assert p["category"].strip(), p["name"]
        assert p["description"].strip(), p["name"]
        # A real persona, not a stub: at least two sentences of prompt.
        assert len(p["system_prompt"]) >= 120, f"thin persona: {p['name']}"
        assert 0.0 <= p["temperature"] <= 1.0, p["name"]
        tools = p.get("tools_allowed")
        if tools is not None:
            assert tools, f"empty whitelist (use null for all): {p['name']}"
            assert set(tools) <= VALID_TOOLS, f"unknown tool in {p['name']}"
        assert 1 <= len(p.get("tags", [])) <= 4, p["name"]


def test_library_loader_returns_all_profiles_marked_built_in() -> None:
    lib = _load_library_profiles()
    assert len(lib) == LIBRARY_SIZE
    assert all(p.built_in for p in lib)
    assert all(p.category for p in lib)
    # Avatars route through the same endpoint as core built-ins.
    assert all(p.avatar.startswith("/v1/wizard/profiles/") for p in lib)


def test_list_profiles_ordering_core_then_library_then_user(tmp_path) -> None:
    profiles = list_profiles(home_dir=tmp_path)
    # 6 core + the library (fresh home has no user profiles).
    assert len(profiles) == 6 + LIBRARY_SIZE
    core_names = [p.name for p in BUILT_IN_PROFILES]
    assert [p.name for p in profiles[:6]] == core_names
    # Library block is sorted by (category, name).
    lib_block = profiles[6:]
    keys = [(p.category, p.name) for p in lib_block]
    assert keys == sorted(keys)


def test_user_profile_overrides_library_entry(tmp_path) -> None:
    lib_name = _catalog()["profiles"][0]["name"]
    pdir = tmp_path / "agent-profiles"
    pdir.mkdir(parents=True)
    (pdir / f"{lib_name}.yaml").write_text(
        f"name: {lib_name}\ntitle: My Override\ndescription: mine\n",
        encoding="utf-8",
    )
    profiles = list_profiles(home_dir=tmp_path)
    match = [p for p in profiles if p.name == lib_name]
    assert len(match) == 1, "override must not duplicate the entry"
    assert match[0].title == "My Override"
    assert match[0].built_in is False


def test_catalog_ships_in_package_data() -> None:
    """The wheel must include the catalog — pyproject's package-data glob
    nvh = ["catalog/*.json", ...] covers it; this guards the glob against
    a future rename that silently drops the library from installs."""
    from importlib import resources

    text = resources.files("nvh.catalog").joinpath("agent-library.json").read_text(
        encoding="utf-8"
    )
    assert '"profiles"' in text


# Local specialists that pinned ``ollama/qwen2.5-coder:7b`` or ``ollama/nemotron``
# until 2026-09-02, when both tags had left the registry. They keep provider
# ``ollama`` and pin no model, so the tier table's pick for this machine applies.
LOCAL_UNPINNED_PROFILES = {
    "deep-reviewer", "legacy-cartographer", "appsec-auditor", "meeting-distiller",
    "vault-gardener", "daily-notes-coach", "zettelkasten-clerk", "sql-explainer",
    "contract-reader", "comfyui-workflow-debugger", "transcript-cleaner",
    "blender-assistant", "resume-editor", "meeting-scribe", "accessibility-reviewer",
}
# The vision trio keeps its pin: llama3.2-vision is a live tier-table pick.
LOCAL_VISION_PROFILES = {"chart-advisor", "render-critique", "alt-text-writer"}


def test_library_pins_no_model_outside_the_tier_table() -> None:
    """A profile either leaves the model to the router (``""``) or pins a tag the
    tier table carries; no profile may name a tag the registry retired."""
    import nvh.core.local_models as lm

    table = {f"ollama/{tag}" for tag in lm.all_tags()}
    for p in _catalog()["profiles"]:
        model = p["model"]
        if p["provider"] == "ollama":
            assert model == "" or model in table, f"{p['name']} pins {model!r}, not a table tag"
        else:
            assert not model.startswith("ollama/"), f"{p['name']} pins a local model on {p['provider']!r}"


def test_local_specialists_defer_to_the_table_pick() -> None:
    by_name = {p["name"]: p for p in _catalog()["profiles"]}
    for name in sorted(LOCAL_UNPINNED_PROFILES):
        p = by_name[name]
        assert p["provider"] == "ollama", name
        assert p["model"] == "", f"{name}: leave the model to the local default"
    for name in sorted(LOCAL_VISION_PROFILES):
        assert by_name[name]["model"] == "ollama/llama3.2-vision", name
    pinned = {p["name"] for p in by_name.values() if p["model"]}
    assert pinned == LOCAL_VISION_PROFILES, pinned ^ LOCAL_VISION_PROFILES
