"""Generate terminal demo GIF for README with typing animation.

Matches the exact style of existing nvHive GIFs: black background,
dark title bar, NVIDIA green prompt, bright text, smooth anti-aliased
font, generous timing between commands.

Usage:
    python scripts/generate_demo_gif.py
    # Output: docs/screenshots/terminal-demo-v2.gif
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Style — sampled from gpu-detection-demo.gif
# ---------------------------------------------------------------------------
WIDTH = 640
HEIGHT = 551
FONT_SIZE = 14
LINE_HEIGHT = 20
PAD_X = 14
PAD_TOP = 40
BG = "#0a0a0a"
TITLE_BAR_BG = "#1c1c1c"
TITLE_BAR_H = 30

# Colors sampled from existing GIFs
NVIDIA_GREEN = "#78bd00"   # prompt $
BRIGHT_GREEN = "#80cb02"   # success text
CYAN = "#61afef"           # step indicators
WHITE = "#d7d7d7"          # regular text
LIGHT_GRAY = "#b0b0b0"    # secondary text
GRAY = "#808080"           # dim text
DIM = "#5a5a5a"            # very dim
YELLOW = "#e5c07b"         # warnings
RED = "#e06c75"            # traffic light
CURSOR_COLOR = "#78bd00"

OUTPUT = Path("docs/screenshots/terminal-demo-v2.gif")

# Timing — matched to existing GIFs (~125ms base)
TYPING_MS = 50        # per character
CURSOR_BLINK = 500
COMMAND_WAIT = 2000   # pause after command output before next
SECTION_WAIT = 2500   # pause between sections
LINE_DELAY = 120      # per output line


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
        draw.ellipse([(10, 9), (22, 21)], fill="#ff5f56")
        draw.ellipse([(28, 9), (40, 21)], fill="#ffbd2e")
        draw.ellipse([(46, 9), (58, 21)], fill="#27c93f")
        draw.text((WIDTH // 2, 8), self.title, fill="#707070", font=FONT, anchor="mt")

        # Lines
        y = PAD_TOP
        max_lines = (HEIGHT - PAD_TOP - 10) // LINE_HEIGHT
        visible = self.lines[-max_lines:] if len(self.lines) > max_lines else self.lines

        for color, text in visible:
            draw.text((PAD_X, y), text, fill=color, font=FONT)
            y += LINE_HEIGHT

        # Cursor
        if cursor and visible:
            last_color, last_text = visible[-1]
            cx = cursor_x if cursor_x is not None else len(last_text)
            text_before = last_text[:cx]
            bbox = FONT.getbbox(text_before)
            cursor_px = PAD_X + (bbox[2] if bbox else 0)
            cursor_y = PAD_TOP + (len(visible) - 1) * LINE_HEIGHT
            if cursor_y < HEIGHT - 20:
                draw.rectangle(
                    [(cursor_px, cursor_y + 1), (cursor_px + 8, cursor_y + LINE_HEIGHT - 2)],
                    fill=CURSOR_COLOR,
                )

        return img

    def frame(self, ms: int, cursor: bool = False, cursor_x: int | None = None):
        self.frames.append((self._render(cursor=cursor, cursor_x=cursor_x), ms))

    def pause(self, ms: int, cursor: bool = True):
        elapsed = 0
        blink = True
        cx = len(self.lines[-1][1]) if self.lines else 0
        while elapsed < ms:
            t = min(CURSOR_BLINK, ms - elapsed)
            self.frame(t, cursor=cursor and blink, cursor_x=cx)
            blink = not blink
            elapsed += t

    def type_cmd(self, prompt: str, command: str):
        self.lines.append((NVIDIA_GREEN, prompt + " "))
        self.frame(300, cursor=True, cursor_x=len(prompt) + 1)
        current = prompt + " "
        for ch in command:
            current += ch
            self.lines[-1] = (WHITE, current)
            self.frame(TYPING_MS, cursor=True, cursor_x=len(current))
        self.pause(600)

    def out(self, color: str, text: str, delay: int = LINE_DELAY):
        self.lines.append((color, text))
        self.frame(delay)

    def blank(self):
        self.lines.append((WHITE, ""))
        self.frame(60)

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

    # --- 3s opening pause with blinking cursor ---
    t.lines.append((NVIDIA_GREEN, "$ "))
    t.pause(3000)
    t.lines.pop()

    # === INSTALL ===
    t.type_cmd("$", "pip install nvhive")
    t.out(LIGHT_GRAY, "Successfully installed nvhive-0.29.3")
    t.pause(COMMAND_WAIT)

    t.blank()
    t.type_cmd("$", "nvh")
    t.pause(800)

    # === STEP 1: Hardware + Local AI ===
    t.blank()
    t.out(BRIGHT_GREEN, "Step 1/3: Hardware + Local AI")
    t.blank()
    t.out(LIGHT_GRAY, "  GPU          NVIDIA GeForce RTX 4080 (48 GB VRAM)")
    t.out(LIGHT_GRAY, "  Total VRAM   48 GB")
    t.out(LIGHT_GRAY, "  Agent Tier   tier_3 - Cloud orch + 70B local")
    t.pause(SECTION_WAIT)

    # Ollama download
    t.blank()
    t.out(WHITE, "  Download Ollama? [Y/n] Y")
    t.out(WHITE, "  Downloading Ollama (~2 GB)...")
    t.pause(400)
    t.out(CYAN, "  Ollama ████████████████████ 2.1/2.1 GB  247 MB/s")
    t.out(BRIGHT_GREEN, "  Installed to ~/.nvh/bin/ollama")
    t.pause(600)
    t.out(WHITE, "  Starting Ollama...")
    t.out(BRIGHT_GREEN, "  Ollama is running.")
    t.pause(COMMAND_WAIT)

    # Model pulls
    t.blank()
    t.out(WHITE, "  Recommended models (48 GB VRAM):")
    t.out(GRAY, "    available  llama3.3:70b")
    t.out(GRAY, "    available  llama3.2-vision")
    t.blank()
    t.out(WHITE, "  Pull 2 model(s)? [Y/n] Y")
    t.pause(400)
    t.out(CYAN, "  llama3.3:70b ████████████████ 40/40 GB  185 MB/s")
    t.out(BRIGHT_GREEN, "  Pulled llama3.3:70b.")
    t.pause(300)
    t.out(CYAN, "  llama3.2-vision ██████████████ 7/7 GB  210 MB/s")
    t.out(BRIGHT_GREEN, "  Pulled llama3.2-vision.")
    t.out(BRIGHT_GREEN, "  Desktop agent: ready")
    t.pause(SECTION_WAIT)

    # === STEP 2: Provider status ===
    t.clear()
    t.out(BRIGHT_GREEN, "Step 2/3: Provider status")
    t.blank()
    t.out(CYAN, "  Provider        Status")
    t.out(YELLOW, "  Groq            not configured")
    t.out(YELLOW, "  OpenAI          not configured")
    t.out(YELLOW, "  Anthropic       not configured")
    t.out(YELLOW, "  Google Gemini   not configured")
    t.out(BRIGHT_GREEN, "  Ollama (local)  running (2 models)")
    t.pause(SECTION_WAIT)

    # === STEP 3: API keys ===
    t.clear()
    t.out(BRIGHT_GREEN, "Step 3/3: Configure API keys")
    t.blank()
    t.out(GRAY, "  Desktop agent is ready!")
    t.out(GRAY, "  I'll open signup pages and watch your clipboard.")
    t.blank()
    t.out(WHITE, "  Open Groq signup page? [Y/n] Y")
    t.out(GRAY, "  Opened https://console.groq.com/keys")
    t.pause(600)
    t.out(GRAY, "  Agent: I see the Groq API Keys page...")
    t.out(GRAY, "  Watching clipboard...")
    t.pause(800)
    t.out(BRIGHT_GREEN, "  Detected key: gsk_xR...CmLa")
    t.out(BRIGHT_GREEN, "  Saved Groq key.")
    t.pause(COMMAND_WAIT)

    t.blank()
    t.out(BRIGHT_GREEN, "  Setup complete! 4 providers, 48 GB VRAM,")
    t.out(BRIGHT_GREEN, "  desktop agent ready.")
    t.pause(SECTION_WAIT)

    # === REPL ===
    t.clear()
    t.out(CYAN, "  ──── NVHive ────")
    t.out(WHITE, "  Advisors: groq, openai, anthropic, google, ollama")
    t.out(GRAY, "  Model: auto   mode: ask")
    t.blank()
    t.pause(COMMAND_WAIT)

    # Question
    t.type_cmd(">", "what GPU do I have?")
    t.out(GRAY, "  [ask -> ollama/nemotron]")
    t.blank()
    t.out(WHITE, "  You have an NVIDIA RTX 4080 with 48 GB VRAM.")
    t.out(WHITE, "  Running CUDA 12.4. Tier 3: 70B models locally.")
    t.blank()
    t.out(GRAY, "  Advisor: ollama | Cost: $0.00 | 290ms")
    t.pause(COMMAND_WAIT)

    # Desktop agent
    t.blank()
    t.type_cmd(">", "take a screenshot")
    t.out(GRAY, "  [agent mode]")
    t.blank()
    t.out(CYAN, "  Step 1: capture_screenshot -> 245 KB")
    t.out(CYAN, "  Step 2: analyze_image [llama3.2-vision]")
    t.blank()
    t.out(WHITE, "  Ubuntu desktop with Konsole terminal open.")
    t.out(WHITE, "  Firefox visible behind it. Taskbar shows 2:15 PM.")
    t.blank()
    t.out(GRAY, "  2 steps | 8.3s | completed")
    t.pause(COMMAND_WAIT)

    # Natural language command
    t.blank()
    t.type_cmd(">", "use anthropic")
    t.out(GRAY, "  [-> /advisor anthropic]")
    t.out(LIGHT_GRAY, "  Advisor set to anthropic")
    t.pause(COMMAND_WAIT)

    # Action
    t.blank()
    t.type_cmd(">", "open firefox")
    t.out(GRAY, "  [action -> Open application]")
    t.out(BRIGHT_GREEN, "  Opened: firefox")
    t.pause(COMMAND_WAIT)

    # Task
    t.blank()
    t.type_cmd(">", "setup comfyui")
    t.out(GRAY, "  [agent mode]")
    t.blank()
    t.out(CYAN, "  Step 1: shell -> git clone ComfyUI")
    t.out(CYAN, "  Step 2: shell -> pip install -r requirements.txt")
    t.out(CYAN, "  Step 3: shell -> python main.py &")
    t.out(CYAN, "  Step 4: screenshot -> ComfyUI running :8188")
    t.blank()
    t.out(GRAY, "  4 steps | 42s | completed")
    t.pause(SECTION_WAIT)

    # === End card ===
    t.clear()
    t.blank()
    t.blank()
    t.blank()
    t.blank()
    t.out(WHITE, "     ╔═══════════════════════════════════════╗")
    t.out(WHITE, "     ║                                       ║")
    t.out(BRIGHT_GREEN, "     ║     pip install nvhive                ║")
    t.out(WHITE, "     ║     github.com/thatcooperguy/nvHive    ║")
    t.out(WHITE, "     ║                                       ║")
    t.out(WHITE, "     ╚═══════════════════════════════════════╝")
    t.pause(5000, cursor=False)

    t.save()


if __name__ == "__main__":
    main()
