"""Tests for setup catalog fallback and status."""

from __future__ import annotations

import json
from pathlib import Path

import nvh.core.local_models as lm
from nvh.integrations import setup_catalog
from nvh.integrations.installs.studio_packs import STUDIO_MODELS


def test_bundled_catalog_has_student_profiles() -> None:
    data = setup_catalog.bundled_catalog()

    assert data["schema_version"] == setup_catalog.SCHEMA_VERSION
    assert {profile["id"] for profile in data["profiles"]} >= {"student", "creator", "music", "full"}
    assert data["models"]
    assert data["comfyui_examples"]


def test_catalog_status_uses_bundled_without_network(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))

    status = setup_catalog.catalog_status(refresh=False)

    assert status["source"] == "bundled"
    assert status["profile_count"] >= 3
    assert status["model_count"] >= 1


# ---------------------------------------------------------------------------
# Catalog ids come from the tier table (2026-09-02: llava-7b / qwen25-coder-7b
# had left STUDIO_MODELS but were still referenced by profiles and models[]).
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "nvh" / "catalog" / "nvhive-catalog.json"


def _profile_ids(catalog: dict) -> dict[str, list[str]]:
    return {profile["id"]: profile["model_ids"] for profile in catalog["profiles"]}


def test_every_catalog_model_id_exists_in_studio_models() -> None:
    studio = {model.id: model for model in STUDIO_MODELS}
    for catalog in (setup_catalog.bundled_catalog(), setup_catalog._generated_fallback_catalog()):
        for profile in catalog["profiles"]:
            for model_id in profile["model_ids"]:
                assert model_id == "recommended" or model_id in studio, (
                    catalog["channel"], profile["id"], model_id,
                )
        for model in catalog["models"]:
            entry = studio[model["id"]]
            assert model["install_target"] == entry.install_target, model["id"]
            assert model["recommended_vram_gb"] == entry.recommended_vram_gb, model["id"]
            assert model["category"] == entry.category, model["id"]
            assert model["title"] == entry.title, model["id"]
            assert lm.pick_for_tag(entry.install_target) is not None, model["id"]


def test_bundled_profiles_carry_the_table_derived_ids() -> None:
    """The static JSON cannot drift from the generator: same profiles, same ids."""
    bundled = json.loads(BUNDLED.read_text(encoding="utf-8"))
    assert bundled["channel"] == "bundled"
    assert bundled["schema_version"] == setup_catalog.SCHEMA_VERSION
    assert _profile_ids(bundled) == _profile_ids(setup_catalog._generated_fallback_catalog())
    have = {model["id"] for model in bundled["models"]}
    need = {i for ids in _profile_ids(bundled).values() for i in ids if i != "recommended"}
    assert need <= have, need - have


def test_profile_model_ids_are_tier_table_picks() -> None:
    derived = _profile_ids(setup_catalog._generated_fallback_catalog())
    chat = lm.pick(setup_catalog.PROFILE_CHAT_BUDGET_GB, "chat").catalog_id
    chat_plus = lm.pick(setup_catalog.PROFILE_CODE_BUDGET_GB, "chat").catalog_id
    code = lm.pick(setup_catalog.PROFILE_CODE_BUDGET_GB, "code").catalog_id
    vision = lm.pick(setup_catalog.PROFILE_VISION_BUDGET_GB, "vision").catalog_id
    embed = lm.pick(setup_catalog.PROFILE_CHAT_BUDGET_GB, "embed").catalog_id
    assert derived == {
        "student": [chat, chat_plus, embed],
        "creator": [chat, vision],
        "music": [chat],
        "agent": [code, embed],
        "game": [code, vision],
        "full": ["recommended"],
    }
    # Unique and ordered; an unknown budget still resolves (the ladder walks down).
    assert setup_catalog.table_model_ids((4.0, "chat"), (4.0, "chat")) == [chat]
    assert setup_catalog.table_model_ids((0.0, "vision")) == [lm.pick(0.0, "vision").catalog_id]
