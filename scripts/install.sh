#!/bin/sh
# =============================================================================
# nvHive -- curl-installable setup
#
# Install:
#   curl -fsSL https://nvhive.dev/install | sh
#
# Uninstall:
#   curl -fsSL https://nvhive.dev/install | sh -s -- --uninstall
#
# What this does:
#   1. Detects OS, Python, pip/pipx
#   2. Installs nvHive via pipx (preferred) or pip
#   3. Auto-detects OpenClaw / Claw Code / Claude Desktop configs
#   4. Imports existing API keys via `nvh migrate`
#   5. Enables free providers (Ollama, LLM7)
#   6. Runs `nvh test --quick` to verify
#   7. Prints next steps
#
# Requirements:
#   - Python 3.10+
#   - macOS or Linux
#   - No sudo required
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

NVH_MIN_PYTHON_MINOR=10

info()    { printf "${BLUE}[nvhive]${RESET} %s\n" "$*"; }
success() { printf "${GREEN}[nvhive]${RESET} %s\n" "$*"; }
warn()    { printf "${YELLOW}[nvhive]${RESET} %s\n" "$*"; }
error()   { printf "${RED}[nvhive]${RESET} %s\n" "$*" >&2; }
die()     { error "$*"; exit 1; }

banner() {
    printf "\n"
    printf "${BOLD}${GREEN}"
    printf "  _______ ___   ___ __  __ _\n"
    printf " |  __ \\ \\ \\ / / |  \\/  (_)\n"
    printf " | |  | |\\ V /| |_| | |_  __   _____\n"
    printf " | |  | | > < |  _  | | \\ \\ / / _ \\\\\n"
    printf " | |__| |/ . \\| | | | |  \\ V /  __/\n"
    printf " |_____//_/ \\_\\_| |_|_|   \\_/ \\___|\n"
    printf "${RESET}\n"
    printf "  ${BOLD}nvHive${RESET} -- Multi-LLM Orchestration Platform\n"
    printf "  ${DIM}23 providers. 25 free models. One CLI.${RESET}\n"
    printf "\n"
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
ACTION="install"
for arg in "$@"; do
    case "$arg" in
        --uninstall) ACTION="uninstall" ;;
        --help|-h)
            printf "Usage: install.sh [--uninstall]\n"
            printf "  --uninstall   Remove nvHive and its configuration\n"
            exit 0
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Detect OS
# ---------------------------------------------------------------------------
detect_os() {
    OS="$(uname -s)"
    ARCH="$(uname -m)"
    case "$OS" in
        Darwin) OS_NAME="macOS" ;;
        Linux)  OS_NAME="Linux" ;;
        *)      die "Unsupported OS: $OS. nvHive supports macOS and Linux." ;;
    esac
    info "Detected: $OS_NAME ($ARCH)"
}

# ---------------------------------------------------------------------------
# Detect Python
# ---------------------------------------------------------------------------
detect_python() {
    PYTHON=""
    for cmd in python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -eq 3 ] && [ "$minor" -ge "$NVH_MIN_PYTHON_MINOR" ]; then
                PYTHON="$cmd"
                PYTHON_VERSION="$ver"
                break
            fi
        fi
    done

    if [ -z "$PYTHON" ]; then
        error "Python 3.${NVH_MIN_PYTHON_MINOR}+ is required but not found."
        printf "\n"
        if [ "$OS_NAME" = "macOS" ]; then
            printf "  Install Python via Homebrew:\n"
            printf "    ${BOLD}brew install python@3.12${RESET}\n"
        else
            printf "  Install Python:\n"
            printf "    ${BOLD}sudo apt install python3.12 python3.12-venv${RESET}  (Debian/Ubuntu)\n"
            printf "    ${BOLD}sudo dnf install python3.12${RESET}                  (Fedora)\n"
        fi
        exit 1
    fi

    success "Python $PYTHON_VERSION ($PYTHON)"
}

