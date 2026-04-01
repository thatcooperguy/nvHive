#!/bin/bash
# =============================================================================
# Hive — cloud GPU instance setup
#
# Target: Linux cloud VMs with NVIDIA GPUs (no root required)
#   AWS g5/p4, GCP a2/a3, Lambda Labs, CoreWeave, Linux Desktop
#
# Usage:
#   chmod +x scripts/cloud-setup.sh && ./scripts/cloud-setup.sh
# Or via curl:
#   curl -sSL https://raw.githubusercontent.com/your-org/aiproject/main/scripts/cloud-setup.sh | bash
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

info()    { echo -e "${BLUE}[hive-cloud]${RESET} $*"; }
success() { echo -e "${GREEN}[hive-cloud]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[hive-cloud]${RESET} $*"; }
error()   { echo -e "${RED}[hive-cloud]${RESET} $*" >&2; }
die()     { error "$*"; exit 1; }
step()    { echo -e "\n${BOLD}${CYAN}==>${RESET}${BOLD} $*${RESET}"; }

banner() {
  echo ""
  echo -e "${BOLD}╔══════════════════════════════════════════════╗${RESET}"
  echo -e "${BOLD}║      Hive — Cloud GPU Instance Setup         ║${RESET}"
  echo -e "${BOLD}║  AWS g5/p4 · GCP a2/a3 · Lambda · CoreWeave ║${RESET}"
  echo -e "${BOLD}╚══════════════════════════════════════════════╝${RESET}"
  echo ""
}

# ---------------------------------------------------------------------------
# Locate project root
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "$PWD")"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd || echo "$PWD")"

banner
info "Project root: $PROJECT_ROOT"
info "Running as:   $(whoami) (uid=$(id -u))"

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
CLOUD_PROVIDER="unknown"
INSTANCE_TYPE="unknown"
GPU_NAME="unknown"
GPU_VRAM_GB=0
GPU_COUNT=0
GPU_AVAILABLE=false
HAS_ROOT=false
PUBLIC_IP=""

