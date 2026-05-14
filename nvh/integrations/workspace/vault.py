"""Rootless nvHive Vault and optional Obsidian support.

The vault is plain Markdown under ``NVH_HOME``. Obsidian is treated as an
optional viewer/editor, not as the storage layer, so memory survives even when
Obsidian is not installed.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import stat
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.integrations.workspace.storage import ensure_storage, storage_layout

OBSIDIAN_RELEASES_API = "https://api.github.com/repos/obsidianmd/obsidian-releases/releases/latest"
OBSIDIAN_DOWNLOAD_PAGE = "https://obsidian.md/download"

VAULT_DIRS = [
    "System",
    "Models",
    "ComfyUI",
    "Projects",
    "Troubleshooting",
    "Wizard Memory",
    "Decisions",
    "Conflicts",
    "Process Playbooks",
]

KNOWN_CONFLICTS = [
    {
        "title": "Vault vs Obsidian",
        "summary": (
            "nvHive Vault is the durable Markdown memory. Obsidian is only an "
            "optional viewer so setup never depends on a GUI app."
        ),
    },
    {
        "title": "Latest vs compatible",
        "summary": (
            "Installers should prefer latest compatible stable releases, but allow "
            "pins when a class, image, or support team needs repeatability."
        ),
    },
    {
        "title": "Automatic model download vs consent",
        "summary": (
            "The wizard can recommend and count down before downloading large "
            "models, but users need a cancel path for bandwidth and disk control."
        ),
    },
    {
        "title": "Default browser profile vs isolated profile",
        "summary": (
            "Rootless launch should avoid corrupting VM browser profiles, while "
            "still allowing an override for users already signed into Firefox."
        ),
    },
    {
        "title": "OS disk vs persistent mount",
        "summary": (
            "Everything mutable belongs under NVH_HOME on the block-backed mount; "
            "the OS image can be read-only or replaced between sessions."
        ),
    },
    {
        "title": "Cloud API convenience vs secret safety",
        "summary": (
            "Provider key helpers should link to official key pages and store keys "
            "in rootless config, never in the Markdown vault by default."
        ),
    },
    {
        "title": "Heavy creative tools vs one-click simplicity",
        "summary": (
            "Game engines, Blender assets, and music tools need disk/time estimates "
            "and graceful deferral when a student only wants local AI chat."
        ),
    },
    {
        "title": "AppImage convenience vs FUSE availability",
        "summary": (
            "Obsidian and other AppImages may need FUSE on some Linux images, so "
            "nvHive should report the fallback instead of asking for sudo."
        ),
    },
]


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._ -]+", "", value).strip().replace(" ", "-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:80] or "note"


def _vault_root(home_dir: str | Path | None = None) -> Path:
    return storage_layout(home_dir).home / "vault"


def _write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return True


def _markdown_count(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for path in root.rglob("*.md") if path.is_file())


def _seed_notes(vault: Path, home: Path) -> dict[str, str]:
    seeded: dict[str, str] = {}

    files = {
        "README.md": f"""# nvHive Vault

This is the durable memory for this nvHive workspace.

- Rootless location: `{vault}`
- Workspace home: `{home}`
- Format: plain Markdown
- Obsidian: optional viewer/editor

Use this vault for product decisions, setup notes, support reports, model
choices, and student-friendly troubleshooting. Do not paste API keys or
secrets here.
""",
        "Wizard Memory/AI Wizard.md": """# AI Wizard Product Memory

AI Wizard helps users run a rootless NVIDIA AI workspace on a Linux GPU VM.

Core rules:

- Never assume root or sudo access.
- Prefer NVH_HOME on the persistent block-backed mount for models, apps, logs, jobs, and config.
- Run boot checks because the base VM image, kernel, driver, CUDA, Python,
  Node, or browser can change between sessions.
- Keep the UI simple: show readiness, recommended actions, and mission cards
  first; hide long diagnostics behind Advanced Details.
- Manual commands are fallback overrides, not the main user journey.
- Use local AI first when ready, then cloud providers only when configured.
- When something fails, summarize logs in plain English and offer safe rootless repairs.
""",
        "Decisions/Product Direction.md": """# Product Direction

Ideas to preserve:

- Linux GPU desktop launch is the priority.
- Rootless install and uninstall must work without OS changes.
- The wizard should open automatically and guide the user.
- Mission cards should stay simple: AI Starter, Graphics Creator Studio,
  Game Dev Lab, Music Producer Studio, and Agent Builder.
- The wizard should recommend models by GPU, VRAM, disk, and current runtime health.
- ComfyUI, Blender, Godot, local models, cloud API keys, and debugging should
  be one-click where possible.
- A local agent helper should understand this product, read local logs, and
  generate support reports.
- Boot health should detect base image drift before the user wastes time.
""",
        "Conflicts/Product Conflicts.md": _conflicts_markdown(),
        "Process Playbooks/Pilot Test Checklist.md": """# Pilot Test Checklist

