#!/bin/sh
# Terminal demo recording script for README GIF
#
# Records the full nvHive first-run experience: GPU detection, Ollama install,
# model pulls, desktop agent API key setup, and natural language commands.
#
# How to record:
#
#   Option A — VHS (recommended, produces GIF directly):
#     go install github.com/charmbracelet/vhs@latest
#     vhs scripts/demo-setup.tape
#
#   Option B — asciinema + agg:
#     pip install asciinema
#     asciinema rec terminal-demo.cast
#     sh scripts/demo-terminal.sh
#     exit
#     cargo install agg
#     agg terminal-demo.cast docs/screenshots/terminal-demo.gif
#
#   Option C — screen recorder (OBS, peek, etc.)
#     Just run this script and record your terminal.
#
# PREREQUISITES:
#   - NVIDIA GPU available (nvidia-smi works)
#   - Internet connection
#   - For full demo: remove ~/.hive/config.yaml to trigger first-run setup
#   - For API key demo: have at least one provider account ready

# Slow typing effect
type_slow() {
    printf "\033[1;32m$\033[0m "
    for char in $(echo "$1" | sed 's/\(.\)/\1\n/g'); do
        printf "%s" "$char"
        sleep 0.04
    done
    printf "\n"
    sleep 0.3
}

pause() {
    sleep "${1:-2}"
}

clear

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   nvHive v0.29 — Full Setup Demo         ║"
echo "  ║   GPU + Local AI + Desktop Agent          ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""
pause 2

# 1. Install
echo ""
echo "  ── Install ──"
echo ""
type_slow 'pip install "nvhive[vision]"'
echo "  Successfully installed nvhive-0.29.3"
pause 2

# 2. First run — triggers setup
echo ""
echo "  ── First Run Setup ──"
echo ""
type_slow "nvh"
nvh 2>/dev/null
pause 3

# 3. After setup — natural language queries
echo ""
echo "  ── Natural Language ──"
echo ""
type_slow 'nvh "What GPU do I have?"'
nvh "What GPU do I have?" 2>/dev/null
pause 3

# 4. Desktop agent — screenshot
echo ""
echo "  ── Desktop Agent ──"
echo ""
type_slow 'nvh "take a screenshot and describe my desktop"'
nvh "take a screenshot and describe my desktop" 2>/dev/null
pause 3

# 5. Interactive REPL — natural language commands
echo ""
echo "  ── Interactive REPL ──"
echo ""
type_slow "nvh"
echo ""
echo "  ──── NVHive ────"
echo "  Advisors: groq (free), openai, anthropic, google (free), ollama (free)"
echo "  Model: auto   mode: ask"
echo ""
echo "  Just type your question and press Enter."
echo ""
pause 2

# Natural language → command
echo ""
type_slow "use anthropic"
echo "  [→ /advisor anthropic]"
echo "  Advisor set to anthropic"
pause 2

# Action
echo ""
type_slow "open firefox"
echo "  [action → Open application or URL]"
echo "  Opened: firefox"
pause 2

# Task
echo ""
type_slow "setup comfyui"
echo "  [agent mode → setup comfyui]"
echo "  Step 1: shell → git clone https://github.com/comfyanonymous/ComfyUI"
echo "  Step 2: shell → pip install -r requirements.txt"
echo "  Step 3: shell → python main.py &"
echo "  Step 4: capture_screenshot → ComfyUI running on localhost:8188"
echo "  4 step(s) | 5 tool call(s) | 42.1s | completed"
pause 3

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   pip install \"nvhive[vision]\"                ║"
echo "  ║   github.com/thatcooperguy/nvHive             ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""
pause 3