# ---------------------------------------------------------------------------
# Step 1: Detect cloud environment
# ---------------------------------------------------------------------------
detect_cloud() {
  step "Detecting cloud environment"

  # Check for root
  if [[ "$(id -u)" == "0" ]]; then
    HAS_ROOT=true
    warn "Running as root. This script is designed for rootless use, but will adapt."
  fi

  # AWS IMDSv2 (instance metadata service)
  if curl -s --max-time 2 -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" \
        -o /dev/null 2>/dev/null; then
    TOKEN=$(curl -s --max-time 2 -X PUT "http://169.254.169.254/latest/api/token" \
              -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" 2>/dev/null || echo "")
    if [[ -n "$TOKEN" ]]; then
      CLOUD_PROVIDER="aws"
      INSTANCE_TYPE=$(curl -s --max-time 2 \
        -H "X-aws-ec2-metadata-token: $TOKEN" \
        "http://169.254.169.254/latest/meta-data/instance-type" 2>/dev/null || echo "unknown")
      PUBLIC_IP=$(curl -s --max-time 2 \
        -H "X-aws-ec2-metadata-token: $TOKEN" \
        "http://169.254.169.254/latest/meta-data/public-ipv4" 2>/dev/null || echo "")
      success "AWS instance detected: $INSTANCE_TYPE"
    fi
  fi

  # GCP Metadata Server
  if [[ "$CLOUD_PROVIDER" == "unknown" ]]; then
    GCP_MACHINE=$(curl -s --max-time 2 \
      -H "Metadata-Flavor: Google" \
      "http://metadata.google.internal/computeMetadata/v1/instance/machine-type" \
      2>/dev/null || echo "")
    if [[ -n "$GCP_MACHINE" ]]; then
      CLOUD_PROVIDER="gcp"
      INSTANCE_TYPE="${GCP_MACHINE##*/}"  # strip the full resource path
      PUBLIC_IP=$(curl -s --max-time 2 \
        -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/externalIp" \
        2>/dev/null || echo "")
      success "GCP instance detected: $INSTANCE_TYPE"
    fi
  fi

  # Azure IMDS
  if [[ "$CLOUD_PROVIDER" == "unknown" ]]; then
    AZURE_META=$(curl -s --max-time 2 \
      -H "Metadata: true" \
      "http://169.254.169.254/metadata/instance?api-version=2021-02-01" \
      2>/dev/null || echo "")
    if echo "$AZURE_META" | grep -q '"azEnvironment"' 2>/dev/null; then
      CLOUD_PROVIDER="azure"
      INSTANCE_TYPE=$(echo "$AZURE_META" | grep -o '"vmSize":"[^"]*"' | cut -d'"' -f4 || echo "unknown")
      PUBLIC_IP=$(curl -s --max-time 2 \
        "https://api.ipify.org" 2>/dev/null || echo "")
      success "Azure instance detected: $INSTANCE_TYPE"
    fi
  fi

  # Lambda Labs / CoreWeave / other GPU clouds — fall back to hostname heuristics
  if [[ "$CLOUD_PROVIDER" == "unknown" ]]; then
    HOSTNAME_VAL=$(hostname -f 2>/dev/null || hostname)
    if echo "$HOSTNAME_VAL" | grep -qiE 'lambda|coreweave|vast|runpod|paperspace|tensordock'; then
      CLOUD_PROVIDER="gpu_cloud"
      info "GPU cloud instance detected via hostname: $HOSTNAME_VAL"
    fi
  fi

  # Linux Desktop — check for NVIDIA vGPU signature in DMI
  if [[ "$CLOUD_PROVIDER" == "unknown" ]]; then
    if [[ -f /sys/class/dmi/id/board_vendor ]] && \
       grep -qi "nvidia" /sys/class/dmi/id/board_vendor 2>/dev/null; then
      CLOUD_PROVIDER="cloud_desktop"
      info "NVIDIA-hosted instance detected (Linux Desktop or similar)"
    fi
  fi

  # Get public IP if we don't have one yet
  if [[ -z "$PUBLIC_IP" ]]; then
    PUBLIC_IP=$(curl -s --max-time 5 "https://api.ipify.org" 2>/dev/null \
      || curl -s --max-time 5 "https://ifconfig.me" 2>/dev/null \
      || echo "")
  fi

  if [[ "$CLOUD_PROVIDER" == "unknown" ]]; then
    warn "Could not identify cloud provider. Continuing with generic GPU setup."
    warn "Supported: AWS, GCP, Azure, Lambda Labs, CoreWeave, Linux Desktop"
  fi

  info "Cloud provider: $CLOUD_PROVIDER | Instance type: $INSTANCE_TYPE"
  [[ -n "$PUBLIC_IP" ]] && info "Public IP: $PUBLIC_IP"
}

# ---------------------------------------------------------------------------
# Step 2: Detect GPU via nvidia-smi
# ---------------------------------------------------------------------------
detect_gpu() {
  step "Detecting NVIDIA GPU"

  if ! command -v nvidia-smi &>/dev/null; then
    warn "nvidia-smi not found in PATH."
    warn "If NVIDIA drivers are installed, add them to PATH:"
    warn "  export PATH=\$PATH:/usr/local/cuda/bin"
    GPU_AVAILABLE=false
    return
  fi

  if ! nvidia-smi &>/dev/null; then
    warn "nvidia-smi found but failed — GPU may not be accessible."
    GPU_AVAILABLE=false
    return
  fi

  GPU_AVAILABLE=true
  GPU_COUNT=$(nvidia-smi --query-gpu=count --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ' || echo "0")
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | xargs || echo "unknown")
  GPU_VRAM_MIB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1 | grep -o '[0-9]*' | head -1 || echo "0")
  GPU_VRAM_GB=$(echo "scale=1; $GPU_VRAM_MIB / 1024" | bc 2>/dev/null || echo "0")
  DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | xargs || echo "unknown")

  success "GPU detected: $GPU_NAME x$GPU_COUNT"
  info "VRAM per GPU: ${GPU_VRAM_GB} GB | Driver: $DRIVER_VER"

  # Recommend model based on VRAM
  local vram_int=${GPU_VRAM_GB%.*}
  if (( vram_int >= 80 )); then
    RECOMMENDED_MODEL="nemotron:70b"
    info "Recommended model: nemotron:70b (full Nemotron 70B — needs 48 GB+ VRAM)"
  elif (( vram_int >= 48 )); then
    RECOMMENDED_MODEL="nemotron"
    info "Recommended model: nemotron (Nemotron 70B quantized Q4)"
  elif (( vram_int >= 24 )); then
    RECOMMENDED_MODEL="llama3.3:70b-instruct-q4_K_M"
    info "Recommended model: llama3.3:70b (Q4, fits in 24 GB VRAM)"
  elif (( vram_int >= 16 )); then
    RECOMMENDED_MODEL="nemotron-small"
    info "Recommended model: nemotron-small (4B — comfortable in 16 GB VRAM)"
  elif (( vram_int >= 8 )); then
    RECOMMENDED_MODEL="nemotron-small"
    info "Recommended model: nemotron-small (4B — fits in 8 GB VRAM)"
  else
    RECOMMENDED_MODEL="llama3.2:3b"
    info "Recommended model: llama3.2:3b (small model for <8 GB VRAM)"
  fi
}

