"""Auto-detect installed AI platforms and configure nvHive integration.

Scans for:
- NemoClaw / OpenShell (openshell CLI)
- OpenClaw (openclaw CLI or config files)
- Claude Code (claude CLI)
- Cursor (cursor CLI or config directory)
- Claude Desktop (config file)

Each platform gets a register function that performs the actual setup.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Platform:
    """Detected AI platform that can integrate with nvHive."""
    name: str
    display_name: str
    detected: bool = False
    detection_method: str = ""  # how we found it
    already_configured: bool = False
    config_path: str = ""
    integration_type: str = ""  # "mcp" or "inference"
    notes: list[str] = field(default_factory=list)


def _cmd_exists(cmd: str) -> str | None:
    """Check if a command exists, return its path or None."""
    return shutil.which(cmd)


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file, return None if missing or invalid."""
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return None


def detect_platforms() -> list[Platform]:
    """Scan the system for AI platforms that can integrate with nvHive."""
    platforms: list[Platform] = []

    # --- NemoClaw / OpenShell ---
    nemoclaw = Platform(
        name="nemoclaw",
        display_name="NemoClaw",
        integration_type="inference",
    )
    openshell_path = _cmd_exists("openshell")
    nemoclaw_path = _cmd_exists("nemoclaw")
    if openshell_path or nemoclaw_path:
        nemoclaw.detected = True
        nemoclaw.detection_method = f"CLI: {openshell_path or nemoclaw_path}"
        # Check if nvhive is already registered as a provider
        try:
            result = subprocess.run(
                ["openshell", "provider", "list"],
                capture_output=True, text=True, timeout=5,
            )
            if "nvhive" in result.stdout.lower():
                nemoclaw.already_configured = True
                nemoclaw.notes.append("nvhive provider already registered")
        except Exception:
            pass
    platforms.append(nemoclaw)

    # --- OpenClaw ---
    openclaw = Platform(
        name="openclaw",
        display_name="OpenClaw",
        integration_type="mcp",
    )
    openclaw_cli = _cmd_exists("openclaw")
    # Check common config locations
    openclaw_configs = [
        Path.cwd() / "openclaw.json",
        Path.home() / ".openclaw" / "config.json",
        Path.home() / ".config" / "openclaw" / "config.json",
    ]
    for cfg in openclaw_configs:
        data = _read_json(cfg)
        if data is not None:
            openclaw.detected = True
            openclaw.detection_method = f"Config: {cfg}"
            openclaw.config_path = str(cfg)
            mcp_servers = data.get("mcpServers", {})
            if "nvhive" in mcp_servers:
                openclaw.already_configured = True
                openclaw.notes.append("nvhive MCP server already configured")
            break
    if not openclaw.detected and openclaw_cli:
        openclaw.detected = True
        openclaw.detection_method = f"CLI: {openclaw_cli}"
    platforms.append(openclaw)

    # --- Claude Code ---
    claude_code = Platform(
        name="claude_code",
        display_name="Claude Code",
        integration_type="mcp",
    )
    claude_cli = _cmd_exists("claude")
    if claude_cli:
        claude_code.detected = True
        claude_code.detection_method = f"CLI: {claude_cli}"
        # Check if nvhive MCP is already registered
        try:
            result = subprocess.run(
                ["claude", "mcp", "list"],
                capture_output=True, text=True, timeout=5,
            )
            if "nvhive" in result.stdout.lower():
                claude_code.already_configured = True
                claude_code.notes.append("nvhive MCP server already registered")
        except Exception:
            pass
    platforms.append(claude_code)

    # --- Cursor ---
    cursor = Platform(
        name="cursor",
        display_name="Cursor",
        integration_type="mcp",
    )
    cursor_cli = _cmd_exists("cursor")
    cursor_configs = [
        Path.home() / ".cursor" / "mcp.json",
    ]
    if sys.platform == "darwin":
        cursor_configs.append(
            Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "settings.json"
        )
    elif sys.platform == "win32":
        appdata = Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "settings.json"
        cursor_configs.append(appdata)

    for cfg in cursor_configs:
        data = _read_json(cfg)
        if data is not None:
            cursor.detected = True
            cursor.detection_method = f"Config: {cfg}"
            cursor.config_path = str(cfg)
            mcp_servers = data.get("mcpServers", {})
            if "nvhive" in mcp_servers:
                cursor.already_configured = True
            break
    if not cursor.detected and cursor_cli:
        cursor.detected = True
        cursor.detection_method = f"CLI: {cursor_cli}"
    platforms.append(cursor)

    # --- Claude Desktop ---
    claude_desktop = Platform(
        name="claude_desktop",
        display_name="Claude Desktop",
        integration_type="mcp",
    )
    desktop_configs = []
    if sys.platform == "darwin":
        desktop_configs.append(
            Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        )
    elif sys.platform == "win32":
        desktop_configs.append(
            Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
        )
    elif sys.platform == "linux":
        desktop_configs.append(
            Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
        )

    for cfg in desktop_configs:
        if cfg.exists():
            claude_desktop.detected = True
            claude_desktop.detection_method = f"Config: {cfg}"
            claude_desktop.config_path = str(cfg)
            data = _read_json(cfg)
            if data and "nvhive" in data.get("mcpServers", {}):
                claude_desktop.already_configured = True
            break
    platforms.append(claude_desktop)

    return platforms