Use this before calling a Linux GPU VM build ready:

1. Fresh uninstall and reinstall from the GitHub curl command.
2. Confirm NVH_HOME lands on the persistent block-backed mount.
3. Confirm browser opens to the setup wizard without manual commands.
4. Confirm boot health shows a clear ready/warn state.
5. Download the recommended local model and run a one-question test.
6. Install ComfyUI and confirm the app launches.
7. Install one creative/game/music pack and confirm receipt/job history.
8. Copy a support report and verify secrets are redacted.
9. Disconnect/reconnect or reboot, then confirm the vault, models, and receipts persist.
""",
        "Process Playbooks/Support Report Flow.md": """# Support Report Flow

When a user reports a failure:

1. Ask them to click Copy Support Report from setup.
2. Check job history first, then boot health, then runtime status.
3. Confirm the failure is rootless-fixable before suggesting any OS-level change.
4. Prefer latest compatible downloads, but record any pin needed for repeatability.
5. Add the final fix or workaround back into this vault.
""",
    }

    for relative, content in files.items():
        path = vault / relative
        if _write_if_missing(path, content):
            seeded[relative] = str(path)

    obsidian_dir = vault / ".obsidian"
    obsidian_dir.mkdir(parents=True, exist_ok=True)
    app_json = obsidian_dir / "app.json"
    if _write_if_missing(app_json, json.dumps({"alwaysUpdateLinks": True}, indent=2)):
        seeded[".obsidian/app.json"] = str(app_json)
    appearance_json = obsidian_dir / "appearance.json"
    if _write_if_missing(
        appearance_json,
        json.dumps({"accentColor": "#76B900", "theme": "obsidian"}, indent=2),
    ):
        seeded[".obsidian/appearance.json"] = str(appearance_json)
    plugins_json = obsidian_dir / "community-plugins.json"
    if _write_if_missing(plugins_json, "[]"):
        seeded[".obsidian/community-plugins.json"] = str(plugins_json)

    return seeded


def _conflicts_markdown() -> str:
    rows = "\n".join(f"- **{item['title']}**: {item['summary']}" for item in KNOWN_CONFLICTS)
    return f"""# Product Conflicts

These are intentional tradeoffs AI Wizard should explain instead of hiding.

{rows}
"""


def init_vault(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Create the rootless Markdown vault and seed product memory."""
    storage = ensure_storage(home_dir, min_free_gb=0, activate=True)
    layout = storage.layout
    vault = layout.home / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    for dirname in VAULT_DIRS:
        (vault / dirname).mkdir(parents=True, exist_ok=True)

    seeded = _seed_notes(vault, layout.home)
    append_path = vault / "System" / "Boot Health.md"
    append_path.write_text(
        f"""# Boot Health

Last refreshed: {_now()}

- NVH_HOME: `{layout.home}`
- Python: `{platform.python_version()}`
- Platform: `{platform.platform()}`
- Machine: `{platform.machine()}`

Use AI Wizard support reports for redacted logs, receipts, and compatibility checks.
""",
        encoding="utf-8",
    )

    return {
        "initialized": True,
        "vault_dir": str(vault),
        "home_dir": str(layout.home),
        "seeded_files": seeded,
        "markdown_files": _markdown_count(vault),
        "conflicts": KNOWN_CONFLICTS,
        "obsidian": obsidian_status(home_dir=layout.home),
    }


