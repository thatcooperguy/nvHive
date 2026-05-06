"""Install receipts for rootless workstation tools.

Receipts are small JSON manifests written under ``NVH_HOME`` after nvHive
installs a tool, model, or workspace. They make setup resumable and auditable
without requiring root access or a system package database.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nvh.integrations.storage import storage_layout

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class InstallReceipt:
    """Manifest for one rootless install artifact."""

    id: str
    kind: str
    item_id: str
    title: str
    status: str
    installed_at: str
    updated_at: str
    install_path: str
    version: str | None = None
    source_urls: list[str] = field(default_factory=list)
    launchers: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    no_root: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def receipts_root(
    *,
    create: bool = True,
    home_dir: str | Path | None = None,
) -> Path:
    root = storage_layout(home_dir).home / "receipts"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def receipt_id(kind: str, item_id: str) -> str:
    return f"{kind}:{item_id}"


def _safe_slug(value: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)
    slug = slug.strip("._")
    if not slug:
        raise KeyError("Invalid receipt id")
    return slug


def _receipt_path(identifier: str, home_dir: str | Path | None = None) -> Path:
    return receipts_root(home_dir=home_dir) / f"{_safe_slug(identifier)}.json"


def write_receipt(
    *,
    kind: str,
    item_id: str,
    title: str,
    install_path: str | Path,
    status: str = "installed",
    version: str | None = None,
    source_urls: list[str] | tuple[str, ...] | None = None,
    launchers: list[str] | tuple[str, ...] | None = None,
    models: list[str] | tuple[str, ...] | None = None,
    files: list[str] | tuple[str, ...] | None = None,
    metadata: dict[str, Any] | None = None,
    no_root: bool = True,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Create or update one install receipt."""
    identifier = receipt_id(kind, item_id)
    path = _receipt_path(identifier, home_dir=home_dir)
    previous: dict[str, Any] = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    now = _now()
    receipt = InstallReceipt(
        id=identifier,
        kind=kind,
        item_id=item_id,
        title=title,
        status=status,
        installed_at=str(previous.get("installed_at") or now),
        updated_at=now,
        install_path=str(install_path),
        version=version,
        source_urls=list(source_urls or []),
        launchers=list(launchers or []),
        models=list(models or []),
        files=list(files or []),
        no_root=no_root,
        metadata=metadata or {},
    )
    data = receipt.as_dict()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return enrich_receipt(data)


def load_receipt(identifier: str, home_dir: str | Path | None = None) -> dict[str, Any]:
    path = _receipt_path(identifier, home_dir=home_dir)
    if not path.exists():
        raise KeyError(f"Unknown receipt: {identifier}")
    return enrich_receipt(json.loads(path.read_text(encoding="utf-8")))


def list_receipts(
    *,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 100,
    home_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return recent receipts sorted by updated time descending."""
    receipts: list[dict[str, Any]] = []
    root = receipts_root(create=False, home_dir=home_dir)
    if not root.exists():
        return []
    for path in root.glob("*.json"):
        try:
            data = enrich_receipt(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
        if kind and data.get("kind") != kind:
            continue
        if status and data.get("status") != status:
            continue
        receipts.append(data)
    receipts.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
    return receipts[: max(1, min(limit, 500))]


def enrich_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Add lightweight health data without mutating the on-disk manifest."""
    install_path = Path(str(receipt.get("install_path", ""))).expanduser()
    launchers = [Path(str(path)).expanduser() for path in receipt.get("launchers", [])]
    files = [Path(str(path)).expanduser() for path in receipt.get("files", [])]
    missing_launchers = [str(path) for path in launchers if not path.exists()]
    missing_files = [str(path) for path in files if not path.exists()]
    health = {
        "install_path_exists": install_path.exists(),
        "missing_launchers": missing_launchers,
        "missing_files": missing_files,
        "healthy": install_path.exists() and not missing_launchers and not missing_files,
    }
    return {**receipt, "health": health}


def receipt_summary(home_dir: str | Path | None = None) -> dict[str, Any]:
    receipts = list_receipts(home_dir=home_dir)
    by_kind: dict[str, int] = {}
    unhealthy = 0
    for receipt in receipts:
        by_kind[receipt["kind"]] = by_kind.get(receipt["kind"], 0) + 1
        if not receipt.get("health", {}).get("healthy", False):
            unhealthy += 1
    return {
        "count": len(receipts),
        "by_kind": by_kind,
        "unhealthy": unhealthy,
        "root": str(receipts_root(create=False, home_dir=home_dir)),
        "receipts": receipts[:10],
    }


def repair_plan(identifier: str, home_dir: str | Path | None = None) -> dict[str, Any]:
    receipt = load_receipt(identifier, home_dir=home_dir)
    kind = receipt["kind"]
    item_id = receipt["item_id"]
    commands: list[str]
    if kind == "studio-pack":
        commands = [f"nvh studio --install {item_id} -y"]
    elif kind == "studio-model":
        target = receipt.get("metadata", {}).get("install_target") or item_id
        commands = [f"nvh studio --install-models {target} -y"]
    elif kind == "comfyui":
        commands = ["nvh workstation --with-comfyui -y"]
    else:
        commands = [f"nvh setup repair {identifier}"]
    return {
        "receipt": receipt,
        "safe_to_run_without_root": True,
        "commands": commands,
        "reason": "Re-run the rootless installer for this item and refresh the receipt.",
    }


def uninstall_plan(identifier: str, home_dir: str | Path | None = None) -> dict[str, Any]:
    receipt = load_receipt(identifier, home_dir=home_dir)
    paths = [receipt["install_path"], *receipt.get("launchers", []), *receipt.get("files", [])]
    unique_paths = []
    for path in paths:
        if path and path not in unique_paths:
            unique_paths.append(path)
    return {
        "receipt": receipt,
        "safe_to_run_without_root": True,
        "destructive": True,
        "target_paths": unique_paths,
        "commands": [f"rm -rf {path!r}" for path in unique_paths],
        "reason": "Preview only. nvHive records the paths so a user can remove rootless files deliberately.",
    }
