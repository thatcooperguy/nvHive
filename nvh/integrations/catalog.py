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

from nvh.integrations.storage import storage_layout

DEFAULT_CATALOG_URL = (
    "https://raw.githubusercontent.com/thatcooperguy/nvHive/main/"
    "nvh/catalog/nvhive-catalog.json"
)
CATALOG_ENV = "NVH_CATALOG_URL"
SCHEMA_VERSION = 1


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


def _generated_fallback_catalog() -> dict[str, Any]:
    from nvh.integrations.comfyui import examples_as_dicts
    from nvh.integrations.studio_packs import catalog_as_dicts, model_catalog_as_dicts

    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now(),
        "channel": "generated-fallback",
        "profiles": [
            {
                "id": "student",
                "title": "Student Starter",
                "pack_ids": ["starter"],
                "model_ids": ["gemma3-4b", "qwen3-8b", "nomic-embed-text"],
                "description": "Small, useful local AI lab for coursework and experiments.",
            },
            {
                "id": "creator",
                "title": "Creator Studio",
                "pack_ids": ["creative", "comfy"],
                "model_ids": ["gemma3-4b", "llava-7b"],
                "description": "ComfyUI, Blender, and vision helpers for media projects.",
            },
            {
                "id": "agent",
                "title": "Agent Builder",
                "pack_ids": ["agents"],
                "model_ids": ["qwen25-coder-7b", "nomic-embed-text"],
                "description": "Local agent libraries, coding model, and embeddings.",
            },
            {
                "id": "full",
                "title": "Full Workstation",
                "pack_ids": ["all"],
                "model_ids": ["recommended"],
                "description": "Everything nvHive can install without root access.",
            },
        ],
        "packs": catalog_as_dicts(),
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
