#!/usr/bin/env bash
# integration-test-install.sh — end-to-end install verification in a
# disposable container. Catches regressions that only surface on a real
# fresh rig.
#
# What this does:
#   1. Spins up a clean Ubuntu 24.04 container with NO nvidia-smi, NO
#      Ollama, NO Python deps — same shape as a fresh cloud-desktop user.
#   2. Stubs `nvidia-smi` so install.sh's GPU detection returns empty —
#      simulates the failure mode that hit the real rig on 2026-05-22
#      where GPU_NAME ended up empty and Ollama was never installed.
#   3. Sets NVH_INSTALL_LAUNCH=0 so the install completes synchronously
#      (doesn't block on a foreground WebUI). The headless assertions
#      below then verify the install did what it should have done.
#   4. Asserts the install left the user with a working stack:
#        - $NVH_HOME/bin/ollama is an executable file (not a directory!
#          — see PR #79 EACCES history)
#        - At least one model is in $NVH_HOME/models OR the install log
#          shows pull_nvwizard_model_cli was invoked with the fallback
#          chain
#        - $NVH_HOME/logs/install.log captured the run
#
# How to run:
#   ./tools/integration-test-install.sh
#
# This is intentionally NOT in the default CI matrix because it
# downloads Ollama (~600 MB) and pulls a model (~1.7-7.9 GB). Run it
# locally before merging anything that touches install.sh. CI can opt
# in via a manually-triggered workflow when the team is ready to pay
# the runtime + bandwidth.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_TAG="nvhive-install-integration-test:latest"

# Allow the user to point at a specific test image — useful if they want
# to test against a custom base.
DOCKERFILE="${NVH_TEST_DOCKERFILE:-}"

# Allow skipping the model pull for a fast smoke test that only verifies
# Ollama itself installs + binds.
NVH_TEST_SKIP_MODEL="${NVH_TEST_SKIP_MODEL:-0}"

cleanup() {
    docker rm -f nvhive-test-rig >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "================================================================"
echo "  nvHive install integration test"
echo "================================================================"
echo "  Repo:         $REPO_ROOT"
echo "  Image tag:    $IMAGE_TAG"
echo "  Skip model:   $NVH_TEST_SKIP_MODEL  (NVH_TEST_SKIP_MODEL=1 to skip)"
echo "================================================================"
echo

# Build a thin test image that has just enough OS to run install.sh.
# Stubbed `nvidia-smi` returns nothing so GPU_NAME ends up empty —
# the exact failure mode that bit the real rig on 2026-05-22.
BUILD_CTX="$(mktemp -d)"
trap 'cleanup; rm -rf "$BUILD_CTX"' EXIT

cat >"$BUILD_CTX/Dockerfile" <<'DOCKERFILE'
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl git ca-certificates \
    python3 python3-venv python3-pip \
    file procps lsof \
    && rm -rf /var/lib/apt/lists/*

# Stub nvidia-smi to return empty — simulates the cloud-rig failure
# mode where the GPU is present but userspace tools are missing /
# off-PATH. install.sh must STILL install Ollama on this rig.
RUN printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/nvidia-smi && \
    chmod +x /usr/local/bin/nvidia-smi

# Test user — install.sh refuses to run as root.
RUN useradd -m -s /bin/bash kiosk
USER kiosk
WORKDIR /home/kiosk

# Copy the repo into the test home (faster than git clone in container).
COPY --chown=kiosk:kiosk . /home/kiosk/nvHive-repo
DOCKERFILE

echo "[1/4] Building test image (cached unless Ubuntu / repo changed)..."
docker build -t "$IMAGE_TAG" -f "$BUILD_CTX/Dockerfile" "$REPO_ROOT" >/dev/null

echo "[2/4] Running install.sh inside the container..."
docker run --name nvhive-test-rig \
    -e NVH_INSTALL_LAUNCH=0 \
    -e NVH_INSTALL_MODEL_DOWNLOAD="${NVH_TEST_SKIP_MODEL:+0}" \
    "$IMAGE_TAG" \
    bash -c "
        set -e
        cd /home/kiosk/nvHive-repo
        # Run install.sh against the local clone so we don't hit
        # github.com for the source — same code path otherwise.
        NVH_REPO=/home/kiosk/nvHive-repo bash install.sh 2>&1 | tail -200
    "

echo "[3/4] Asserting post-install state..."
docker start nvhive-test-rig >/dev/null

# Assertion 1: Ollama binary exists as an executable FILE (not a dir).
docker exec nvhive-test-rig bash -c '
    set -e
    if [ ! -x "$HOME/nvhive/bin/ollama" ]; then
        echo "FAIL: \$HOME/nvhive/bin/ollama is not an executable file"
        ls -la "$HOME/nvhive/bin/" || true
        exit 1
    fi
    if [ -d "$HOME/nvhive/bin/ollama" ]; then
        echo "FAIL: \$HOME/nvhive/bin/ollama is a DIRECTORY (PR #79 regression)"
        exit 1
    fi
    echo "  OK: \$HOME/nvhive/bin/ollama is a regular executable file"
'

# Assertion 2: install.log captured the run + names the right steps.
docker exec nvhive-test-rig bash -c '
    set -e
    log="$HOME/nvhive/logs/install.log"
    if [ ! -s "$log" ]; then
        echo "FAIL: $log missing or empty"
        exit 1
    fi
    if ! grep -q "install_rootless_ollama_binary\|Downloading Ollama\|Installing Ollama\|Ollama " "$log"; then
        echo "FAIL: install.log has no Ollama-install evidence"
        tail -50 "$log"
        exit 1
    fi
    echo "  OK: install.log captured Ollama install steps"
'

# Assertion 3 (skip-able): model-pull code path was hit.
if [ "$NVH_TEST_SKIP_MODEL" != "1" ]; then
    docker exec nvhive-test-rig bash -c '
        set -e
        log="$HOME/nvhive/logs/install.log"
        if ! grep -q "pull_nvwizard_model_cli\|Pulling.*model\|nvwizard_fallback_chain\|bootstrap_omni_via_hf\|moondream\|llama3.2-vision" "$log"; then
            echo "FAIL: install.log has no model-pull evidence (the WebUI-deferred regression)"
            tail -100 "$log"
            exit 1
        fi
        echo "  OK: install.log captured model-pull attempt"
    '
fi

echo "[4/4] All assertions passed."
echo
echo "================================================================"
echo "  PASS — install path leaves a usable rootless workspace"
echo "================================================================"
