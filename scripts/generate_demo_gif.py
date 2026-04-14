"""Generate terminal demo GIF for README with typing animation.

Renders terminal frames with character-by-character typing effect,
blinking cursor, and realistic timing. Matches the style of the
existing nvidia-demo.gif and bench-demo.gif.

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
WIDTH = 640
HEIGHT = 551
FONT_SIZE = 13
LINE_HEIGHT = 18
PAD_X = 14
PAD_TOP = 40
BG = "#0d1117"
TITLE_BAR_BG = "#21262d"
GREEN = "#76B900"
BRIGHT_GREEN = "#3fb950"
CYAN = "#58a6ff"
WHITE = "#e6edf3"
GRAY = "#8b949e"
DIM = "#484f58"
YELLOW = "#d29922"
RED = "#f85149"
CURSOR_COLOR = "#76B900"

OUTPUT = Path("docs/screenshots/terminal-demo.gif")
TYPING_MS = 35       # ms per character when typing
CURSOR_BLINK = 400   # ms per cursor blink frame
PAUSE_MS = 3000      # initial pause
LINE_PAUSE = 80      # ms pause after each output line appears
COMMAND_PAUSE = 1200  # pause after a command finishes before next


def _get_font(size: int = FONT_SIZE) -> ImageFont.FreeTypeFont:
    for name in ["Consolas", "Menlo", "DejaVuSansMono", "LiberationMono", "cour.ttf"]:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


FONT = _get_font()


# ---------------------------------------------------------------------------
# Terminal state — accumulates lines, renders frames
# ---------------------------------------------------------------------------

class Terminal:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []  # (color, text)
        self.frames: list[tuple[Image.Image, int]] = []
        self.cursor_col = 0  # character position of cursor on current line
        self.title = "nvHive v0.29"

    def _render(self, cursor: bool = False, cursor_x: int | None = None) -> Image.Image:
        img = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(img)

        # Title bar
        draw.rectangle([(0, 0), (WIDTH, 32)], fill=TITLE_BAR_BG)
        draw.ellipse([(10, 10), (22, 22)], fill=RED)
        draw.ellipse([(28, 10), (40, 22)], fill=YELLOW)
        draw.ellipse([(46, 10), (58, 22)], fill=BRIGHT_GREEN)
        draw.text((WIDTH // 2, 10), self.title, fill=GRAY, font=FONT, anchor="mt")

        # Lines
        y = PAD_TOP
        max_lines = (HEIGHT - PAD_TOP - 10) // LINE_HEIGHT
        visible = self.lines[-max_lines:] if len(self.lines) > max_lines else self.lines

        for color, text in visible:
            draw.text((PAD_X, y), text, fill=color, font=FONT)
            y += LINE_HEIGHT

        # Cursor
        if cursor:
            cx = cursor_x if cursor_x is not None else 0
            # Measure text width up to cursor position
            if visible:
                last_color, last_text = visible[-1]
                text_before = last_text[:cx] if cx <= len(last_text) else last_text
                bbox = FONT.getbbox(text_before)
                cursor_px = PAD_X + (bbox[2] if bbox else 0)
            else:
                cursor_px = PAD_X
            cursor_y = PAD_TOP + (len(visible) - 1) * LINE_HEIGHT
            if cursor_y < HEIGHT - 20:
                draw.rectangle(
                    [(cursor_px, cursor_y), (cursor_px + 8, cursor_y + LINE_HEIGHT - 2)],
                    fill=CURSOR_COLOR,
                )

        return img

    def add_frame(self, duration: int, cursor: bool = False, cursor_x: int | None = None):
        self.frames.append((self._render(cursor=cursor, cursor_x=cursor_x), duration))

    def pause(self, ms: int = PAUSE_MS, cursor: bool = True):
        """Add blinking cursor frames for a pause."""
        elapsed = 0
        blink_on = True
        while elapsed < ms:
            t = min(CURSOR_BLINK, ms - elapsed)
            cx = len(self.lines[-1][1]) if self.lines else 0
            self.add_frame(t, cursor=cursor and blink_on, cursor_x=cx)
            blink_on = not blink_on
            elapsed += t

    def type_command(self, prompt: str, command: str, prompt_color: str = GREEN):
        """Animate typing a command character by character."""
        # Start with just the prompt
        self.lines.append((prompt_color, prompt + " "))
        self.add_frame(200, cursor=True, cursor_x=len(prompt) + 1)

        # Type each character
        current = prompt + " "
        for ch in command:
            current += ch
            self.lines[-1] = (WHITE, current)
            self.add_frame(TYPING_MS, cursor=True, cursor_x=len(current))

        # Brief pause after typing, before output
        self.pause(400)

    def output(self, color: str, text: str, delay: int = LINE_PAUSE):
        """Add an output line."""
        self.lines.append((color, text))
        self.add_frame(delay)

    def blank(self):
        self.lines.append((WHITE, ""))
        self.add_frame(50)

    def save(self):
        images = [img for img, _ in self.frames]
        durations = [d for _, d in self.frames]
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


# ---------------------------------------------------------------------------
# Build the demo
# ---------------------------------------------------------------------------

def main():
    t = Terminal()

    # --- 3s initial pause with blinking cursor ---
    t.lines.append((GREEN, "$ "))
    t.pause(3000)

    # --- Install ---
    t.lines.pop()  # remove the prompt
    t.type_command("$", 'pip install "nvhive[vision]"')
    t.output(GRAY, "Successfully installed nvhive-0.29.3")
    t.pause(800)

    # --- Launch nvh ---
    t.blank()
    t.type_command("$", "nvh")
    t.pause(600)

    # --- Step 1: Hardware + Local AI ---
    t.blank()
    t.output(BRIGHT_GREEN, "Step 1/3: Hardware + Local AI", 300)
    t.blank()
    t.output(DIM, "  GPU          NVIDIA GeForce RTX 4080 (48 GB VRAM)", 200)
    t.output(DIM, "  Total VRAM   48 GB", 200)
    t.output(DIM, "  Agent Tier   tier_3 - Cloud orch + 70B local", 200)
    t.pause(1000)

    # Ollama download
    t.blank()
    t.output(WHITE, "  Ollama is not installed. Download it now? [Y/n] Y")
    t.output(WHITE, "  Downloading Ollama (~2 GB)...")
    t.output(CYAN, "  Ollama ████████████████████  2.1/2.1 GB  247 MB/s")
    t.output(BRIGHT_GREEN, "  Installed Ollama to ~/.nvh/bin/ollama")
    t.pause(600)

    t.output(WHITE, "  Starting Ollama...")
    t.output(BRIGHT_GREEN, "  Ollama is running.")
    t.pause(800)

    # Model pulls
    t.blank()
    t.output(WHITE, "  Recommended models for your GPU (48 GB VRAM):")
    t.output(DIM, "    available   llama3.3:70b")
    t.output(DIM, "    available   llama3.2-vision")
    t.blank()
    t.output(WHITE, "  Pull 2 recommended model(s)? [Y/n] Y")
    t.output(CYAN, "  llama3.3:70b  █████████████  40/40 GB  185 MB/s")
    t.output(BRIGHT_GREEN, "  Pulled llama3.3:70b.")
    t.output(CYAN, "  llama3.2-vision  ███████████  7/7 GB  210 MB/s")
    t.output(BRIGHT_GREEN, "  Pulled llama3.2-vision.")
    t.output(BRIGHT_GREEN, "  Desktop agent: ready (vision model loaded)")
    t.pause(1500)

    # --- Step 2: Provider status ---
    t.blank()
    t.output(BRIGHT_GREEN, "Step 2/3: Provider status", 300)
    t.blank()
    t.output(CYAN, "  Provider        Status             ", 100)
    t.output(WHITE, "  Groq            not configured", 100)
    t.output(WHITE, "  OpenAI          not configured", 100)
    t.output(WHITE, "  Anthropic       not configured", 100)
    t.output(WHITE, "  Google Gemini   not configured", 100)
    t.output(BRIGHT_GREEN, "  Ollama (local)  running (2 models)", 100)
    t.pause(1500)

    # --- Step 3: API keys ---
    t.blank()
    t.output(BRIGHT_GREEN, "Step 3/3: Configure API keys", 300)
    t.blank()
    t.output(DIM, "  Desktop agent is ready!")
    t.output(WHITE, "  Open Groq signup page? [Y/n] Y")
    t.output(DIM, "  Opened https://console.groq.com/keys")
    t.output(DIM, "  Agent: I see the Groq console dashboard...")
    t.output(DIM, "  Watching clipboard for API key...")
    t.output(BRIGHT_GREEN, "  Detected key: gsk_xR...CmLa")
    t.output(BRIGHT_GREEN, "  Saved Groq key.")
    t.pause(2000)

    # --- Setup complete ---
    t.blank()
    t.output(BRIGHT_GREEN, "  Setup complete! 4 providers, 48 GB, desktop agent ready.")
    t.pause(2000)

    # Clear for REPL demo
    t.lines.clear()
    t.blank()
    t.output(CYAN, "  ──── NVHive ────", 200)
    t.output(WHITE, "  Advisors: groq, openai, anthropic, google, ollama")
    t.output(DIM, "  Model: auto   mode: ask")
    t.pause(1500)

    # --- Natural language question ---
    t.blank()
    t.type_command(">", "what GPU do I have?")
    t.output(DIM, "  [ask -> ollama/nemotron]")
    t.output(WHITE, "  You have an NVIDIA RTX 4080 with 48 GB VRAM,")
    t.output(WHITE, "  running CUDA 12.4. Tier 3: runs 70B models locally.")
    t.output(DIM, "  Advisor: ollama | Cost: $0.0000 | 290ms")
    t.pause(COMMAND_PAUSE)

    # --- Desktop agent ---
    t.blank()
    t.type_command(">", "take a screenshot")
    t.output(DIM, "  [agent mode]")
    t.output(CYAN, "  Step 1: capture_screenshot -> screenshot.png")
    t.output(CYAN, "  Step 2: analyze_image -> [llama3.2-vision]")
    t.output(WHITE, "  Ubuntu desktop with Konsole terminal open.")
    t.output(WHITE, "  Firefox visible behind it. Taskbar shows 2:15 PM.")
    t.output(DIM, "  2 steps | 2 tool calls | 8.3s | completed")
    t.pause(COMMAND_PAUSE)

    # --- Natural language -> command ---
    t.blank()
    t.type_command(">", "use anthropic")
    t.output(DIM, "  [-> /advisor anthropic]")
    t.output(GRAY, "  Advisor set to anthropic")
    t.pause(800)

    # --- Action ---
    t.blank()
    t.type_command(">", "open firefox")
    t.output(DIM, "  [action -> Open application]")
    t.output(BRIGHT_GREEN, "  Opened: firefox")
    t.pause(800)

    # --- Task ---
    t.blank()
    t.type_command(">", "setup comfyui")
    t.output(DIM, "  [agent mode -> setup comfyui]")
    t.output(CYAN, "  Step 1: shell -> git clone ComfyUI")
    t.output(CYAN, "  Step 2: shell -> pip install -r requirements.txt")
    t.output(CYAN, "  Step 3: shell -> python main.py &")
    t.output(CYAN, "  Step 4: capture_screenshot -> ComfyUI running")
    t.output(DIM, "  4 steps | 5 tool calls | 42s | completed")
    t.pause(2500)

    # --- End card ---
    t.lines.clear()
    t.blank()
    t.blank()
    t.output(WHITE, "  ╔═════════════════════════════════════════╗", 100)
    t.output(WHITE, "  ║                                         ║", 100)
    t.output(BRIGHT_GREEN, '  ║   pip install "nvhive[vision]"          ║', 100)
    t.output(WHITE, "  ║   github.com/thatcooperguy/nvHive        ║", 100)
    t.output(WHITE, "  ║                                         ║", 100)
    t.output(WHITE, "  ╚═════════════════════════════════════════╝", 100)
    t.pause(4000, cursor=False)

    t.save()


if __name__ == "__main__":
    main()
