#!/bin/bash
# =============================================================================
# Hive — Ollama model setup
#
# Pulls a curated set of local models into the Ollama instance.
# GPU VRAM is detected automatically and the right model set is chosen.
# Can be run standalone or via Docker Compose:
#
#   ./scripts/ollama-setup.sh                             # auto-detect GPU, pull recommended models
#   ./scripts/ollama-setup.sh --all                       # pull everything regardless of GPU
#   docker compose exec ollama /scripts/ollama-setup.sh  # inside the container
#
# VRAM tiers (auto mode):
#   < 6 GB   — nemotron-mini only
#   6–12 GB  — nemotron-mini + nemotron-small
#   12–24 GB — nemotron-mini + nemotron-small + codellama
#   24 GB+   — everything including full nemotron (70B)
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

info()    { echo -e "${BLUE}[ollama-setup]${RESET} $*"; }
success() { echo -e "${GREEN}[ollama-setup]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[ollama-setup]${RESET} $*"; }
error()   { echo -e "${RED}[ollama-setup]${RESET} $*" >&2; }

# ---------------------------------------------------------------------------
# Parse flags
# ---------------------------------------------------------------------------
PULL_ALL=false
for arg in "$@"; do
  case "$arg" in
    --all) PULL_ALL=true ;;
    --help|-h)
      echo "Usage: $0 [--all]"
      echo "  --all   Pull every model regardless of GPU VRAM"
      exit 0
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Determine the Ollama endpoint
# ---------------------------------------------------------------------------
OLLAMA_ENDPOINT="${OLLAMA_HOST:-http://localhost:11434}"

# ---------------------------------------------------------------------------
# Determine how to invoke ollama
# ---------------------------------------------------------------------------
if command -v ollama &>/dev/null; then
  OLLAMA_CMD="ollama"
elif command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
  OLLAMA_CMD="docker compose exec -T ollama ollama"
else
  error "Neither the 'ollama' binary nor Docker Compose was found."
  error "Run this script from the project root, or inside the ollama container."
  exit 1
fi

# ---------------------------------------------------------------------------
# GPU auto-detection
# ---------------------------------------------------------------------------
# Outputs: VRAM_MB (integer), GPU_NAME (string), GPU_DETECTED (true/false)
# Falls back gracefully when nvidia-smi is absent.
GPU_DETECTED=false
GPU_NAME="none"
VRAM_MB=0
TOTAL_VRAM_MB=0

