"""Generate terminal demo GIF for README with typing animation.

Matches the exact style of existing nvHive GIFs (gpu-detection-demo.gif,
bench-demo.gif): black background, dark title bar, green $ prompt,
green block cursor, character-by-character typing.

Usage:
    python scripts/generate_demo_gif.py
    # Output: docs/screenshots/terminal-demo.gif
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Style — matched to existing nvHive GIFs
# ---------------------------------------------------------------------------
WIDTH = 640
HEIGHT = 551
FONT_SIZE = 13
LINE_HEIGHT = 17
PAD_X = 12
PAD_TOP = 38
BG = "#000000"
TITLE_BAR_BG = "#1a1a1a"
TITLE_BAR_H = 28
GREEN = "#4ec900"
BRIGHT_GREEN = "#3fb950"
CYAN = "#58a6ff"
WHITE = "#d4d4d4"
GRAY = "#888888"
DIM = "#555555"
YELLOW = "#e5c07b"
RED = "#e06c75"
CURSOR_COLOR = "#4ec900"

OUTPUT = Path("docs/screenshots/terminal-demo.gif")
TYPING_MS = 32
CURSOR_BLINK = 500
LINE_PAUSE = 60


def _get_font(size: int = FONT_SIZE) -> ImageFont.FreeTypeFont:
    for name in ["Consolas", "Menlo", "DejaVuSansMono", "LiberationMono", "cour.ttf"]:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


FONT = _get_font()


class Terminal:
    def __init__(self, title: str = "nvHive v0.29"):
        self.lines: list[tuple[str, str]] = []
        self.frames: list[tuple[Image.Image, int]] = []
        self.title = title

    def _render(self, cursor: bool = False, cursor_x: int | None = None) -> Image.Image:
        img = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(img)

        # Title bar
        draw.rounded_rectangle([(0, 0), (WIDTH, TITLE_BAR_H)], radius=8, fill=TITLE_BAR_BG)
        draw.rectangle([(0, TITLE_BAR_H - 4), (WIDTH, TITLE_BAR_H)], fill=TITLE_BAR_BG)
        # Traffic lights
        draw.ellipse([(10, 8), (22, 20)], fill="#ff5f56")
        draw.ellipse([(28, 8), (40, 20)], fill="#ffbd2e")
        draw.ellipse([(46, 8), (58, 20)], fill="#27c93f")
        # Title
        draw.text((WIDTH // 2, 7), self.title, fill="#666666", font=FONT, anchor="mt")

        # Lines
        y = PAD_TOP
        max_lines = (HEIGHT - PAD_TOP - 8) // LINE_HEIGHT
        visible = self.lines[-max_lines:] if len(self.lines) > max_lines else self.lines

        for color, text in visible:
            draw.text((PAD_X, y), text, fill=color, font=FONT)
            y += LINE_HEIGHT

        # Cursor block
        if cursor and visible:
            last_color, last_text = visible[-1]
            cx = cursor_x if cursor_x is not None else len(last_text)
            text_before = last_text[:cx]
            bbox = FONT.getbbox(text_before)
            cursor_px = PAD_X + (bbox[2] if bbox else 0)
            cursor_y = PAD_TOP + (len(visible) - 1) * LINE_HEIGHT
            if cursor_y < HEIGHT - 16:
                draw.rectangle(
                    [(cursor_px, cursor_y), (cursor_px + 7, cursor_y + LINE_HEIGHT - 3)],
                    fill=CURSOR_COLOR,
                )

        return img

    def add_frame(self, duration: int, cursor: bool = False, cursor_x: int | None = None):
        self.frames.append((self._render(cursor=cursor, cursor_x=cursor_x), duration))

    def pause(self, ms: int, cursor: bool = True):
        elapsed = 0
        blink = True
        cx = len(self.lines[-1][1]) if self.lines else 0
        while elapsed < ms:
            t = min(CURSOR_BLINK, ms - elapsed)
            self.add_frame(t, cursor=cursor and blink, cursor_x=cx)
            blink = not blink
            elapsed += t

    def type_cmd(self, prompt: str, command: str):
        self.lines.append((GREEN, prompt + " "))
        self.add_frame(150, cursor=True, cursor_x=len(prompt) + 1)
        current = prompt + " "
        for ch in command:
            current += ch
            self.lines[-1] = (WHITE, current)
            self.add_frame(TYPING_MS, cursor=True, cursor_x=len(current))
        self.pause(350)

    def out(self, color: str, text: str, delay: int = LINE_PAUSE):
        self.lines.append((color, text))
        self.add_frame(delay)

    def blank(self):
        self.lines.append((WHITE, ""))
        self.add_frame(30)

    def clear(self):
        self.lines.clear()

    def save(self):
        images = [img for img, _ in self.frames]
        durations = [d for _, d in self.frames]
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        images[0].save(
            OUTPUT, save_all=True, append_images=images[1:],
            duration=durations, loop=0, optimize=True,
        )
        print(f"Generated: {OUTPUT} ({OUTPUT.stat().st_size / 1024:.0f} KB, {len(images)} frames)")


def main():
    t = Terminal("nvHive — First Run")

    # --- 3s pause with blinking cursor ---
    t.lines.append((GREEN, "$ "))
    t.pause(3000)
    t.lines.pop()

    # --- Install ---
    t.type_cmd("$", "pip install nvhive")
    t.out(GRAY, "Successfully installed nvhive-0.29.3")
    t.pause(600)
    t.blank()
    t.type_cmd("$", "nvh")
    t.pause(400)

    # --- Step 1 ---
    t.blank()
    t.out(BRIGHT_GREEN, "Step 1/3: Hardware + Local AI")
    t.blank()
    t.out(DIM, "  GPU          NVIDIA GeForce RTX 4080 (48 GB VRAM)")
    t.out(DIM, "  Total VRAM   48 GB")
    t.out(DIM, "  Agent Tier   tier_3 - Cloud orch + 70B local")
    t.pause(1200)

    t.blank()
    t.out(WHITE, "  Download Ollama? [Y/n] Y")
    t.out(WHITE, "  Downloading Ollama (~2 GB)...")
    t.out(CYAN, "  Ollama ████████████████████ 2.1/2.1 GB 247 MB/s")
    t.out(BRIGHT_GREEN, "  Installed to ~/.nvh/bin/ollama")
    t.out(WHITE, "  Starting Ollama...")
    t.out(BRIGHT_GREEN, "  Ollama is running.")
    t.pause(1000)

    t.blank()
    t.out(WHITE, "  Recommended models (48 GB VRAM):")
    t.out(DIM, "    available  llama3.3:70b")
    t.out(DIM, "    available  llama3.2-vision")
    t.out(WHITE, "  Pull 2 model(s)? [Y/n] Y")
    t.out(CYAN, "  llama3.3:70b ███████████████ 40/40 GB")
    t.out(BRIGHT_GREEN, "  Pulled llama3.3:70b.")
    t.out(CYAN, "  llama3.2-vision █████████████ 7/7 GB")
    t.out(BRIGHT_GREEN, "  Pulled llama3.2-vision.")
    t.out(BRIGHT_GREEN, "  Desktop agent: ready")
    t.pause(1800)

    # --- Step 2 ---
    t.clear()
    t.out(BRIGHT_GREEN, "Step 2/3: Provider status")
    t.blank()
    t.out(CYAN, "  Provider        Status")
    t.out(YELLOW, "  Groq            not configured")
    t.out(YELLOW, "  OpenAI          not configured")
    t.out(YELLOW, "  Anthropic       not configured")
    t.out(YELLOW, "  Google Gemini   not configured")
    t.out(BRIGHT_GREEN, "  Ollama (local)  running (2 models)")
    t.pause(1800)

    # --- Step 3 ---
    t.clear()
    t.out(BRIGHT_GREEN, "Step 3/3: Configure API keys")
    t.blank()
    t.out(DIM, "  Desktop agent is ready!")
    t.out(DIM, "  I'll open each signup page in your browser")
    t.out(DIM, "  and watch your clipboard for the key.")
    t.blank()
    t.out(WHITE, "  Open Groq signup page? [Y/n] Y")
    t.out(DIM, "  Opened https://console.groq.com/keys")
    t.out(DIM, "  Agent: I see the Groq API Keys page...")
    t.out(DIM, "  Watching clipboard...")
    t.out(BRIGHT_GREEN, "  Detected key: gsk_xR...CmLa")
    t.out(BRIGHT_GREEN, "  Saved Groq key.")
    t.pause(2000)

    t.blank()
    t.out(BRIGHT_GREEN, "  Setup complete! 4 providers, 48 GB VRAM,")
    t.out(BRIGHT_GREEN, "  desktop agent ready.")
    t.pause(2500)

    # --- REPL ---
    t.clear()
    t.out(CYAN, "  ──── NVHive ────")
    t.out(WHITE, "  Advisors: groq, openai, anthropic, google, ollama")
    t.out(DIM, "  Model: auto   mode: ask")
    t.blank()
    t.pause(1200)

    t.type_cmd(">", "what GPU do I have?")
    t.out(DIM, "  [ask -> ollama/nemotron]")
    t.out(WHITE, "  You have an NVIDIA RTX 4080 with 48 GB VRAM.")
    t.out(WHITE, "  Running CUDA 12.4. Tier 3: 70B models locally.")
    t.out(DIM, "  Advisor: ollama | Cost: $0.00 | 290ms")
    t.pause(1500)

    t.blank()
    t.type_cmd(">", "take a screenshot")
    t.out(DIM, "  [agent mode]")
    t.out(CYAN, "  Step 1: capture_screenshot -> 245 KB")
    t.out(CYAN, "  Step 2: analyze_image [llama3.2-vision]")
    t.out(WHITE, "  Ubuntu desktop with Konsole open.")
    t.out(WHITE, "  Firefox visible behind terminal.")
    t.out(DIM, "  2 steps | 8.3s | completed")
    t.pause(1500)

    t.blank()
    t.type_cmd(">", "use anthropic")
    t.out(DIM, "  [-> /advisor anthropic]")
    t.pause(600)

    t.blank()
    t.type_cmd(">", "open firefox")
    t.out(DIM, "  [action -> Open application]")
    t.out(BRIGHT_GREEN, "  Opened: firefox")
    t.pause(800)

    t.blank()
    t.type_cmd(">", "setup comfyui")
    t.out(DIM, "  [agent mode]")
    t.out(CYAN, "  Step 1: shell -> git clone ComfyUI")
    t.out(CYAN, "  Step 2: shell -> pip install -r requirements.txt")
    t.out(CYAN, "  Step 3: shell -> python main.py &")
    t.out(CYAN, "  Step 4: screenshot -> ComfyUI running :8188")
    t.out(DIM, "  4 steps | 42s | completed")
    t.pause(2500)

    # --- End ---
    t.clear()
    t.blank()
    t.blank()
    t.out(WHITE, "  ╔═══════════════════════════════════════╗")
    t.out(WHITE, "  ║                                       ║")
    t.out(BRIGHT_GREEN, "  ║   pip install nvhive                  ║")
    t.out(WHITE, "  ║   github.com/thatcooperguy/nvHive      ║")
    t.out(WHITE, "  ║                                       ║")
    t.out(WHITE, "  ╚═══════════════════════════════════════╝")
    t.pause(4000, cursor=False)

    t.save()


if __name__ == "__main__":
    main()
