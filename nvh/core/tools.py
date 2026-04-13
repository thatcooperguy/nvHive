"""NVHive Tool System — give LLMs the ability to act.

Tools are registered functions that LLMs can call during a query.
When a tool is enabled, the LLM receives tool descriptions in its
system prompt and can request tool calls.

Built-in tools:
  read_file     — read a file's contents
  write_file    — write/create a file
  list_files    — list files in a directory (glob patterns)
  search_files  — search file contents (grep-like)
  run_code      — execute code in sandbox
  shell         — run a shell command (sandboxed)

Tools are opt-in per query:
  nvh ask "Read main.py and fix the bug" --tools

Or in the REPL:
  /tools on
"""

from __future__ import annotations

import glob as globmod
import os
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema for parameters
    handler: Callable           # async function that executes the tool
    safe: bool = True           # safe tools can run without confirmation

@dataclass
class ToolResult:
    tool_name: str
    success: bool
    output: str
    error: str = ""

class ToolRegistry:
    def __init__(self, workspace: str | None = None, include_system: bool = True):
        self._tools: dict[str, Tool] = {}
        self.workspace = workspace or os.getcwd()
        self._register_builtins()
        if include_system:
            try:
                from nvh.core.system_tools import register_system_tools
                register_system_tools(self)
            except Exception:
                pass  # system tools are optional
            try:
                from nvh.core.browser_tools import register_browser_tools
                register_browser_tools(self)
            except Exception:
                pass  # browser tools are optional
            try:
                from nvh.core.vision_tools import register_vision_tools
                register_vision_tools(self)
            except Exception:
                pass  # vision tools are optional

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def get_tool_descriptions(self) -> str:
        """Format tool descriptions for injection into system prompt."""
        lines = ["Available tools (call by including a tool_call block in your response):"]
        for tool in self._tools.values():
            params = ", ".join(
                f"{k}: {v.get('type', 'string')}"
                for k, v in tool.parameters.get("properties", {}).items()
            )
            lines.append(f"  - {tool.name}({params}): {tool.description}")
        return "\n".join(lines)

    async def execute(self, tool_name: str, arguments: dict) -> ToolResult:
        """Execute a tool by name with given arguments.

        Guardrails are checked before execution and cannot be bypassed
        by --yes or auto_approve_safe. If a guardrail fires, the tool
        call is rejected and the error is shown to the user (not fed
        back to the LLM for retry).
        """
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output="",
                error=f"Unknown tool: {tool_name}",
            )

        # ── Guardrail checks ──────────────────────────────────────────
        try:
            from nvh.core.agent_guardrails import (
                GuardrailError,
                check_command,
                check_file_read,
                check_path,
                check_write_size,
                redact_secrets,
                truncate_output,
            )

            workspace = pathlib.Path(self.workspace)

            if tool_name == "shell" or tool_name == "run_code":
                cmd = arguments.get("command", "")
                check_command(cmd)

            if tool_name == "read_file":
                path = arguments.get("path", "")
                check_file_read(path)
                check_path(self._resolve_path(path), workspace)

            if tool_name == "write_file":
                path = arguments.get("path", "")
                content = arguments.get("content", "")
                check_path(self._resolve_path(path), workspace)
                check_write_size(content, path)

        except GuardrailError as e:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output="",
                error=f"GUARDRAIL: {e}",
            )
        except ImportError:
            pass  # guardrails module not available — proceed without

        try:
            result = await tool.handler(**arguments)
            output = str(result)

            # Redact any secrets from the output before it goes back
            # to the LLM context
            try:
                output = redact_secrets(output)
                output = truncate_output(output)
            except Exception:
                pass

            return ToolResult(tool_name=tool_name, success=True, output=output)
        except Exception as e:
            return ToolResult(tool_name=tool_name, success=False, output="", error=str(e))

    def _register_builtins(self) -> None:
        """Register built-in tools."""

        async def read_file(path: str) -> str:
            """Read a file's contents."""
            full_path = self._resolve_path(path)
            if not os.path.isfile(full_path):
                raise FileNotFoundError(f"File not found: {path}")
            with open(full_path) as f:
                content = f.read()
            if len(content) > 100_000:
                content = content[:100_000] + f"\n... (truncated, {len(content)} chars total)"
            return content

        async def write_file(path: str, content: str) -> str:
            """Write content to a file."""
            full_path = self._resolve_path(path)
            os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
            return f"Written {len(content)} chars to {path}"

        async def list_files(pattern: str = "*", directory: str = ".") -> str:
            """List files matching a glob pattern."""
            full_dir = self._resolve_path(directory)
            matches = globmod.glob(os.path.join(full_dir, pattern), recursive=True)
            # Limit results
            if len(matches) > 100:
                matches = matches[:100]
                matches.append(f"... ({len(matches)} total, showing first 100)")
            return "\n".join(os.path.relpath(m, self.workspace) for m in matches)

        async def search_files(query: str, pattern: str = "*.py", directory: str = ".") -> str:
            """Search file contents for a string."""
            full_dir = self._resolve_path(directory)
            results = []
            files = globmod.glob(os.path.join(full_dir, "**", pattern), recursive=True)
            for fpath in files[:50]:  # limit files searched
                try:
                    with open(fpath) as f:
                        for i, line in enumerate(f, 1):
                            if query.lower() in line.lower():
                                rel = os.path.relpath(fpath, self.workspace)
                                results.append(f"{rel}:{i}: {line.rstrip()}")
                                if len(results) >= 30:
                                    break
                except (UnicodeDecodeError, PermissionError):
                    continue
                if len(results) >= 30:
                    break
            return "\n".join(results) if results else f"No matches for '{query}'"

        async def run_code(code: str, language: str = "python") -> str:
            """Execute code in a sandboxed environment."""
            from nvh.sandbox.executor import SandboxExecutor
            executor = SandboxExecutor()
            result = await executor.execute(code=code, language=language)
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.timed_out:
                output += "\n(execution timed out)"
            return output

        async def shell(command: str) -> str:
            """Run a shell command (sandboxed).

            When Docker sandbox mode is enabled (--sandbox or NVH_SANDBOX=1),
            commands run inside a Docker container for full isolation.
            Otherwise falls through to the existing SandboxExecutor.
            """
            try:
                from nvh.core.docker_sandbox import run_in_sandbox, sandbox_enabled
                if sandbox_enabled():
                    return await run_in_sandbox(command, self.workspace)
            except ImportError:
                pass
            return await run_code(command, language="bash")

        # Register all
        self.register(Tool(
            name="read_file",
            description="Read a file's contents",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path to read"}},
                "required": ["path"],
            },
            handler=read_file,
        ))
        self.register(Tool(
            name="write_file",
            description="Write content to a file",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            handler=write_file,
            safe=False,
        ))
        self.register(Tool(
            name="list_files",
            description="List files matching a glob pattern",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "default": "*"},
                    "directory": {"type": "string", "default": "."},
                },
                "required": [],
            },
            handler=list_files,
        ))
        self.register(Tool(
            name="search_files",
            description="Search file contents for a string",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "pattern": {"type": "string", "default": "*.py"},
                    "directory": {"type": "string", "default": "."},
                },
                "required": ["query"],
            },
            handler=search_files,
        ))
        self.register(Tool(
            name="run_code",
            description="Execute code in a sandboxed environment",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "language": {"type": "string", "default": "python"},
                },
                "required": ["code"],
            },
            handler=run_code,
            safe=False,
        ))
        self.register(Tool(
            name="shell",
            description="Run a shell command",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            handler=shell,
            safe=False,
        ))

        async def web_search(query: str, num_results: int = 5, engine: str = "auto") -> str:
            """Search the web and return top results with snippets.

            Engines: auto (tries best available), duckduckgo, google, brave, searxng.
            DuckDuckGo is the default — no API key needed.
            Set BRAVE_SEARCH_KEY or GOOGLE_SEARCH_KEY + GOOGLE_CX for those engines.
            """
            import os
            import re

            import httpx

            if engine == "auto":
                # Pick best available search engine
                if os.environ.get("BRAVE_SEARCH_KEY"):
                    engine = "brave"
                elif os.environ.get("GOOGLE_SEARCH_KEY") and os.environ.get("GOOGLE_CX"):
                    engine = "google"
                elif os.environ.get("SEARXNG_URL"):
                    engine = "searxng"
                else:
                    engine = "duckduckgo"

            async with httpx.AsyncClient() as client:
                if engine == "brave":
                    # Brave Search API (free tier: 2000 queries/month)
                    key = os.environ.get("BRAVE_SEARCH_KEY", "")
                    resp = await client.get(
                        "https://api.search.brave.com/res/v1/web/search",
                        params={"q": query, "count": num_results},
                        headers={"Accept": "application/json", "X-Subscription-Token": key},
                        timeout=10,
                    )
                    data = resp.json()
                    results = []
                    for i, r in enumerate(data.get("web", {}).get("results", [])[:num_results]):
                        results.append(f"{i+1}. {r.get('title', '')}\n   {r.get('url', '')}\n   {r.get('description', '')}\n")
                    return "\n".join(results) if results else f"No results for: {query}"

                elif engine == "google":
                    # Google Custom Search API (free tier: 100 queries/day)
                    key = os.environ.get("GOOGLE_SEARCH_KEY", "")
                    cx = os.environ.get("GOOGLE_CX", "")
                    resp = await client.get(
                        "https://www.googleapis.com/customsearch/v1",
                        params={"key": key, "cx": cx, "q": query, "num": num_results},
                        timeout=10,
                    )
                    data = resp.json()
                    results = []
                    for i, item in enumerate(data.get("items", [])[:num_results]):
                        results.append(f"{i+1}. {item.get('title', '')}\n   {item.get('link', '')}\n   {item.get('snippet', '')}\n")
                    return "\n".join(results) if results else f"No results for: {query}"

                elif engine == "searxng":
                    # SearXNG (self-hosted, privacy-focused)
                    searxng_url = os.environ.get("SEARXNG_URL", "https://searx.be")
                    resp = await client.get(
                        f"{searxng_url}/search",
                        params={"q": query, "format": "json", "categories": "general"},
                        timeout=10,
                    )
                    data = resp.json()
                    results = []
                    for i, r in enumerate(data.get("results", [])[:num_results]):
                        results.append(f"{i+1}. {r.get('title', '')}\n   {r.get('url', '')}\n   {r.get('content', '')}\n")
                    return "\n".join(results) if results else f"No results for: {query}"

                else:
                    # DuckDuckGo (default, no API key needed)
                    url = "https://html.duckduckgo.com/html/"
                    resp = await client.post(url, data={"q": query}, timeout=10,
                                             headers={"User-Agent": "NVHive/0.1"})
                    results = []
                    links = re.findall(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', resp.text)
                    snippets = re.findall(r'class="result__snippet">(.*?)</span>', resp.text, re.DOTALL)
                    for i, (link_match, title) in enumerate(links[:num_results]):
                        title_clean = re.sub(r'<[^>]+>', '', title).strip()
                        snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
                        actual_url = link_match
                        if "uddg=" in actual_url:
                            from urllib.parse import parse_qs, unquote, urlparse
                            parsed = urlparse(actual_url)
                            params = parse_qs(parsed.query)
                            actual_url = unquote(params.get("uddg", [actual_url])[0])
                        results.append(f"{i+1}. {title_clean}\n   {actual_url}\n   {snippet}\n")
                    return "\n".join(results) if results else f"No results for: {query}"

        async def web_fetch(url: str, max_chars: int = 10000) -> str:
            """Fetch a web page and extract readable text content."""
            import html as html_mod
            import ipaddress
            import re
            from urllib.parse import urlparse

            import httpx

            # SSRF protection: block private/internal URLs
            parsed = urlparse(url)
            hostname = parsed.hostname or ""
            if not hostname:
                return "Error: Invalid URL"

            # Block private IPs, loopback, link-local, and cloud metadata
            blocked_hosts = {"169.254.169.254", "metadata.google.internal", "localhost", "127.0.0.1", "0.0.0.0"}
            if hostname in blocked_hosts:
                return "Error: Access to internal/metadata URLs is blocked for security"

            try:
                ip = ipaddress.ip_address(hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return f"Error: Access to private IP {hostname} is blocked for security"
            except ValueError:
                pass  # Not an IP — hostname is fine

            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(url, timeout=15,
                                        headers={"User-Agent": "NVHive/0.1"})
                resp.raise_for_status()
                html = resp.text
                html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
                html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
                text = re.sub(r'<[^>]+>', ' ', html)
                text = re.sub(r'\s+', ' ', text).strip()
                text = html_mod.unescape(text)
                if len(text) > max_chars:
                    text = text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
                return text

        self.register(Tool(
            name="web_search",
            description="Search the web and return top results with snippets",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {"type": "integer", "default": 5, "description": "Number of results to return"},
                },
                "required": ["query"],
            },
            handler=web_search,
            safe=True,
        ))
        self.register(Tool(
            name="web_fetch",
            description="Fetch a web page and extract readable text content",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_chars": {"type": "integer", "default": 10000, "description": "Maximum characters to return"},
                },
                "required": ["url"],
            },
            handler=web_fetch,
            safe=True,
        ))

        async def screenshot(region: str = "full") -> str:
            """Take a screenshot and describe it using a multimodal LLM."""
            import base64
            import subprocess
            import sys
            import tempfile

            path = tempfile.mktemp(suffix=".png")

            if sys.platform == "darwin":
                # macOS: screencapture is always available
                try:
                    subprocess.run(
                        ["screencapture", "-x", path],
                        timeout=5, capture_output=True, check=True,
                    )
                except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    return "Screenshot failed on macOS — screencapture unavailable."
            else:
                # Linux: try various screenshot tools
                for cmd in [
                    ["gnome-screenshot", "-f", path],
                    ["scrot", path],
                    ["import", "-window", "root", path],   # ImageMagick
                    ["xfce4-screenshooter", "-f", "-s", path],
                ]:
                    try:
                        subprocess.run(cmd, timeout=5, capture_output=True)
                        if os.path.exists(path):
                            break
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        continue

            if not os.path.exists(path):
                return (
                    "Screenshot failed — no screenshot tool found. "
                    "Install: sudo apt install scrot"
                )

            # Read and base64 encode
            with open(path, "rb") as f:
                img_data = base64.b64encode(f.read()).decode()

            return (
                f"Screenshot saved to {path}. "
                f"Base64 data length: {len(img_data)} chars. "
                "Use a multimodal model to analyze it."
            )

        self.register(Tool(
            name="screenshot",
            description=(
                "Take a screenshot of the current screen and return its path and base64 data "
                "for analysis by a multimodal model"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "default": "full",
                        "description": "Screen region to capture: full (default)",
                    },
                },
                "required": [],
            },
            handler=screenshot,
            safe=True,
        ))

        async def imagine(prompt: str, provider: str = "auto", size: str = "1024x1024") -> str:
            """Generate an image from a text prompt using AI image generation."""
            from nvh.core.image_gen import generate_image
            output_path = await generate_image(prompt=prompt, provider=provider, size=size)
            return f"Image generated and saved to: {output_path}"

        self.register(Tool(
            name="imagine",
            description="Generate an image from a text description using AI image generation",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Text description of the image to generate",
                    },
                    "provider": {
                        "type": "string",
                        "default": "auto",
                        "description": "Image provider: auto, openai, stability, pollinations",
                    },
                    "size": {
                        "type": "string",
                        "default": "1024x1024",
                        "description": "Image dimensions, e.g. 1024x1024",
                    },
                },
                "required": ["prompt"],
            },
            handler=imagine,
            safe=True,
        ))

    def _resolve_path(self, path: str) -> str:
        """Resolve a path relative to workspace, preventing traversal."""
        resolved = os.path.normpath(os.path.join(self.workspace, path))
        # Prevent path traversal outside workspace
        if not resolved.startswith(os.path.normpath(self.workspace)):
            raise PermissionError(f"Path traversal blocked: {path}")
        return resolved