detect_gpu_vram() {
  if ! command -v nvidia-smi &>/dev/null; then
    warn "nvidia-smi not found — running in CPU mode."
    return
  fi

  local smi_out
  smi_out=$(nvidia-smi \
    --query-gpu=name,memory.total \
    --format=csv,noheader,nounits 2>/dev/null) || return

  if [[ -z "$smi_out" ]]; then
    warn "nvidia-smi returned no GPU information."
    return
  fi

  GPU_DETECTED=true
  local count=0

  while IFS=',' read -r name mem; do
    name="${name#"${name%%[![:space:]]*}"}"   # ltrim
    name="${name%"${name##*[![:space:]]}"}"   # rtrim
    mem="${mem#"${mem%%[![:space:]]*}"}"
    mem="${mem%"${mem##*[![:space:]]}"}"

    mem_int="${mem%.*}"   # drop decimal if any
    mem_int="${mem_int:-0}"

    TOTAL_VRAM_MB=$(( TOTAL_VRAM_MB + mem_int ))
    count=$(( count + 1 ))

    # Use first GPU name for the summary message
    if [[ $count -eq 1 ]]; then
      GPU_NAME="$name"
      VRAM_MB="$mem_int"
    fi
  done <<< "$smi_out"

  # For multi-GPU systems, use total VRAM for tier selection
  if [[ $count -gt 1 ]]; then
    GPU_NAME="${count}x GPU (${GPU_NAME} ...)"
    VRAM_MB="$TOTAL_VRAM_MB"
  fi
}

detect_gpu_vram

# Convert MB to GB for display
if [[ "$VRAM_MB" -gt 0 ]]; then
  VRAM_GB=$(( VRAM_MB / 1024 ))
else
  VRAM_GB=0
fi

# ---------------------------------------------------------------------------
# Determine which models to pull
# ---------------------------------------------------------------------------
# Arrays of model/description/size tuples (parallel arrays)
MODELS=()
DESCS=()
SIZES=()

add_model() {
  MODELS+=("$1")
  DESCS+=("$2")
  SIZES+=("$3")
}

if [[ "$PULL_ALL" == "true" ]]; then
  info "Manual override: --all flag set — pulling every model."
  add_model "nemotron-mini"  "NVIDIA Nemotron Mini 4B — lightweight, fast on any GPU"                            "~2.0 GB"
  add_model "nemotron-small" "NVIDIA Nemotron Small — RECOMMENDED default, great quality/speed balance"          "~5.0 GB"
  add_model "nemotron"       "NVIDIA Nemotron 70B — best local quality, 131K context (needs 48GB+ VRAM)"        "~40 GB"
  add_model "llama3.2:1b"   "Llama 3.2 1B — lightweight fallback, fast on CPU"                                  "~1.3 GB"
  add_model "llama3.1:8b"   "Llama 3.1 8B — balanced quality/speed alternative"                                 "~4.7 GB"
  add_model "codellama"      "Code generation, review, and explanation"                                          "~3.8 GB"

elif [[ "$GPU_DETECTED" == "false" ]] || [[ "$VRAM_GB" -lt 6 ]]; then
  # No GPU or < 6 GB VRAM: mini only
  if [[ "$GPU_DETECTED" == "true" ]]; then
    info "Detected: ${GPU_NAME} (${VRAM_GB} GB VRAM) — pulling CPU/mini models only."
  else
    info "No NVIDIA GPU detected — pulling CPU-compatible models only."
  fi
  add_model "nemotron-mini" "NVIDIA Nemotron Mini 4B — lightweight, runs on CPU and low-VRAM GPU" "~2.0 GB"
  add_model "llama3.2:1b"  "Llama 3.2 1B — tiny fallback, fast on CPU"                           "~1.3 GB"

elif [[ "$VRAM_GB" -lt 12 ]]; then
  # 6–12 GB VRAM: mini + small
  info "Detected: ${GPU_NAME} (${VRAM_GB} GB VRAM) — pulling recommended models..."
  add_model "nemotron-mini"  "NVIDIA Nemotron Mini 4B — fast, low-memory option"                 "~2.0 GB"
  add_model "nemotron-small" "NVIDIA Nemotron Small — RECOMMENDED for this GPU tier"             "~5.0 GB"
  add_model "llama3.2:1b"   "Llama 3.2 1B — tiny fallback"                                      "~1.3 GB"

elif [[ "$VRAM_GB" -lt 24 ]]; then
  # 12–24 GB VRAM: mini + small + codellama
  info "Detected: ${GPU_NAME} (${VRAM_GB} GB VRAM) — pulling recommended models..."
  add_model "nemotron-mini"  "NVIDIA Nemotron Mini 4B — fast, low-memory option"                 "~2.0 GB"
  add_model "nemotron-small" "NVIDIA Nemotron Small — RECOMMENDED default"                       "~5.0 GB"
  add_model "codellama"      "Code generation, review, and explanation"                          "~3.8 GB"
  add_model "llama3.2:1b"   "Llama 3.2 1B — tiny fallback"                                      "~1.3 GB"

elif [[ "$VRAM_GB" -lt 80 ]]; then
  # 24-80 GB VRAM: full suite with 70B nemotron
  info "Detected: ${GPU_NAME} (${VRAM_GB} GB VRAM) — pulling full model suite..."
  add_model "nemotron-mini"  "NVIDIA Nemotron Mini 4B — fast option"                             "~2.0 GB"
  add_model "nemotron-small" "NVIDIA Nemotron Small — balanced quality/speed"                    "~5.0 GB"
  add_model "nemotron"       "NVIDIA Nemotron 70B — best local quality, 131K context"            "~40 GB"
  add_model "codellama"      "Code generation, review, and explanation"                          "~3.8 GB"
  add_model "llama3.1:8b"   "Llama 3.1 8B — alternative balanced model"                         "~4.7 GB"

else
  # 80 GB+ VRAM: flagship tier — Nemotron 120B fits (quantized ~70GB)
  info "Detected: ${GPU_NAME} (${VRAM_GB} GB VRAM) — flagship GPU detected! Pulling full suite + 120B..."
  add_model "nemotron-mini"  "NVIDIA Nemotron Mini 4B — fast option"                             "~2.0 GB"
  add_model "nemotron-small" "NVIDIA Nemotron Small — balanced quality/speed"                    "~5.0 GB"
  add_model "nemotron"       "NVIDIA Nemotron 70B — high quality, 131K context"                  "~40 GB"
  add_model "nemotron:120b"  "NVIDIA Nemotron 120B — FLAGSHIP, maximum quality"                  "~70 GB"
  add_model "codellama"      "Code generation, review, and explanation"                          "~3.8 GB"
  add_model "llama3.1:8b"   "Llama 3.1 8B — alternative balanced model"                         "~4.7 GB"
fi

# ---------------------------------------------------------------------------
# Wait for Ollama to be ready
# ---------------------------------------------------------------------------
wait_for_ollama() {
  local max=30
  local attempt=0
  info "Waiting for Ollama at $OLLAMA_ENDPOINT..."
  while (( attempt < max )); do
    if curl -sf "$OLLAMA_ENDPOINT/api/tags" &>/dev/null; then
      success "Ollama is ready."
      return 0
    fi
    attempt=$(( attempt + 1 ))
    sleep 2
  done
  error "Ollama did not become available within 60 s at $OLLAMA_ENDPOINT."
  error "Make sure the stack is running: docker compose up -d"
  exit 1
}

# ---------------------------------------------------------------------------
# Disk usage helper
# ---------------------------------------------------------------------------
show_disk_usage() {
  local label="$1"
  local used
  if command -v df &>/dev/null; then
    used=$(df -h "$HOME/.ollama" 2>/dev/null | awk 'NR==2 {print $3 " used, " $4 " available"}' || echo "unknown")
    info "$label — disk: $used"
  fi
}

# ---------------------------------------------------------------------------
# Pull a model (idempotent)
# ---------------------------------------------------------------------------
pull_model() {
  local model="$1"
  local description="$2"
  local size_hint="$3"

  echo ""
  echo -e "${BOLD}--------------------------------------------------------------${RESET}"
  echo -e "${BOLD}Model: ${model}${RESET}  (${description})"
  echo -e "Approximate size: ${size_hint}"
  echo -e "${BOLD}--------------------------------------------------------------${RESET}"

  if $OLLAMA_CMD list 2>/dev/null | grep -q "^${model}"; then
    success "Already installed: $model — skipping."
    return 0
  fi

  info "Pulling $model..."
  show_disk_usage "Before pull"

  if $OLLAMA_CMD pull "$model"; then
    success "Pulled: $model"
    show_disk_usage "After pull"
  else
    warn "Failed to pull $model. You can retry manually:"
    warn "  $OLLAMA_CMD pull $model"
  fi
}

# ---------------------------------------------------------------------------
# List currently installed models
# ---------------------------------------------------------------------------
list_models() {
  echo ""
  info "Currently installed models:"
  $OLLAMA_CMD list 2>/dev/null || warn "Could not list models."
  echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}=====================================================${RESET}"
echo -e "${BOLD}  Hive — Ollama Model Setup${RESET}"
echo -e "${BOLD}  NVIDIA Nemotron Edition${RESET}"
echo -e "${BOLD}=====================================================${RESET}"
echo ""
info "Endpoint: $OLLAMA_ENDPOINT"
echo ""

wait_for_ollama
list_models
show_disk_usage "Disk before all pulls"

for i in "${!MODELS[@]}"; do
  pull_model "${MODELS[$i]}" "${DESCS[$i]}" "${SIZES[$i]}"
done

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}${GREEN}=====================================================${RESET}"
echo -e "${BOLD}${GREEN}  Ollama setup complete!${RESET}"
echo -e "${BOLD}${GREEN}=====================================================${RESET}"
list_models
show_disk_usage "Total disk after setup"

echo ""
echo -e "${BOLD}Tips:${RESET}"
echo "  Run a quick Nemotron test:"
echo "    $OLLAMA_CMD run nemotron-small 'Hello! What can you do?'"
echo ""
echo "  Pull everything manually (ignores GPU tier):"
echo "    ./scripts/ollama-setup.sh --all"
echo ""
echo "  Pull additional models:"
echo "    $OLLAMA_CMD pull mistral"
echo "    $OLLAMA_CMD pull phi3"
echo "    $OLLAMA_CMD pull gemma2:2b"
echo ""
echo "  Browse the full library: https://ollama.com/library"
echo "  NVIDIA Nemotron on Ollama: https://ollama.com/library/nemotron"
echo ""
