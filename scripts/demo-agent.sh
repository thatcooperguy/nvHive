#!/bin/sh
# Agent demo recording script for README GIF
#
# How to record:
#   1. Install asciinema: pip install asciinema  (or brew/scoop)
#   2. Start recording: asciinema rec agent-demo.cast --cols 100 --rows 30
#   3. Run this script: sh scripts/demo-agent.sh
#   4. Stop recording: exit (or Ctrl+D)
#   5. Convert to GIF: agg agent-demo.cast docs/screenshots/agent-demo.gif
#      (install agg: cargo install agg)
#
# IMPORTANT: Make sure providers are set up first:
#   nvh health  (verify providers are live)
#   nvh agent run --setup  (pull local models if on GPU)

# Slow typing effect (same as demo-terminal.sh)
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
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   nvHive v0.12.0 — Agentic Coding Demo       ║"
echo "  ║   Plan · Execute · Verify                     ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""
pause 2

# 1. Show GPU tier detection
echo ""
echo "  ── GPU Tier Detection ──"
echo ""
type_slow "nvh agent run --setup"
nvh agent run --setup 2>/dev/null
pause 3

# 2. Agent coding task
echo ""
echo "  ── Agentic Coding ──"
echo ""
type_slow 'nvh agent run "Add a /v1/ping endpoint that returns pong" -y'
nvh agent run "Add a /v1/ping endpoint that returns pong" -y 2>/dev/null
pause 4

# 3. Code review
echo ""
echo "  ── Multi-Model Code Review ──"
echo ""
type_slow "nvh review"
nvh review 2>/dev/null
pause 3

# 4. Test generation
echo ""
echo "  ── AI Test Generation ──"
echo ""
type_slow "nvh test-gen nvh/core/council.py"
nvh test-gen nvh/core/council.py 2>/dev/null
pause 3

# 5. Council consensus
echo ""
echo "  ── Council Consensus ──"
echo ""
type_slow 'nvh convene "Redis vs Postgres for session storage?"'
nvh convene "Redis vs Postgres for session storage?" 2>/dev/null
pause 3

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   pip install nvhive                          ║"
echo "  ║   github.com/thatcooperguy/nvHive             ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""
pause 3
