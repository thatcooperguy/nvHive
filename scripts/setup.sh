#!/bin/bash
# =============================================================================
# Hive — one-command setup for Ubuntu / Linux
# Designed for college students and gamers on NVIDIA Ubuntu PCs.
# Users may NOT have root access — this script installs Docker in rootless mode.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/your-org/aiproject/main/scripts/setup.sh | bash
# Or locally:
#   chmod +x scripts/setup.sh && ./scripts/setup.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${BLUE}[hive]${RESET} $*"; }
success() { echo -e "${GREEN}[hive]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[hive]${RESET} $*"; }
error()   { echo -e "${RED}[hive]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }

banner() {
  echo ""
  echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
  echo -e "${BOLD}║          Hive — Multi-LLM Platform       ║${RESET}"
  echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
  echo ""
}

# ---------------------------------------------------------------------------
# Locate the project root (handles both local run and piped curl | bash)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "$PWD")"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd || echo "$PWD")"

banner

info "Project root: $PROJECT_ROOT"

# ---------------------------------------------------------------------------
# 1. Check / install Docker (rootless mode — no sudo needed)
# ---------------------------------------------------------------------------
install_docker_rootless() {
  info "Docker not found. Installing Docker in rootless mode (no root required)..."

  # Prerequisites check
  if ! command -v curl &>/dev/null; then
    die "curl is required to install Docker. Ask your sysadmin to install curl."
  fi

  # Download and run the official rootless install helper
  # See: https://docs.docker.com/engine/security/rootless/
  info "Downloading Docker rootless setup script..."
  curl -fsSL https://get.docker.com/rootless | sh

  # Add the rootless Docker bin to PATH for this session
  export PATH="$HOME/bin:$PATH"
  export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/docker.sock"

  # Add to shell rc files so it persists
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [[ -f "$rc" ]]; then
      if ! grep -q 'docker rootless' "$rc" 2>/dev/null; then
        cat >> "$rc" <<'EOF'

# Docker rootless (added by Hive setup)
export PATH="$HOME/bin:$PATH"
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/docker.sock"
EOF
        info "Updated $rc with rootless Docker environment."
      fi
    fi
  done

  success "Docker rootless installed. You may need to start the systemd user service:"
  info "  systemctl --user start docker"
  info "  systemctl --user enable docker"
  echo ""
}

check_docker() {
  info "Checking for Docker..."

  if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    local docker_version
    docker_version=$(docker --version | awk '{print $3}' | tr -d ',')
    success "Docker found: $docker_version"
    return 0
  fi

  # Docker binary exists but daemon is not running
  if command -v docker &>/dev/null; then
    warn "Docker binary found but daemon is not running."

    # Try starting the rootless systemd unit first
    if systemctl --user start docker 2>/dev/null; then
      sleep 2
      if docker info &>/dev/null 2>&1; then
        success "Started Docker rootless daemon via systemd."
        return 0
      fi
    fi

    # Try starting the system-level daemon (requires root — may fail)
    warn "Attempting to start system Docker (may require sudo)..."
    if sudo systemctl start docker 2>/dev/null; then
      success "Started system Docker daemon."
      return 0
    fi

    die "Docker daemon could not be started. Run: systemctl --user start docker"
  fi

  # Not installed at all
  install_docker_rootless
}

check_docker_compose() {
  info "Checking for Docker Compose..."
  if docker compose version &>/dev/null 2>&1; then
    success "Docker Compose plugin found."
  elif command -v docker-compose &>/dev/null; then
    success "docker-compose (standalone) found."
    # Alias for the rest of this script
    docker() { command docker "$@" 2>/dev/null || docker-compose "$@"; }
  else
    warn "Docker Compose not found. Attempting to install the plugin..."
    # Rootless users can install the plugin into ~/.docker/cli-plugins
    mkdir -p "$HOME/.docker/cli-plugins"
    COMPOSE_VERSION=$(curl -fsSL https://api.github.com/repos/docker/compose/releases/latest | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/')
    ARCH=$(uname -m)
    curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}" \
      -o "$HOME/.docker/cli-plugins/docker-compose"
    chmod +x "$HOME/.docker/cli-plugins/docker-compose"
    success "Docker Compose plugin installed to ~/.docker/cli-plugins/docker-compose"
  fi
}

# ---------------------------------------------------------------------------
# 2. Check for NVIDIA Container Toolkit (GPU support for Ollama)
# ---------------------------------------------------------------------------
check_nvidia() {
  info "Checking for NVIDIA GPU and Container Toolkit..."

  if ! command -v nvidia-smi &>/dev/null; then
    warn "nvidia-smi not found. Ollama will run on CPU (slower but works fine)."
    warn "If you have an NVIDIA GPU, install the NVIDIA Container Toolkit:"
    warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
    GPU_AVAILABLE=false
    return
  fi

  local gpu_name
  gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "unknown")
  info "NVIDIA GPU detected: $gpu_name"

  if docker run --rm --gpus all nvidia/cuda:12.0-base-ubuntu22.04 nvidia-smi &>/dev/null 2>&1; then
    success "NVIDIA Container Toolkit is working. Ollama will use GPU acceleration."
    GPU_AVAILABLE=true
  else
    warn "NVIDIA GPU found but Container Toolkit is not configured."
    warn "To enable GPU support, install the toolkit:"
    warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
    warn "  Then: nvidia-ctk runtime configure --runtime=docker --config=\$HOME/.config/docker/daemon.json"
    warn "  Then: systemctl --user restart docker"
    warn ""
    warn "Falling back to CPU mode for now."
    GPU_AVAILABLE=false
  fi
}

