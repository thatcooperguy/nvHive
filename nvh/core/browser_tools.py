"""NVHive Browser & Extended OS Tools.

Adds browser automation (via Playwright) and expanded OS tools
(HTTP requests, process management, Docker) to the tool registry.
Gracefully degrades when optional dependencies are missing.
"""

from __future__ import annotations

import platform
import re
import subprocess

from nvh.core.tools import Tool, ToolRegistry


def _strip_html(html: str) -> str:
    """Remove HTML tags, scripts, styles and collapse whitespace."""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def register_browser_tools(registry: ToolRegistry) -> None:
    """Register browser automation and OS tools into *registry*."""

    # ── Browser tools ────────────────────────────────────────────────

    async def browser_navigate(url: str) -> str:
        """Open a headless browser, navigate to *url*, return page text."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return (
                "Playwright is not installed. "
                "Install it with: pip install playwright && playwright install chromium"
            )
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, timeout=30_000)
                title = page.title()
                text = _strip_html(page.content())
                return f"Title: {title}\n\n{text[:5000]}"
            finally:
                browser.close()

    async def browser_screenshot(
        url: str, output_path: str = "screenshot.png"
    ) -> str:
        """Navigate to *url*, save a screenshot, return the file path."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "Playwright is not installed."
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, timeout=30_000)
                page.screenshot(path=output_path)
                return f"Screenshot saved to {output_path}"
            finally:
                browser.close()

    async def browser_fill_form(url: str, selectors: dict) -> str:
        """Navigate to *url*, fill form fields, return resulting text."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return "Playwright is not installed."
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, timeout=30_000)
                for sel, value in selectors.items():
                    if value == "click":
                        page.click(sel, timeout=30_000)
                    else:
                        page.fill(sel, value, timeout=30_000)
                page.wait_for_load_state("networkidle", timeout=30_000)
                return _strip_html(page.content())[:5000]
            finally:
                browser.close()

    # ── HTTP tool ────────────────────────────────────────────────────

    async def http_request(
        method: str,
        url: str,
        headers: dict | None = None,
        body: str | None = None,
    ) -> str:
        """Make an HTTP request and return status + body."""
        import httpx

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.request(
                method.upper(),
                url,
                headers=headers,
                content=body,
                timeout=30,
            )
            text = resp.text[:5000]
            return f"HTTP {resp.status_code}\n\n{text}"

    # ── Process tools ────────────────────────────────────────────────

    async def process_list() -> str:
        """Return a list of running processes (cross-platform)."""
        cmd = "tasklist" if platform.system() == "Windows" else "ps aux"
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10,
        )
        return result.stdout[:5000] or result.stderr

    async def process_kill(pid_or_name: str) -> str:
        """Kill a process by PID or name (cross-platform)."""
        if platform.system() == "Windows":
            try:
                int(pid_or_name)
                cmd = f"taskkill /F /PID {pid_or_name}"
            except ValueError:
                cmd = f"taskkill /F /IM {pid_or_name}"
        else:
            try:
                int(pid_or_name)
                cmd = f"kill -9 {pid_or_name}"
            except ValueError:
                cmd = f"pkill -9 -f {pid_or_name}"
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10,
        )
        return result.stdout or result.stderr or "Done."

    # ── Docker tools ─────────────────────────────────────────────────

    async def docker_ps() -> str:
        """List running Docker containers."""
        try:
            result = subprocess.run(
                ["docker", "ps"], capture_output=True, text=True, timeout=10,
            )
            return result.stdout or result.stderr
        except FileNotFoundError:
            return "Docker not available"

    async def docker_run(
        image: str, command: str = "", ports: str = ""
    ) -> str:
        """Run a Docker container."""
        try:
            cmd = ["docker", "run", "-d"]
            if ports:
                cmd += ["-p", ports]
            cmd.append(image)
            if command:
                cmd += command.split()
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30,
            )
            return result.stdout or result.stderr
        except FileNotFoundError:
            return "Docker not available"

    # ── Registration ─────────────────────────────────────────────────

    _TOOLS = [
        ("browser_navigate", "Open a URL in a headless browser and return page text",
         {"url": {"type": "string"}}, browser_navigate, True),
        ("browser_screenshot", "Take a screenshot of a URL",
         {"url": {"type": "string"}, "output_path": {"type": "string", "default": "screenshot.png"}},
         browser_screenshot, False),
        ("browser_fill_form", "Fill and submit a web form",
         {"url": {"type": "string"}, "selectors": {"type": "object"}},
         browser_fill_form, False),
        ("http_request", "Make an HTTP request (GET/POST/PUT/DELETE)",
         {"method": {"type": "string"}, "url": {"type": "string"},
          "headers": {"type": "object"}, "body": {"type": "string"}},
         http_request, True),
        ("process_list", "List running processes",
         {}, process_list, True),
        ("process_kill", "Kill a process by PID or name",
         {"pid_or_name": {"type": "string"}}, process_kill, False),
        ("docker_ps", "List running Docker containers",
         {}, docker_ps, True),
        ("docker_run", "Run a Docker container",
         {"image": {"type": "string"}, "command": {"type": "string"},
          "ports": {"type": "string"}}, docker_run, False),
    ]

    for name, desc, props, handler, safe in _TOOLS:
        required = [k for k, v in props.items() if "default" not in v and v.get("type") != "object"]
        registry.register(Tool(
            name=name,
            description=desc,
            parameters={"type": "object", "properties": props, "required": required},
            handler=handler,
            safe=safe,
        ))
