"""Remote setup catalog with bundled fallback.

The catalog lets nvHive update recommended profiles, model picks, and ComfyUI
starter workflows between package releases. Network access is optional: the
WebUI and CLI always fall back to a bundled catalog.
"""

from __future__ import annotations

import json
import os
import time
from importlib import resources
from pathlib import Path
from typing import Any

from nvh.integrations.workspace.storage import storage_layout

DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/thatcooperguy/nvHive/main/"
    "nvh/catalog/nvhive-catalog.json"
)
CATALOG_ENV = "NVH_CATALOG_URL"
SCHEMA_VERSION = 1

# Budgets the setup profiles are sized for -- the 8-12 GB student card they
# were written around: the smallest GPU tier's chat pick, the 8 GB tier's
# code pick and the 12 GB tier's vision pick (below that the vision column is
# the moondream fallback, not a creative tool). Ids come from the tier table
# (nvh/core/local_models.py) so a retired tag can never reappear here; the
# bundled nvh/catalog/nvhive-catalog.json carries the same ids and
# tests/test_setup_catalog.py pins the two together.
PROFILE_CHAT_BUDGET_GB = 4.0
PROFILE_CODE_BUDGET_GB = 8.0
PROFILE_VISION_BUDGET_GB = 12.0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def catalog_cache_dir(*, create: bool = True) -> Path:
    root = storage_layout().cache_dir / "catalog"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def catalog_cache_path(*, create: bool = True) -> Path:
    return catalog_cache_dir(create=create) / "nvhive-catalog.json"


def _validate_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    if int(catalog.get("schema_version", 0)) != SCHEMA_VERSION:
        raise ValueError("Unsupported catalog schema_version")
    for key in ("profiles", "models", "packs", "comfyui_examples"):
        if not isinstance(catalog.get(key), list):
            raise ValueError(f"Catalog is missing list field: {key}")
    return catalog


def table_model_ids(*picks: tuple[float, str]) -> list[str]:
    """Catalog ids of tier-table picks, unique and in order: ``(budget_gb, use_case)`` pairs."""
    from nvh.core import local_models

    ids: list[str] = []
    for budget_gb, use_case in picks:
        chosen = local_models.pick(budget_gb, use_case)
        if chosen is not None and chosen.catalog_id not in ids:
            ids.append(chosen.catalog_id)
    return ids


def fallback_profiles() -> list[dict[str, Any]]:
    """The setup profiles, their ``model_ids`` derived from the tier table."""
    chat = (PROFILE_CHAT_BUDGET_GB, "chat")
    chat_plus = (PROFILE_CODE_BUDGET_GB, "chat")
    code = (PROFILE_CODE_BUDGET_GB, "code")
    vision = (PROFILE_VISION_BUDGET_GB, "vision")
    embed = (PROFILE_CHAT_BUDGET_GB, "embed")
    return [
        {
            "id": "student",
            "title": "AI Starter",
            "pack_ids": ["starter"],
            "model_ids": table_model_ids(chat, chat_plus, embed),
            "description": "First-time local AI lab for chat, homework, coding help, GitHub, and the helper agent.",
        },
        {
            "id": "creator",
            "title": "Graphics Creator Studio",
            "pack_ids": ["creative", "comfy"],
            "model_ids": table_model_ids(chat, vision),
            "description": "ComfyUI, Blender, image/video workflows, and vision helpers for graphics projects.",
        },
        {
            "id": "music",
            "title": "Music Producer Studio",
            "pack_ids": ["music"],
            "model_ids": table_model_ids(chat),
            "description": "AI music generation, stems, transcription, and rootless DAW helpers.",
        },
        {
            "id": "agent",
            "title": "Agent Builder",
            "pack_ids": ["agents"],
            "model_ids": table_model_ids(code, embed),
            "description": "Local agent libraries, coding model, and embeddings.",
        },
        {
            "id": "game",
            "title": "Game Dev Lab",
            "pack_ids": ["game", "creative"],
            "model_ids": table_model_ids(code, vision),
            "description": "Game prototyping, Blender assets, and mod helper workspace.",
        },
        {
            "id": "full",
            "title": "Power User Workstation",
            "pack_ids": ["all"],
            "model_ids": ["recommended"],
            "description": "Everything nvHive can install without root access, guarded by host checks.",
        },
    ]


def _generated_fallback_catalog() -> dict[str, Any]:
    from nvh.integrations.installs.comfyui import examples_as_dicts
    from nvh.integrations.installs.studio_packs import (
        BLENDER_VERSION,
        catalog_as_dicts,
        model_catalog_as_dicts,
    )

    packs = catalog_as_dicts()
    for pack in packs:
        if pack.get("id") == "blender-creative":
            pack["latest_version"] = BLENDER_VERSION

    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now(),
        "channel": "generated-fallback",
        "profiles": fallback_profiles(),
        "packs": packs,
        "models": model_catalog_as_dicts(),
        "comfyui_examples": examples_as_dicts(),
    }


def bundled_catalog() -> dict[str, Any]:
    try:
        payload = (
            resources.files("nvh.catalog")
            .joinpath("nvhive-catalog.json")
            .read_text(encoding="utf-8")
        )
        return _validate_catalog(json.loads(payload))
    except Exception:
        return _generated_fallback_catalog()


def _read_cached_catalog() -> dict[str, Any] | None:
    path = catalog_cache_path(create=False)
    if not path.exists():
        return None
    try:
        return _validate_catalog(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _write_cached_catalog(catalog: dict[str, Any]) -> None:
    path = catalog_cache_path(create=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _fetch_remote_catalog(url: str, timeout: float) -> dict[str, Any]:
    import httpx

    response = httpx.get(url, follow_redirects=True, timeout=timeout)
    response.raise_for_status()
    return _validate_catalog(response.json())


def load_setup_catalog(
    *,
    refresh: bool = False,
    url: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Load setup catalog from remote, cache, or bundled fallback."""
    catalog_url = url or os.environ.get(CATALOG_ENV) or DEFAULT_CATALOG_URL
    errors: list[str] = []

    if refresh:
        try:
            catalog = _fetch_remote_catalog(catalog_url, timeout)
            catalog["_cached_at"] = _now()
            _write_cached_catalog(catalog)
            return {
                "source": "remote",
                "url": catalog_url,
                "catalog": catalog,
                "error": None,
            }
        except Exception as exc:
            errors.append(str(exc))

    cached = _read_cached_catalog()
    if cached is not None:
        return {
            "source": "cache",
            "url": catalog_url,
            "catalog": cached,
            "error": "; ".join(errors) if errors else None,
        }

    return {
        "source": "bundled",
        "url": catalog_url,
        "catalog": bundled_catalog(),
        "error": "; ".join(errors) if errors else None,
    }


def catalog_status(*, refresh: bool = False) -> dict[str, Any]:
    loaded = load_setup_catalog(refresh=refresh)
    catalog = loaded["catalog"]
    return {
        "source": loaded["source"],
        "url": loaded["url"],
        "error": loaded["error"],
        "schema_version": catalog.get("schema_version"),
        "updated_at": catalog.get("updated_at"),
        "profile_count": len(catalog.get("profiles", [])),
        "pack_count": len(catalog.get("packs", [])),
        "model_count": len(catalog.get("models", [])),
        "comfyui_example_count": len(catalog.get("comfyui_examples", [])),
    }
