#!/bin/bash
# =============================================================================
# Hive — Portable Setup for Mounted Home Directory VMs
#
# For cloud GPU instances where:
# - New VM is generated each session (ephemeral OS)
# - User's home directory is mounted from persistent storage
# - No root access, no system package installation
# - Docker may or may not be available
# - NVIDIA GPU is available via driver pre-installed on the VM
#
# Everything installs to ~/hive/ — no root, no system changes.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/.../portable-setup.sh | bash
#   # or
#   ~/hive/scripts/portable-setup.sh
# =============================================================================

set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${BLUE}[hive]${RESET} $*"; }
success() { echo -e "${GREEN}[hive]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[hive]${RESET} $*"; }
error()   { echo -e "${RED}[hive]${RESET} $*" >&2; }

HIVE_HOME="${HIVE_HOME:-$HOME/hive}"
HIVE_DATA="${HIVE_HOME}/data"
HIVE_VENV="${HIVE_HOME}/venv"
HIVE_PORT="${HIVE_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"

# ---------------------------------------------------------------------------
# Step 1: Check prerequisites
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}${GREEN}=====================================================${RESET}"
echo -e "${BOLD}${GREEN}  Hive — Portable Setup${RESET}"
echo -e "${BOLD}${GREEN}  No root required. Installs to ~/hive/${RESET}"
echo -e "${BOLD}${GREEN}=====================================================${RESET}"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    error "Python 3 not found. This VM should have Python pre-installed."
    error "If not, contact your cloud provider."
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python: $PY_VERSION"

# Check pip
if ! python3 -m pip --version &>/dev/null 2>&1; then
    warn "pip not found. Installing pip to user directory..."
    python3 -m ensurepip --user 2>/dev/null || {
        curl -sSL https://bootstrap.pypa.io/get-pip.py | python3 - --user
    }
fi

# Check GPU
GPU_NAME="none"
VRAM_GB=0
if command -v nvidia-smi &>/dev/null; then
    GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null || echo "")
    if [[ -n "$GPU_INFO" ]]; then
        GPU_NAME=$(echo "$GPU_INFO" | head -1 | cut -d',' -f1 | xargs)
        VRAM_MB=$(echo "$GPU_INFO" | head -1 | cut -d',' -f2 | xargs)
        VRAM_GB=$((VRAM_MB / 1024))
        success "GPU: $GPU_NAME ($VRAM_GB GB VRAM)"
    fi
else
    warn "nvidia-smi not found — GPU detection failed"
fi

# ---------------------------------------------------------------------------
# Step 2: Set up Hive directory
# ---------------------------------------------------------------------------
info "Setting up $HIVE_HOME..."
mkdir -p "$HIVE_HOME" "$HIVE_DATA"

# Check if Hive is already installed (persistent across sessions)
if [[ -d "$HIVE_VENV" ]] && [[ -f "$HIVE_VENV/bin/council" ]]; then
    info "Hive already installed. Checking for updates..."
    EXISTING=true
else
    EXISTING=false
fi

# ---------------------------------------------------------------------------
# Step 3: Clone or update the repo
# ---------------------------------------------------------------------------
REPO_DIR="$HIVE_HOME/repo"
if [[ -d "$REPO_DIR/.git" ]]; then
    info "Updating Hive repository..."
    cd "$REPO_DIR" && git pull --quiet 2>/dev/null || true
else
    info "Cloning Hive repository..."
    if command -v git &>/dev/null; then
        git clone --depth 1 https://github.com/thatcooperguy/nvHive.git "$REPO_DIR" 2>/dev/null || {
            error "Failed to clone repo. Check network access."
            exit 1
        }
    else
        # No git — download as tarball
        info "git not found, downloading as archive..."
        mkdir -p "$REPO_DIR"
        curl -sSL https://github.com/thatcooperguy/nvHive/archive/refs/heads/main.tar.gz | \
            tar xz -C "$REPO_DIR" --strip-components=1
    fi
fi

# ---------------------------------------------------------------------------
# Step 4: Create/reuse Python virtual environment
# ---------------------------------------------------------------------------
if [[ ! -d "$HIVE_VENV" ]]; then
    info "Creating Python virtual environment..."
    python3 -m venv "$HIVE_VENV"
fi

# Activate venv
source "$HIVE_VENV/bin/activate"

# Install/update Hive CLI
info "Installing Hive CLI..."
pip install -q --upgrade pip 2>/dev/null
pip install -q -e "$REPO_DIR" 2>/dev/null

