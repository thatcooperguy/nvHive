#!/bin/bash
# =============================================================================
# NVHive — One-Line Installer
#
# Works on any Linux machine with NO root access.
# Everything lives in ~/nvh/ (persists on mounted home directories).
#
# Install:
#   curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash
#
# On new VM sessions (same mounted home dir):
#   Just type 'nvh' — the installer auto-heals the venv if Python moved.
#
# What lives in ~/nvh/:
#   ~/nvh/repo/       — the NVHive source code
#   ~/nvh/venv/       — Python virtual environment
#   ~/nvh/ollama      — Ollama binary (for local AI)
#   ~/nvh/models/     — Downloaded AI models (can be large)
#   ~/.hive/          — Config, database, API keys
# =============================================================================

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; R='\033[0;31m'; D='\033[0;90m'; N='\033[0m'

NVH_HOME="${NVH_HOME:-$HOME/nvh}"
NVH_VENV="$NVH_HOME/venv"
NVH_REPO="$NVH_HOME/repo"

echo ""
echo -e "${G}╔══════════════════════════════════════╗${N}"
echo -e "${G}║       NVHive — Quick Install         ║${N}"
echo -e "${G}║   No root needed. Installs to ~/nvh  ║${N}"
echo -e "${G}╚══════════════════════════════════════╝${N}"
echo ""

# ---------------------------------------------------------------------------
# Find Python — check common locations since the VM may have it anywhere
# ---------------------------------------------------------------------------
find_python() {
    for py in python3.12 python3.11 python3.10 python3; do
        if command -v "$py" &>/dev/null; then
            echo "$py"
            return 0
        fi
    done
    # Check common non-PATH locations
    for loc in /usr/bin/python3 /usr/local/bin/python3 /opt/conda/bin/python3; do
        if [ -x "$loc" ]; then
            echo "$loc"
            return 0
        fi
    done
    return 1
}

PYTHON=$(find_python) || {
    echo -e "${R}Python 3 not found anywhere.${N}"
    exit 1
}
echo -e "${D}Python: $($PYTHON --version 2>&1) [$PYTHON]${N}"

# ---------------------------------------------------------------------------
# Detect GPU
# ---------------------------------------------------------------------------
GPU_NAME=""; VRAM_GB=0
if command -v nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | xargs)
    VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | xargs)
    VRAM_GB=$(( ${VRAM_MB:-0} / 1024 ))
    [ -n "$GPU_NAME" ] && echo -e "${G}GPU: $GPU_NAME (${VRAM_GB}GB VRAM)${N}"
fi
[ -z "$GPU_NAME" ] && echo -e "${Y}No NVIDIA GPU detected — CPU mode${N}"

# --- Detect Linux Desktop ---
CLOUD_DETECTED=false
if [ -f "/etc/nvidia/cloud.conf" ] || [ -f "/etc/nvidia/grid.conf" ] || \
   [ -n "${CLOUD_SESSION_ID:-}" ] || [ -n "${NVIDIA_CLOUD_SESSION:-}" ]; then
    CLOUD_DETECTED=true
    echo -e "${G}Linux Desktop session detected!${N}"
fi
# Also check GPU name for cloud virtual GPUs
if echo "${GPU_NAME:-}" | grep -qi "grid\|virtual\|tesla t10"; then
    CLOUD_DETECTED=true
fi
if [ "$CLOUD_DETECTED" = "true" ]; then
    echo -e "${G}  Optimizing for cloud environment...${N}"
fi

# ---------------------------------------------------------------------------
# Fast path: already installed — just heal the venv if needed
# ---------------------------------------------------------------------------
heal_venv() {
    # Venvs break when the system Python moves (new VM, different path).
    # Fix: recreate the venv using the current Python, then reinstall.
    local venv_python="$NVH_VENV/bin/python3"

    # Test if the existing venv works
    if [ -f "$venv_python" ] && "$venv_python" -c "import sys" 2>/dev/null; then
        return 0  # venv is healthy
    fi

    echo -e "${Y}Healing Python venv (new VM detected)...${N}"

    # Save installed packages list if possible
    local pkg_list=""
    if [ -f "$NVH_VENV/bin/pip" ] && "$NVH_VENV/bin/pip" --version &>/dev/null 2>&1; then
        pkg_list=$("$NVH_VENV/bin/pip" freeze 2>/dev/null || true)
    fi

    # Recreate venv with current Python (preserves site-packages if possible)
    $PYTHON -m venv --clear "$NVH_VENV" 2>/dev/null || {
        # --clear failed, nuke and recreate
        rm -rf "$NVH_VENV"
        $PYTHON -m venv "$NVH_VENV"
    }

    # Reinstall nvhive
    source "$NVH_VENV/bin/activate"
    pip install -q --upgrade pip 2>/dev/null
    if [ -d "$NVH_REPO" ]; then
        pip install -q -e "$NVH_REPO" 2>/dev/null
    fi

    echo -e "${G}Venv healed.${N}"
    return 0
}

