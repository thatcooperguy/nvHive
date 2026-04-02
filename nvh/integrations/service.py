"""Service/daemon management for nvHive proxy.

Generates platform-specific service files so the nvHive proxy
starts automatically and stays running for NemoClaw/OpenClaw.

Supported:
- Linux: systemd user service
- macOS: launchd plist
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_nvh_bin() -> str:
    """Find the nvh binary path."""
    import shutil
    path = shutil.which("nvh") or shutil.which("nvhive")
    if path:
        return path
    # Fallback: python -m nvh.cli.main
    return f"{sys.executable} -m nvh.cli.main"


def generate_systemd_service(
    host: str = "127.0.0.1",
    port: int = 8000,
) -> str:
    """Generate a systemd user service file for the nvHive proxy."""
    nvh = _find_nvh_bin()
    return f"""[Unit]
Description=NVHive Multi-LLM Proxy
Documentation=https://github.com/thatcooperguy/nvHive
After=network.target

[Service]
Type=simple
ExecStart={nvh} serve --host {host} --port {port}
Restart=on-failure
RestartSec=5
Environment=HOME={Path.home()}

[Install]
WantedBy=default.target
"""


def generate_launchd_plist(
    host: str = "127.0.0.1",
    port: int = 8000,
) -> str:
    """Generate a macOS launchd plist for the nvHive proxy."""
    nvh = _find_nvh_bin()
    label = "com.nvhive.proxy"

    # If nvh is a script, use the interpreter
    args_xml = ""
    if nvh.startswith(sys.executable):
        parts = nvh.split()
        for part in parts:
            args_xml += f"        <string>{part}</string>\n"
    else:
        args_xml = f"        <string>{nvh}</string>\n"
    args_xml += "        <string>serve</string>\n"
    args_xml += "        <string>--host</string>\n"
    args_xml += f"        <string>{host}</string>\n"
    args_xml += "        <string>--port</string>\n"
    args_xml += f"        <string>{port}</string>\n"

    log_dir = Path.home() / "Library" / "Logs" / "nvhive"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml.rstrip()}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/proxy.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/proxy.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>{Path.home()}</string>
        <key>PATH</key>
        <string>{os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}</string>
    </dict>
</dict>
</plist>
"""


def install_systemd_service(host: str = "127.0.0.1", port: int = 8000) -> tuple[bool, str]:
    """Install the systemd user service."""
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "nvhive-proxy.service"

    content = generate_systemd_service(host, port)
    service_file.write_text(content)

    # Enable and start
    import subprocess
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "nvhive-proxy"], check=True, capture_output=True)
        subprocess.run(["systemctl", "--user", "start", "nvhive-proxy"], check=True, capture_output=True)
        return True, f"Service installed and started: {service_file}"
    except subprocess.CalledProcessError as e:
        return False, f"Service file written to {service_file} but start failed: {e.stderr.decode().strip()}"
    except FileNotFoundError:
        return False, f"Service file written to {service_file} — run: systemctl --user enable --now nvhive-proxy"


def install_launchd_service(host: str = "127.0.0.1", port: int = 8000) -> tuple[bool, str]:
    """Install the macOS launchd service."""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_file = plist_dir / "com.nvhive.proxy.plist"

    # Create log directory
    log_dir = Path.home() / "Library" / "Logs" / "nvhive"
    log_dir.mkdir(parents=True, exist_ok=True)

    content = generate_launchd_plist(host, port)
    plist_file.write_text(content)

    import subprocess
    try:
        # Unload first if already loaded
        subprocess.run(
            ["launchctl", "unload", str(plist_file)],
            capture_output=True,
        )
        subprocess.run(
            ["launchctl", "load", str(plist_file)],
            check=True, capture_output=True,
        )
        return True, f"Service installed and started: {plist_file}"
    except subprocess.CalledProcessError as e:
        return False, f"Plist written to {plist_file} but load failed: {e.stderr.decode().strip()}"
    except FileNotFoundError:
        return False, f"Plist written to {plist_file} — run: launchctl load {plist_file}"


def uninstall_service() -> tuple[bool, str]:
    """Uninstall the nvHive proxy service."""
    import subprocess

    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.nvhive.proxy.plist"
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink()
            return True, f"Removed {plist}"
        return False, "Service not installed"
    else:
        service = Path.home() / ".config" / "systemd" / "user" / "nvhive-proxy.service"
        if service.exists():
            subprocess.run(["systemctl", "--user", "stop", "nvhive-proxy"], capture_output=True)
            subprocess.run(["systemctl", "--user", "disable", "nvhive-proxy"], capture_output=True)
            service.unlink()
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            return True, f"Removed {service}"
        return False, "Service not installed"


def service_status() -> tuple[bool, str]:
    """Check if the nvHive proxy service is running."""
    import subprocess

    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.nvhive.proxy.plist"
        if not plist.exists():
            return False, "Not installed"
        try:
            result = subprocess.run(
                ["launchctl", "list", "com.nvhive.proxy"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                return True, "Running"
            return False, "Installed but not running"
        except Exception:
            return False, "Cannot check status"
    else:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "nvhive-proxy"],
                capture_output=True, text=True,
            )
            status = result.stdout.strip()
            return status == "active", status.capitalize()
        except FileNotFoundError:
            return False, "systemctl not available"
