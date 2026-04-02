"""Local hostname setup for nvHive.

Adds 'nvhive' as a local hostname alias so users can type
http://nvhive:3000 for the WebUI and http://nvhive:8000 for the API
instead of http://localhost.

Works in three tiers:
1. Full access: modifies /etc/hosts (requires sudo)
2. Sandbox/no-root: skips /etc/hosts, uses localhost with friendly aliases
3. Always works: localhost:PORT is the universal fallback

Does NOT touch port 80 — avoids conflicts with existing services.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HOSTNAME = "nvhive"
HOSTS_ENTRY = f"127.0.0.1  {HOSTNAME}"
WEBUI_PORT = 3000
API_PORT = 8000


def is_sandbox() -> bool:
    """Detect if running in a sandboxed/restricted environment."""
    indicators = [
        not os.access("/etc/hosts", os.W_OK) and not _has_sudo(),
        os.environ.get("SANDBOX", ""),
        os.environ.get("FLATPAK_ID", ""),
        os.environ.get("SNAP", ""),
        os.environ.get("container", ""),  # podman/docker
        Path("/.dockerenv").exists(),
    ]
    return any(indicators)


def _has_sudo() -> bool:
    """Check if sudo is available without a password."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_hostname_configured() -> bool:
    """Check if 'nvhive' hostname resolves to localhost."""
    # First check /etc/hosts
    hosts_file = Path("/etc/hosts")
    try:
        if hosts_file.exists() and HOSTNAME in hosts_file.read_text():
            return True
    except PermissionError:
        pass

    # Also try DNS resolution (works even without /etc/hosts access)
    try:
        import socket
        addr = socket.gethostbyname(HOSTNAME)
        return addr in ("127.0.0.1", "::1")
    except Exception:
        return False


def get_access_urls() -> dict[str, str]:
    """Return the best URLs for accessing nvHive services.

    Returns localhost-based URLs in sandbox environments,
    hostname-based URLs when configured.
    """
    if is_hostname_configured():
        host = HOSTNAME
    else:
        host = "localhost"
    return {
        "webui": f"http://{host}:{WEBUI_PORT}",
        "api": f"http://{host}:{API_PORT}",
        "docs": f"http://{host}:{API_PORT}/docs",
    }


def add_hostname() -> tuple[bool, str]:
    """Add 'nvhive' to /etc/hosts pointing to 127.0.0.1.

    In sandbox/no-root environments, returns localhost guidance
    instead of failing.

    Returns (success, message).
    """
    if is_hostname_configured():
        return True, (
            f"'{HOSTNAME}' is already configured\n"
            f"  WebUI: http://{HOSTNAME}:{WEBUI_PORT}\n"
            f"  API:   http://{HOSTNAME}:{API_PORT}"
        )

    # Sandbox/container detection — skip /etc/hosts entirely
    if is_sandbox():
        return True, (
            "Sandbox environment detected — using localhost\n"
            f"  WebUI: http://localhost:{WEBUI_PORT}\n"
            f"  API:   http://localhost:{API_PORT}\n"
            "  Hostname setup skipped (no root access needed)"
        )

    if sys.platform == "win32":
        hosts_path = Path(r"C:\Windows\System32\drivers\etc\hosts")
        return False, (
            f"Add this line to {hosts_path}:\n"
            f"  {HOSTS_ENTRY}\n"
            "Run as Administrator to edit.\n"
            f"Or just use http://localhost:{WEBUI_PORT}"
        )

    # Linux/macOS: try sudo, fall back gracefully
    try:
        result = subprocess.run(
            ["sudo", "-n", "sh", "-c",
             f'echo "{HOSTS_ENTRY}" >> /etc/hosts'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return True, (
                f"Added '{HOSTNAME}' to /etc/hosts\n"
                f"  WebUI: http://{HOSTNAME}:{WEBUI_PORT}\n"
                f"  API:   http://{HOSTNAME}:{API_PORT}"
            )
    except Exception:
        pass

    # sudo not available or needs password — provide both options
    return False, (
        f"Optional: enable http://{HOSTNAME} (requires sudo):\n"
        f'  sudo sh -c \'echo "{HOSTS_ENTRY}" >> /etc/hosts\'\n'
        "\n"
        "Works without it:\n"
        f"  WebUI: http://localhost:{WEBUI_PORT}\n"
        f"  API:   http://localhost:{API_PORT}"
    )


def remove_hostname() -> tuple[bool, str]:
    """Remove 'nvhive' from /etc/hosts."""
    if not is_hostname_configured():
        return True, f"'{HOSTNAME}' is not in /etc/hosts"

    if sys.platform == "darwin":
        cmd = f"sudo sed -i '' '/{HOSTNAME}/d' /etc/hosts"
    else:
        cmd = f"sudo sed -i '/{HOSTNAME}/d' /etc/hosts"
    return False, (
        f"Remove '{HOSTNAME}' from /etc/hosts:\n  {cmd}"
    )