# ---------------------------------------------------------------------------
# Step 3: Install Docker in rootless mode if needed
# ---------------------------------------------------------------------------
install_docker_rootless() {
  info "Docker not found. Installing in rootless mode (no root required)..."

  command -v curl &>/dev/null || die "curl is required to install Docker. Ask your sysadmin: apt install curl"
  command -v uidmap &>/dev/null || {
    warn "uidmap not found — rootless Docker needs it. Trying to install..."
    if command -v apt-get &>/dev/null; then
      apt-get install -y uidmap 2>/dev/null || die "Could not install uidmap. Ask your sysadmin: apt install uidmap"
    else
      die "Install uidmap manually, then re-run this script."
    fi
  }

  curl -fsSL https://get.docker.com/rootless | sh

  export PATH="$HOME/bin:$PATH"
  export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"

  # Persist to shell rc files
  for rc in "$HOME/.bashrc" "$HOME/.profile"; do
    if [[ -f "$rc" ]] && ! grep -q 'docker rootless' "$rc" 2>/dev/null; then
      cat >> "$rc" <<'RCEOF'

# Docker rootless (added by Hive cloud-setup)
export PATH="$HOME/bin:$PATH"
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"
RCEOF
      info "Updated $rc with rootless Docker environment."
    fi
  done

  systemctl --user start docker 2>/dev/null  || true
  systemctl --user enable docker 2>/dev/null || true

  success "Docker rootless installed."
}

check_docker() {
  step "Checking Docker"

  if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    DOCKER_VER=$(docker --version | awk '{print $3}' | tr -d ',')
    success "Docker found: $DOCKER_VER"
    return 0
  fi

  # Docker binary exists but daemon is not running
  if command -v docker &>/dev/null; then
    warn "Docker binary found but daemon is not running."
    if systemctl --user start docker 2>/dev/null; then
      sleep 2
      docker info &>/dev/null 2>&1 && { success "Started Docker rootless daemon."; return 0; }
    fi
    die "Docker daemon could not be started. Run: systemctl --user start docker"
  fi

  install_docker_rootless
}