def vault_status(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Return vault status without requiring Obsidian."""
    layout = storage_layout(home_dir)
    vault = layout.home / "vault"
    initialized = vault.exists() and (vault / "README.md").exists()
    return {
        "initialized": initialized,
        "vault_dir": str(vault),
        "home_dir": str(layout.home),
        "markdown_files": _markdown_count(vault),
        "directories": {name: str(vault / name) for name in VAULT_DIRS},
        "conflicts": KNOWN_CONFLICTS,
        "obsidian": obsidian_status(home_dir=layout.home),
    }


def append_vault_memory(
    title: str,
    body: str,
    *,
    category: str = "Wizard Memory",
    tags: list[str] | None = None,
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Append a Markdown memory note under the vault."""
    status = init_vault(home_dir)
    vault = Path(status["vault_dir"])
    safe_category = category if category in VAULT_DIRS else "Wizard Memory"
    target_dir = vault / safe_category
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = _now()
    filename = f"{timestamp.replace(':', '').replace('+', 'Z')}-{_slug(title)}.md"
    path = target_dir / filename
    tag_line = " ".join(f"#{_slug(tag).lower()}" for tag in (tags or []) if tag.strip())
    path.write_text(
        f"""# {title.strip()}

Created: {timestamp}
Category: {safe_category}
Tags: {tag_line or "none"}

{body.strip()}
""",
        encoding="utf-8",
    )
    return {
        "saved": True,
        "path": str(path),
        "category": safe_category,
        "title": title.strip(),
        "vault_dir": str(vault),
    }


def _platform_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x86_64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return machine


def _obsidian_paths(home_dir: str | Path | None = None) -> dict[str, Path]:
    layout = storage_layout(home_dir)
    root = layout.apps_dir / "obsidian"
    return {
        "root": root,
        "appimage": root / "Obsidian.AppImage",
        "launcher": layout.bin_dir / "nvhive-vault",
        "vault": layout.home / "vault",
    }


def obsidian_status(home_dir: str | Path | None = None) -> dict[str, Any]:
    paths = _obsidian_paths(home_dir)
    system_obsidian = shutil.which("obsidian")
    installed = paths["appimage"].exists() or bool(system_obsidian)
    arch = _platform_arch()
    linux = platform.system().lower() == "linux"
    return {
        "installed": installed,
        "rootless_app": paths["appimage"].exists(),
        "app_path": str(paths["appimage"]) if paths["appimage"].exists() else system_obsidian,
        "launcher_path": str(paths["launcher"]),
        "open_command": (
            f'"{paths["launcher"]}"'
            if paths["launcher"].exists()
            else f'open "{paths["vault"]}" in Obsidian'
        ),
        "installable": linux and arch == "x86_64",
        "reason": (
            ""
            if linux and arch == "x86_64"
            else "Rootless Obsidian auto-install currently targets Linux x86_64 AppImage builds."
        ),
        "download_url": OBSIDIAN_DOWNLOAD_PAGE,
        "vault_dir": str(paths["vault"]),
    }


def _write_obsidian_launcher(home_dir: str | Path | None = None) -> Path:
    layout = storage_layout(home_dir)
    paths = _obsidian_paths(layout.home)
    paths["launcher"].parent.mkdir(parents=True, exist_ok=True)
    script = f"""#!/usr/bin/env bash
set -e
export NVH_HOME="${{NVH_HOME:-{layout.home}}}"
VAULT="${{NVH_HOME}}/vault"
APP="${{NVH_HOME}}/apps/obsidian/Obsidian.AppImage"
if [ -x "$APP" ]; then
  exec "$APP" "$VAULT"
fi
if command -v obsidian >/dev/null 2>&1; then
  exec obsidian "$VAULT"
fi
echo "Obsidian is not installed. Your nvHive Vault is at: $VAULT"
exit 1
"""
    paths["launcher"].write_text(script, encoding="utf-8", newline="\n")
    paths["launcher"].chmod(
        paths["launcher"].stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return paths["launcher"]


def _latest_obsidian_appimage_asset() -> dict[str, Any]:
    req = urllib.request.Request(
        OBSIDIAN_RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "nvhive-rootless-vault"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        release = json.loads(response.read().decode("utf-8"))
    assets = release.get("assets") or []
    for asset in assets:
        name = str(asset.get("name") or "")
        if (
            name.endswith(".AppImage")
            and "arm64" not in name.lower()
            and "aarch64" not in name.lower()
        ):
            return asset
    raise RuntimeError("No Linux x86_64 Obsidian AppImage asset found in the latest release.")


def install_obsidian(
    home_dir: str | Path | None = None,
    *,
    force_update: bool = False,
) -> dict[str, Any]:
    """Install Obsidian AppImage under NVH_HOME/apps when supported."""
    init_vault(home_dir)
    layout = storage_layout(home_dir)
    paths = _obsidian_paths(layout.home)
    paths["root"].mkdir(parents=True, exist_ok=True)

    status = obsidian_status(home_dir=layout.home)
    launcher = _write_obsidian_launcher(layout.home)
    if paths["appimage"].exists() and not force_update:
        status.update({
            "installed": True,
            "rootless_app": True,
            "app_path": str(paths["appimage"]),
            "launcher_path": str(launcher),
            "message": "Obsidian is already installed in NVH_HOME.",
        })
        return status

    if not status["installable"]:
        status.update({
            "installed": paths["appimage"].exists() or bool(shutil.which("obsidian")),
            "launcher_path": str(launcher),
            "message": (
                status["reason"] or "Obsidian auto-install is not available on this platform."
            ),
        })
        return status

    asset = _latest_obsidian_appimage_asset()
    url = asset.get("browser_download_url")
    if not url:
        raise RuntimeError("Latest Obsidian release asset did not include a download URL.")

    tmp_path = paths["root"] / "Obsidian.AppImage.download"
    urllib.request.urlretrieve(url, tmp_path)
    tmp_path.replace(paths["appimage"])
    paths["appimage"].chmod(
        paths["appimage"].stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    return {
        "installed": True,
        "rootless_app": True,
        "app_path": str(paths["appimage"]),
        "launcher_path": str(launcher),
        "open_command": f'"{launcher}"',
        "installable": True,
        "reason": "",
        "download_url": url,
        "vault_dir": str(paths["vault"]),
        "message": (
            "Obsidian installed rootlessly. If the VM lacks FUSE, open the vault folder "
            "in any Markdown editor or use Obsidian from the OS image."
        ),
    }