# Verify
if ! command -v council &>/dev/null; then
    error "Hive installation failed."
    exit 1
fi
success "Hive CLI installed: $(council version 2>/dev/null || echo 'v0.1.0')"

# ---------------------------------------------------------------------------
# Step 5: Configure
# ---------------------------------------------------------------------------
CONFIG_DIR="$HOME/.hive"
mkdir -p "$CONFIG_DIR"

if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
    info "Creating default configuration..."
    council config init --force 2>/dev/null || true
fi

# Set data directory to persistent storage
export HIVE_DATA_DIR="$HIVE_DATA"

# ---------------------------------------------------------------------------
# Step 6: Set up Ollama (if Docker available, or direct binary)
# ---------------------------------------------------------------------------
OLLAMA_RUNNING=false

# Check if Ollama is already running
if curl -sf http://localhost:11434/api/tags &>/dev/null; then
    success "Ollama already running!"
    OLLAMA_RUNNING=true
fi

if [[ "$OLLAMA_RUNNING" == "false" ]]; then
    # Try Docker first
    if command -v docker &>/dev/null; then
        info "Starting Ollama via Docker..."
        docker run -d \
            --name hive-ollama \
            --gpus all \
            -v "$HIVE_DATA/ollama:/root/.ollama" \
            -p 11434:11434 \
            --restart unless-stopped \
            ollama/ollama:latest 2>/dev/null && OLLAMA_RUNNING=true || true
    fi

    # If Docker failed, try running Ollama directly
    if [[ "$OLLAMA_RUNNING" == "false" ]]; then
        OLLAMA_BIN="$HIVE_HOME/bin/ollama"
        if [[ ! -f "$OLLAMA_BIN" ]]; then
            info "Installing Ollama binary to ~/hive/bin/..."
            mkdir -p "$HIVE_HOME/bin"
            curl -sSL https://ollama.com/download/ollama-linux-amd64 -o "$OLLAMA_BIN"
            chmod +x "$OLLAMA_BIN"
        fi

        if [[ -f "$OLLAMA_BIN" ]]; then
            export OLLAMA_MODELS="$HIVE_DATA/ollama/models"
            mkdir -p "$OLLAMA_MODELS"
            info "Starting Ollama directly..."
            "$OLLAMA_BIN" serve &>/dev/null &
            OLLAMA_PID=$!
            echo "$OLLAMA_PID" > "$HIVE_DATA/ollama.pid"

            # Wait for Ollama to be ready
            for i in $(seq 1 30); do
                if curl -sf http://localhost:11434/api/tags &>/dev/null; then
                    OLLAMA_RUNNING=true
                    break
                fi
                sleep 1
            done
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Step 7: Pull recommended model
# ---------------------------------------------------------------------------
if [[ "$OLLAMA_RUNNING" == "true" ]]; then
    # Select model based on VRAM
    if [[ "$VRAM_GB" -ge 80 ]]; then
        RECOMMENDED_MODEL="nemotron:120b"
    elif [[ "$VRAM_GB" -ge 24 ]]; then
        RECOMMENDED_MODEL="nemotron"
    elif [[ "$VRAM_GB" -ge 6 ]]; then
        RECOMMENDED_MODEL="nemotron-small"
    else
        RECOMMENDED_MODEL="nemotron-mini"
    fi

    # Check if model already pulled (persistent storage)
    OLLAMA_CMD="${OLLAMA_BIN:-ollama}"
    if command -v docker &>/dev/null && docker ps --format '{{.Names}}' | grep -q hive-ollama; then
        OLLAMA_CMD="docker exec hive-ollama ollama"
    fi

    if $OLLAMA_CMD list 2>/dev/null | grep -q "$RECOMMENDED_MODEL"; then
        success "Model $RECOMMENDED_MODEL already installed."
    else
        info "Pulling $RECOMMENDED_MODEL (this may take a few minutes on first run)..."
        $OLLAMA_CMD pull "$RECOMMENDED_MODEL" &
        PULL_PID=$!
        info "Model pull running in background (PID: $PULL_PID)."
        info "You can start using Hive while the model downloads."
    fi
fi

# ---------------------------------------------------------------------------
# Step 8: Start Hive API
# ---------------------------------------------------------------------------
info "Starting Hive API server on port $HIVE_PORT..."

