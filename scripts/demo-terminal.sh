#!/bin/sh
# Terminal demo recording script for README GIF
#
# How to record:
#   1. Install asciinema: brew install asciinema
#   2. Start recording: asciinema rec terminal-demo.cast
#   3. Run this script: sh scripts/demo-terminal.sh
#   4. Stop recording: exit (or Ctrl+D)
#   5. Convert to GIF: agg terminal-demo.cast docs/screenshots/terminal-demo.gif
#      (install agg: cargo install agg)
#
# Or use any screen recorder (OBS, Kap, etc.) pointed at your terminal.
#
# IMPORTANT: Make sure providers are set up first:
#   export GROQ_API_KEY=your_key
#   nvh health  (verify providers are live)

# Slow typing effect
type_slow() {
    printf "\033[1;32m❯\033[0m "
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
echo "  ║   nvHive v0.12.0 — Terminal Demo         ║"
echo "  ║   Multi-LLM Platform                     ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""
pause 2

# 1. Health check — show resilience
echo ""
echo "  ── Provider Health ──"
echo ""
type_slow "nvh health"
nvh health 2>/dev/null
pause 3

# 2. Simple query with routing info
echo ""
echo "  ── Smart Routing ──"
echo ""
type_slow 'nvh "What is a binary search tree?"'
nvh "What is a binary search tree?" 2>/dev/null
pause 3

# 3. Escalation demo
echo ""
echo "  ── Confidence-Gated Escalation ──"
echo ""
type_slow 'nvh ask --escalate "Design a distributed consensus algorithm"'
nvh ask --escalate "Design a distributed consensus algorithm" 2>/dev/null
pause 3

# 4. Council with confidence
echo ""
echo "  ── Council Consensus ──"
echo ""
type_slow 'nvh convene "Should we use SQL or NoSQL for a real-time analytics platform?"'
nvh convene "Should we use SQL or NoSQL for a real-time analytics platform?" 2>/dev/null
pause 3

# 5. Routing intelligence
echo ""
echo "  ── Adaptive Routing Intelligence ──"
echo ""
type_slow "nvh routing-stats"
nvh routing-stats 2>/dev/null
pause 3

# 6. NVIDIA dashboard
echo ""
echo "  ── NVIDIA Infrastructure ──"
echo ""
type_slow "nvh nvidia"
nvh nvidia 2>/dev/null
pause 2

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   pip install nvhive                      ║"
echo "  ║   github.com/thatcooperguy/nvHive         ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""
pause 3