# ---------------------------------------------------------------------------
# Step 4: Configure NVIDIA Container Toolkit for rootless Docker
# ---------------------------------------------------------------------------
configure_nvidia_toolkit() {
  step "Configuring NVIDIA Container Toolkit"

  if ! command -v nvidia-ctk &>/dev/null; then
    warn "nvidia-ctk not found. Attempting installation..."

    if command -v apt-get &>/dev/null; then
      # Distribution packages
      if ! command -v curl &>/dev/null; then apt-get install -y curl; fi
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --dearmor -o /tmp/nvidia-container-toolkit.gpg 2>/dev/null || {
          warn "Could not add NVIDIA GPG key. You may need root."
          warn "Ask your sysadmin to install nvidia-container-toolkit."
          warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
          return 1
        }
      DISTRO=$(. /etc/os-release && echo "$ID$VERSION_ID")
      curl -fsSL "https://nvidia.github.io/libnvidia-container/$DISTRO/libnvidia-container.list" \
        | sed 's#deb https://#deb [signed-by=/tmp/nvidia-container-toolkit.gpg] https://#g' \
        | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list &>/dev/null || {
          warn "Could not add NVIDIA apt repo (may need root). Skipping toolkit install."
          return 1
        }
      apt-get update -qq && apt-get install -y nvidia-container-toolkit
    else
      warn "Non-Debian system detected. Install nvidia-container-toolkit manually."
      warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
      return 1
    fi
  fi

  # Configure the NVIDIA runtime for rootless Docker
  DOCKER_CONFIG_DIR="$HOME/.config/docker"
  mkdir -p "$DOCKER_CONFIG_DIR"

  if [[ -f "$DOCKER_CONFIG_DIR/daemon.json" ]]; then
    info "Existing Docker daemon.json found. Checking for NVIDIA runtime entry..."
    if grep -q '"nvidia"' "$DOCKER_CONFIG_DIR/daemon.json" 2>/dev/null; then
      success "NVIDIA runtime already configured in daemon.json."
    else
      warn "NVIDIA runtime not found in daemon.json. Attempting nvidia-ctk configure..."
      nvidia-ctk runtime configure --runtime=docker \
        --config="$DOCKER_CONFIG_DIR/daemon.json" 2>/dev/null \
        && success "NVIDIA runtime configured." \
        || warn "nvidia-ctk configure failed. Manual edit may be required."
    fi
  else
    nvidia-ctk runtime configure --runtime=docker \
      --config="$DOCKER_CONFIG_DIR/daemon.json" 2>/dev/null \
      && success "NVIDIA runtime configured in $DOCKER_CONFIG_DIR/daemon.json." \
      || warn "nvidia-ctk configure failed. GPU passthrough may not work."
  fi

  # Restart rootless Docker to pick up the new runtime
  if systemctl --user restart docker 2>/dev/null; then
    sleep 2
    success "Docker rootless daemon restarted with NVIDIA runtime."
  else
    warn "Could not restart Docker daemon. You may need to restart it manually:"
    warn "  systemctl --user restart docker"
  fi
}

# ---------------------------------------------------------------------------
# Step 5: Verify GPU is accessible inside Docker
# ---------------------------------------------------------------------------
verify_gpu_in_docker() {
  step "Verifying GPU access inside Docker"

  if [[ "$GPU_AVAILABLE" != "true" ]]; then
    warn "No GPU detected — skipping GPU-in-Docker verification."
    return
  fi

  info "Running test container with --gpus all ..."
  if docker run --rm --gpus all \
      -e NVIDIA_VISIBLE_DEVICES=all \
      nvidia/cuda:12.0-base-ubuntu22.04 nvidia-smi &>/dev/null 2>&1; then
    success "GPU is accessible inside Docker containers."
  else
    warn "GPU is NOT accessible inside Docker."
    warn "Check your NVIDIA Container Toolkit configuration:"
    warn "  docker info | grep -i runtime"
    warn "  nvidia-ctk runtime configure --runtime=docker --config=\$HOME/.config/docker/daemon.json"
    warn "  systemctl --user restart docker"
  fi
}

