#!/bin/bash
# =============================================================================
# NVHive — macOS Installer
#
# Install:
#   curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install-mac.sh | bash
#
# Everything lives in NVH_HOME (default ~/.nvh), exactly like install.sh:
#   $NVH_HOME/repo/       — the NVHive source code
#   $NVH_HOME/venv/       — Python virtual environment
#   $NVH_HOME/bin/        — rootless Ollama fallback
#   $NVH_HOME/models/     — downloaded AI models
#   $NVH_HOME/config/     — config.yaml, API keys (.env)   ($NVH_CONFIG / HIVE_CONFIG_HOME)
#   $NVH_HOME/state/      — the SQLite state database
# Set NVH_HOME before running to install somewhere else (an external drive);
# the installer writes $NVH_HOME/nvh-env.sh and sources it from ~/.zshrc so
# every later `nvh` finds the same home.
# =============================================================================

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; R='\033[0;31m'; D='\033[0;90m'; N='\033[0m'

NVH_HOME="${NVH_HOME:-${NVHIVE_HOME:-$HOME/.nvh}}"
NVH_VENV="$NVH_HOME/venv"
NVH_REPO="$NVH_HOME/repo"
NVH_BIN="$NVH_HOME/bin"
NVH_MODELS="$NVH_HOME/models"
OLLAMA_MODELS="${OLLAMA_MODELS:-$NVH_MODELS/ollama}"
# Config dir: NVH_CONFIG is the primary override, HIVE_CONFIG_HOME the legacy alias.
NVH_CONFIG="${NVH_CONFIG:-${HIVE_CONFIG_HOME:-$NVH_HOME/config}}"
HIVE_CONFIG_HOME="$NVH_CONFIG"
export NVH_HOME NVH_BIN NVH_MODELS OLLAMA_MODELS NVH_CONFIG HIVE_CONFIG_HOME

write_nvh_env() {
    # Persist literal paths safely for both Bash and the macOS default Zsh.
    # Values containing spaces, dollar signs or backticks must not become code.
    {
        printf 'export NVH_HOME=%q\n' "$NVH_HOME"
        printf 'export NVH_VENV=%q\n' "$NVH_VENV"
        printf 'export NVH_BIN=%q\n' "$NVH_BIN"
        printf 'export NVH_MODELS=%q\n' "$NVH_MODELS"
        printf 'export OLLAMA_MODELS=%q\n' "$OLLAMA_MODELS"
        printf 'export NVH_CONFIG=%q\n' "$NVH_CONFIG"
        printf 'export HIVE_CONFIG_HOME=%q\n' "$HIVE_CONFIG_HOME"
        printf 'export PATH="$NVH_VENV/bin:$NVH_BIN:$PATH"\n'
    } > "$NVH_HOME/nvh-env.sh"
    chmod 600 "$NVH_HOME/nvh-env.sh"
}

persist_nvh_environment() {
    mkdir -p "$NVH_HOME" "$NVH_BIN" "$NVH_MODELS" "$OLLAMA_MODELS" "$NVH_CONFIG"
    write_nvh_env
    local rc="$HOME/.zshrc" env_path hook
    printf -v env_path '%q' "$NVH_HOME/nvh-env.sh"
    hook="[ -f $env_path ] && source $env_path"
    if ! grep -qxF -- "$hook" "$rc" 2>/dev/null; then
        printf '\n# NVHive — Multi-LLM Orchestration (NVH_HOME, config dir, PATH)\n%s\n' "$hook" >> "$rc"
    fi
}

write_default_config() {
    # Render nvh's own default config.yaml into $1 with the venv Python:
    # nvh.config.settings.generate_default_config() builds the advisor
    # stanzas from the provider spec table and fills the Ollama default from
    # the tier table, so this is the file `nvh config init` writes (install.sh
    # does the same). The render goes to a sibling temp file and is moved into
    # place only when it is non-blank, so a failure leaves nothing behind; its
    # stderr reaches the terminal. Returns 1 on failure after one honest line
    # naming `nvh config init`; the caller continues, it is not fatal.
    local cfg="$1"
    local tmp="$cfg.tmp.$$"
    if ! "$NVH_VENV/bin/python" -c 'from nvh.config.settings import generate_default_config; import sys; sys.stdout.write(generate_default_config())' >"$tmp" \
        || ! grep -q '[^[:space:]]' "$tmp"; then
        rm -f "$tmp"
        echo -e "${Y}Could not render the default config from the installed nvh; run 'nvh config init' to create $cfg.${N}"
        return 1
    fi
    mv -f "$tmp" "$cfg"
}

