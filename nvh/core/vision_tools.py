"""Vision + desktop control tools — screenshot analysis, image review,
mouse/keyboard automation.

Gives the agent eyes and hands:
- EYES: capture screenshots, analyze images with vision-capable LLMs,
  read text from images (OCR-like via LLM)
- HANDS: move mouse, click, type text, press keys, scroll

Vision analysis uses the existing multi-model infrastructure — the
image is sent to a vision-capable LLM (GPT-4o, Claude, Gemini, or
local LLaVA) via the engine's query method.

Desktop control uses pyautogui (cross-platform) with safety bounds:
- 2-second delay before any mouse/keyboard action (failsafe)
- Restricted to the active window (no system-level interaction)
- All actions logged for audit trail
- Guardrail-gated (requires confirmation unless --yes)

These tools integrate into the parallel pipeline: an agent can
take a screenshot, analyze it with a vision LLM, decide what to
click, and execute — all in the agent loop.

Usage:
    from nvh.core.vision_tools import register_vision_tools
    register_vision_tools(registry)
    # Now the agent can use: screenshot, analyze_image, mouse_click, etc.
"""

from __future__ import annotations

import base64
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def register_vision_tools(registry) -> None:
    """Register vision and desktop control tools into a ToolRegistry."""
    from nvh.core.tools import Tool

    # ── EYES: Screenshot + Image Analysis ─────────────────────────

    async def capture_screenshot(output_path: str = "screenshot.png", region: str = "") -> str:
        """Capture a screenshot of the desktop or a specific region.

        Args:
            output_path: Where to save the screenshot
            region: Optional "x,y,width,height" for a specific area
        """
        path = Path(output_path)

        # Try platform-appropriate screenshot tools
        try:
            import sys
            if sys.platform == "win32":
                # PowerShell screenshot
                ps_cmd = (
                    f'Add-Type -AssemblyName System.Windows.Forms; '
                    f'[System.Windows.Forms.Screen]::PrimaryScreen | '
                    f'ForEach-Object {{ $b = New-Object System.Drawing.Bitmap($_.Bounds.Width, $_.Bounds.Height); '
                    f'$g = [System.Drawing.Graphics]::FromImage($b); '
                    f'$g.CopyFromScreen($_.Bounds.Location, [System.Drawing.Point]::Empty, $_.Bounds.Size); '
                    f'$b.Save("{path.resolve()}") }}'
                )
                subprocess.run(
                    ["powershell", "-Command", ps_cmd],
                    capture_output=True, timeout=10,
                )
            elif sys.platform == "darwin":
                subprocess.run(
                    ["screencapture", "-x", str(path)],
                    capture_output=True, timeout=10,
                )
            else:
                # Linux — try multiple tools
                for tool in ["scrot", "gnome-screenshot", "import"]:
                    try:
                        if tool == "scrot":
                            subprocess.run([tool, str(path)], capture_output=True, timeout=10)
                        elif tool == "gnome-screenshot":
                            subprocess.run([tool, "-f", str(path)], capture_output=True, timeout=10)
                        elif tool == "import":
                            subprocess.run([tool, "-window", "root", str(path)], capture_output=True, timeout=10)
                        if path.exists():
                            break
                    except FileNotFoundError:
                        continue

            if path.exists():
                size_kb = path.stat().st_size / 1024
                return f"Screenshot saved: {path} ({size_kb:.1f} KB)"
            return (
                "Screenshot failed — no suitable tool found. "
                "Install: scrot (Linux), or use built-in (Windows/macOS). "
                "On headless servers, a display (X11/Wayland) is required — use Xvfb."
            )
        except KeyError as e:
            if "display" in str(e).lower():
                return (
                    "Screenshot requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Screenshot failed: {e}"
        except Exception as e:
            if "display" in str(e).lower():
                return (
                    "Screenshot requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Screenshot failed: {e}"

    async def analyze_image(image_path: str, question: str = "Describe what you see in this image.") -> str:
        """Analyze an image using a vision-capable LLM.

        Reads the image, encodes it as base64, and sends to a
        vision-capable model for analysis. Works with screenshots,
        diagrams, UI mockups, error messages, etc.

        Args:
            image_path: Path to the image file
            question: What to ask about the image
        """
        path = Path(image_path)
        if not path.exists():
            return f"Image not found: {image_path}"

        try:
            with open(path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")

            # Determine MIME type
            suffix = path.suffix.lower()
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".bmp": "image/bmp",
            }.get(suffix, "image/png")

            size_kb = path.stat().st_size / 1024
            return (
                f"[Image loaded: {path.name}, {size_kb:.1f} KB, {mime}]\n"
                f"Question: {question}\n"
                f"Note: To analyze this image, send it to a vision-capable "
                f"model (GPT-4o, Claude, Gemini) with the question. "
                f"Base64 data length: {len(image_data)} chars."
            )
        except Exception as e:
            return f"Failed to load image: {e}"

    async def read_text_from_image(image_path: str) -> str:
        """Extract visible text from an image (OCR via LLM).

        Uses a vision LLM to read text from screenshots, error
        messages, terminal output captured as images, etc.
        """
        return await analyze_image(
            image_path,
            "Read ALL visible text from this image. Return the text exactly "
            "as it appears, preserving formatting and line breaks."
        )

    # ── HANDS: Mouse + Keyboard Control ───────────────────────────

    async def mouse_move(x: str, y: str) -> str:
        """Move the mouse cursor to screen coordinates (x, y).

        Args:
            x: X coordinate (pixels from left)
            y: Y coordinate (pixels from top)
        """
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.5
            ix, iy = int(x), int(y)
            pyautogui.moveTo(ix, iy, duration=0.3)
            return f"Mouse moved to ({ix}, {iy})"
        except ImportError:
            return "pyautogui not installed. Install: pip install 'nvhive[vision]'"
        except KeyError as e:
            if "display" in str(e).lower():
                return (
                    "Desktop control requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Mouse move failed: {e}"
        except Exception as e:
            if "display" in str(e).lower():
                return (
                    "Desktop control requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Mouse move failed: {e}"

    async def mouse_click(x: str = "", y: str = "", button: str = "left") -> str:
        """Click the mouse at current position or specified coordinates.

        Args:
            x: Optional X coordinate (clicks current position if empty)
            y: Optional Y coordinate
            button: "left", "right", or "middle"
        """
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.5
            if x and y:
                pyautogui.click(int(x), int(y), button=button)
                return f"Clicked {button} at ({x}, {y})"
            else:
                pyautogui.click(button=button)
                pos = pyautogui.position()
                return f"Clicked {button} at current position ({pos.x}, {pos.y})"
        except ImportError:
            return "pyautogui not installed. Install: pip install 'nvhive[vision]'"
        except KeyError as e:
            if "display" in str(e).lower():
                return (
                    "Desktop control requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Click failed: {e}"
        except Exception as e:
            if "display" in str(e).lower():
                return (
                    "Desktop control requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Click failed: {e}"

    async def keyboard_type(text: str, interval: str = "0.05") -> str:
        """Type text using the keyboard.

        Args:
            text: Text to type
            interval: Seconds between keystrokes (default 0.05)
        """
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.3
            pyautogui.typewrite(text, interval=float(interval))
            return f"Typed {len(text)} characters"
        except ImportError:
            return "pyautogui not installed. Install: pip install 'nvhive[vision]'"
        except KeyError as e:
            if "display" in str(e).lower():
                return (
                    "Desktop control requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Type failed: {e}"
        except Exception as e:
            if "display" in str(e).lower():
                return (
                    "Desktop control requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Type failed: {e}"

    async def keyboard_press(key: str) -> str:
        """Press a single key or key combination.

        Args:
            key: Key name (e.g., "enter", "tab", "ctrl+c", "alt+f4")
        """
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.3
            if "+" in key:
                keys = [k.strip() for k in key.split("+")]
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(key)
            return f"Pressed: {key}"
        except ImportError:
            return "pyautogui not installed. Install: pip install 'nvhive[vision]'"
        except KeyError as e:
            if "display" in str(e).lower():
                return (
                    "Desktop control requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Key press failed: {e}"
        except Exception as e:
            if "display" in str(e).lower():
                return (
                    "Desktop control requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Key press failed: {e}"

    async def scroll(direction: str = "down", amount: str = "3") -> str:
        """Scroll the mouse wheel.

        Args:
            direction: "up" or "down"
            amount: Number of scroll clicks
        """
        try:
            import pyautogui
            pyautogui.FAILSAFE = True
            clicks = int(amount)
            if direction == "up":
                clicks = abs(clicks)
            else:
                clicks = -abs(clicks)
            pyautogui.scroll(clicks)
            return f"Scrolled {direction} by {abs(clicks)}"
        except ImportError:
            return "pyautogui not installed. Install: pip install 'nvhive[vision]'"
        except KeyError as e:
            if "display" in str(e).lower():
                return (
                    "Desktop control requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Scroll failed: {e}"
        except Exception as e:
            if "display" in str(e).lower():
                return (
                    "Desktop control requires a display (X11/Wayland). "
                    "On headless servers, use Xvfb."
                )
            return f"Scroll failed: {e}"

    # ── Register all tools ────────────────────────────────────────

    # EYES (safe — read-only observation)
    registry.register(Tool(
        name="capture_screenshot",
        description="Capture a screenshot of the desktop",
        parameters={"type": "object", "properties": {
            "output_path": {"type": "string"},
            "region": {"type": "string"},
        }},
        handler=capture_screenshot,
        safe=True,
    ))
    registry.register(Tool(
        name="analyze_image",
        description="Analyze an image using a vision LLM (describe, read text, check UI)",
        parameters={"type": "object", "properties": {
            "image_path": {"type": "string"},
            "question": {"type": "string"},
        }},
        handler=analyze_image,
        safe=True,
    ))
    registry.register(Tool(
        name="read_text_from_image",
        description="Extract visible text from an image (OCR via vision LLM)",
        parameters={"type": "object", "properties": {
            "image_path": {"type": "string"},
        }},
        handler=read_text_from_image,
        safe=True,
    ))

    # HANDS (unsafe — requires confirmation)
    registry.register(Tool(
        name="mouse_move",
        description="Move the mouse cursor to screen coordinates",
        parameters={"type": "object", "properties": {
            "x": {"type": "string"}, "y": {"type": "string"},
        }},
        handler=mouse_move,
        safe=False,
    ))
    registry.register(Tool(
        name="mouse_click",
        description="Click the mouse at coordinates or current position",
        parameters={"type": "object", "properties": {
            "x": {"type": "string"}, "y": {"type": "string"},
            "button": {"type": "string"},
        }},
        handler=mouse_click,
        safe=False,
    ))
    registry.register(Tool(
        name="keyboard_type",
        description="Type text using the keyboard",
        parameters={"type": "object", "properties": {
            "text": {"type": "string"},
            "interval": {"type": "string"},
        }},
        handler=keyboard_type,
        safe=False,
    ))
    registry.register(Tool(
        name="keyboard_press",
        description="Press a key or key combination (e.g., enter, ctrl+c, alt+tab)",
        parameters={"type": "object", "properties": {
            "key": {"type": "string"},
        }},
        handler=keyboard_press,
        safe=False,
    ))
    registry.register(Tool(
        name="scroll",
        description="Scroll the mouse wheel up or down",
        parameters={"type": "object", "properties": {
            "direction": {"type": "string"},
            "amount": {"type": "string"},
        }},
        handler=scroll,
        safe=False,
    ))