# ---------------------------------------------------------------------------
# Step 6: Set up .env file and generate a secure API key
# ---------------------------------------------------------------------------
setup_env() {
  step "Setting up environment (.env)"

  cd "$PROJECT_ROOT"

  if [[ ! -f ".env.example" ]]; then
    warn ".env.example not found at $PROJECT_ROOT/.env.example — creating minimal .env"
    cat > .env <<ENVEOF
# Hive environment — generated by cloud-setup.sh
HIVE_API_KEY=$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -d '\n/+=' | head -c 32)
NEXT_PUBLIC_API_URL=http://${PUBLIC_IP:-localhost}:8000
ENVEOF
    return
  fi

  if [[ ! -f ".env" ]]; then
    cp ".env.example" ".env"
    success "Copied .env.example to .env"
  else
    info ".env already exists — keeping existing file."
  fi

  # Generate a random HIVE_API_KEY if not already set (cloud instances are exposed)
  if ! grep -q 'HIVE_API_KEY=' ".env" 2>/dev/null \
     || grep -q 'HIVE_API_KEY=$' ".env" 2>/dev/null \
     || grep -q 'HIVE_API_KEY=""' ".env" 2>/dev/null; then
    GENERATED_KEY=$(openssl rand -hex 32 2>/dev/null \
      || head -c 32 /dev/urandom | base64 | tr -d '\n/+=' | head -c 32)
    if grep -q 'HIVE_API_KEY' ".env" 2>/dev/null; then
      sed -i "s|^HIVE_API_KEY=.*|HIVE_API_KEY=$GENERATED_KEY|" ".env"
    else
      echo "HIVE_API_KEY=$GENERATED_KEY" >> ".env"
    fi
    success "Generated random HIVE_API_KEY and saved to .env"
    warn "Your API key: $GENERATED_KEY"
    warn "Keep this secret — it secures access to your Hive instance."
  fi

  # Set the public URL so the frontend knows where to find the API
  if [[ -n "$PUBLIC_IP" ]]; then
    if grep -q 'NEXT_PUBLIC_API_URL' ".env" 2>/dev/null; then
      sed -i "s|^NEXT_PUBLIC_API_URL=.*|NEXT_PUBLIC_API_URL=http://${PUBLIC_IP}:8000|" ".env"
    else
      echo "NEXT_PUBLIC_API_URL=http://${PUBLIC_IP}:8000" >> ".env"
    fi
    info "Set NEXT_PUBLIC_API_URL=http://${PUBLIC_IP}:8000"
  fi

  # Write recommended model to .env for the cloud init container
  if grep -q 'OLLAMA_PULL_MODEL' ".env" 2>/dev/null; then
    sed -i "s|^OLLAMA_PULL_MODEL=.*|OLLAMA_PULL_MODEL=${RECOMMENDED_MODEL}|" ".env"
  else
    echo "OLLAMA_PULL_MODEL=${RECOMMENDED_MODEL}" >> ".env"
  fi
  info "Ollama will pull: $RECOMMENDED_MODEL (set OLLAMA_PULL_MODEL in .env to override)"
}

# ---------------------------------------------------------------------------
# Step 7: Basic firewall (ufw) — only if available and not already configured
# ---------------------------------------------------------------------------
configure_firewall() {
  step "Configuring firewall (ufw)"

  if ! command -v ufw &>/dev/null; then
    info "ufw not found — skipping firewall configuration."
    info "If your cloud provider has security groups, open ports 3000 and 8000 there."
    return
  fi

  UFW_STATUS=$(ufw status 2>/dev/null | head -1 || echo "Status: inactive")
  if echo "$UFW_STATUS" | grep -q "inactive"; then
    warn "ufw is inactive. Enabling with sensible defaults..."
    # Allow SSH before enabling (prevents lockout)
    ufw allow ssh    2>/dev/null || warn "Could not allow SSH (may need root)"
    ufw allow 3000   2>/dev/null || warn "Could not allow port 3000"
    ufw allow 8000   2>/dev/null || warn "Could not allow port 8000"
    ufw --force enable 2>/dev/null || warn "Could not enable ufw (may need root)"
    success "ufw enabled: ports 22 (SSH), 3000 (UI), 8000 (API) open."
  else
    info "ufw is already active. Ensuring ports 3000 and 8000 are open..."
    ufw allow 3000 2>/dev/null || warn "Could not allow port 3000 (may need root)"
    ufw allow 8000 2>/dev/null || warn "Could not allow port 8000 (may need root)"
    success "Firewall rules updated."
  fi
}

# ---------------------------------------------------------------------------
# Step 8: Start the stack with cloud overrides
# ---------------------------------------------------------------------------
start_stack() {
  step "Starting Hive stack (cloud mode)"

  cd "$PROJECT_ROOT"

  # Build and start with the cloud overlay
  COMPOSE_FILES="-f docker-compose.yaml -f docker-compose.cloud.yaml"

  info "Command: docker compose $COMPOSE_FILES up -d --build"
  docker compose $COMPOSE_FILES up -d --build

  success "Hive cloud stack started."
}

