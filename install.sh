#!/bin/bash
# =============================================================================
# NVHive — One-Line Installer
#
# Works on any Linux machine with NO root access.
# Everything lives in NVH_HOME. On cloud desktops, set NVH_HOME to the
# mounted file volume that survives every session before running this script:
#
#   export NVH_HOME=/mnt/persist/nvhive
#
# Install:
#   curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash
#
# On new VM sessions (same mounted home dir):
#   Just type 'nvh' — the installer auto-heals the venv if Python moved.
#
# What lives in NVH_HOME:
#   $NVH_HOME/repo/       — NVHive source code
#   $NVH_HOME/venv/       — Python virtual environment
#   $NVH_HOME/bin/        — rootless launchers and Ollama
#   $NVH_HOME/models/     — downloaded AI models (can be large)
#   $NVH_HOME/config/     — config, database, API keys
#   $NVH_HOME/cache/      — pip, torch, Hugging Face, temp caches
# =============================================================================

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; B='\033[0;34m'; R='\033[0;31m'; D='\033[0;90m'; N='\033[0m'

free_gb_for_path() {
    df -Pk "$1" 2>/dev/null | awk 'NR==2 {printf "%d", $4 / 1048576}'
}

score_nvh_home_candidate() {
    local base="${1%/}"
    [ -n "$base" ] || return 1
    [ -d "$base" ] || return 1
    [ -w "$base" ] || return 1

    local name home free_gb score
    name="$(basename "$base")"
    home="$base/nvhive"
    case "$name" in
        nvh|nvhive|.nvh) home="$base" ;;
    esac

    score=0
    case "$base" in
        "$HOME"|"$HOME/"*) score=$((score - 15)) ;;
        /mnt/*|/media/*|/workspace*|/data*|/persistent*|/storage*) score=$((score + 45)) ;;
    esac
    case "$base" in
        *persist*|*Persist*|*workspace*|*Workspace*|*project*|*Project*|*data*|*Data*)
            score=$((score + 20))
            ;;
        *tmp*|*cache*|*Cache*)
            score=$((score - 40))
            ;;
    esac
    free_gb="$(free_gb_for_path "$base")"
    free_gb="${free_gb:-0}"
    if [ "$free_gb" -ge 100 ]; then
        score=$((score + 35))
    elif [ "$free_gb" -ge 50 ]; then
        score=$((score + 30))
    elif [ "$free_gb" -ge 20 ]; then
        score=$((score + 20))
    elif [ "$free_gb" -ge 10 ]; then
        score=$((score + 8))
    else
        score=$((score - 15))
    fi
    if [ -f "$home/nvh-env.sh" ] || [ -d "$home/repo" ] || [ -d "$home/models" ]; then
        score=$((score + 30))
    fi
    printf '%s|%s\n' "$score" "$home"
}

detect_nvh_home() {
    local roots=()
    local env_name env_value root child scored score home best_score best_home

    if [ -d "$HOME/nvh/repo" ] && [ ! -d "$HOME/.nvh/repo" ]; then
        printf '%s\n' "$HOME/nvh"
        return 0
    fi

    for env_name in NVH_MOUNT PERSISTENT_HOME PERSISTENT_DIR PERSISTENT_STORAGE WORKSPACE PROJECTS PROJECT_HOME DATA_DIR; do
        env_value="${!env_name:-}"
        [ -n "$env_value" ] && roots+=("$env_value")
    done
    roots+=("/mnt" "/media/${USER:-}" "/workspace" "/data" "/persistent" "/storage")

    best_score=-999
    best_home=""
    for root in "${roots[@]}"; do
        [ -n "$root" ] || continue
        if scored="$(score_nvh_home_candidate "$root")"; then
            score="${scored%%|*}"
            home="${scored#*|}"
            if [ "$score" -gt "$best_score" ]; then
                best_score="$score"
                best_home="$home"
            fi
        fi
        [ -d "$root" ] || continue
        for child in "$root"/*; do
            [ -d "$child" ] || continue
            if scored="$(score_nvh_home_candidate "$child")"; then
                score="${scored%%|*}"
                home="${scored#*|}"
                if [ "$score" -gt "$best_score" ]; then
                    best_score="$score"
                    best_home="$home"
                fi
            fi
        done
    done

    if [ -n "$best_home" ] && [ "$best_score" -ge 55 ]; then
        printf '%s\n' "$best_home"
        return 0
    fi
    return 1
}

if [ -z "${NVH_HOME:-}" ]; then
    if NVH_HOME="$(detect_nvh_home)"; then
        NVH_HOME_AUTOPILOT=true
    else
        NVH_HOME="$HOME/.nvh"
        NVH_HOME_AUTOPILOT=false
    fi
    NVH_HOME_CONFIGURED=false
else
    NVH_HOME_CONFIGURED=true
    NVH_HOME_AUTOPILOT=false
fi
NVH_VENV="$NVH_HOME/venv"
NVH_REPO="$NVH_HOME/repo"
NVH_BIN="$NVH_HOME/bin"
NVH_MODELS="$NVH_HOME/models"
NVH_CACHE="$NVH_HOME/cache"
NVH_LOGS="$NVH_HOME/logs"
NVH_STUDIO_HOME="${NVH_STUDIO_HOME:-$NVH_HOME/studio}"
COMFYUI_HOME="${COMFYUI_HOME:-$NVH_HOME/comfyui}"
OLLAMA_MODELS="${OLLAMA_MODELS:-$NVH_MODELS/ollama}"
HIVE_CONFIG_HOME="${HIVE_CONFIG_HOME:-$NVH_HOME/config}"
export NVH_HOME NVH_BIN NVH_MODELS NVH_CACHE NVH_LOGS NVH_STUDIO_HOME COMFYUI_HOME OLLAMA_MODELS HIVE_CONFIG_HOME
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$NVH_CACHE/xdg}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$NVH_CACHE/pip}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$NVH_CACHE/uv}"
export HF_HOME="${HF_HOME:-$NVH_CACHE/huggingface}"
export HUGGINGFACE_HUB_CACHE="${HUGGINGFACE_HUB_CACHE:-$HF_HOME/hub}"
export TORCH_HOME="${TORCH_HOME:-$NVH_CACHE/torch}"
export TMPDIR="${TMPDIR:-$NVH_CACHE/tmp}"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
export NVH_NO_OS_MOD="${NVH_NO_OS_MOD:-0}"

write_nvh_env() {
cat > "$NVH_HOME/nvh-env.sh" << ENVEOF
export NVH_HOME="$NVH_HOME"
export NVH_VENV="$NVH_VENV"
export NVH_BIN="$NVH_BIN"
export NVH_MODELS="$NVH_MODELS"
export NVH_CACHE="$NVH_CACHE"
export NVH_LOGS="$NVH_LOGS"
export NVH_STUDIO_HOME="$NVH_STUDIO_HOME"
export COMFYUI_HOME="$COMFYUI_HOME"
export OLLAMA_MODELS="$OLLAMA_MODELS"
export HIVE_CONFIG_HOME="$HIVE_CONFIG_HOME"
export XDG_CACHE_HOME="$XDG_CACHE_HOME"
export PIP_CACHE_DIR="$PIP_CACHE_DIR"
export UV_CACHE_DIR="$UV_CACHE_DIR"
export HF_HOME="$HF_HOME"
export HUGGINGFACE_HUB_CACHE="$HUGGINGFACE_HUB_CACHE"
export TORCH_HOME="$TORCH_HOME"
export TMPDIR="$TMPDIR"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
export PATH="$NVH_VENV/bin:$NVH_BIN:\$PATH"
ENVEOF
chmod 600 "$NVH_HOME/nvh-env.sh" 2>/dev/null || true
}

mkdir -p "$NVH_BIN" "$NVH_MODELS" "$OLLAMA_MODELS" "$NVH_CACHE" "$NVH_LOGS" "$NVH_STUDIO_HOME" "$COMFYUI_HOME" "$HIVE_CONFIG_HOME" "$TMPDIR"
write_nvh_env
export PATH="$NVH_VENV/bin:$NVH_BIN:$PATH"

echo ""
echo -e "${G}╔══════════════════════════════════════╗${N}"
echo -e "${G}║       NVHive — Quick Install         ║${N}"
echo -e "${G}║  No root. Installs to NVH_HOME     ║${N}"
echo -e "${G}╚══════════════════════════════════════╝${N}"
echo ""

# ---------------------------------------------------------------------------
# Find Python — check common locations since the VM may have it anywhere
# ---------------------------------------------------------------------------
if [ "$NVH_HOME_CONFIGURED" = "false" ]; then
    if [ "$NVH_HOME_AUTOPILOT" = "true" ]; then
        echo -e "${G}Mount autopilot selected ${NVH_HOME}${N}"
        echo -e "${D}Override anytime with: export NVH_HOME=/path/on/persistent/mount${N}"
    else
        echo -e "${Y}NVH_HOME was not set; using ${G}$NVH_HOME${N}"
        echo -e "${D}For cloud desktops, set NVH_HOME to the mounted persistent file volume before install.${N}"
        echo -e "${D}Example: export NVH_HOME=/mnt/persist/nvhive${N}"
    fi
    echo ""
fi
echo -e "${D}Persistent home: $NVH_HOME${N}"
echo -e "${D}Activate later:  source $NVH_HOME/nvh-env.sh${N}"
echo ""

find_python() {
    for py in python3.12 python3.11 python3.10; do
        if command -v "$py" &>/dev/null; then
            echo "$py"
            return 0
        fi
    done
    # Prefer rootless Python distributions before generic system python3.
    for loc in \
        "$HOME/miniforge3/bin/python" \
        "$HOME/miniconda3/bin/python" \
        "$HOME/mambaforge/bin/python" \
        "$HOME/.conda/bin/python" \
        "$HOME/.local/share/mamba/bin/python" \
        /opt/conda/bin/python3 \
        /opt/conda/bin/python; do
        if [ -x "$loc" ]; then
            echo "$loc"
            return 0
        fi
    done
    for py in python3; do
        if command -v "$py" &>/dev/null; then
            echo "$py"
            return 0
        fi
    done
    # Check common non-PATH system locations last; these often lack ensurepip.
    for loc in /usr/bin/python3 /usr/local/bin/python3; do
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
# Detect active conda/micromamba/venv — if one is active, offer to install
# into it instead of creating a fresh venv at $NVH_HOME/venv. This avoids the
# common case of users pip-installing into their existing env and ending up
# with `nvh` at ~/<env>/bin/nvh, not on PATH unless the env is activated.
# ---------------------------------------------------------------------------
ACTIVE_ENV_KIND=""; ACTIVE_ENV_NAME=""; ACTIVE_ENV_PATH=""
if [ -n "${MAMBA_ROOT_PREFIX:-}" ] && [ -n "${CONDA_PREFIX:-}" ]; then
    ACTIVE_ENV_KIND="micromamba"
    ACTIVE_ENV_NAME="${CONDA_DEFAULT_ENV:-$(basename "$CONDA_PREFIX")}"
    ACTIVE_ENV_PATH="$CONDA_PREFIX"
elif [ -n "${CONDA_PREFIX:-}" ]; then
    ACTIVE_ENV_KIND="conda"
    ACTIVE_ENV_NAME="${CONDA_DEFAULT_ENV:-$(basename "$CONDA_PREFIX")}"
    ACTIVE_ENV_PATH="$CONDA_PREFIX"
elif [ -n "${VIRTUAL_ENV:-}" ]; then
    ACTIVE_ENV_KIND="venv"
    ACTIVE_ENV_NAME="$(basename "$VIRTUAL_ENV")"
    ACTIVE_ENV_PATH="$VIRTUAL_ENV"
fi

USE_ACTIVE_ENV=false
if [ -n "$ACTIVE_ENV_KIND" ] && [ -z "${NVH_FORCE_VENV:-}" ]; then
    echo -e "${Y}Detected active $ACTIVE_ENV_KIND env: ${G}$ACTIVE_ENV_NAME${N}"
    echo -e "${D}  ($ACTIVE_ENV_PATH)${N}"
    if [ "${NVH_USE_ACTIVE_ENV:-0}" = "1" ]; then
        USE_ACTIVE_ENV=true
    elif [ -t 0 ]; then
        read -r -p "  Install into this env instead of $NVH_HOME/venv? [Y/n] " ANSWER
        case "${ANSWER:-Y}" in
            n|N|no|NO) USE_ACTIVE_ENV=false ;;
            *)         USE_ACTIVE_ENV=true ;;
        esac
    else
        echo -e "${D}Non-interactive install will use $NVH_HOME/venv for persistence.${N}"
        echo -e "${D}Set NVH_USE_ACTIVE_ENV=1 to explicitly install into the active env.${N}"
    fi
fi

if [ "$USE_ACTIVE_ENV" = "true" ]; then
    NVH_VENV="$ACTIVE_ENV_PATH"
    PYTHON="$ACTIVE_ENV_PATH/bin/python"
    [ -x "$PYTHON" ] || PYTHON="$ACTIVE_ENV_PATH/bin/python3"
    write_nvh_env
    echo -e "${G}Installing into $ACTIVE_ENV_KIND env '$ACTIVE_ENV_NAME'${N}"
fi

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

download_to_file() {
    local url="$1"
    local target="$2"
    mkdir -p "$(dirname "$target")"
    if command -v curl &>/dev/null; then
        curl -fsSL "$url" -o "$target"
    elif command -v wget &>/dev/null; then
        wget -qO "$target" "$url"
    else
        return 1
    fi
}

env_python_path() {
    for py in "$NVH_VENV/bin/python" "$NVH_VENV/bin/python3"; do
        if [ -x "$py" ]; then
            echo "$py"
            return 0
        fi
    done
    return 1
}

activate_nvh_python_env() {
    if [ -f "$NVH_VENV/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$NVH_VENV/bin/activate"
    else
        export PATH="$NVH_VENV/bin:$PATH"
    fi
}

safe_remove_python_env() {
    local home="${NVH_HOME%/}"
    case "$NVH_VENV" in
        "$home"/venv|"$home"/runtimes/conda/nvhive)
            rm -rf "$NVH_VENV"
            ;;
        *)
            echo -e "${R}Refusing to remove unexpected Python env path: $NVH_VENV${N}"
            return 1
            ;;
    esac
}

find_conda_manager() {
    for exe in \
        "${MAMBA_EXE:-}" \
        "${CONDA_EXE:-}" \
        "$HOME/miniforge3/bin/mamba" \
        "$HOME/miniforge3/bin/conda" \
        "$HOME/miniconda3/bin/conda" \
        "$HOME/mambaforge/bin/mamba" \
        "$HOME/mambaforge/bin/conda" \
        "$HOME/.local/bin/micromamba"; do
        if [ -n "$exe" ] && [ -x "$exe" ]; then
            echo "$exe"
            return 0
        fi
    done
    for cmd in micromamba mamba conda; do
        if command -v "$cmd" &>/dev/null; then
            command -v "$cmd"
            return 0
        fi
    done
    return 1
}

create_managed_python_env() {
    local manager prefix
    manager="$(find_conda_manager)" || return 1
    prefix="$NVH_HOME/runtimes/conda/nvhive"
    mkdir -p "$(dirname "$prefix")"

    if [ ! -x "$prefix/bin/python" ]; then
        echo -e "${Y}System venv is unavailable; creating rootless Python 3.12 env with $(basename "$manager")...${N}"
        "$manager" create -y -p "$prefix" python=3.12 pip >>"$NVH_LOGS/conda-create.log" 2>&1 || {
            echo -e "${Y}Managed Python env creation failed. Log: $NVH_LOGS/conda-create.log${N}"
            return 1
        }
    else
        echo -e "${G}Using existing rootless managed Python env: $prefix${N}"
    fi

    NVH_VENV="$prefix"
    PYTHON="$prefix/bin/python"
    export PATH="$NVH_VENV/bin:$NVH_BIN:$PATH"
    write_nvh_env
    "$PYTHON" -m pip --version >/dev/null 2>&1
}

bootstrap_pip_in_env() {
    local env_python get_pip
    env_python="$(env_python_path)" || return 1
    if "$env_python" -m pip --version >/dev/null 2>&1; then
        return 0
    fi

    get_pip="$NVH_CACHE/bootstrap/get-pip.py"
    if [ ! -s "$get_pip" ]; then
        echo -e "${Y}Python venv has no pip; bootstrapping pip rootlessly...${N}"
        download_to_file "https://bootstrap.pypa.io/get-pip.py" "$get_pip" || return 1
    fi
    "$env_python" "$get_pip" --no-warn-script-location pip setuptools wheel >>"$NVH_LOGS/pip-bootstrap.log" 2>&1 || return 1
    "$env_python" -m pip --version >/dev/null 2>&1
}

create_rootless_venv() {
    local mode="${1:-}"
    local env_python
    mkdir -p "$NVH_LOGS" "$NVH_CACHE/bootstrap"

    if [ "$mode" = "--clear" ]; then
        safe_remove_python_env || return 1
    fi

    if "$PYTHON" -m venv "$NVH_VENV" >"$NVH_LOGS/venv-create.log" 2>&1; then
        if bootstrap_pip_in_env; then
            env_python="$(env_python_path)"
            "$env_python" -m pip install -q --upgrade pip 2>/dev/null || true
            activate_nvh_python_env
            write_nvh_env
            return 0
        fi
    fi

    echo -e "${Y}System Python could not create a pip-ready venv without OS packages.${N}"
    safe_remove_python_env || return 1
    if "$PYTHON" -m venv --without-pip "$NVH_VENV" >>"$NVH_LOGS/venv-create.log" 2>&1; then
        if bootstrap_pip_in_env; then
            env_python="$(env_python_path)"
            "$env_python" -m pip install -q --upgrade pip 2>/dev/null || true
            activate_nvh_python_env
            write_nvh_env
            return 0
        fi
    fi

    safe_remove_python_env || true
    if create_managed_python_env; then
        activate_nvh_python_env
        return 0
    fi

    echo -e "${R}Could not create a rootless Python environment.${N}"
    echo -e "${D}Tried: python venv, venv without pip + get-pip, and conda/mamba fallback.${N}"
    echo -e "${D}Logs: $NVH_LOGS/venv-create.log $NVH_LOGS/pip-bootstrap.log $NVH_LOGS/conda-create.log${N}"
    return 1
}

# ---------------------------------------------------------------------------
# Fast path: already installed — just heal the venv if needed
# ---------------------------------------------------------------------------
heal_venv() {
    # Venvs break when the system Python moves (new VM, different path).
    # Fix: recreate the venv using the current Python, then reinstall.
    local venv_python

    # Test if the existing venv works
    if venv_python="$(env_python_path)" && "$venv_python" -c "import sys" 2>/dev/null; then
        return 0  # venv is healthy
    fi

    echo -e "${Y}Healing Python venv (new VM detected)...${N}"

    # Save installed packages list if possible
    local pkg_list=""
    if [ -f "$NVH_VENV/bin/pip" ] && "$NVH_VENV/bin/pip" --version &>/dev/null 2>&1; then
        pkg_list=$("$NVH_VENV/bin/pip" freeze 2>/dev/null || true)
    fi

    # Recreate without root. Handles Debian/Ubuntu Python builds missing ensurepip.
    create_rootless_venv --clear || return 1

    # Reinstall nvhive
    activate_nvh_python_env
    pip install -q --upgrade pip 2>/dev/null
    if [ -d "$NVH_REPO" ]; then
        pip install -q -e "$NVH_REPO[serve,nvidia]" 2>"$NVH_LOGS/pip-install.log"
    fi

    echo -e "${G}Venv healed.${N}"
    return 0
}

if [ -d "$NVH_REPO" ] && [ -d "$NVH_VENV" ]; then
    # Existing install found — heal if needed, then activate
    heal_venv
    activate_nvh_python_env

    # Quick git pull for updates (non-blocking)
    if [ -d "$NVH_REPO/.git" ] && command -v git &>/dev/null; then
        (cd "$NVH_REPO" && git pull --quiet 2>/dev/null && pip install -q -e ".[serve,nvidia]" 2>"$NVH_LOGS/pip-install.log") || true
    fi

    # Verify nvh command works
    if command -v nvh &>/dev/null; then
        echo -e "${G}NVHive ready.${N}"
    else
        echo -e "${Y}Reinstalling...${N}"
        pip install -q -e "$NVH_REPO[serve,nvidia]" 2>"$NVH_LOGS/pip-install.log"
    fi

    # Ensure Ollama is running
    OLLAMA_BIN="$NVH_BIN/ollama"
    [ -x "$NVH_HOME/ollama" ] && [ ! -x "$OLLAMA_BIN" ] && OLLAMA_BIN="$NVH_HOME/ollama"
    if [ -x "$OLLAMA_BIN" ] && ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
        echo -e "${D}Starting Ollama...${N}"
        OLLAMA_MODELS="$OLLAMA_MODELS" "$OLLAMA_BIN" serve &>/dev/null &
        sleep 2
    fi

    export PATH="$NVH_VENV/bin:$NVH_BIN:$PATH"

    # Ensure .bashrc has our PATH
    RC="$HOME/.bashrc"; [ -f "$HOME/.zshrc" ] && RC="$HOME/.zshrc"
    grep -q "nvh-env.sh" "$RC" 2>/dev/null || {
        echo "" >> "$RC"
        echo "# NVHive" >> "$RC"
        echo "source \"$NVH_HOME/nvh-env.sh\"" >> "$RC"
        echo "export PATH=\"$NVH_VENV/bin:\$PATH\"" >> "$RC"
        echo "[ -x \"$NVH_BIN/ollama\" ] && ! curl -sf http://localhost:11434/api/tags &>/dev/null && OLLAMA_MODELS=\"$OLLAMA_MODELS\" \"$NVH_BIN/ollama\" serve &>/dev/null &" >> "$RC"
    }

    echo ""
    echo -e "  Type ${G}nvh${N} to start chatting"
    echo ""
    exit 0
fi

# ---------------------------------------------------------------------------
# Fresh install
# ---------------------------------------------------------------------------
echo -e "${B}Fresh install - setting up $NVH_HOME/...${N}"
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

# Create venv (skip when installing into an already-active env)
if [ "$USE_ACTIVE_ENV" = "true" ]; then
    echo -e "${B}Using existing $ACTIVE_ENV_KIND env: $ACTIVE_ENV_NAME${N}"
    # Env is already activated by the user's shell; just upgrade pip in-place.
    "$PYTHON" -m pip install -q --upgrade pip 2>/dev/null || true
else
    echo -e "${B}Creating Python environment...${N}"
    create_rootless_venv || exit 1
fi

# Install
echo -e "${B}Installing NVHive (~60s)...${N}"
if [ "$USE_ACTIVE_ENV" = "true" ]; then
    "$PYTHON" -m pip install -q -e "$NVH_REPO[serve,nvidia]" 2>"$NVH_LOGS/pip-install.log" || {
        echo -e "${R}Install failed. Check Python version (need 3.11+).${N}"
        echo -e "${D}Log: $NVH_LOGS/pip-install.log${N}"
        exit 1
    }
else
    pip install -q -e "$NVH_REPO[serve,nvidia]" 2>"$NVH_LOGS/pip-install.log" || {
        echo -e "${R}Install failed. Check Python version (need 3.11+).${N}"
        echo -e "${D}Log: $NVH_LOGS/pip-install.log${N}"
        exit 1
    }
fi

# Verify
command -v nvh &>/dev/null || {
    echo -e "${R}nvh command not found after install.${N}"
    exit 1
}

export PATH="$NVH_VENV/bin:$NVH_BIN:$PATH"

# ---------------------------------------------------------------------------
# Auto-create config with zero-signup providers enabled
# ---------------------------------------------------------------------------
HIVE_DIR="$HIVE_CONFIG_HOME"
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
    default_model: ollama/nemotron-mini
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

# Set up .bashrc — only when we own the venv. If the user installed into
# their existing conda/mamba/venv, they'll activate it themselves; adding a
# PATH export would shadow their own activation logic.
if [ "${NVH_NO_OS_MOD:-0}" = "1" ]; then
    echo -e "${D}Skipping .bashrc PATH edit because NVH_NO_OS_MOD=1.${N}"
elif [ "$USE_ACTIVE_ENV" = "true" ]; then
    echo -e "${D}Skipping .bashrc PATH edit — using existing $ACTIVE_ENV_KIND env.${N}"
    echo -e "${D}  Remember to activate '$ACTIVE_ENV_NAME' before running nvh.${N}"
else
    RC="$HOME/.bashrc"; [ -f "$HOME/.zshrc" ] && RC="$HOME/.zshrc"
    grep -q "nvh-env.sh" "$RC" 2>/dev/null || {
        echo "" >> "$RC"
        echo "# NVHive — Multi-LLM Orchestration" >> "$RC"
        echo "source \"$NVH_HOME/nvh-env.sh\"" >> "$RC"
        echo "export PATH=\"$NVH_VENV/bin:\$PATH\"" >> "$RC"
        echo "# Auto-start Ollama on login if installed" >> "$RC"
        echo "[ -x \"$NVH_BIN/ollama\" ] && ! curl -sf http://localhost:11434/api/tags &>/dev/null 2>&1 && OLLAMA_MODELS=\"$OLLAMA_MODELS\" \"$NVH_BIN/ollama\" serve &>/dev/null &" >> "$RC"
    }
fi

# ---------------------------------------------------------------------------
# Set up Ollama (local AI) — only if we have a GPU
# ---------------------------------------------------------------------------
if [ -n "$GPU_NAME" ]; then
    OLLAMA_BIN="$NVH_BIN/ollama"
    if [ ! -f "$OLLAMA_BIN" ]; then
        echo -e "${B}Installing Ollama (local AI)...${N}"
        curl -sSL https://ollama.com/download/ollama-linux-amd64 -o "$OLLAMA_BIN" 2>/dev/null
        chmod +x "$OLLAMA_BIN"
    fi

    # Start Ollama
    if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
        echo -e "${B}Starting Ollama...${N}"
        mkdir -p "$OLLAMA_MODELS"
        OLLAMA_MODELS="$OLLAMA_MODELS" "$OLLAMA_BIN" serve &>/dev/null &
        sleep 3
    fi

    # Pick model based on VRAM. Only real Ollama registry tags — earlier
    # tiers referenced nemotron:120b / nemotron-small which return 404.
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        if [ "$VRAM_GB" -ge 24 ]; then MODEL="nemotron"
        elif [ "$VRAM_GB" -ge 8 ]; then MODEL="llama3.1:8b"
        else MODEL="nemotron-mini"; fi

        if ! "$OLLAMA_BIN" list 2>/dev/null | grep -q "$MODEL"; then
            echo -e "${B}Pulling $MODEL in background (you can start using nvh now)...${N}"
            OLLAMA_MODELS="$OLLAMA_MODELS" "$OLLAMA_BIN" pull "$MODEL" &>/dev/null &
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
if [ "$USE_ACTIVE_ENV" = "true" ]; then
    case "$ACTIVE_ENV_KIND" in
        micromamba) ACT="micromamba activate $ACTIVE_ENV_NAME" ;;
        conda)      ACT="conda activate $ACTIVE_ENV_NAME" ;;
        venv)       ACT="source $ACTIVE_ENV_PATH/bin/activate" ;;
    esac
    echo -e "  ${Y}Env:${N} installed into $ACTIVE_ENV_KIND env ${G}$ACTIVE_ENV_NAME${N}"
    echo -e "  ${Y}Next shell:${N} run ${G}$ACT${N} before using nvh"
    echo ""
fi
echo -e "  ${G}nvh${N}            Start chatting (works immediately)"
echo -e "  ${G}nvh workstation${N} Create desktop launcher + student GPU lab checklist"
echo -e "  ${G}nvh workstation --all -y${N} Full local AI + ComfyUI + WebUI setup"
echo -e "  ${G}nvh studio --install starter -y${N} Rootless LLMs + agents + game-dev packs"
echo -e "  ${G}nvh setup${N}      Add more free AI providers"
echo -e "  ${G}nvh webui${N}      Launch the browser dashboard"
echo -e "  ${G}nvh bench${N}      Benchmark your GPU"
echo -e "  ${G}nvh status${N}     System overview"
echo -e "  ${G}nvh update${N}     Pull latest version"
echo ""
echo -e "  ${D}Install dir: $NVH_HOME/${N}"
echo -e "  ${D}Config: $HIVE_CONFIG_HOME/config.yaml${N}"
echo -e "  ${D}Activate: source $NVH_HOME/nvh-env.sh${N}"
echo -e "  ${D}On reconnect: just type 'nvh'${N}"
echo ""
echo -e "  ${G}Start now:${N}"
echo -e "  ${G}  nvh${N}"
echo ""
# Make nvh available in the CURRENT shell (not just future ones)
echo -e "${D}(If 'nvh' is not found, run: source ~/.bashrc)${N}"
echo ""