# ---------------------------------------------------------------------------
# Detect pip / pipx
# ---------------------------------------------------------------------------
detect_installer() {
    USE_PIPX=false
    PIPX_CMD=""
    PIP_CMD=""

    # Check pipx first (preferred)
    if command -v pipx >/dev/null 2>&1; then
        USE_PIPX=true
        PIPX_CMD="pipx"
        success "pipx found (preferred installer)"
        return
    fi

    # Check pip
    if "$PYTHON" -m pip --version >/dev/null 2>&1; then
        PIP_CMD="$PYTHON -m pip"
        success "pip found"
    else
        warn "pip not found. Attempting to bootstrap..."
        if "$PYTHON" -m ensurepip --user >/dev/null 2>&1; then
            PIP_CMD="$PYTHON -m pip"
            success "pip bootstrapped"
        else
            die "Could not find or install pip. Install it manually and retry."
        fi
    fi

    # Offer to install pipx
    info "pipx is recommended for CLI tools (isolates dependencies)."
    info "Installing pipx..."
    if $PIP_CMD install --user pipx >/dev/null 2>&1; then
        # Ensure pipx is on PATH
        if command -v pipx >/dev/null 2>&1; then
            USE_PIPX=true
            PIPX_CMD="pipx"
            success "pipx installed"
        elif "$PYTHON" -m pipx --version >/dev/null 2>&1; then
            USE_PIPX=true
            PIPX_CMD="$PYTHON -m pipx"
            success "pipx installed (via python -m pipx)"
        else
            warn "pipx installed but not on PATH. Falling back to pip."
        fi
    else
        warn "Could not install pipx. Using pip instead."
    fi
}

# ---------------------------------------------------------------------------
# Install nvHive
# ---------------------------------------------------------------------------
install_nvhive() {
    info "Installing nvHive..."

    if command -v nvh >/dev/null 2>&1; then
        existing_ver=$(nvh version 2>/dev/null || echo "unknown")
        warn "nvHive is already installed (${existing_ver}). Upgrading..."
        if [ "$USE_PIPX" = true ]; then
            $PIPX_CMD upgrade nvhive >/dev/null 2>&1 || $PIPX_CMD install --force nvhive >/dev/null 2>&1
        else
            $PIP_CMD install --user --upgrade nvhive >/dev/null 2>&1
        fi
    else
        if [ "$USE_PIPX" = true ]; then
            $PIPX_CMD install nvhive >/dev/null 2>&1
        else
            $PIP_CMD install --user nvhive >/dev/null 2>&1
        fi
    fi

    # Verify installation
    if command -v nvh >/dev/null 2>&1; then
        installed_ver=$(nvh version 2>/dev/null || echo "unknown")
        success "nvHive $installed_ver installed"
    else
        # pipx/pip --user may not be on PATH yet
        warn "nvh command not found on PATH."
        if [ "$USE_PIPX" = true ]; then
            $PIPX_CMD ensurepath >/dev/null 2>&1 || true
            info "Run: ${BOLD}source ~/.bashrc${RESET} (or restart your shell) then re-run this script."
        else
            user_bin=$("$PYTHON" -c "import site; print(site.getusersitepackages().replace('lib/python','bin').split('/lib')[0] + '/bin')" 2>/dev/null || echo "\$HOME/.local/bin")
            info "Add to your PATH: ${BOLD}export PATH=\"$user_bin:\$PATH\"${RESET}"
        fi
        die "Could not verify nvh installation. Fix your PATH and retry."
    fi
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
uninstall_nvhive() {
    info "Uninstalling nvHive..."

    if command -v pipx >/dev/null 2>&1; then
        pipx uninstall nvhive 2>/dev/null && success "Removed nvhive (pipx)" || true
    fi

    if command -v pip3 >/dev/null 2>&1; then
        pip3 uninstall -y nvhive 2>/dev/null && success "Removed nvhive (pip)" || true
    fi

    # Remove config directory
    config_dir="${HOME}/.nvhive"
    if [ -d "$config_dir" ]; then
        printf "${YELLOW}Remove configuration at $config_dir? [y/N]${RESET} "
        read -r answer </dev/tty 2>/dev/null || answer="n"
        case "$answer" in
            [yY]|[yY][eE][sS])
                rm -rf "$config_dir"
                success "Removed $config_dir"
                ;;
            *)
                info "Kept $config_dir"
                ;;
        esac
    fi

    success "nvHive uninstalled."
    exit 0
}

