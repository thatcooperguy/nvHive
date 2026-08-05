"""MCP client — attach external Model Context Protocol servers as Wizard tools.

The 2026-08-05 roadmap research ranked "MCP client support" the #1 critical
gap: every leading stack (Open WebUI, LibreChat, the Claude ecosystem) lets
users plug external MCP tool servers into chat. This module is nvHive's
implementation:

  1. Users declare servers in ``$NVH_HOME/config/mcp-servers.json`` using
     the same ``mcpServers`` shape as Claude Desktop, so existing configs
     paste straight in::

        {
          "mcpServers": {
            "filesystem": {
              "command": "npx",
              "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
              "env": {},
              "auto_approve": ["read_file", "list_directory"],
              "enabled": true
            }
          }
        }

  2. ``nvh mcp refresh`` (or API-server startup) connects to each enabled
     server over stdio, lists its tools, and writes a cache to
     ``$NVH_HOME/state/mcp-tools-cache.json``.

  3. ``register_mcp_tools(reg)`` — called from the Wizard registry build —
     reads that cache *synchronously* (registry builds happen on every chat
     turn; we never blockingly spawn servers there) and registers one
     namespaced WizardTool per MCP tool: ``mcp_<server>_<tool>``.

  4. Tool execution spawns a short-lived stdio session per call, with hard
     timeouts, so a wedged external server can never hang the chat loop or
     leak processes.

Safety model
============
External MCP tools run arbitrary third-party code, so every MCP tool
defaults to ``safety_class="confirm"`` — the WebUI renders a "Do this?"
card before execution. A per-server ``auto_approve`` list promotes named
tools to ``auto`` for users who trust a specific server's read-only
operations. This mirrors the trust posture of Claude Desktop's allowlists.

Soft dependency
===============
The ``mcp`` SDK ships in the ``nvhive[mcp]`` extra. Every import of it in
this module is function-local; without the extra, configuration parsing
and status reporting still work, and actionable errors point at
``pip install 'nvhive[mcp]'``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Hard ceilings so a wedged external server can't hang the chat loop.
# Overridable via env for slow-starting servers (npx cold cache, etc.).
CONNECT_TIMEOUT_S = 20.0
CALL_TIMEOUT_S = 60.0

_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")

MISSING_SDK_HINT = (
    "the MCP client needs the 'mcp' package — install with: "
    "pip install 'nvhive[mcp]'"
)


# ────────────────────────────────────────────────────────────────────────────
# Paths + config
# ────────────────────────────────────────────────────────────────────────────


def mcp_config_path(home_dir: str | Path | None = None) -> Path:
    from nvh.integrations.workspace.storage import storage_layout

    return storage_layout(home_dir).config_dir / "mcp-servers.json"


def tools_cache_path(home_dir: str | Path | None = None) -> Path:
    from nvh.integrations.workspace.storage import storage_layout

    return storage_layout(home_dir).state_dir / "mcp-tools-cache.json"


def load_mcp_config(home_dir: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{server_name: spec}`` for every *enabled* configured server.

    Missing file → ``{}`` (feature simply off). Malformed JSON logs one
    warning and returns ``{}`` — a broken config must never break the
    Wizard. Spec fields: ``command`` (required), ``args`` (list),
    ``env`` (dict), ``auto_approve`` (list of tool names), ``enabled``
    (default True).
    """
    path = mcp_config_path(home_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("mcp-servers.json unreadable (%s): %s", path, exc)
        return {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        logger.warning("mcp-servers.json missing 'mcpServers' object: %s", path)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            logger.warning("mcp server %r: spec is not an object — skipped", name)
            continue
        if spec.get("enabled", True) is False:
            continue
        if not spec.get("command"):
            logger.warning("mcp server %r: no 'command' — skipped", name)
            continue
        out[str(name)] = spec
    return out


def _sanitize(part: str) -> str:
    """Lowercase + collapse anything non-identifier to '_' for tool names."""
    return _NAME_SANITIZE_RE.sub("_", part.lower()).strip("_")


def namespaced_tool_name(server: str, tool: str) -> str:
    """Stable registry name the LLM emits: ``mcp_<server>_<tool>``."""
    return f"mcp_{_sanitize(server)}_{_sanitize(tool)}"


# ────────────────────────────────────────────────────────────────────────────
# Tools cache — written by refresh, read synchronously at registry build
# ────────────────────────────────────────────────────────────────────────────


def read_tools_cache(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Return the cached ``{server: {ok, error, refreshed_at, tools[]}}`` map."""
    path = tools_cache_path(home_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("mcp tools cache unreadable: %s", exc)
        return {}


def _write_tools_cache(
    cache: dict[str, Any], home_dir: str | Path | None = None
) -> None:
    path = tools_cache_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1), encoding="utf-8")


# ────────────────────────────────────────────────────────────────────────────
# Async client operations (stdio transport)
# ────────────────────────────────────────────────────────────────────────────


def _server_params(spec: dict[str, Any]) -> Any:
    from mcp.client.stdio import StdioServerParameters

    return StdioServerParameters(
        command=str(spec["command"]),
        args=[str(a) for a in spec.get("args", [])],
        env={str(k): str(v) for k, v in (spec.get("env") or {}).items()} or None,
    )


async def list_server_tools(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Connect to one server, list its tools, disconnect.

    Returns ``[{name, description, input_schema}]``. Raises on connect
    failure / timeout — callers convert to per-server error status.
    """
    import anyio
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    with anyio.fail_after(CONNECT_TIMEOUT_S):
        async with stdio_client(_server_params(spec)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema or {},
        }
        for t in listed.tools
    ]


async def call_mcp_tool(
    server: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    home_dir: str | Path | None = None,
    timeout_s: float = CALL_TIMEOUT_S,
) -> dict[str, Any]:
    """Execute one tool on one configured server via a short-lived session.

    A fresh stdio session per call costs ~100-300ms of process spawn but
    guarantees a wedged server can't hold state or hang later calls —
    the right trade for arbitrary third-party subprocesses. Returns
    ``{ok, content?|error, is_error?}``.
    """
    try:
        import anyio
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client
    except ImportError:
        return {"ok": False, "error": MISSING_SDK_HINT}

    config = load_mcp_config(home_dir)
    spec = config.get(server)
    if spec is None:
        return {"ok": False, "error": f"MCP server '{server}' is not configured/enabled"}

    try:
        with anyio.fail_after(timeout_s):
            async with stdio_client(_server_params(spec)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments or {})
    except TimeoutError:
        return {"ok": False, "error": f"MCP call {server}/{tool} timed out after {timeout_s:.0f}s"}
    except Exception as exc:
        return {"ok": False, "error": f"MCP call {server}/{tool} failed: {exc}"}

    # Flatten content items: text joined; anything else summarized by type.
    parts: list[str] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(f"[{getattr(item, 'type', 'content')}]")
    return {
        "ok": not bool(getattr(result, "isError", False)),
        "content": "\n".join(parts),
        "is_error": bool(getattr(result, "isError", False)),
        "server": server,
        "tool": tool,
    }


async def refresh_all_tools(
    home_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Refresh the tools cache for every enabled server. Returns the cache.

    Per-server failures are recorded in the cache (``ok: false`` +
    ``error``) rather than raised, so one dead server never hides the
    others' tools.
    """
    try:
        import anyio  # noqa: F401 — presence check for the SDK stack
        import mcp  # noqa: F401
    except ImportError:
        logger.warning("mcp refresh skipped: %s", MISSING_SDK_HINT)
        return read_tools_cache(home_dir)

    config = load_mcp_config(home_dir)
    cache: dict[str, Any] = {}
    for name, spec in config.items():
        entry: dict[str, Any] = {"refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            entry["tools"] = await list_server_tools(spec)
            entry["ok"] = True
        except Exception as exc:
            entry["ok"] = False
            entry["error"] = str(exc)[:300]
            entry["tools"] = []
            logger.warning("mcp server %s refresh failed: %s", name, exc)
        cache[name] = entry
    _write_tools_cache(cache, home_dir)
    return cache


def servers_status(home_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Config + cache summary for the CLI/API/WebUI status surfaces."""
    config = load_mcp_config(home_dir)
    cache = read_tools_cache(home_dir)
    out = []
    for name, spec in config.items():
        entry = cache.get(name) or {}
        out.append({
            "name": name,
            "command": spec.get("command", ""),
            "auto_approve": list(spec.get("auto_approve", [])),
            "cached": bool(entry),
            "ok": bool(entry.get("ok")),
            "error": entry.get("error"),
            "refreshed_at": entry.get("refreshed_at"),
            "tool_count": len(entry.get("tools", [])),
            "tools": [t["name"] for t in entry.get("tools", [])],
        })
    return out


# ────────────────────────────────────────────────────────────────────────────
# Wizard registry integration
# ────────────────────────────────────────────────────────────────────────────


def register_mcp_tools(reg: Any, home_dir: str | Path | None = None) -> None:
    """Register cached MCP tools into a WizardToolRegistry.

    Called from ``default_registry()`` on every registry build, so it must
    be fast and synchronous: it only reads the JSON cache. Servers that
    haven't been refreshed contribute nothing until ``nvh mcp refresh``
    (or API-server startup) populates the cache.

    Safety: ``confirm`` by default; a tool named in its server's
    ``auto_approve`` list registers as ``auto``.
    """
    from nvh.integrations.wizard.tools import WizardTool

    config = load_mcp_config(home_dir)
    if not config:
        return
    cache = read_tools_cache(home_dir)

    for server, spec in config.items():
        entry = cache.get(server)
        if not entry or not entry.get("ok"):
            continue
        auto_approve = {str(t) for t in spec.get("auto_approve", [])}
        for tool in entry.get("tools", []):
            tool_name = tool.get("name", "")
            if not tool_name:
                continue
            reg_name = namespaced_tool_name(server, tool_name)
            schema = tool.get("input_schema") or {}
            properties = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            parameters = {
                key: {
                    "type": (val or {}).get("type", "string"),
                    "description": (val or {}).get("description", ""),
                    "required": key in required,
                }
                for key, val in properties.items()
            }
            safety = "auto" if tool_name in auto_approve else "confirm"

            def _make_handler(srv: str, tl: str):
                async def _handler(arguments: dict[str, Any]) -> dict[str, Any]:
                    return await call_mcp_tool(srv, tl, arguments, home_dir=home_dir)
                return _handler

            try:
                reg.register(WizardTool(
                    name=reg_name,
                    description=(
                        f"[MCP:{server}] {tool.get('description') or tool_name}"
                    )[:300],
                    safety_class=safety,
                    parameters=parameters,
                    handler=_make_handler(server, tool_name),
                    summary_template=f"Run MCP tool {tool_name} on {server}",
                ))
            except Exception as exc:
                logger.warning("mcp tool %s registration failed: %s", reg_name, exc)