if [ -d "$NVH_REPO" ] && [ -d "$NVH_VENV" ]; then
    # Existing install found — heal if needed, then activate
    heal_venv
    source "$NVH_VENV/bin/activate"

    # Quick git pull for updates (non-blocking)
    if [ -d "$NVH_REPO/.git" ] && command -v git &>/dev/null; then
        (cd "$NVH_REPO" && git pull --quiet 2>/dev/null && pip install -q -e . 2>/dev/null) || true
    fi

    # Verify nvh command works
    if command -v nvh &>/dev/null; then
        echo -e "${G}NVHive ready.${N}"
    else
        echo -e "${Y}Reinstalling...${N}"
        pip install -q -e "$NVH_REPO" 2>/dev/null
    fi

    # Ensure Ollama is running
    if [ -f "$NVH_HOME/ollama" ] && ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
        echo -e "${D}Starting Ollama...${N}"
        OLLAMA_MODELS="$NVH_HOME/models" "$NVH_HOME/ollama" serve &>/dev/null &
        sleep 2
    fi

    export PATH="$NVH_VENV/bin:$PATH"

    # Ensure .bashrc has our PATH
    RC="$HOME/.bashrc"; [ -f "$HOME/.zshrc" ] && RC="$HOME/.zshrc"
    grep -q "nvh/venv/bin" "$RC" 2>/dev/null || {
        echo "" >> "$RC"
        echo "# NVHive" >> "$RC"
        echo "export PATH=\"$NVH_VENV/bin:\$PATH\"" >> "$RC"
        echo "[ -f \"$NVH_HOME/ollama\" ] && ! curl -sf http://localhost:11434/api/tags &>/dev/null && OLLAMA_MODELS=\"$NVH_HOME/models\" \"$NVH_HOME/ollama\" serve &>/dev/null &" >> "$RC"
    }

    echo ""
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
        echo -e "${R}Git clone failed. Trying tarball...${N}"
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
    echo -e "${R}Install failed. Check Python version (need 3.12+).${N}"
    exit 1
}

# Verify
command -v nvh &>/dev/null || {
    echo -e "${R}nvh command not found after install.${N}"
    exit 1
}

export PATH="$NVH_VENV/bin:$PATH"

# ---------------------------------------------------------------------------
# Auto-create config with zero-signup providers enabled
# ---------------------------------------------------------------------------
HIVE_DIR="$HOME/.hive"
mkdir -p "$HIVE_DIR"
if [ ! -f "$HIVE_DIR/config.yaml" ]; then
    echo -e "${B}Creating auto-config (Ollama + LLM7 enabled by default)...${N}"
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

  google:
    api_key: ${GOOGLE_API_KEY}
    default_model: gemini/gemini-2.5-flash
    enabled: false

  github:
    api_key: ${GITHUB_TOKEN}
    default_model: gpt-4o-mini
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

# Set up .bashrc
RC="$HOME/.bashrc"; [ -f "$HOME/.zshrc" ] && RC="$HOME/.zshrc"
grep -q "nvh/venv/bin" "$RC" 2>/dev/null || {
    echo "" >> "$RC"
    echo "# NVHive — Multi-LLM Orchestration" >> "$RC"
    echo "export PATH=\"$NVH_VENV/bin:\$PATH\"" >> "$RC"
    echo "# Auto-start Ollama on login if installed" >> "$RC"
    echo "[ -f \"$NVH_HOME/ollama\" ] && ! curl -sf http://localhost:11434/api/tags &>/dev/null 2>&1 && OLLAMA_MODELS=\"$NVH_HOME/models\" \"$NVH_HOME/ollama\" serve &>/dev/null &" >> "$RC"
}

# ---------------------------------------------------------------------------
# Set up Ollama (local AI) — only if we have a GPU
# ---------------------------------------------------------------------------
if [ -n "$GPU_NAME" ]; then
    OLLAMA_BIN="$NVH_HOME/ollama"
    if [ ! -f "$OLLAMA_BIN" ]; then
        echo -e "${B}Installing Ollama (local AI)...${N}"
        curl -sSL https://ollama.com/download/ollama-linux-amd64 -o "$OLLAMA_BIN" 2>/dev/null
        chmod +x "$OLLAMA_BIN"
    fi

    # Start Ollama
    if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
        echo -e "${B}Starting Ollama...${N}"
        mkdir -p "$NVH_HOME/models"
        OLLAMA_MODELS="$NVH_HOME/models" "$OLLAMA_BIN" serve &>/dev/null &
        sleep 3
    fi

    # Pick model based on VRAM
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        if [ "$VRAM_GB" -ge 80 ]; then MODEL="nemotron:120b"
        elif [ "$VRAM_GB" -ge 24 ]; then MODEL="nemotron"
        elif [ "$VRAM_GB" -ge 6 ]; then MODEL="nemotron-small"
        else MODEL="nemotron-mini"; fi

        if ! "$OLLAMA_BIN" list 2>/dev/null | grep -q "$MODEL"; then
            echo -e "${B}Pulling $MODEL in background (you can start using nvh now)...${N}"
            OLLAMA_MODELS="$NVH_HOME/models" "$OLLAMA_BIN" pull "$MODEL" &>/dev/null &
        else
            echo -e "${G}Model $MODEL ready.${N}"
        fi
    fi
fi

echo ""
echo -e "${G}╔══════════════════════════════════════╗${N}"
echo -e "${G}║       NVHive is ready!               ║${N}"
echo -e "${G}╚══════════════════════════════════════╝${N}"
echo ""
echo -e "  ${G}nvh${N}            Start chatting (works immediately)"
echo -e "  ${G}nvh setup${N}      Add more free AI providers"
echo -e "  ${G}nvh bench${N}      Benchmark your GPU"
echo -e "  ${G}nvh status${N}     System overview"
echo -e "  ${G}nvh update${N}     Pull latest version"
echo ""
echo -e "  ${D}Install dir: ~/nvh/${N}"
echo -e "  ${D}Config: ~/.hive/config.yaml${N}"
echo -e "  ${D}On reconnect: just type 'nvh'${N}"
echo ""
echo -e "  ${G}Start now:${N}"
echo -e "  ${G}  nvh${N}"
echo ""
# Make nvh available in the CURRENT shell (not just future ones)
echo -e "${D}(If 'nvh' is not found, run: source ~/.bashrc)${N}"
echo ""