# ---------------------------------------------------------------------------
# 3. Environment file
# ---------------------------------------------------------------------------
setup_env() {
  info "Setting up environment file..."

  if [[ -f "$PROJECT_ROOT/.env" ]]; then
    info ".env already exists — skipping copy."
  else
    cp "$PROJECT_ROOT/.env.example" "$PROJECT_ROOT/.env"
    success "Copied .env.example to .env"
    warn "Open .env and add your API keys to enable cloud LLM providers."
  fi
}

# ---------------------------------------------------------------------------
# 4. Start the stack
# ---------------------------------------------------------------------------
start_stack() {
  info "Starting Hive stack with Docker Compose..."
  cd "$PROJECT_ROOT"

  docker compose up -d --build

  success "Stack started."
}

# ---------------------------------------------------------------------------
# 5. Wait for services to be ready
# ---------------------------------------------------------------------------
wait_for_service() {
  local name="$1"
  local url="$2"
  local max_attempts=30
  local attempt=0

  info "Waiting for $name to be ready..."
  while (( attempt < max_attempts )); do
    if curl -sf "$url" &>/dev/null; then
      success "$name is ready."
      return 0
    fi
    attempt=$(( attempt + 1 ))
    sleep 2
  done

  warn "$name did not become healthy within 60 s. Check logs: docker compose logs $name"
  return 1
}

# ---------------------------------------------------------------------------
# 6. Pull Ollama quick-start model
# ---------------------------------------------------------------------------
pull_ollama_model() {
  info "Pulling Ollama quick-start model (llama3.2:1b, ~1.3 GB)..."

  if docker compose exec -T ollama ollama list 2>/dev/null | grep -q 'llama3.2:1b'; then
    success "llama3.2:1b is already present — skipping."
    return
  fi

  if docker compose exec -T ollama ollama pull llama3.2:1b; then
    success "llama3.2:1b pulled successfully."
  else
    warn "Model pull failed. You can retry later:"
    warn "  docker compose exec ollama ollama pull llama3.2:1b"
  fi
}

# ---------------------------------------------------------------------------
# 7. Print summary
# ---------------------------------------------------------------------------
print_summary() {
  echo ""
  echo -e "${BOLD}${GREEN}============================================================${RESET}"
  echo -e "${BOLD}${GREEN}  Hive is running!${RESET}"
  echo -e "${BOLD}${GREEN}============================================================${RESET}"
  echo ""
  echo -e "  ${BOLD}Web UI:${RESET}   http://localhost:3000"
  echo -e "  ${BOLD}API:${RESET}      http://localhost:8000"
  echo -e "  ${BOLD}API Docs:${RESET} http://localhost:8000/docs"
  echo -e "  ${BOLD}Ollama:${RESET}   http://localhost:11434"
  echo ""
  if [[ "${GPU_AVAILABLE:-false}" == "true" ]]; then
    echo -e "  ${GREEN}GPU acceleration: ENABLED${RESET}"
  else
    echo -e "  ${YELLOW}GPU acceleration: CPU mode (install NVIDIA Container Toolkit to enable)${RESET}"
  fi
  echo ""
  echo -e "${BOLD}Next steps:${RESET}"
  echo -e "  1. Add your API keys to ${BOLD}.env${RESET} and run: docker compose restart hive-api"
  echo -e "  2. Pull more Ollama models: ${BOLD}./scripts/ollama-setup.sh${RESET}"
  echo -e "  3. View logs: ${BOLD}docker compose logs -f${RESET}"
  echo -e "  4. Stop everything: ${BOLD}docker compose down${RESET}"
  echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
GPU_AVAILABLE=false

check_docker
check_docker_compose
check_nvidia
setup_env
start_stack

wait_for_service "hive-api" "http://localhost:8000/v1/health" || true
wait_for_service "hive-web" "http://localhost:3000" || true
wait_for_service "ollama"      "http://localhost:11434/api/tags" || true

pull_ollama_model

print_summary
