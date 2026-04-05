#!/bin/sh
# Migration demo recording script — shows OpenClaw → nvHive flow
#
# Record with: asciinema rec migration-demo.cast
# Convert with: agg migration-demo.cast docs/screenshots/migration-demo.gif

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
echo "  ║   OpenClaw → nvHive Migration             ║"
echo "  ║   60 seconds. Zero code changes.          ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""
pause 2

# 1. Install
echo "  ── Step 1: Install ──"
echo ""
type_slow "pip install nvhive"
echo "  Successfully installed nvhive-0.5.0"
pause 2

# 2. Migrate
echo ""
echo "  ── Step 2: Migrate ──"
echo ""
type_slow "nvh migrate --from openclaw"
nvh migrate --from openclaw 2>/dev/null
pause 3

# 3. Verify
echo ""
echo "  ── Step 3: Verify ──"
echo ""
type_slow "nvh health"
nvh health 2>/dev/null
pause 3

# 4. First query
echo ""
echo "  ── Step 4: First Query ──"
echo ""
type_slow 'nvh "Hello from nvHive!"'
nvh "Hello from nvHive!" 2>/dev/null
pause 3

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Migration complete.                     ║"
echo "  ║   23 providers. Automatic failover.       ║"
echo "  ║   Your workflow never breaks again.       ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""
pause 3
