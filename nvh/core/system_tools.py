"""NVHive System Tools — AI can manage the desktop on Linux, macOS, and Windows.

Extends the tool registry with system-level capabilities:
- Process management (list, find, monitor)
- Application launching (open apps, URLs, files)
- File management (organize, find, disk usage)
- Clipboard operations
- System info (memory, CPU, disk, network)
- User-level package management (pip, npm — no root needed)
- Desktop notifications
- Environment variables and PATH management

All tools operate at USER level — no root, no sudo, no system changes.
Safe for Linux Desktop instances with mounted home directories.

Platform support:
- Linux: full support (original behaviour)
- macOS: POSIX commands where identical; macOS-specific alternatives otherwise
- Windows: tasklist, taskkill, os.startfile, PowerShell clipboard/notifications
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from nvh.core.tools import Tool, ToolRegistry


def register_system_tools(registry: ToolRegistry) -> None:
    """Register all system-level tools into an existing ToolRegistry."""

    # =================================================================
    # Process Management
    # =================================================================

    async def list_processes(filter: str = "", sort_by: str = "cpu") -> str:
        """List running processes, optionally filtered by name."""
        try:
            if sys.platform == "win32":
                # tasklist /FO CSV gives a stable parseable format on Windows
                proc = await asyncio.create_subprocess_shell(
                    "tasklist /FO CSV /NH",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                import csv
                import io
                reader = csv.reader(io.StringIO(stdout.decode(errors="replace")))
                rows = list(reader)
                # columns: Image Name, PID, Session Name, Session#, Mem Usage
                header = f"{'NAME':<30} {'PID':>6} {'MEM':>12}"
                lines_out = [header]
                for row in rows:
                    if len(row) >= 5:
                        name, pid, _, _, mem = row[0], row[1], row[2], row[3], row[4]
                        lines_out.append(f"{name:<30} {pid:>6} {mem:>12}")
                if filter:
                    lines_out = [lines_out[0]] + [ln for ln in lines_out[1:] if filter.lower() in ln.lower()]
                return "\n".join(lines_out[:26])
            else:
                # Linux and macOS both support ps aux; --sort is GNU/procps only
                if sys.platform == "darwin":
                    # macOS ps uses different sort syntax — sort in Python instead
                    cmd = "ps aux"
                else:
                    cmd = f"ps aux --sort=-{'%mem' if sort_by == 'memory' else '%cpu'}"

                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                lines = stdout.decode().splitlines()

                if sys.platform == "darwin" and len(lines) > 1:
                    # Sort by CPU (col 2) or MEM (col 3) on macOS
                    col = 3 if sort_by == "memory" else 2
                    try:
                        header = lines[0]
                        data = sorted(lines[1:], key=lambda ln: float(ln.split()[col]) if len(ln.split()) > col else 0, reverse=True)
                        lines = [header] + data
                    except Exception:
                        pass

                if filter:
                    header = lines[0] if lines else ""
                    filtered = [ln for ln in lines[1:] if filter.lower() in ln.lower()]
                    return header + "\n" + "\n".join(filtered[:20])

                return "\n".join(lines[:25])
        except Exception as e:
            return f"Error: {e}"

    registry.register(Tool(
        name="list_processes",
        description="List running processes (optionally filter by name, sort by cpu or memory)",
        parameters={"type": "object", "properties": {
            "filter": {"type": "string", "default": "", "description": "Filter by process name"},
            "sort_by": {"type": "string", "default": "cpu", "description": "Sort: cpu or memory"},
        }, "required": []},
        handler=list_processes, safe=True,
    ))

    async def kill_process(pid: int = 0, name: str = "") -> str:
        """Kill a process by PID or name (user's processes only)."""
        if sys.platform == "win32":
            if pid:
                proc = await asyncio.create_subprocess_shell(
                    f"taskkill /PID {pid} /F",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                out = stdout.decode() + stderr.decode()
                return out.strip() or f"Killed PID {pid}"
            elif name:
                proc = await asyncio.create_subprocess_shell(
                    f'taskkill /IM "{name}" /F',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                out = stdout.decode() + stderr.decode()
                return out.strip() or f"Killed processes matching '{name}'"
        else:
            # Linux and macOS are both POSIX — os.kill / pkill work on both
            if pid:
                try:
                    os.kill(pid, 15)  # SIGTERM
                    return f"Sent SIGTERM to PID {pid}"
                except ProcessLookupError:
                    return f"PID {pid} not found"
                except PermissionError:
                    return f"Permission denied for PID {pid} (not your process)"
            elif name:
                proc = await asyncio.create_subprocess_shell(
                    f"pkill -f '{name}' 2>&1",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                return stdout.decode() + stderr.decode() or f"Sent kill signal to processes matching '{name}'"
        return "Provide either pid or name"

    registry.register(Tool(
        name="kill_process",
        description="Kill a process by PID or name (your processes only, not system)",
        parameters={"type": "object", "properties": {
            "pid": {"type": "integer", "description": "Process ID"},
            "name": {"type": "string", "description": "Process name pattern"},
        }, "required": []},
        handler=kill_process, safe=False,
    ))

    # =================================================================
    # Application Launching
    # =================================================================

    async def open_app(target: str) -> str:
        """Open an application, URL, or file with the system default handler."""
        try:
            if sys.platform == "win32":
                # os.startfile is Windows-only; works for files, URLs, and app names
                try:
                    os.startfile(target)  # type: ignore[attr-defined]
                    return f"Opened: {target}"
                except Exception:
                    proc = await asyncio.create_subprocess_shell(
                        f'start "" "{target}"',
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                    return f"Opened: {target}"
            elif sys.platform == "darwin":
                proc = await asyncio.create_subprocess_exec(
                    "open", target,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                return f"Opened: {target}"
            else:
                # Linux
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "xdg-open", target,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()
                    return f"Opened: {target}"
                except FileNotFoundError:
                    proc = await asyncio.create_subprocess_shell(
                        f"{target} &",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    return f"Launched: {target}"
        except Exception as e:
            return f"Failed to open: {e}"

    registry.register(Tool(
        name="open",
        description="Open an application, URL, or file (e.g., 'firefox', 'https://google.com', 'report.pdf')",
        parameters={"type": "object", "properties": {
            "target": {"type": "string", "description": "App name, URL, or file path"},
        }, "required": ["target"]},
        handler=open_app, safe=True,
    ))

    async def open_terminal(command: str = "") -> str:
        """Open a new terminal window, optionally running a command."""
        if sys.platform == "win32":
            # Prefer Windows Terminal (wt), fall back to cmd
            if shutil.which("wt"):
                args = ["wt", "cmd", "/k", command] if command else ["wt"]
            else:
                args = ["cmd", "/k", command] if command else ["start", "cmd"]
            try:
                if args[0] == "start":
                    await asyncio.create_subprocess_shell(
                        "start cmd",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                else:
                    await asyncio.create_subprocess_exec(
                        *args,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                return "Opened terminal" + (f" running: {command}" if command else "")
            except Exception as e:
                return f"Failed to open terminal: {e}"

        elif sys.platform == "darwin":
            # Try iTerm2 first, then fall back to Terminal.app
            if shutil.which("osascript"):
                if command:
                    script = (
                        f'tell application "Terminal" to do script "{command}"'
                        if not shutil.which("iterm2")
                        else f'tell application "iTerm" to create window with default profile command "{command}"'
                    )
                else:
                    script = 'tell application "Terminal" to activate'
                try:
                    await asyncio.create_subprocess_exec(
                        "osascript", "-e", script,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    return "Opened Terminal" + (f" running: {command}" if command else "")
                except Exception:
                    pass
            # Fallback: open -a Terminal
            try:
                await asyncio.create_subprocess_exec(
                    "open", "-a", "Terminal",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                return "Opened Terminal.app"
            except Exception as e:
                return f"Failed to open terminal: {e}"

        else:
            # Linux — try common terminal emulators
            terminals = ["gnome-terminal", "xfce4-terminal", "konsole", "xterm"]
            for term in terminals:
                if shutil.which(term):
                    try:
                        if command:
                            await asyncio.create_subprocess_exec(
                                term, "--", "bash", "-c", f"{command}; exec bash",
                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                            )
                        else:
                            await asyncio.create_subprocess_exec(
                                term,
                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                            )
                        return f"Opened {term}" + (f" running: {command}" if command else "")
                    except Exception:
                        continue
            return "No terminal emulator found"

    registry.register(Tool(
        name="open_terminal",
        description="Open a new terminal window, optionally running a command in it",
        parameters={"type": "object", "properties": {
            "command": {"type": "string", "default": "", "description": "Command to run in the new terminal"},
        }, "required": []},
        handler=open_terminal, safe=True,
    ))

    # =================================================================
    # File Management
    # =================================================================

    async def find_files(name: str = "", extension: str = "", directory: str = "~",
                         min_size: str = "", max_age_days: int = 0) -> str:
        """Find files by name, extension, size, or age.

        Uses pathlib.Path.rglob() — works on Linux, macOS, and Windows
        without requiring the `find` command.
        """
        import datetime
        dir_path = Path(os.path.expanduser(directory))

        # Build glob pattern
        if extension:
            ext = extension if extension.startswith(".") else f".{extension}"
        elif name:
            pass
        else:
            pass

        # Parse min_size to bytes (e.g. "10M" -> 10*1024*1024)
        min_bytes = 0
        if min_size:
            try:
                units = {"k": 1024, "m": 1024**2, "g": 1024**3}
                if min_size[-1].lower() in units:
                    min_bytes = int(min_size[:-1]) * units[min_size[-1].lower()]
                else:
                    min_bytes = int(min_size)
            except ValueError:
                pass

        cutoff = None
        if max_age_days > 0:
            cutoff = datetime.datetime.now().timestamp() - max_age_days * 86400

        try:
            results = []
            # rglob with depth limit via manual traversal
            def _walk(root: Path, depth: int) -> None:
                if depth > 5:
                    return
                try:
                    for item in root.iterdir():
                        if item.is_symlink():
                            continue
                        if item.is_file():
                            # Apply filters
                            if name and name.lower() not in item.name.lower():
                                continue
                            if extension and not item.name.lower().endswith(ext.lower()):
                                continue
                            if min_bytes and item.stat().st_size < min_bytes:
                                continue
                            if cutoff and item.stat().st_mtime < cutoff:
                                continue
                            results.append(str(item))
                            if len(results) >= 30:
                                return
                        elif item.is_dir():
                            _walk(item, depth + 1)
                            if len(results) >= 30:
                                return
                except PermissionError:
                    pass

            await asyncio.get_event_loop().run_in_executor(None, _walk, dir_path, 0)
            return "\n".join(results) if results else "No files found"
        except Exception as e:
            return f"Error: {e}"

    registry.register(Tool(
        name="find_files",
        description="Find files by name, extension, size, or modification date",
        parameters={"type": "object", "properties": {
            "name": {"type": "string", "description": "Filename pattern to match"},
            "extension": {"type": "string", "description": "File extension (e.g., py, pdf, jpg)"},
            "directory": {"type": "string", "default": "~", "description": "Directory to search"},
            "min_size": {"type": "string", "description": "Minimum size (e.g., 10M, 1G)"},
            "max_age_days": {"type": "integer", "default": 0, "description": "Modified within N days"},
        }, "required": []},
        handler=find_files, safe=True,
    ))

    async def disk_usage(path: str = "~") -> str:
        """Show disk usage for a directory.

        shutil.disk_usage() is cross-platform. Top-directories are computed
        via os.scandir() instead of `du` so this works on Windows too.
        """
        expanded = os.path.expanduser(path)

        def _dir_size_shallow(p: str) -> list[tuple[int, str]]:
            """Return (size_bytes, name) for immediate children of p."""
            sizes = []
            try:
                with os.scandir(p) as it:
                    for entry in it:
                        try:
                            if entry.is_file(follow_symlinks=False):
                                sizes.append((entry.stat().st_size, entry.path))
                            elif entry.is_dir(follow_symlinks=False):
                                # Fast shallow estimate: sum direct children only
                                sub_size = 0
                                try:
                                    with os.scandir(entry.path) as sub:
                                        for sub_entry in sub:
                                            try:
                                                if sub_entry.is_file(follow_symlinks=False):
                                                    sub_size += sub_entry.stat().st_size
                                            except OSError:
                                                pass
                                except PermissionError:
                                    pass
                                sizes.append((sub_size, entry.path))
                        except OSError:
                            pass
            except PermissionError:
                pass
            return sorted(sizes, reverse=True)[:10]

        try:
            usage = shutil.disk_usage(expanded)
            total_gb = usage.total / (1024**3)
            used_gb  = usage.used  / (1024**3)
            free_gb  = usage.free  / (1024**3)
            pct = (usage.used / usage.total) * 100

            top = await asyncio.get_event_loop().run_in_executor(
                None, _dir_size_shallow, expanded
            )

            result  = f"Disk usage for {path}:\n"
            result += f"  Total: {total_gb:.1f} GB\n"
            result += f"  Used:  {used_gb:.1f} GB ({pct:.0f}%)\n"
            result += f"  Free:  {free_gb:.1f} GB\n\n"
            result += "Top entries by size:\n"
            for size, name in top:
                size_str = (f"{size/1024**3:.1f}G" if size >= 1024**3
                            else f"{size/1024**2:.0f}M" if size >= 1024**2
                            else f"{size/1024:.0f}K")
                result += f"  {size_str:>6}  {name}\n"
            return result
        except Exception as e:
            return f"Error: {e}"

    registry.register(Tool(
        name="disk_usage",
        description="Show disk usage for a directory with top subdirectories by size",
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "default": "~", "description": "Directory to analyze"},
        }, "required": []},
        handler=disk_usage, safe=True,
    ))

    async def move_file(source: str, destination: str) -> str:
        """Move or rename a file/directory."""
        src = os.path.expanduser(source)
        dst = os.path.expanduser(destination)
        try:
            shutil.move(src, dst)
            return f"Moved: {source} → {destination}"
        except Exception as e:
            return f"Error: {e}"

    registry.register(Tool(
        name="move_file",
        description="Move or rename a file or directory",
        parameters={"type": "object", "properties": {
            "source": {"type": "string", "description": "Source path"},
            "destination": {"type": "string", "description": "Destination path"},
        }, "required": ["source", "destination"]},
        handler=move_file, safe=False,
    ))

    async def delete_file(path: str, recursive: bool = False) -> str:
        """Delete a file or directory."""
        expanded = os.path.expanduser(path)
        try:
            if os.path.isdir(expanded):
                if recursive:
                    shutil.rmtree(expanded)
                    return f"Deleted directory: {path}"
                else:
                    return f"{path} is a directory. Use recursive=true to delete."
            else:
                os.remove(expanded)
                return f"Deleted: {path}"
        except Exception as e:
            return f"Error: {e}"

    registry.register(Tool(
        name="delete_file",
        description="Delete a file or directory (use recursive=true for directories)",
        parameters={"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to delete"},
            "recursive": {"type": "boolean", "default": False, "description": "Delete directories recursively"},
        }, "required": ["path"]},
        handler=delete_file, safe=False,
    ))

    # =================================================================
    # System Information
    # =================================================================

    async def system_info() -> str:
        """Get system information: CPU, RAM, disk, network, uptime."""
        import platform as _platform
        info = []

        async def _run(cmd: str) -> str:
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                return stdout.decode(errors="replace").strip()
            except Exception:
                return ""

        try:
            # Hostname — all platforms
            hostname = _platform.node()
            machine  = _platform.machine()
            info.append(f"System: {hostname} ({_platform.system()} {_platform.release()}, {machine})")

            if sys.platform == "win32":
                # CPU via wmic
                cpu_out = await _run("wmic cpu get Name /value")
                cpu_name = next((ln.split("=",1)[1] for ln in cpu_out.splitlines() if "Name=" in ln), "")
                cpu_count = _platform.os.cpu_count() or 0
                info.append(f"CPU: {cpu_name.strip()} ({cpu_count} logical cores)")

                # RAM via wmic
                mem_out = await _run("wmic OS get TotalVisibleMemorySize,FreePhysicalMemory /value")
                total = free = 0
                for ln in mem_out.splitlines():
                    if "TotalVisibleMemorySize=" in ln:
                        total = int(ln.split("=",1)[1].strip() or 0)
                    elif "FreePhysicalMemory=" in ln:
                        free  = int(ln.split("=",1)[1].strip() or 0)
                used = total - free
                info.append(f"RAM: {total//1024}MB total, {used//1024}MB used, {free//1024}MB free")

                # Uptime via systeminfo (first line)
                sys_out = await _run("systeminfo | findstr /C:\"System Boot Time\"")
                if sys_out:
                    info.append(f"Boot: {sys_out.strip()}")

                # Network
                net_out = await _run("ipconfig")
                if net_out:
                    # Just keep the adapter summary lines
                    net_lines = [ln for ln in net_out.splitlines()
                                 if "adapter" in ln.lower() or "IPv4" in ln or "IPv6" in ln]
                    info.append("Network:\n" + "\n".join(net_lines[:10]))

            elif sys.platform == "darwin":
                # CPU via sysctl
                cpu_name  = await _run("sysctl -n machdep.cpu.brand_string")
                cpu_count = await _run("sysctl -n hw.logicalcpu")
                info.append(f"CPU: {cpu_name} ({cpu_count} cores)")

                # RAM via sysctl
                mem_bytes = await _run("sysctl -n hw.memsize")
                try:
                    mem_gb = int(mem_bytes) / (1024**3)
                    info.append(f"RAM: {mem_gb:.0f} GB total")
                except ValueError:
                    pass

                # Uptime
                uptime = await _run("uptime")
                info.append(f"Uptime: {uptime}")

                # Disk
                disk = await _run("df -h ~")
                info.append(f"Disk:\n{disk}")

                # Network
                net = await _run("ifconfig | grep -E '(^[a-z]|inet )'")
                info.append(f"Network:\n{net}")

            else:
                # Linux (original behaviour)
                uptime = await _run("hostname; uptime -p 2>/dev/null || uptime")
                info.append(f"System: {uptime}")

                cpu_out = await _run("nproc && cat /proc/cpuinfo | grep 'model name' | head -1 | cut -d: -f2")
                parts = cpu_out.splitlines()
                if len(parts) >= 2:
                    info.append(f"CPU: {parts[1].strip()} ({parts[0]} cores)")

                ram = await _run("free -h | grep Mem")
                info.append(f"RAM: {ram}")

                disk = await _run("df -h ~ | tail -1")
                info.append(f"Disk: {disk}")

                net = await _run("ip -br addr 2>/dev/null | head -5 || ifconfig 2>/dev/null | head -10")
                info.append(f"Network:\n{net}")

        except Exception as e:
            info.append(f"Error: {e}")

        return "\n\n".join(info)

    registry.register(Tool(
        name="system_info",
        description="Get system information: CPU, RAM, disk, network, uptime",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=system_info, safe=True,
    ))

    # =================================================================
    # Package Management (user-level, no root)
    # =================================================================

    async def pip_install(package: str) -> str:
        """Install a Python package (user-level, no root needed)."""
        proc = await asyncio.create_subprocess_exec(
            "pip", "install", "--user", package,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            return f"Installed: {package}\n{stdout.decode().splitlines()[-1] if stdout else ''}"
        return f"Failed: {stderr.decode()[:500]}"

    registry.register(Tool(
        name="pip_install",
        description="Install a Python package (user-level, no root needed)",
        parameters={"type": "object", "properties": {
            "package": {"type": "string", "description": "Package name (e.g., requests, pandas)"},
        }, "required": ["package"]},
        handler=pip_install, safe=False,
    ))

    async def pip_list() -> str:
        """List installed Python packages."""
        proc = await asyncio.create_subprocess_exec(
            "pip", "list", "--format=columns",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        lines = stdout.decode().splitlines()
        return "\n".join(lines[:30]) + (f"\n... ({len(lines)} total)" if len(lines) > 30 else "")

    registry.register(Tool(
        name="pip_list",
        description="List installed Python packages",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=pip_list, safe=True,
    ))

    # =================================================================
    # Clipboard
    # =================================================================

    async def get_clipboard() -> str:
        """Read current clipboard contents."""
        if sys.platform == "win32":
            # PowerShell Get-Clipboard
            try:
                proc = await asyncio.create_subprocess_shell(
                    'powershell -NoProfile -Command "Get-Clipboard"',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode == 0:
                    content = stdout.decode(errors="replace")
                    if len(content) > 5000:
                        return content[:5000] + f"\n... (truncated, {len(content)} chars)"
                    return content
            except Exception:
                pass
            return "Clipboard not available"

        # macOS and Linux — try in order of preference
        candidates: list[list[str]]
        if sys.platform == "darwin":
            candidates = [["pbpaste"]]
        else:
            candidates = [
                ["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"],
                ["pbpaste"],
            ]

        for cmd in candidates:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
                if proc.returncode == 0:
                    content = stdout.decode()
                    if len(content) > 5000:
                        return content[:5000] + f"\n... (truncated, {len(content)} chars)"
                    return content
            except (TimeoutError, FileNotFoundError):
                continue
        return "Clipboard tools not available (install xclip: apt install xclip)"

    registry.register(Tool(
        name="get_clipboard",
        description="Read the current clipboard contents",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=get_clipboard, safe=True,
    ))

    async def set_clipboard(content: str) -> str:
        """Write text to the clipboard."""
        if sys.platform == "win32":
            # clip.exe reads from stdin; escape the content via PowerShell for safety
            try:
                proc = await asyncio.create_subprocess_shell(
                    'powershell -NoProfile -Command "param($t) Set-Clipboard $t" -t -',
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.communicate(input=content.encode("utf-8")), timeout=5)
                if proc.returncode == 0:
                    return f"Copied {len(content)} chars to clipboard"
            except Exception:
                pass
            # Fallback: clip.exe
            try:
                proc = await asyncio.create_subprocess_exec(
                    "clip",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.communicate(input=content.encode("utf-16-le")), timeout=5)
                if proc.returncode == 0:
                    return f"Copied {len(content)} chars to clipboard"
            except Exception:
                pass
            return "Clipboard not available"

        # macOS and Linux
        if sys.platform == "darwin":
            candidates: list[list[str]] = [["pbcopy"]]
        else:
            candidates = [
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
                ["pbcopy"],
            ]

        for cmd in candidates:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.communicate(input=content.encode()), timeout=3)
                if proc.returncode == 0:
                    return f"Copied {len(content)} chars to clipboard"
            except (TimeoutError, FileNotFoundError):
                continue
        return "Clipboard tools not available"

    registry.register(Tool(
        name="set_clipboard",
        description="Copy text to the clipboard",
        parameters={"type": "object", "properties": {
            "content": {"type": "string", "description": "Text to copy"},
        }, "required": ["content"]},
        handler=set_clipboard, safe=True,
    ))

    # =================================================================
    # Environment
    # =================================================================

    async def get_env(name: str = "") -> str:
        """Get environment variable(s). Without name, shows all."""
        if name:
            val = os.environ.get(name, "")
            return f"{name}={val}" if val else f"{name} is not set"
        # Show all (filtered for safety — no secrets)
        safe_vars = {k: v for k, v in sorted(os.environ.items())
                     if "KEY" not in k.upper() and "SECRET" not in k.upper()
                     and "TOKEN" not in k.upper() and "PASSWORD" not in k.upper()}
        return "\n".join(f"{k}={v}" for k, v in list(safe_vars.items())[:40])

    registry.register(Tool(
        name="get_env",
        description="Get environment variables (secrets are hidden)",
        parameters={"type": "object", "properties": {
            "name": {"type": "string", "default": "", "description": "Variable name (empty = show all)"},
        }, "required": []},
        handler=get_env, safe=True,
    ))

    async def set_env(name: str, value: str) -> str:
        """Set an environment variable for the current session."""
        os.environ[name] = value
        return f"Set {name}={value} (current session only)"

    registry.register(Tool(
        name="set_env",
        description="Set an environment variable (current session only, not persistent)",
        parameters={"type": "object", "properties": {
            "name": {"type": "string", "description": "Variable name"},
            "value": {"type": "string", "description": "Variable value"},
        }, "required": ["name", "value"]},
        handler=set_env, safe=False,
    ))

    # =================================================================
    # Desktop Notifications
    # =================================================================

    async def notify_user(title: str, message: str) -> str:
        """Send a desktop notification to the user.

        - Linux: notify-send
        - macOS: osascript
        - Windows: PowerShell toast notification
        """
        if sys.platform == "win32":
            # PowerShell toast via BurntToast or simple balloon via WScript
            script = (
                f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                f"ContentType = WindowsRuntime] | Out-Null; "
                f"$t = [Windows.UI.Notifications.ToastNotificationManager]"
                f"::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{title}')) | Out-Null; "
                f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode('{message}')) | Out-Null; "
                f"$toast = [Windows.UI.Notifications.ToastNotification]::new($t); "
                f"[Windows.UI.Notifications.ToastNotificationManager]"
                f"::CreateToastNotifier('NVHive').Show($toast)"
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    "powershell", "-NoProfile", "-Command", script,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=5)
                if proc.returncode == 0:
                    return f"Notification sent: {title}"
            except Exception:
                pass
            # Fallback: msg command (works in most Windows editions)
            try:
                proc = await asyncio.create_subprocess_shell(
                    f'msg "%username%" "{title}: {message}"',
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=5)
                return f"Notification sent: {title}"
            except Exception:
                return f"Notification (no GUI): [{title}] {message}"

        elif sys.platform == "darwin":
            try:
                script = f'display notification "{message}" with title "{title}"'
                proc = await asyncio.create_subprocess_exec(
                    "osascript", "-e", script,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                return f"Notification sent: {title}"
            except FileNotFoundError:
                return f"Desktop notifications not available. Message: [{title}] {message}"

        else:
            # Linux
            try:
                proc = await asyncio.create_subprocess_exec(
                    "notify-send", title, message,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                return f"Notification sent: {title}"
            except FileNotFoundError:
                return f"Desktop notifications not available. Message: [{title}] {message}"

    registry.register(Tool(
        name="notify",
        description="Send a desktop notification to the user",
        parameters={"type": "object", "properties": {
            "title": {"type": "string", "description": "Notification title"},
            "message": {"type": "string", "description": "Notification body"},
        }, "required": ["title", "message"]},
        handler=notify_user, safe=True,
    ))

    # =================================================================
    # Download
    # =================================================================

    async def download_file(url: str, destination: str = "~/Downloads/") -> str:
        """Download a file from a URL."""
        from urllib.parse import urlparse

        import httpx

        dest = os.path.expanduser(destination)
        if os.path.isdir(dest):
            filename = os.path.basename(urlparse(url).path) or "download"
            dest = os.path.join(dest, filename)

        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                resp = await client.get(url, timeout=60)
                resp.raise_for_status()
                os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(resp.content)
                size_kb = len(resp.content) / 1024
                return f"Downloaded: {dest} ({size_kb:.0f} KB)"
        except Exception as e:
            return f"Download failed: {e}"

    registry.register(Tool(
        name="download",
        description="Download a file from a URL to the local filesystem",
        parameters={"type": "object", "properties": {
            "url": {"type": "string", "description": "URL to download"},
            "destination": {"type": "string", "default": "~/Downloads/", "description": "Save location"},
        }, "required": ["url"]},
        handler=download_file, safe=False,
    ))
