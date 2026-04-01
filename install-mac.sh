#!/bin/bash
# =============================================================================
# NVHive — macOS Installer
#
# Install:
#   curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install-mac.sh | bash
#
# What lives in ~/nvh/:
#   ~/nvh/repo/       — the NVHive source code
#   ~/nvh/venv/       — Python virtual environment
#   ~/.hive/          — Config, database, API keys
# =============================================================================

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; R='\033[0;31m'; D='\033[0;90m'; N='\033[0m'

NVH_HOME="${NVH_HOME:-$HOME/nvh}"
NVH_VENV="$NVH_HOME/venv"
NVH_REPO="$NVH_HOME/repo"

echo ""
echo -e "${G}╔══════════════════════════════════════╗${N}"
echo -e "${G}║    NVHive — macOS Quick Install      ║${N}"
echo -e "${G}╚══════════════════════════════════════╝${N}"
echo ""

# ---------------------------------------------------------------------------
# Check for Homebrew
# ---------------------------------------------------------------------------
if ! command -v brew &>/dev/null; then
    echo -e "${Y}Homebrew not found. Installing...${N}"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon
    if [ -f "/opt/homebrew/bin/brew" ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi
echo -e "${D}Homebrew: $(brew --version | head -1)${N}"

# ---------------------------------------------------------------------------
# Find or install Python 3.12+
# ---------------------------------------------------------------------------
find_python() {
    for py in python3.12 python3.13 python3.11; do
        if command -v "$py" &>/dev/null; then
            echo "$py"; return 0
        fi
    done
    # Homebrew locations
    for loc in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
        if [ -x "$loc" ]; then echo "$loc"; return 0; fi
    done
    return 1
}

PYTHON=$(find_python 2>/dev/null) || {
    echo -e "${Y}Python 3.12 not found — installing via Homebrew...${N}"
    brew install python@3.12
    PYTHON=$(find_python) || { echo -e "${R}Python install failed.${N}"; exit 1; }
}
echo -e "${D}Python: $($PYTHON --version 2>&1) [$PYTHON]${N}"

# ---------------------------------------------------------------------------
# Detect Apple Silicon vs Intel, and unified memory
# ---------------------------------------------------------------------------
CHIP=$(uname -m)
APPLE_SILICON=false
MEM_GB=0

if [ "$CHIP" = "arm64" ]; then
    APPLE_SILICON=true
    # Detect chip generation (M1/M2/M3/M4)
    CHIP_NAME=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || system_profiler SPHardwareDataType 2>/dev/null | grep "Chip:" | awk -F': ' '{print $2}' | xargs)
    # Unified memory acts as VRAM for Metal
    MEM_TOTAL=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
    MEM_GB=$(( MEM_TOTAL / 1024 / 1024 / 1024 ))
    echo -e "${G}Apple Silicon detected: ${CHIP_NAME:-M-series} (${MEM_GB}GB unified memory)${N}"
    echo -e "${G}GPU: Metal (Ollama uses Metal acceleration — no CUDA needed)${N}"
else
    echo -e "${Y}Intel Mac detected — CPU mode (Metal not available for Ollama)${N}"
fi

# ---------------------------------------------------------------------------
# Fast path: already installed — heal venv if needed
# ---------------------------------------------------------------------------
if [ -d "$NVH_REPO" ] && [ -d "$NVH_VENV" ]; then
    venv_python="$NVH_VENV/bin/python3"
    if ! [ -f "$venv_python" ] || ! "$venv_python" -c "import sys" 2>/dev/null; then
        echo -e "${Y}Healing Python venv...${N}"
        rm -rf "$NVH_VENV"
        $PYTHON -m venv "$NVH_VENV"
        source "$NVH_VENV/bin/activate"
        pip install -q --upgrade pip 2>/dev/null
        pip install -q -e "$NVH_REPO" 2>/dev/null
    else
        source "$NVH_VENV/bin/activate"
        (cd "$NVH_REPO" && git pull --quiet 2>/dev/null && pip install -q -e . 2>/dev/null) || true
    fi
    export PATH="$NVH_VENV/bin:$PATH"
    echo -e "${G}NVHive ready.${N}"
    echo -e "  Type ${G}nvh${N} to start chatting"
    echo ""
    exit 0
fi

# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------
echo -e "${B}Fresh install — setting up ~/nvh/...${N}"
mkdir -p "$NVH_HOME"

# Clone repo
echo -e "${B}Downloading NVHive...${N}"
if command -v git &>/dev/null; then
    git clone --depth 1 -q https://github.com/thatcooperguy/nvHive.git "$NVH_REPO" 2>/dev/null || {
        mkdir -p "$NVH_REPO"
        curl -sSL https://github.com/thatcooperguy/nvHive/archive/refs/heads/main.tar.gz | tar xz -C "$NVH_REPO" --strip-components=1
    }
else
    mkdir -p "$NVH_REPO"
    curl -sSL https://github.com/thatcooperguy/nvHive/archive/refs/heads/main.tar.gz | tar xz -C "$NVH_REPO" --strip-components=1
fi

# Create venv
echo -e "${B}Creating Python environment...${N}"
$PYTHON -m venv "$NVH_VENV"
source "$NVH_VENV/bin/activate"
pip install -q --upgrade pip 2>/dev/null

