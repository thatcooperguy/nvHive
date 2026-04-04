#!/bin/sh
# =============================================================================
# nvHive -- Migration Script
#
# For users who already have nvHive installed and want to import configs
# from OpenClaw, Claw Code, or Claude Desktop.
#
# Usage:
#   ./scripts/migrate.sh
#   ./scripts/migrate.sh --dry-run
#   ./scripts/migrate.sh --from openclaw
# =============================================================================

set -eu

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

info()    { printf "${BLUE}[nvhive]${RESET} %s\n" "$*"; }
success() { printf "${GREEN}[nvhive]${RESET} %s\n" "$*"; }
warn()    { printf "${YELLOW}[nvhive]${RESET} %s\n" "$*"; }
error()   { printf "${RED}[nvhive]${RESET} %s\n" "$*" >&2; }
die()     { error "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
DRY_RUN=""
SOURCE=""
for arg in "$@"; do
    case "$arg" in
        --dry-run)      DRY_RUN="--dry-run" ;;
        --from)         : ;;  # next arg handled below
        openclaw|claw-code)
            SOURCE="--from $arg" ;;
        --help|-h)
            printf "Usage: migrate.sh [--dry-run] [--from openclaw|claw-code]\n"
            printf "\n"
            printf "Options:\n"
            printf "  --dry-run           Preview changes without importing\n"
            printf "  --from <source>     Only import from a specific source\n"
            exit 0
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
printf "\n"
printf "${BOLD}nvHive Migration Tool${RESET}\n"
printf "${DIM}Import configs from OpenClaw, Claw Code, and Claude Desktop${RESET}\n"
printf "\n"

if ! command -v nvh >/dev/null 2>&1; then
    error "nvHive is not installed."
    printf "\n"
    printf "  Install it first:\n"
    printf "    ${BOLD}curl -fsSL https://nvhive.dev/install | sh${RESET}\n"
    printf "\n"
    exit 1
fi

info "nvHive version: $(nvh version 2>/dev/null || echo 'unknown')"

# ---------------------------------------------------------------------------
# Scan for existing tools
# ---------------------------------------------------------------------------
info "Scanning for existing AI tool configurations..."
printf "\n"

FOUND=0

# OpenClaw
for p in "$HOME/.openclaw" "$HOME/.config/openclaw"; do
    if [ -d "$p" ]; then
        printf "  ${GREEN}+${RESET} OpenClaw config found at ${BOLD}$p${RESET}\n"
        FOUND=$((FOUND + 1))
        break
    fi
done

# Claw Code
for p in "$HOME/.claw" "$HOME/.config/claw-code"; do
    if [ -d "$p" ]; then
        printf "  ${GREEN}+${RESET} Claw Code config found at ${BOLD}$p${RESET}\n"
        FOUND=$((FOUND + 1))
        break
    fi
done

# Claude Desktop
if [ -f "$HOME/.claude/claude_desktop_config.json" ]; then
    printf "  ${GREEN}+${RESET} Claude Desktop config found\n"
    FOUND=$((FOUND + 1))
fi

# API keys in environment
KEY_COUNT=0
KEY_NAMES=""
for var in OPENAI_API_KEY ANTHROPIC_API_KEY GROQ_API_KEY GOOGLE_API_KEY \
           MISTRAL_API_KEY COHERE_API_KEY XAI_API_KEY DEEPSEEK_API_KEY \
           FIREWORKS_API_KEY TOGETHER_API_KEY GITHUB_TOKEN; do
    eval val="\${$var:-}"
    if [ -n "$val" ]; then
        provider=$(echo "$var" | sed 's/_API_KEY//' | sed 's/_TOKEN//' | tr '[:upper:]' '[:lower:]')
        KEY_NAMES="${KEY_NAMES}${KEY_NAMES:+, }$provider"
        KEY_COUNT=$((KEY_COUNT + 1))
    fi
done

if [ "$KEY_COUNT" -gt 0 ]; then
    printf "  ${GREEN}+${RESET} API keys in environment: ${BOLD}$KEY_NAMES${RESET}\n"
    FOUND=$((FOUND + 1))
fi

printf "\n"

if [ "$FOUND" -eq 0 ]; then
    warn "No existing AI tool configurations found."
    printf "\n"
    printf "  nvHive checked:\n"
    printf "    - OpenClaw:       ~/.openclaw/, ~/.config/openclaw/\n"
    printf "    - Claw Code:      ~/.claw/, ~/.config/claw-code/\n"
    printf "    - Claude Desktop: ~/.claude/claude_desktop_config.json\n"
    printf "    - Environment:    OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.\n"
    printf "\n"
    printf "  Get started from scratch: ${BOLD}nvh config init${RESET}\n"
    printf "\n"
    exit 0
fi

# ---------------------------------------------------------------------------
# Run migration
# ---------------------------------------------------------------------------
if [ -n "$DRY_RUN" ]; then
    info "Dry run -- showing what would be imported:"
    printf "\n"
fi

# shellcheck disable=SC2086
nvh migrate $SOURCE $DRY_RUN

if [ -n "$DRY_RUN" ]; then
    printf "\n"
    info "To perform the migration, run without --dry-run:"
    printf "    ${BOLD}./scripts/migrate.sh${RESET}\n"
    printf "\n"
    exit 0
fi

# ---------------------------------------------------------------------------
# Run quick test
# ---------------------------------------------------------------------------
printf "\n"
info "Verifying installation..."
printf "\n"

if nvh test --quick 2>/dev/null; then
    printf "\n"
    success "All checks passed."
else
    printf "\n"
    warn "Some checks had warnings. Run 'nvh test' for full diagnostics."
fi

# ---------------------------------------------------------------------------
# Suggest benchmark
# ---------------------------------------------------------------------------
printf "\n"
printf "${BOLD}${GREEN}============================================================${RESET}\n"
printf "${BOLD}${GREEN}  Migration complete!${RESET}\n"
printf "${BOLD}${GREEN}============================================================${RESET}\n"
printf "\n"
printf "  ${BOLD}Suggested next steps:${RESET}\n"
printf "\n"
printf "    1. Benchmark free providers (costs \$0):\n"
printf "       ${BOLD}nvh benchmark --mode council-free${RESET}\n"
printf "\n"
printf "    2. Try a multi-LLM council query:\n"
printf "       ${BOLD}nvh convene \"Compare Python and Rust for CLI tools\"${RESET}\n"
printf "\n"
printf "    3. Launch the web dashboard:\n"
printf "       ${BOLD}nvh webui${RESET}\n"
printf "\n"
printf "  ${DIM}nvHive routes across 23 providers (25 free models)${RESET}\n"
printf "  ${DIM}so you are never locked into one provider's pricing.${RESET}\n"
printf "\n"
