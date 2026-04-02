"""OpenClaw / NemoClaw tool integration.

OpenClaw uses MCP (Model Context Protocol) for tool access.
This module provides helper functions for generating the OpenClaw
configuration needed to register nvHive as a tool server.

Usage in OpenClaw:
    Add to your openclaw.json or agent config:

    {
      "mcpServers": {
        "nvhive": {
          "command": "python",
          "args": ["-m", "nvh.mcp_server"]
        }
      }
    }

    Or if nvHive is installed via pip:

    {
      "mcpServers": {
        "nvhive": {
          "command": "nvhive-mcp"
        }
      }
    }

Then any OpenClaw agent can use nvHive tools:
    - ask: Smart-routed LLM query across 22 providers
    - ask_safe: Local-only (Ollama) query
    - council: Multi-model consensus
    - throwdown: Two-pass deep analysis
    - status: System status and GPU info
    - list_advisors: Available providers
    - list_cabinets: Agent presets
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def generate_openclaw_config(
    use_entry_point: bool = True,
    python_path: str = "python",
) -> dict[str, Any]:
    """Generate the OpenClaw MCP server configuration for nvHive.

    Args:
        use_entry_point: If True, use the `nvhive-mcp` entry point.
            If False, use `python -m nvh.mcp_server`.
        python_path: Path to python executable (for non-entry-point mode).

    Returns:
        Dict suitable for merging into openclaw.json mcpServers section.
    """
    if use_entry_point:
        return {
            "nvhive": {
                "command": "nvhive-mcp",
                "args": [],
            }
        }
    return {
        "nvhive": {
            "command": python_path,
            "args": ["-m", "nvh.mcp_server"],
        }
    }


def generate_nemoclaw_agent_config(
    agent_name: str = "nvhive-enhanced",
    default_model: str = "auto",
) -> dict[str, Any]:
    """Generate a NemoClaw agent configuration that uses nvHive tools.

    This creates an agent that has access to nvHive's MCP tools
    AND uses nvHive as its inference provider.

    Args:
        agent_name: Name for the agent configuration.
        default_model: Default nvHive virtual model (auto, safe, council, throwdown).

    Returns:
        Dict suitable for a NemoClaw agent definition.
    """
    return {
        "name": agent_name,
        "description": (
            "Agent with nvHive multi-LLM orchestration — smart routing, "
            "council consensus, and throwdown analysis."
        ),
        "inference": {
            "provider": "nvhive",
            "model": default_model,
        },
        "mcpServers": {
            "nvhive": {
                "command": "nvhive-mcp",
                "args": [],
            }
        },
        "tools": [
            "ask",
            "ask_safe",
            "council",
            "throwdown",
            "status",
            "list_advisors",
            "list_cabinets",
        ],
    }


def write_openclaw_config(
    output_path: Path | None = None,
    merge_existing: bool = True,
) -> Path:
    """Write or merge nvHive MCP config into an openclaw.json file.

    Args:
        output_path: Path to write. Defaults to ./openclaw.json.
        merge_existing: If True and file exists, merge into existing config.

    Returns:
        Path to the written config file.
    """
    path = output_path or Path("openclaw.json")

    config: dict[str, Any] = {}
    if merge_existing and path.exists():
        config = json.loads(path.read_text())

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    config["mcpServers"].update(generate_openclaw_config())

    path.write_text(json.dumps(config, indent=2) + "\n")
    return path
