"""Generate terminal demo GIF for README.

Renders terminal frames using Pillow and assembles into an animated GIF.
No screen recording needed — runs on any machine with Pillow installed.

Usage:
    python scripts/generate_demo_gif.py
    # Output: docs/screenshots/terminal-demo.gif
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WIDTH = 820
FONT_SIZE = 14
LINE_HEIGHT = 20
PADDING_X = 16
PADDING_TOP = 44  # space for title bar
BG = "#1a1a2e"
TITLE_BAR = "#16213e"
GREEN = "#76B900"
BRIGHT_GREEN = "#27c93f"
CYAN = "#00bcd4"
WHITE = "#e0e0e0"
GRAY = "#888888"
DIM = "#666666"
YELLOW = "#ffbd2e"
RED = "#ff5f56"

OUTPUT = Path("docs/screenshots/terminal-demo.gif")


def _get_font(size: int = FONT_SIZE, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Get a monospace font. Falls back to default if not found."""
    names = [
        "Consolas", "Menlo", "DejaVuSansMono", "LiberationMono",
        "CourierNew", "Courier New", "monospace",
    ]
    if bold:
        names = [f"{n}-Bold" for n in names] + [f"{n} Bold" for n in names] + names
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    # Last resort
    try:
        return ImageFont.truetype("cour.ttf", size)
    except (OSError, IOError):
        return ImageFont.load_default()


FONT = _get_font()
FONT_BOLD = _get_font(bold=True)


# ---------------------------------------------------------------------------
# Frame rendering
# ---------------------------------------------------------------------------