def register_claude_code() -> tuple[bool, str]:
    """Register nvHive MCP server with Claude Code via CLI."""
    try:
        result = subprocess.run(
            ["claude", "mcp", "add", "nvhive", "--", "nvhive-mcp"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return True, "Registered nvhive MCP server with Claude Code"
        return False, f"Registration failed: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "claude CLI not found"
    except Exception as e:
        return False, str(e)


def register_openclaw(config_path: str | None = None) -> tuple[bool, str]:
    """Register nvHive MCP server in OpenClaw config."""
    from nvh.integrations.openclaw import write_openclaw_config
    try:
        path = write_openclaw_config(
            output_path=Path(config_path) if config_path else None,
        )
        return True, f"Config written to {path}"
    except Exception as e:
        return False, str(e)


def register_nemoclaw(host: str = "127.0.0.1", port: int = 8000) -> tuple[bool, str]:
    """Register nvHive as a NemoClaw inference provider via openshell CLI."""
    endpoint_host = "host.openshell.internal" if host in ("127.0.0.1", "0.0.0.0", "localhost") else host
    try:
        result = subprocess.run(
            [
                "openshell", "provider", "create",
                "--name", "nvhive",
                "--type", "openai",
                "--credential", "OPENAI_API_KEY=nvhive",
                "--config", f"OPENAI_BASE_URL=http://{endpoint_host}:{port}/v1/proxy",
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return True, "Registered nvhive inference provider with NemoClaw"
        return False, f"Registration failed: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "openshell CLI not found"
    except Exception as e:
        return False, str(e)


def register_cursor(config_path: str | None = None) -> tuple[bool, str]:
    """Register nvHive MCP server in Cursor config."""
    path = Path(config_path) if config_path else Path.home() / ".cursor" / "mcp.json"
    try:
        config: dict[str, Any] = {}
        if path.exists():
            config = json.loads(path.read_text())
        if "mcpServers" not in config:
            config["mcpServers"] = {}
        config["mcpServers"]["nvhive"] = {"command": "nvhive-mcp"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n")
        return True, f"Config written to {path}"
    except Exception as e:
        return False, str(e)


def register_claude_desktop() -> tuple[bool, str]:
    """Register nvHive MCP server in Claude Desktop config."""
    if sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif sys.platform == "win32":
        path = Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    else:
        path = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

    try:
        config: dict[str, Any] = {}
        if path.exists():
            config = json.loads(path.read_text())
        if "mcpServers" not in config:
            config["mcpServers"] = {}
        config["mcpServers"]["nvhive"] = {
            "command": "nvhive-mcp",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n")
        return True, f"Config written to {path}"
    except Exception as e:
        return False, str(e)