# Load .env if it exists
if [[ -f "$REPO_DIR/.env" ]]; then
    set -a
    source "$REPO_DIR/.env"
    set +a
elif [[ -f "$HOME/.council/.env" ]]; then
    set -a
    source "$HOME/.council/.env"
    set +a
fi

# Start the API server in background
council serve --host 0.0.0.0 --port "$HIVE_PORT" &>/dev/null &
API_PID=$!
echo "$API_PID" > "$HIVE_DATA/api.pid"

# Wait for API to be ready
for i in $(seq 1 15); do
    if curl -sf "http://localhost:$HIVE_PORT/v1/health" &>/dev/null; then
        break
    fi
    sleep 1
done

if curl -sf "http://localhost:$HIVE_PORT/v1/health" &>/dev/null; then
    success "Hive API running on port $HIVE_PORT"
else
    warn "API may still be starting..."
fi

# ---------------------------------------------------------------------------
# Step 9: Create convenience scripts
# ---------------------------------------------------------------------------
cat > "$HIVE_HOME/start.sh" << 'STARTEOF'
#!/bin/bash
# Start Hive services
source ~/hive/venv/bin/activate
export HIVE_DATA_DIR=~/hive/data

# Start Ollama if not running
if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
    if [ -f ~/hive/bin/ollama ]; then
        export OLLAMA_MODELS=~/hive/data/ollama/models
        ~/hive/bin/ollama serve &>/dev/null &
    fi
fi

# Start API
council serve --host 0.0.0.0 --port ${HIVE_PORT:-8000} &>/dev/null &
echo "Hive started. API: http://localhost:${HIVE_PORT:-8000}"
STARTEOF
chmod +x "$HIVE_HOME/start.sh"

cat > "$HIVE_HOME/stop.sh" << 'STOPEOF'
#!/bin/bash
# Stop Hive services
[ -f ~/hive/data/api.pid ] && kill $(cat ~/hive/data/api.pid) 2>/dev/null
[ -f ~/hive/data/ollama.pid ] && kill $(cat ~/hive/data/ollama.pid) 2>/dev/null
docker stop hive-ollama 2>/dev/null
echo "Hive stopped."
STOPEOF
chmod +x "$HIVE_HOME/stop.sh"

# Add to PATH for this session
export PATH="$HIVE_VENV/bin:$HIVE_HOME/bin:$PATH"

# Add to .bashrc for future sessions
MARKER="# Hive AI"
if ! grep -q "$MARKER" "$HOME/.bashrc" 2>/dev/null; then
    cat >> "$HOME/.bashrc" << EOF

$MARKER
export PATH="$HIVE_VENV/bin:$HIVE_HOME/bin:\$PATH"
export HIVE_DATA_DIR="$HIVE_DATA"
source "$HIVE_VENV/bin/activate" 2>/dev/null
EOF
fi

# ---------------------------------------------------------------------------
# Done!
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}${GREEN}=====================================================${RESET}"
echo -e "${BOLD}${GREEN}  Hive is ready!${RESET}"
echo -e "${BOLD}${GREEN}=====================================================${RESET}"
echo ""
echo -e "  ${BOLD}API:${RESET}    http://localhost:$HIVE_PORT"
echo -e "  ${BOLD}Docs:${RESET}   http://localhost:$HIVE_PORT/docs"
echo -e "  ${BOLD}CLI:${RESET}    council query \"Hello!\""
echo ""
if [[ "$GPU_NAME" != "none" ]]; then
    echo -e "  ${BOLD}GPU:${RESET}    $GPU_NAME ($VRAM_GB GB)"
    echo -e "  ${BOLD}Model:${RESET}  $RECOMMENDED_MODEL"
fi
echo ""
echo -e "  ${BOLD}Config:${RESET}   ~/.hive/config.yaml"
echo -e "  ${BOLD}Data:${RESET}     ~/hive/data/"
echo -e "  ${BOLD}Start:${RESET}    ~/hive/start.sh"
echo -e "  ${BOLD}Stop:${RESET}     ~/hive/stop.sh"
echo ""
echo -e "  Create a ${BOLD}HIVE.md${RESET} file in your project to inject context into all LLM prompts."
echo ""
echo -e "  ${BOLD}Next steps:${RESET}"
echo -e "    1. Add API keys:  council provider login openai"
echo -e "    2. Try a query:   council query \"What is machine learning?\""
echo -e "    3. Try council:   council convene \"Should I use Python or Rust?\" --auto-agents"
echo ""