def _render_frame(lines: list[tuple[str, str]], height: int | None = None) -> Image.Image:
    """Render a terminal frame from colored text lines.

    Each line is (color, text). Use "#BOLD" prefix for bold.
    """
    if height is None:
        height = PADDING_TOP + len(lines) * LINE_HEIGHT + 20

    img = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(img)

    # Title bar
    draw.rectangle([(0, 0), (WIDTH, 36)], fill=TITLE_BAR)
    draw.ellipse([(12, 12), (24, 24)], fill=RED)
    draw.ellipse([(32, 12), (44, 24)], fill=YELLOW)
    draw.ellipse([(52, 12), (64, 24)], fill=BRIGHT_GREEN)
    draw.text((WIDTH // 2, 12), "nvHive v0.29", fill="#a0a0b0", font=FONT, anchor="mt")

    # Lines
    y = PADDING_TOP
    for color, text in lines:
        font = FONT
        if color.startswith("#BOLD"):
            font = FONT_BOLD
            color = color[5:]
        draw.text((PADDING_X, y), text, fill=color, font=font)
        y += LINE_HEIGHT

    return img


# ---------------------------------------------------------------------------
# Frame definitions — each frame is shown for a duration (ms)
# ---------------------------------------------------------------------------

def _build_frames() -> list[tuple[Image.Image, int]]:
    """Build all frames with durations."""
    frames = []

    # --- Frame 1: Install ---
    frames.append((_render_frame([
        (GREEN, "$ pip install \"nvhive[vision]\""),
        (GRAY, "Successfully installed nvhive-0.29.3"),
        (WHITE, ""),
        (GREEN, "$ nvh"),
    ]), 2500))

    # --- Frame 2: Welcome + Step 1 GPU ---
    frames.append((_render_frame([
        ("#BOLD" + BRIGHT_GREEN, "  Welcome to nvHive"),
        (WHITE, ""),
        ("#BOLD" + BRIGHT_GREEN, "Step 1/3: Hardware + Local AI"),
        (WHITE, ""),
        (DIM,   "  GPU          NVIDIA GeForce RTX 4080 (48 GB VRAM)"),
        (DIM,   "  Total VRAM   48 GB"),
        (DIM,   "  Agent Tier   tier_3 - Cloud orchestrator + 70B local worker"),
    ]), 3000))

    # --- Frame 3: Ollama download ---
    frames.append((_render_frame([
        ("#BOLD" + BRIGHT_GREEN, "Step 1/3: Hardware + Local AI"),
        (WHITE, ""),
        (WHITE, "  Ollama is not installed. Download it now? [Y/n] Y"),
        (WHITE, "  Downloading Ollama (this is ~2 GB)..."),
        (CYAN,  "  Ollama  ████████████████████████████  2.1/2.1 GB  247 MB/s"),
        (BRIGHT_GREEN, "  Extraction complete."),
        (BRIGHT_GREEN, "  Installed Ollama to /home/kiosk/.nvh/bin/ollama"),
        (WHITE, ""),
        (WHITE, "  Starting Ollama..."),
        (BRIGHT_GREEN, "  Ollama is running."),
    ]), 4000))

    # --- Frame 4: Model pulls ---
    frames.append((_render_frame([
        ("#BOLD" + BRIGHT_GREEN, "Step 1/3: Hardware + Local AI"),
        (WHITE, ""),
        (WHITE, "  Recommended models for your GPU (48 GB VRAM):"),
        (DIM,   "    available   llama3.3:70b"),
        (DIM,   "    available   llama3.2-vision"),
        (WHITE, ""),
        (WHITE, "  Pull 2 recommended model(s)? [Y/n] Y"),
        (CYAN,  "  llama3.3:70b: success  ██████████████  40.0/40.0 GB  185 MB/s"),
        (BRIGHT_GREEN, "  Pulled llama3.3:70b."),
        (CYAN,  "  llama3.2-vision: success  ████████████  7.0/7.0 GB  210 MB/s"),
        (BRIGHT_GREEN, "  Pulled llama3.2-vision."),
        (BRIGHT_GREEN, "  Desktop agent: ready (vision model loaded)"),
    ]), 4500))

    # --- Frame 5: Provider status ---
    frames.append((_render_frame([
        ("#BOLD" + BRIGHT_GREEN, "Step 2/3: Provider status"),
        (WHITE, ""),
        ("#BOLD" + CYAN, "  Provider        Status              Signup"),
        (WHITE, "  Groq            not configured       console.groq.com/keys"),
        (WHITE, "  OpenAI          not configured       platform.openai.com/api-keys"),
        (WHITE, "  Anthropic       not configured       console.anthropic.com"),
        (WHITE, "  Google Gemini   not configured       aistudio.google.com/apikey"),
        (BRIGHT_GREEN, "  Ollama (local)  running (2 models)"),
    ]), 3000))

    # --- Frame 6: API key setup with desktop agent ---
    frames.append((_render_frame([
        ("#BOLD" + BRIGHT_GREEN, "Step 3/3: Configure API keys"),
        (WHITE, ""),
        (DIM,   "  Desktop agent is ready! For each provider I'll:"),
        (DIM,   "    1. Open the signup page in your browser"),
        (DIM,   "    2. Take a screenshot to verify the page"),
        (DIM,   "    3. Watch your clipboard for the API key"),
        (WHITE, ""),
        (WHITE, "  Open Groq signup page in browser? [Y/n] Y"),
        (DIM,   "  Opened https://console.groq.com/keys"),
        (DIM,   "  Agent: I see the Groq console with an API Keys section."),
        (DIM,   "  Watching clipboard — copy your API key..."),
        (BRIGHT_GREEN, "  Detected key: gsk_xR...CmLa"),
        (BRIGHT_GREEN, "  Saved Groq key."),
    ]), 5000))

    # --- Frame 7: Setup complete ---
    frames.append((_render_frame([
        ("#BOLD" + BRIGHT_GREEN, "  Setup complete! 4 provider(s) configured, 48 GB VRAM, desktop agent ready."),
        (WHITE, ""),
        ("#BOLD" + WHITE, "  Next steps:"),
        (WHITE, "    Try a query:       nvh \"What is the meaning of life?\""),
        (WHITE, "    Interactive chat:  nvh"),
        (WHITE, "    Desktop agent:    nvh \"take a screenshot\""),
    ]), 3000))

    # --- Frame 8: REPL banner ---
    frames.append((_render_frame([
        ("#BOLD" + CYAN, "  ──── NVHive ────"),
        (WHITE, "  Advisors: groq (free), openai, anthropic, google (free), ollama (free)"),
        (WHITE, "  Model: auto   mode: ask"),
        (WHITE, ""),
        (DIM,   "  Just type your question and press Enter."),
    ]), 2000))

    # --- Frame 9: Natural language question ---
    frames.append((_render_frame([
        (GREEN, "  > what GPU do I have?"),
        (DIM,   "  [ask → ollama/nemotron]"),
        (WHITE, ""),
        (WHITE, "  You have an NVIDIA GeForce RTX 4080 with 48 GB of VRAM."),
        (WHITE, "  It's running CUDA 12.4 with driver 560.35. This GPU is in"),
        (WHITE, "  nvHive tier_3, capable of running 70B models locally."),
        (WHITE, ""),
        (DIM,   "  Advisor: ollama | Tokens: 18/52 | Cost: $0.0000 | 290ms"),
    ]), 3500))

    # --- Frame 10: Desktop agent screenshot ---
    frames.append((_render_frame([
        (GREEN, "  > take a screenshot and describe what you see"),
        (DIM,   "  [agent mode → take a screenshot and describe what you see]"),
        (WHITE, ""),
        (CYAN,  "  Step 1: capture_screenshot → screenshot.png (245.3 KB)"),
        (CYAN,  "  Step 2: analyze_image → [Vision: llama3.2-vision:11b]"),
        (WHITE, ""),
        (WHITE, "  I see an Ubuntu desktop with KDE Plasma. A Konsole terminal"),
        (WHITE, "  is open showing the nvHive REPL. Firefox is visible behind it"),
        (WHITE, "  with the Groq API page. The taskbar shows 2:15 PM."),
        (WHITE, ""),
        (DIM,   "  2 step(s) | 2 tool call(s) | 8.3s | completed"),
    ]), 4500))

    # --- Frame 11: Natural language commands ---
    frames.append((_render_frame([
        (GREEN, "  > use anthropic"),
        (DIM,   "  [→ /advisor anthropic]"),
        (GRAY,  "  Advisor set to anthropic"),
        (WHITE, ""),
        (GREEN, "  > open firefox"),
        (DIM,   "  [action → Open application or URL]"),
        (BRIGHT_GREEN, "  Opened: firefox"),
    ]), 3000))

    # --- Frame 12: Setup comfyui ---
    frames.append((_render_frame([
        (GREEN, "  > setup comfyui"),
        (DIM,   "  [agent mode → setup comfyui]"),
        (WHITE, ""),
        (CYAN,  "  Step 1: shell → git clone https://github.com/comfyanonymous/ComfyUI"),
        (CYAN,  "  Step 2: shell → pip install -r requirements.txt"),
        (CYAN,  "  Step 3: shell → python main.py &"),
        (CYAN,  "  Step 4: capture_screenshot → ComfyUI running on localhost:8188"),
        (WHITE, ""),
        (DIM,   "  4 step(s) | 5 tool call(s) | 42.1s | completed"),
    ]), 4500))

    # --- Frame 13: Closing ---
    frames.append((_render_frame([
        (WHITE, ""),
        (WHITE, "  ╔═══════════════════════════════════════════════╗"),
        (WHITE, "  ║                                               ║"),
        (BRIGHT_GREEN, "  ║   pip install \"nvhive[vision]\"                ║"),
        (WHITE, "  ║   github.com/thatcooperguy/nvHive              ║"),
        (WHITE, "  ║                                               ║"),
        (WHITE, "  ╚═══════════════════════════════════════════════╝"),
    ]), 3000))

    return frames


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    frames_with_duration = _build_frames()

    # Normalize heights to the max
    max_h = max(f.height for f, _ in frames_with_duration)
    normalized = []
    for img, duration in frames_with_duration:
        if img.height < max_h:
            new_img = Image.new("RGB", (WIDTH, max_h), BG)
            new_img.paste(img, (0, 0))
            normalized.append((new_img, duration))
        else:
            normalized.append((img, duration))

    # Save as animated GIF
    images = [img for img, _ in normalized]
    durations = [d for _, d in normalized]

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        OUTPUT,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"Generated: {OUTPUT} ({size_kb:.0f} KB, {len(images)} frames)")


if __name__ == "__main__":
    main()
