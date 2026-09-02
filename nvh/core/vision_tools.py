"""Vision + desktop control tools — screenshot analysis, image review,
mouse/keyboard automation.

Gives the agent eyes and hands:
- EYES: capture screenshots, analyze images with vision-capable LLMs,
  read text from images (OCR-like via LLM)
- HANDS: move mouse, click, type text, press keys, scroll

Vision analysis tries local Ollama vision models first (llama3.2-vision,
minicpm-v, llava), then falls back to cloud APIs (GPT-4o, Gemini, Claude).

Desktop control uses pyautogui (cross-platform) with safety bounds:
- 0.5-second pause before mouse/keyboard actions
- All actions logged for audit trail
- Guardrail-gated (requires confirmation unless --yes)
"""

from __future__ import annotations

import base64
import logging
import time as _time
from pathlib import Path

from nvh.utils.ollama import ollama_base_url

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vision model cache (avoids querying /api/tags on every analyze_image call)
# ---------------------------------------------------------------------------
_vision_model_cache: tuple[float, str | None] = (0.0, None)
_VISION_CACHE_TTL = 60.0  # seconds


def _ensure_display() -> bool:
    """Ensure DISPLAY is set on Linux for pyautogui/screenshot tools.

    Auto-detects DISPLAY from X11 sockets if not already set.
    Returns True if a display is available.
    """
    import os
    import sys

    if sys.platform != "linux":
        return True
    if os.environ.get("DISPLAY"):
        return True

    # Check X11 sockets
    x11_dir = Path("/tmp/.X11-unix")
    if x11_dir.exists():
        for sock in sorted(x11_dir.iterdir()):
            if sock.name.startswith("X"):
                display = f":{sock.name[1:]}"
                os.environ["DISPLAY"] = display
                logger.info("Auto-detected DISPLAY=%s", display)
                # Also set XAUTHORITY if present
                xauth = Path.home() / ".Xauthority"
                if xauth.exists() and not os.environ.get("XAUTHORITY"):
                    os.environ["XAUTHORITY"] = str(xauth)
                return True
    return False


def _detect_ollama_vision_model() -> str | None:
    """Check if Ollama has a vision-capable model installed (cached)."""
    global _vision_model_cache

    now = _time.monotonic()
    if now - _vision_model_cache[0] < _VISION_CACHE_TTL:
        return _vision_model_cache[1]

    result = None
    try:
        import httpx
        resp = httpx.get(f"{ollama_base_url()}/api/tags", timeout=3)
        if resp.status_code == 200:
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            # Known vision-capable models, in preference order
            # llama3.2-vision first — best spatial/coordinate grounding
            vision_models = [
                "llama3.2-vision", "minicpm-v", "llava", "bakllava",
                "llava-llama3", "llava-phi3", "moondream",
            ]
            for vm in vision_models:
                for installed in models:
                    if vm in installed:
                        result = installed
                        break
                if result:
                    break
    except Exception:
        pass

    _vision_model_cache = (now, result)
    return result


async def _analyze_with_ollama(image_data: str, question: str, model: str) -> str | None:
    """Call Ollama's native vision API directly."""
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ollama_base_url()}/api/chat",
                json={
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": question,
                        "images": [image_data],
                    }],
                    "stream": False,
                },
                timeout=60,
            )
            if resp.status_code == 200:
                return resp.json().get("message", {}).get("content", "")
    except Exception as exc:
        logger.debug("Ollama vision failed: %s", exc)
    return None


async def _analyze_with_cloud(image_data: str, mime: str, question: str) -> str | None:
    """Fall back to cloud vision LLM via litellm (GPT-5.6, Gemini, Claude)."""
    try:
        import litellm

        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{image_data}",
                }},
            ],
        }]

        # Try providers in order: GPT-5.6 Terra (best spatial accuracy), Gemini, Claude
        import os
        models_to_try = []
        if os.environ.get("OPENAI_API_KEY"):
            models_to_try.append("gpt-5.6-terra")
        if os.environ.get("GOOGLE_API_KEY"):
            models_to_try.append("gemini/gemini-3.7-flash")
        if os.environ.get("ANTHROPIC_API_KEY"):
            models_to_try.append("claude-sonnet-5")

        for model in models_to_try:
            try:
                resp = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    max_tokens=1024,
                    timeout=30,
                )
                content = resp.choices[0].message.content
                if content:
                    return content
            except Exception as exc:
                logger.debug("Cloud vision (%s) failed: %s", model, exc)
                continue
    except Exception as exc:
        logger.debug("Cloud vision failed: %s", exc)
    return None


