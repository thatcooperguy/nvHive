"""NVHive Notifications — desktop alerts when tasks complete.

Uses:
- Linux: notify-send (libnotify)
- macOS: osascript
- Fallback: terminal bell

No additional dependencies needed.
"""

import asyncio
import sys


async def notify(title: str, message: str, urgency: str = "normal") -> bool:
    """Send a desktop notification. Returns True if delivered."""

    if sys.platform == "linux":
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", "-u", urgency, title, message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0
        except FileNotFoundError:
            pass

    elif sys.platform == "darwin":
        try:
            script = f'display notification "{message}" with title "{title}"'
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0
        except FileNotFoundError:
            pass

    # Fallback: terminal bell
    print(f"\a[{title}] {message}")
    return False

async def notify_task_complete(task_name: str, result_preview: str, cost: str = ""):
    """Notify when a long-running task completes."""
    msg = result_preview[:100]
    if cost:
        msg += f" (${cost})"
    await notify(f"NVHive: {task_name}", msg)