# ---------------------------------------------------------------------------
# Step 9: Wait for services
# ---------------------------------------------------------------------------
wait_for_service() {
  local name="$1"
  local url="$2"
  local max_attempts="${3:-30}"
  local attempt=0

  info "Waiting for $name to be ready (up to $((max_attempts * 2))s)..."
  while (( attempt < max_attempts )); do
    if curl -sf "$url" &>/dev/null; then
      success "$name is ready."
      return 0
    fi
    attempt=$(( attempt + 1 ))
    sleep 2
  done

  warn "$name did not become healthy within $((max_attempts * 2))s."
  warn "Check logs: docker compose -f docker-compose.yaml -f docker-compose.cloud.yaml logs $name"
  return 1
}

# ---------------------------------------------------------------------------
# Step 10: Print access summary
# ---------------------------------------------------------------------------
print_summary() {
  local base_url="${PUBLIC_IP:-localhost}"

  echo ""
  echo -e "${BOLD}${GREEN}================================================================${RESET}"
  echo -e "${BOLD}${GREEN}  Hive is running on your cloud instance!${RESET}"
  echo -e "${BOLD}${GREEN}================================================================${RESET}"
  echo ""
  echo -e "  ${BOLD}Cloud Provider:${RESET}  $CLOUD_PROVIDER"
  echo -e "  ${BOLD}Instance Type:${RESET}   $INSTANCE_TYPE"
  echo -e "  ${BOLD}Public IP:${RESET}       ${PUBLIC_IP:-not detected}"
  echo ""
  echo -e "  ${BOLD}Web UI:${RESET}          http://${base_url}:3000"
  echo -e "  ${BOLD}API:${RESET}             http://${base_url}:8000"
  echo -e "  ${BOLD}API Docs:${RESET}        http://${base_url}:8000/docs"
  echo -e "  ${BOLD}Ollama:${RESET}          http://localhost:11434 (localhost only)"
  echo ""

  if [[ "$GPU_AVAILABLE" == "true" ]]; then
    echo -e "  ${GREEN}GPU:              $GPU_NAME x$GPU_COUNT (${GPU_VRAM_GB} GB VRAM each)${RESET}"
    echo -e "  ${GREEN}Flash Attention:  ENABLED (OLLAMA_FLASH_ATTENTION=1)${RESET}"
    echo -e "  ${GREEN}Parallel slots:   4 (OLLAMA_NUM_PARALLEL=4)${RESET}"
    echo -e "  ${GREEN}Pulling model:    $RECOMMENDED_MODEL${RESET}"
  else
    echo -e "  ${YELLOW}GPU:              not detected — Ollama will run on CPU${RESET}"
  fi

  echo ""
  echo -e "${BOLD}Security:${RESET}"
  echo -e "  A random HIVE_API_KEY was generated and saved to .env"
  echo -e "  Use it in API requests: Authorization: Bearer <key>"
  echo -e "  To rotate the key: regenerate HIVE_API_KEY in .env and restart"
  echo ""
  echo -e "${BOLD}Next steps:${RESET}"
  echo -e "  1. Add your LLM API keys to .env and restart: docker compose restart hive-api"
  echo -e "  2. For HTTPS, activate the Caddy profile:"
  echo -e "       HIVE_DOMAIN=your.domain.com docker compose \\"
  echo -e "         -f docker-compose.yaml -f docker-compose.cloud.yaml --profile caddy up -d"
  echo -e "  3. Pull a larger model: docker compose exec ollama ollama pull nemotron"
  echo -e "  4. View logs: docker compose -f docker-compose.yaml -f docker-compose.cloud.yaml logs -f"
  echo -e "  5. Stop everything: docker compose -f docker-compose.yaml -f docker-compose.cloud.yaml down"
  echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
detect_cloud
detect_gpu
check_docker

if [[ "$GPU_AVAILABLE" == "true" ]]; then
  configure_nvidia_toolkit
  verify_gpu_in_docker
else
  warn "No GPU detected. Ollama will run in CPU mode."
  warn "If you have an NVIDIA GPU, install drivers first:"
  warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
fi

setup_env
configure_firewall
start_stack

wait_for_service "hive-api" "http://localhost:8000/v1/health" 30 || true
wait_for_service "hive-web" "http://localhost:3000"            30 || true
wait_for_service "ollama"      "http://localhost:11434/api/tags"  30 || true

print_summary