# ---------------------------------------------------------------------------
# Auto-detect existing AI tools
# ---------------------------------------------------------------------------
detect_existing_tools() {
    info "Checking for existing AI tool configurations..."

    FOUND_TOOLS=0

    # OpenClaw
    if [ -d "$HOME/.openclaw" ] || [ -d "$HOME/.config/openclaw" ]; then
        success "Found: OpenClaw configuration"
        FOUND_TOOLS=$((FOUND_TOOLS + 1))
    fi

    # Claw Code
    if [ -d "$HOME/.claw" ] || [ -d "$HOME/.config/claw-code" ]; then
        success "Found: Claw Code configuration"
        FOUND_TOOLS=$((FOUND_TOOLS + 1))
    fi

    # Claude Desktop
    if [ -f "$HOME/.claude/claude_desktop_config.json" ]; then
        success "Found: Claude Desktop configuration"
        FOUND_TOOLS=$((FOUND_TOOLS + 1))
    fi

    # API keys in environment
    FOUND_KEYS=""
    for var in OPENAI_API_KEY ANTHROPIC_API_KEY GROQ_API_KEY GOOGLE_API_KEY \
               MISTRAL_API_KEY COHERE_API_KEY XAI_API_KEY DEEPSEEK_API_KEY \
               FIREWORKS_API_KEY TOGETHER_API_KEY GITHUB_TOKEN; do
        eval val="\${$var:-}"
        if [ -n "$val" ]; then
            provider=$(echo "$var" | sed 's/_API_KEY//' | sed 's/_TOKEN//' | tr '[:upper:]' '[:lower:]')
            FOUND_KEYS="${FOUND_KEYS}${FOUND_KEYS:+, }$provider"
        fi
    done

    if [ -n "$FOUND_KEYS" ]; then
        success "Found API keys: $FOUND_KEYS"
        FOUND_TOOLS=$((FOUND_TOOLS + 1))
    fi

    if [ "$FOUND_TOOLS" -eq 0 ]; then
        info "No existing AI tool configurations found. Starting fresh."
    fi
}

# ---------------------------------------------------------------------------
# Run migration
# ---------------------------------------------------------------------------
run_migration() {
    if [ "$FOUND_TOOLS" -gt 0 ]; then
        info "Importing existing configurations..."
        nvh migrate 2>/dev/null || warn "Migration had warnings (non-fatal). You can retry: nvh migrate"
    fi
}

# ---------------------------------------------------------------------------
# Auto-enable free providers
# ---------------------------------------------------------------------------
enable_free_providers() {
    info "Checking for free local providers..."

    # Check Ollama
    if command -v ollama >/dev/null 2>&1 || curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
        success "Ollama detected -- local models available"
    else
        info "Ollama not running. Install it later: https://ollama.com"
    fi

    # Enable LLM7 (always-free tier)
    info "Enabling LLM7 free provider tier..."
    nvh config set providers.llm7.enabled true >/dev/null 2>&1 || true
    success "LLM7 free provider enabled"
}

# ---------------------------------------------------------------------------
# Run quick test
# ---------------------------------------------------------------------------
run_quick_test() {
    info "Running quick verification..."
    printf "\n"
    if nvh test --quick 2>/dev/null; then
        printf "\n"
        success "All checks passed."
    else
        printf "\n"
        warn "Some checks had warnings. Run 'nvh test' for full diagnostics."
    fi
}

# ---------------------------------------------------------------------------
# Print next steps
# ---------------------------------------------------------------------------
print_next_steps() {
    printf "\n"
    printf "${BOLD}${GREEN}============================================================${RESET}\n"
    printf "${BOLD}${GREEN}  nvHive is ready!${RESET}\n"
    printf "${BOLD}${GREEN}============================================================${RESET}\n"
    printf "\n"
    printf "  ${BOLD}Quick start:${RESET}\n"
    printf "    nvh \"What is quantum computing?\"       ${DIM}# ask any question${RESET}\n"
    printf "    nvh convene \"Python vs Rust?\"           ${DIM}# multi-LLM council${RESET}\n"
    printf "    nvh benchmark -m council-free           ${DIM}# benchmark free tier${RESET}\n"
    printf "\n"
    printf "  ${BOLD}Configure:${RESET}\n"
    printf "    nvh config init                         ${DIM}# guided setup${RESET}\n"
    printf "    nvh provider login openai               ${DIM}# add a provider${RESET}\n"
    printf "    nvh webui                               ${DIM}# launch web dashboard${RESET}\n"
    printf "\n"
    printf "  ${BOLD}Free models (no API key needed):${RESET}\n"
    printf "    Ollama local models, LLM7, Groq free tier, Google Gemini free\n"
    printf "\n"
    printf "  ${BOLD}Learn more:${RESET}\n"
    printf "    Docs:      https://nvhive.dev/docs\n"
    printf "    GitHub:    https://github.com/thatcooperguy/nvHive\n"
    printf "    Discord:   https://nvhive.dev/discord\n"
    printf "\n"

    if [ "$FOUND_TOOLS" -eq 0 ]; then
        printf "  ${YELLOW}Tip:${RESET} Set API keys in your shell profile (.bashrc / .zshrc)\n"
        printf "  or run: ${BOLD}nvh provider login <provider>${RESET}\n"
        printf "\n"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    banner

    if [ "$ACTION" = "uninstall" ]; then
        uninstall_nvhive
    fi

    detect_os
    detect_python
    detect_installer
    install_nvhive

    printf "\n"
    detect_existing_tools
    run_migration

    printf "\n"
    enable_free_providers

    printf "\n"
    run_quick_test

    print_next_steps
}

main
