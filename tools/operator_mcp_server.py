#!/usr/bin/env python3
"""nvHive Operator MCP server.

Exposes the operator session (Chrome extension, CDP-over-port, or
AppleScript bridge — whichever's reachable) as Model Context Protocol
tools so an AI agent (Claude Code, Cursor, Continue, custom LangGraph)
can drive a streamed remote desktop directly through its tool-use API.

Tool surface (matches the operator-vision.md design):

  operator_attach(streaming_url) — find/open the streaming tab
  operator_run(command, wait_after)
  operator_type(text)
  operator_key(key)
  operator_click(x, y, button)
  operator_screenshot()
  operator_health()
  operator_keepalive(interval)

This is the **stdio** MCP server flavor (the standard for IDE integrations
like Claude Code's `.claude/mcp.json`). Register it in your MCP config:

    {
      "mcpServers": {
        "operator": {
          "command": "/usr/bin/python3",
          "args": ["/Users/ccooper/nvh/tools/operator_mcp_server.py"]
        }
      }
    }

Then the agent gets the tools above and can call them naturally:
"open the GFN tab and run `nvh selfcheck`."

Implementation: bare-bones JSON-RPC-over-stdio. The MCP spec is small
enough that we don't need the official `mcp` Python SDK — keeping zero
external deps so this runs in any CI Python.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

# Make sibling imports work no matter where you launch from.
THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))

from tools.gfn_session import GFNSession  # noqa: E402


# ---------------------------------------------------------------------------
# Lazy singleton session — created on first tool call, never on import.
# ---------------------------------------------------------------------------

_session: GFNSession | None = None


def _get_session() -> GFNSession:
    global _session  # noqa: PLW0603
    if _session is None:
        _session = GFNSession()
    return _session


# ---------------------------------------------------------------------------
# Tool definitions — names + JSON Schema, MCP-spec shape.
# ---------------------------------------------------------------------------


TOOLS: list[dict[str, Any]] = [
    {
        "name": "operator_health",
        "description": (
            "Check that an operator backend is reachable. Returns which "
            "backend is in use (extension / cdp / bridge) and basic status."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "operator_run",
        "description": (
            "Type a shell command into the streamed terminal and press Enter. "
            "Returns ok and the number of characters sent. Use this for any "
            "command-line operation in the remote desktop."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line to run."},
                "wait_after": {"type": "number", "default": 0.5,
                               "description": "Seconds to wait after pressing Enter."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "operator_type",
        "description": "Type literal text. No newline is appended.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "operator_key",
        "description": (
            "Press a single named key (Enter, Tab, Escape, Backspace, "
            "ArrowUp/Down/Left/Right, F1-F12)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "operator_click",
        "description": "Click at absolute coordinates (x, y) in the page viewport.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right"], "default": "left"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "operator_screenshot",
        "description": (
            "Capture a PNG screenshot of the current streaming tab. Returns "
            "base64 PNG data. Use this to see the streamed desktop's state."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "operator_keepalive",
        "description": (
            "Start the bridge-backend keepalive (background mouse-jiggle "
            "every N seconds) so an idle session stays alive. No-op on "
            "extension / cdp backends — they don't need it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"interval": {"type": "number", "default": 20.0}},
        },
    },
]


def _dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Route a tool call to the GFNSession backend."""
    s = _get_session()
    if name == "operator_health":
        return {"ok": True, "backend": s.backend, "detail": s.health()}
    if name == "operator_run":
        return s.run(args["command"], wait_after=args.get("wait_after", 0.5))
    if name == "operator_type":
        return s.type(args["text"])
    if name == "operator_key":
        return s.key(args["key"])
    if name == "operator_click":
        return s.click(int(args["x"]), int(args["y"]), button=args.get("button", "left"))
    if name == "operator_screenshot":
        return s.screenshot()
    if name == "operator_keepalive":
        return s.keepalive(interval=float(args.get("interval", 20.0)))
    return {"ok": False, "error": f"unknown tool: {name}"}


# ---------------------------------------------------------------------------
# JSON-RPC-over-stdio (MCP wire protocol)
# ---------------------------------------------------------------------------


def _send(payload: dict[str, Any]) -> None:
    """Write a JSON-RPC message to stdout (length-prefixed by newline)."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _make_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _make_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _handle(req: dict[str, Any]) -> dict[str, Any] | None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return _make_result(req_id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "nvhive-operator", "version": "0.1.0"},
            "capabilities": {"tools": {}},
        })

    if method == "tools/list":
        return _make_result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        try:
            result = _dispatch(name, args)
        except Exception as exc:
            return _make_error(req_id, -32000, f"{name}: {exc}\n{traceback.format_exc()}")
        return _make_result(req_id, {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": not bool(result.get("ok", True)),
        })

    if method in {"notifications/initialized", "notifications/cancelled"}:
        # Notifications don't get a response.
        return None

    if req_id is None:
        # Unknown notification — ignore.
        return None

    return _make_error(req_id, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(req)
        if resp is not None:
            _send(resp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