set_config_ollama_model() {
    # set_config_ollama_model <config.yaml> <ollama tag> [enable]
    # Rewrite the whole `default_model: ollama/...` line with the shell's tier
    # pick (the same whole-line replacement install.sh makes), so the pick made
    # from this Mac's unified pool wins over the template's own Ollama line; an
    # empty pick leaves that line alone. With a third argument of 1 the Ollama
    # stanza, which nvh's template writes disabled, is switched on as well (the
    # flip `nvh config init` makes when a daemon answers): the caller passes it
    # on Apple Silicon, where this install sets Ollama up and pulls the pick,
    # tier pick or not. The rewrite goes through a sibling temp file and
    # os.replace, so a failure leaves the file as it was.
    local cfg="$1"
    local model="$2"
    local enable="${3:-0}"
    if [ -z "$model" ] && [ "$enable" != "1" ]; then
        return 0
    fi
    CFG="$cfg" MODEL="$model" ENABLE="$enable" "$NVH_VENV/bin/python" - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["CFG"])
model = os.environ["MODEL"]
default_re = re.compile(r'^(\s*)default_model:\s*[\'"]?ollama/')
out_lines = []
for line in path.read_text(encoding="utf-8").split("\n"):
    m = default_re.match(line)
    out_lines.append(f'{m.group(1)}default_model: "ollama/{model}"' if m and model else line)
updated = "\n".join(out_lines)
if os.environ.get("ENABLE") == "1":
    updated = updated.replace(
        "    type: ollama\n    enabled: false",
        "    type: ollama\n    enabled: true",
    )
tmp = path.with_name(f"{path.name}.rewrite.{os.getpid()}")
tmp.write_text(updated, encoding="utf-8")
os.replace(tmp, path)
PY
}

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
        # A fallback tarball install has no Git metadata; reinstall its local
        # package without pretending that an upstream update was performed.
        if [ -d "$NVH_REPO/.git" ] || [ -f "$NVH_REPO/.git" ]; then
            if ! (cd "$NVH_REPO" && git pull --quiet); then
                echo -e "${R}NVHive update did not complete. Resolve the Git error and retry.${N}"
                exit 1
            fi
        fi
        if ! pip install -q -e "$NVH_REPO"; then
            echo -e "${R}NVHive package update failed. Repair the Python environment and retry.${N}"
            exit 1
        fi
    fi
    export PATH="$NVH_VENV/bin:$PATH"
    [ -x "$NVH_VENV/bin/nvh" ] || { echo -e "${R}nvh command not found in the updated environment.${N}"; exit 1; }
    persist_nvh_environment
    echo -e "${G}NVHive ready.${N}"
    echo -e "  Type ${G}nvh${N} to start chatting"
    echo ""
    exit 0
fi

# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------
echo -e "${B}Fresh install — setting up $NVH_HOME ...${N}"
mkdir -p "$NVH_HOME" "$NVH_BIN" "$NVH_MODELS" "$OLLAMA_MODELS" "$NVH_CONFIG"
write_nvh_env

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
# Resolve the local model from the installed package
# ---------------------------------------------------------------------------
# nvh.core.local_models is the one VRAM-tier -> Ollama-tag table every ladder
# reads. On Apple Silicon the unified pool (hw.memsize) is passed in so the
# table takes its OS reserve off it exactly as it does for a GB10; an Intel
# Mac has no Metal Ollama and gets the table's CPU tier. Nothing is typed
# here — until 0.42 this file carried its own nemotron / llama3.1:8b /
# nemotron-mini ladder.
DEFAULT_OLLAMA_MODEL=""
if [ "$APPLE_SILICON" = "true" ] && [ "$MEM_GB" -gt 0 ]; then
    DEFAULT_OLLAMA_MODEL="$("$NVH_VENV/bin/python" -m nvh.cli.main models tiers --pick chat --unified-gb "$MEM_GB" 2>/dev/null | tail -n 1)" || DEFAULT_OLLAMA_MODEL=""