def _desktop_action(func_name: str):
    """Decorator for desktop control tools — handles common errors."""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            try:
                import pyautogui
                pyautogui.FAILSAFE = True
                pyautogui.PAUSE = 0.5
                return await func(pyautogui, *args, **kwargs)
            except ImportError:
                return (
                    "pyautogui not installed. Install: pip install 'nvhive[vision]'\n"
                    "Also needed: pip install python-xlib Pillow"
                )
            except Exception as e:
                if "display" in str(e).lower() or "DISPLAY" in str(e):
                    return (
                        "Desktop control requires a display (X11/Wayland). "
                        "On headless servers, use Xvfb."
                    )
                return f"{func_name} failed: {e}"
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper
    return decorator


def register_vision_tools(registry) -> None:
    """Register vision and desktop control tools into a ToolRegistry."""
    from nvh.core.tools import Tool

    # Auto-detect display for Linux desktop environments
    _ensure_display()

    # ── EYES: Screenshot + Image Analysis ─────────────────────────

    async def capture_screenshot(output_path: str = "screenshot.png", region: str = "") -> str:
        """Capture a screenshot of the desktop or a specific region.

        Args:
            output_path: Where to save the screenshot
            region: Optional "x,y,width,height" for a specific area
        """
        path = Path(output_path)

        # Parse region if provided
        region_tuple = None
        if region:
            try:
                parts = [int(p.strip()) for p in region.split(",")]
                if len(parts) == 4:
                    region_tuple = tuple(parts)
            except ValueError:
                pass

        # Primary method: pyautogui.screenshot() — no external binaries needed
        try:
            import pyautogui
            img = pyautogui.screenshot(region=region_tuple)
            img.save(str(path))
            if path.exists() and path.stat().st_size > 0:
                size_kb = path.stat().st_size / 1024
                return f"Screenshot saved: {path} ({size_kb:.1f} KB)"
        except Exception as e:
            logger.debug("pyautogui screenshot failed: %s", e)

        # Fallback: platform-specific tools
        try:
            import subprocess
            import sys
            if sys.platform == "win32":
                ps_cmd = (
                    f'Add-Type -AssemblyName System.Windows.Forms; '
                    f'[System.Windows.Forms.Screen]::PrimaryScreen | '
                    f'ForEach-Object {{ $b = New-Object System.Drawing.Bitmap($_.Bounds.Width, $_.Bounds.Height); '
                    f'$g = [System.Drawing.Graphics]::FromImage($b); '
                    f'$g.CopyFromScreen($_.Bounds.Location, [System.Drawing.Point]::Empty, $_.Bounds.Size); '
                    f'$b.Save("{path.resolve()}") }}'
                )
                subprocess.run(["powershell", "-Command", ps_cmd], capture_output=True, timeout=10)
            elif sys.platform == "darwin":
                subprocess.run(["screencapture", "-x", str(path)], capture_output=True, timeout=10)
            else:
                # Linux — try KDE spectacle, then scrot, gnome-screenshot, import
                for tool_cmd in [
                    ["spectacle", "-b", "-n", "-o", str(path)],
                    ["scrot", str(path)],
                    ["gnome-screenshot", "-f", str(path)],
                    ["import", "-window", "root", str(path)],
                ]:
                    try:
                        subprocess.run(tool_cmd, capture_output=True, timeout=10)
                        if path.exists() and path.stat().st_size > 0:
                            break
                    except FileNotFoundError:
                        continue

            if path.exists() and path.stat().st_size > 0:
                size_kb = path.stat().st_size / 1024
                return f"Screenshot saved: {path} ({size_kb:.1f} KB)"
        except Exception as e:
            logger.debug("Fallback screenshot failed: %s", e)

        return (
            "Screenshot failed — no suitable method found.\n"
            "Install: pip install 'nvhive[vision]' (needs pyautogui + Pillow + python-xlib)\n"
            "Or install spectacle/scrot on Linux."
        )

    async def analyze_image(image_path: str, question: str = "Describe what you see in this image.") -> str:
        """Analyze an image using a vision-capable LLM.

        Tries local Ollama vision model first (llama3.2-vision, minicpm-v, etc.),
        then falls back to cloud APIs (GPT-4o, Gemini, Claude).

        Args:
            image_path: Path to the image file
            question: What to ask about the image
        """
        path = Path(image_path)
        if not path.exists():
            return f"Image not found: {image_path}"

        # Guard against huge files
        file_size = path.stat().st_size
        if file_size > 20 * 1024 * 1024:
            return f"Image too large ({file_size / 1024 / 1024:.1f} MB). Max 20 MB."

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

            size_kb = file_size / 1024

            # Try local Ollama vision model first (cached detection)
            vision_model = _detect_ollama_vision_model()
            if vision_model:
                result = await _analyze_with_ollama(image_data, question, vision_model)
                if result:
                    return f"[Vision: {vision_model}, {size_kb:.1f} KB]\n{result}"

            # Fall back to cloud vision APIs
            result = await _analyze_with_cloud(image_data, mime, question)
            if result:
                return f"[Vision: cloud, {size_kb:.1f} KB]\n{result}"

            return (
                f"[Image loaded: {path.name}, {size_kb:.1f} KB]\n"
                "No vision model available. Install one locally:\n"
                "  ollama pull llama3.2-vision\n"
                "Or configure a cloud API key (OpenAI, Google, Anthropic)."
            )
        except Exception as e:
            return f"Failed to analyze image: {e}"

    async def read_text_from_image(image_path: str) -> str:
        """Extract visible text from an image (OCR via LLM)."""
        return await analyze_image(
            image_path,
            "Read ALL visible text from this image. Return the text exactly "
            "as it appears, preserving formatting and line breaks."
        )

    # ── HANDS: Mouse + Keyboard Control ───────────────────────────

    @_desktop_action("Mouse move")
    async def mouse_move(pyautogui, x: str, y: str) -> str:
        """Move the mouse cursor to screen coordinates (x, y)."""
        ix, iy = int(x), int(y)
        pyautogui.moveTo(ix, iy, duration=0.3)
        return f"Mouse moved to ({ix}, {iy})"

    @_desktop_action("Click")
    async def mouse_click(pyautogui, x: str = "", y: str = "", button: str = "left") -> str:
        """Click the mouse at current position or specified coordinates."""
        if x and y:
            pyautogui.click(int(x), int(y), button=button)
            return f"Clicked {button} at ({x}, {y})"
        else:
            pyautogui.click(button=button)
            pos = pyautogui.position()
            return f"Clicked {button} at current position ({pos.x}, {pos.y})"

    @_desktop_action("Type")
    async def keyboard_type(pyautogui, text: str, interval: str = "0.05") -> str:
        """Type text using the keyboard."""
        # Use write() for full Unicode support (typewrite is ASCII-only)
        pyautogui.write(text, interval=float(interval))
        return f"Typed {len(text)} characters"

    @_desktop_action("Key press")
    async def keyboard_press(pyautogui, key: str) -> str:
        """Press a single key or key combination (e.g., enter, ctrl+c, alt+tab)."""
        if "+" in key:
            keys = [k.strip() for k in key.split("+")]
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(key)
        return f"Pressed: {key}"

    @_desktop_action("Scroll")
    async def scroll(pyautogui, direction: str = "down", amount: str = "3") -> str:
        """Scroll the mouse wheel up or down."""
        clicks = int(amount)
        if direction == "up":
            clicks = abs(clicks)
        else:
            clicks = -abs(clicks)
        pyautogui.scroll(clicks)
        return f"Scrolled {direction} by {abs(clicks)}"

    # ── Register all tools ────────────────────────────────────────

    # EYES (safe — read-only observation)
    registry.register(Tool(
        name="capture_screenshot",
        description="Capture a screenshot of the desktop. Use region='x,y,w,h' for a specific area.",
        parameters={"type": "object", "properties": {
            "output_path": {"type": "string"},
            "region": {"type": "string"},
        }},
        handler=capture_screenshot,
        safe=True,
    ))
    registry.register(Tool(
        name="analyze_image",
        description="Analyze an image using a vision LLM — describe content, read text, identify UI elements and their coordinates",
        parameters={"type": "object", "properties": {
            "image_path": {"type": "string"},
            "question": {"type": "string"},
        }, "required": ["image_path"]},
        handler=analyze_image,
        safe=True,
    ))
    registry.register(Tool(
        name="read_text_from_image",
        description="Extract visible text from an image (OCR via vision LLM)",
        parameters={"type": "object", "properties": {
            "image_path": {"type": "string"},
        }, "required": ["image_path"]},
        handler=read_text_from_image,
        safe=True,
    ))

    # HANDS (unsafe — requires confirmation)
    registry.register(Tool(
        name="mouse_move",
        description="Move the mouse cursor to screen coordinates",
        parameters={"type": "object", "properties": {
            "x": {"type": "string"}, "y": {"type": "string"},
        }, "required": ["x", "y"]},
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
        description="Type text using the keyboard (supports Unicode)",
        parameters={"type": "object", "properties": {
            "text": {"type": "string"},
            "interval": {"type": "string"},
        }, "required": ["text"]},
        handler=keyboard_type,
        safe=False,
    ))
    registry.register(Tool(
        name="keyboard_press",
        description="Press a key or key combination (e.g., enter, ctrl+c, alt+tab)",
        parameters={"type": "object", "properties": {
            "key": {"type": "string"},
        }, "required": ["key"]},
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
