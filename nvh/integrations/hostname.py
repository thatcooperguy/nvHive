"""Local hostname setup for nvHive.

Adds 'nvhive' as a local hostname alias so users can type
http://nvhive:3000 for the WebUI and http://nvhive:8000 for the API
instead of http://localhost.

This modifies /etc/hosts (requires sudo on Linux/macOS).
Does NOT touch port 80 — avoids conflicts with existing services.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HOSTNAME = "nvhive"
HOSTS_ENTRY = f"127.0.0.1  {HOSTNAME}"
WEBUI_PORT = 3000
API_PORT = 8000


def is_hostname_configured() -> bool:
    """Check if 'nvhive' hostname is already in /etc/hosts."""
    hosts_file = Path("/etc/hosts")
    if not hosts_file.exists():
        return False
    content = hosts_file.read_text()
    return HOSTNAME in content


def add_hostname() -> tuple[bool, str]:
    """Add 'nvhive' to /etc/hosts pointing to 127.0.0.1.

    Returns (success, message).
    """
    if is_hostname_configured():
        return True, (
            f"'{HOSTNAME}' is already configured\n"
            f"  WebUI: http://{HOSTNAME}:{WEBUI_PORT}\n"
            f"  API:   http://{HOSTNAME}:{API_PORT}"
        )

    if sys.platform == "win32":
        hosts_path = Path(r"C:\Windows\System32\drivers\etc\hosts")
        return False, (
            f"Add this line to {hosts_path}:\n"
            f"  {HOSTS_ENTRY}\n"
            "Run as Administrator to edit."
        )

    # Linux/macOS: /etc/hosts (needs sudo)
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

        # sudo needs password
        return False, (
            f"Run this to enable http://{HOSTNAME}:\n"
            f'  sudo sh -c \'echo "{HOSTS_ENTRY}" >> /etc/hosts\'\n'
            f"\n"
            f"  WebUI: http://{HOSTNAME}:{WEBUI_PORT}\n"
            f"  API:   http://{HOSTNAME}:{API_PORT}"
        )
    except Exception as e:
        return False, f"Could not modify /etc/hosts: {e}"


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