# Install
echo -e "${B}Installing NVHive (~60s)...${N}"
pip install -q -e "$NVH_REPO" 2>/dev/null || {
    echo -e "${R}Install failed. Check Python version (need 3.11+).${N}"
    exit 1
}

command -v nvh &>/dev/null || { echo -e "${R}nvh command not found after install.${N}"; exit 1; }
export PATH="$NVH_VENV/bin:$PATH"

# ---------------------------------------------------------------------------
# Auto-config
# ---------------------------------------------------------------------------
HIVE_DIR="$HOME/.hive"
mkdir -p "$HIVE_DIR"
if [ ! -f "$HIVE_DIR/config.yaml" ]; then
    echo -e "${B}Creating auto-config...${N}"
    cat > "$HIVE_DIR/config.yaml" << 'CFGEOF'
version: "1"

defaults:
  mode: ask
  output: text
  stream: true
  max_tokens: 4096
  temperature: 1.0
  show_metadata: true

advisors:
  ollama:
    base_url: http://localhost:11434
    default_model: ollama/nemotron-small
    type: ollama
    enabled: true

  llm7:
    default_model: deepseek-r1-0528
    type: llm7
    enabled: true

  groq:
    api_key: ${GROQ_API_KEY}
    default_model: groq/llama-3.3-70b-versatile
    enabled: false

  openai:
    api_key: ${OPENAI_API_KEY}
    default_model: gpt-4o
    enabled: false

  anthropic:
    api_key: ${ANTHROPIC_API_KEY}
    default_model: claude-sonnet-4-6
    enabled: false

budget:
  daily_limit_usd: 10
  monthly_limit_usd: 50
  hard_stop: true

cache:
  enabled: true
  ttl_seconds: 86400
  max_size: 1000
CFGEOF
    echo -e "${G}Config created: $HIVE_DIR/config.yaml${N}"
fi

# ---------------------------------------------------------------------------
# Add to ~/.zshrc (macOS default shell)
# ---------------------------------------------------------------------------
RC="$HOME/.zshrc"
grep -q "nvh/venv/bin" "$RC" 2>/dev/null || {
    echo "" >> "$RC"
    echo "# NVHive — Multi-LLM Orchestration" >> "$RC"
    echo "export PATH=\"$NVH_VENV/bin:\$PATH\"" >> "$RC"
}

# ---------------------------------------------------------------------------
# Install Ollama (Apple Silicon = Metal acceleration; Intel = CPU)
# ---------------------------------------------------------------------------
if [ "$APPLE_SILICON" = "true" ]; then
    if ! command -v ollama &>/dev/null; then
        echo -e "${B}Installing Ollama (Metal acceleration for Apple Silicon)...${N}"
        brew install ollama 2>/dev/null || {
            echo -e "${Y}Homebrew Ollama failed, trying direct download...${N}"
            curl -sSL https://ollama.com/download/Ollama-darwin.zip -o /tmp/ollama.zip
            unzip -q /tmp/ollama.zip -d /tmp/ollama-app
            # Move the binary to NVH_HOME
            cp /tmp/ollama-app/Ollama.app/Contents/Resources/ollama "$NVH_HOME/ollama"
            chmod +x "$NVH_HOME/ollama"
        }
    fi

    # Start Ollama service
    OLLAMA_BIN=$(command -v ollama 2>/dev/null || echo "$NVH_HOME/ollama")
    if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
        echo -e "${B}Starting Ollama...${N}"
        if command -v ollama &>/dev/null; then
            # brew install creates a service
            brew services start ollama 2>/dev/null || ollama serve &>/dev/null &
        else
            OLLAMA_MODELS="$NVH_HOME/models" "$OLLAMA_BIN" serve &>/dev/null &
        fi
        sleep 3
    fi

    # Pick model based on unified memory
    # Apple Silicon unified memory: more is available than NVIDIA VRAM
    # because Metal can address a larger portion of system RAM
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        if [ "$MEM_GB" -ge 64 ]; then   MODEL="nemotron"       # 70B Q4 fits in 64GB
        elif [ "$MEM_GB" -ge 32 ]; then MODEL="nemotron"       # 7B comfortably
        elif [ "$MEM_GB" -ge 16 ]; then MODEL="nemotron-small" # 8B
        else                             MODEL="nemotron-mini"; fi

        if ! ollama list 2>/dev/null | grep -q "$MODEL"; then
            echo -e "${B}Pulling $MODEL in background (you can start using nvh now)...${N}"
            ollama pull "$MODEL" &>/dev/null &
        else
            echo -e "${G}Model $MODEL ready.${N}"
        fi
    fi
else
    echo -e "${Y}Intel Mac: skipping Ollama install (no Metal acceleration).${N}"
    echo -e "${Y}Use cloud providers: nvh setup${N}"
fi

echo ""
echo -e "${G}╔══════════════════════════════════════╗${N}"
echo -e "${G}║       NVHive is ready!               ║${N}"
echo -e "${G}╚══════════════════════════════════════╝${N}"
echo ""
echo -e "  ${G}nvh${N}            Start chatting"
echo -e "  ${G}nvh setup${N}      Add more free AI providers"
echo -e "  ${G}nvh status${N}     System overview"
echo ""
echo -e "  ${D}Install dir: ~/nvh/${N}"
echo -e "  ${D}Config: ~/.hive/config.yaml${N}"
echo ""
echo -e "${D}(Restart your terminal or run: source ~/.zshrc)${N}"
echo ""
