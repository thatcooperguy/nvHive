"""Contract + behavioral tests for the packaged Agent Library.

The library (nvh/catalog/agent-library.json, added 2026-08-05) ships 100
original in-house-authored agent profiles across ~38 categories. These
tests pin the catalog's integrity invariants and the loader's ordering
contract so a bad regeneration or a packaging slip fails CI instead of
shipping a broken /agents page.
"""

from __future__ import annotations

import json
import re
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
}
# 100 original profiles (2026-08-05) + the two Smart Home profiles (2026-09-02).
LIBRARY_SIZE = 102
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# Ops specialists the concierge routes trouble reports to: each must be able
# to run `diagnose`, the tool whose description says to run it when the user
# reports trouble.
OPS_TROUBLESHOOTERS = {
    "install-medic", "gpu-triage", "provider-keysmith",
    "latency-tuner", "model-librarian", "vram-planner",
}
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