else
    DEFAULT_OLLAMA_MODEL="$("$NVH_VENV/bin/python" -m nvh.cli.main models tiers --pick chat --vram-gb 0 2>/dev/null | tail -n 1)" || DEFAULT_OLLAMA_MODEL=""
fi
if [ -n "$DEFAULT_OLLAMA_MODEL" ]; then
    echo -e "${D}Local model for this Mac: $DEFAULT_OLLAMA_MODEL (nvh models tiers --pick chat)${N}"
else
    echo -e "${Y}Could not read the local model table from the installed nvh; pick a model later with 'nvh models pull --recommended'.${N}"
fi

# ---------------------------------------------------------------------------
# Auto-config
# ---------------------------------------------------------------------------
# nvh owns the one default-config template: `nvh config init` writes
# generate_default_config(), whose advisor stanzas are rendered from the
# provider facts table. The installer writes that same file and then lets
# this Mac's tier pick override the Ollama line; no provider or model is
# typed here. If the installed nvh cannot render it, nothing is written and
# `nvh config init` creates it later.
mkdir -p "$NVH_CONFIG"
if [ ! -f "$NVH_CONFIG/config.yaml" ]; then
    echo -e "${B}Creating auto-config...${N}"
    # The Ollama stanza is switched on only where this install sets Ollama up
    # below (Apple Silicon); an Intel Mac keeps it as nvh renders it.
    OLLAMA_STANZA_ENABLE=0
    if [ "$APPLE_SILICON" = "true" ]; then OLLAMA_STANZA_ENABLE=1; fi
    if write_default_config "$NVH_CONFIG/config.yaml"; then
        if set_config_ollama_model "$NVH_CONFIG/config.yaml" "$DEFAULT_OLLAMA_MODEL" "$OLLAMA_STANZA_ENABLE"; then
            echo -e "${G}Config created: $NVH_CONFIG/config.yaml${N}"
        else
            echo -e "${Y}Config created as nvh renders it: $NVH_CONFIG/config.yaml (the Ollama stanza could not be adjusted; 'nvh config init --force' redoes it once Ollama is up).${N}"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Add to ~/.zshrc (macOS default shell)
# ---------------------------------------------------------------------------
persist_nvh_environment

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
            # Move the binary to $NVH_HOME/bin (the layout's rootless launcher dir)
            mkdir -p "$NVH_BIN"
            cp /tmp/ollama-app/Ollama.app/Contents/Resources/ollama "$NVH_BIN/ollama"
            chmod +x "$NVH_BIN/ollama"
        }
    fi

    # Start Ollama service
    OLLAMA_BIN=$(command -v ollama 2>/dev/null || echo "$NVH_BIN/ollama")
    if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
        echo -e "${B}Starting Ollama...${N}"
        if command -v ollama &>/dev/null; then
            # brew install creates a service
            brew services start ollama 2>/dev/null || ollama serve &>/dev/null &
        else
            "$OLLAMA_BIN" serve &>/dev/null &   # OLLAMA_MODELS is exported above ($NVH_HOME/models/ollama)
        fi
        sleep 3
    fi

    # The model is the tier table's chat pick for this pool (resolved above
    # via `nvh models tiers --pick chat --unified-gb $MEM_GB`).
    MODEL="$DEFAULT_OLLAMA_MODEL"
    if [ -n "$MODEL" ] && curl -sf http://localhost:11434/api/tags &>/dev/null; then
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
echo -e "  ${D}Install dir (NVH_HOME): $NVH_HOME${N}"
echo -e "  ${D}Config: $NVH_CONFIG/config.yaml${N}"
echo ""
echo -e "${D}(Restart your terminal or run: source ~/.zshrc)${N}"
echo ""
