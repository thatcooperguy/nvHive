#!/usr/bin/env bash
# Install the exact clean, committed checkout in a disposable Ubuntu container.
# NVH_TEST_SKIP_MODEL=1 checks binary/daemon/CLI/config only (no model download).
# The default 0 additionally checks the existing model-pull attempt path.
# Requires Docker, Git, and complete Git history (CI checkout: fetch-depth: 0).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NVH_TEST_SKIP_MODEL="${NVH_TEST_SKIP_MODEL:-0}"
case "$NVH_TEST_SKIP_MODEL" in 0|1) ;; *) echo 'NVH_TEST_SKIP_MODEL must be 0 or 1' >&2; exit 2 ;; esac
if [ -n "${NVH_TEST_DOCKERFILE:-}" ]; then
    echo 'NVH_TEST_DOCKERFILE was previously ignored; custom images are not supported by this test.' >&2
    exit 2
fi
git -C "$REPO_ROOT" diff --quiet HEAD -- || {
    echo 'Commit tracked changes before testing; the installer must match the candidate revision.' >&2
    exit 2
}
if [ "$(git -C "$REPO_ROOT" rev-parse --is-shallow-repository)" != false ]; then
    echo 'A complete checkout is required for the isolated Git bundle (fetch-depth: 0).' >&2
    exit 2
fi
EXPECTED_REV="$(git -C "$REPO_ROOT" rev-parse --verify HEAD)"
EXPECTED_TREE="$(git -C "$REPO_ROOT" rev-parse 'HEAD^{tree}')"
BUILD_CTX="$(mktemp -d)"
RUN_ID="$(basename "$BUILD_CTX" | tr '[:upper:]' '[:lower:]')"
IMAGE_TAG="nvhive-install-test:$RUN_ID"
CONTAINER_NAME="nvhive-install-$RUN_ID"
CONTAINER_ID=''
IMAGE_BUILT=0
cleanup() {
    # Remove only resources created by this invocation; never a fixed shared name.
    if [ -n "$CONTAINER_ID" ]; then docker rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true; fi
    if [ "$IMAGE_BUILT" = 1 ]; then docker image rm "$IMAGE_TAG" >/dev/null 2>&1 || true; fi
    rm -rf -- "$BUILD_CTX"
}
trap cleanup EXIT
git -C "$REPO_ROOT" bundle create "$BUILD_CTX/candidate.bundle" HEAD
git -C "$REPO_ROOT" bundle verify "$BUILD_CTX/candidate.bundle"

# No working tree COPY: untracked files, local .git/config credentials, caches,
# and host home files cannot enter the build context. Only committed objects do.
cat > "$BUILD_CTX/Dockerfile" <<'DOCKERFILE'
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl git ca-certificates python3 python3-venv python3-pip \
    file procps lsof zstd \
    && rm -rf /var/lib/apt/lists/*
RUN printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/nvidia-smi && \
    chmod +x /usr/local/bin/nvidia-smi
RUN useradd -m -s /bin/bash kiosk
USER kiosk
WORKDIR /home/kiosk
COPY --chown=kiosk:kiosk candidate.bundle /home/kiosk/candidate.bundle
RUN git init -q /home/kiosk/candidate && \
    git -C /home/kiosk/candidate fetch -q /home/kiosk/candidate.bundle HEAD:refs/heads/main && \
    git -C /home/kiosk/candidate checkout -q main && \
    git config --global url.file:///home/kiosk/candidate.insteadOf https://github.com/thatcooperguy/nvHive.git
DOCKERFILE

echo "Installing candidate $EXPECTED_REV (tree $EXPECTED_TREE); skip model: $NVH_TEST_SKIP_MODEL"
docker build -t "$IMAGE_TAG" -f "$BUILD_CTX/Dockerfile" "$BUILD_CTX"
IMAGE_BUILT=1
MODEL_DOWNLOAD_ENV=auto
if [ "$NVH_TEST_SKIP_MODEL" = 1 ]; then MODEL_DOWNLOAD_ENV=0; fi
# No GPU passthrough, privileged mode, host networking, published ports, volumes,
# or inherited provider credentials. Network downloads stay inside the container.
CONTAINER_ID="$(docker run -d --name "$CONTAINER_NAME" \
    -e NVH_INSTALL_LAUNCH=0 \
    -e NVH_HOME=/home/kiosk/nvhive \
    -e NVH_CONFIG=/home/kiosk/custom-config \
    -e NVH_INSTALL_MODEL_DOWNLOAD="$MODEL_DOWNLOAD_ENV" \
    -e NVH_TEST_EXPECTED_REV="$EXPECTED_REV" \
    -e NVH_TEST_EXPECTED_TREE="$EXPECTED_TREE" \
    -e NVH_TEST_SKIP_MODEL="$NVH_TEST_SKIP_MODEL" \
    "$IMAGE_TAG" sleep infinity)"

docker exec "$CONTAINER_ID" bash -c '
    set -euo pipefail
    cd /home/kiosk/candidate
    test "$(git rev-parse HEAD)" = "$NVH_TEST_EXPECTED_REV"
    # Both the installer and its installed package must come from this candidate.
    # The disposable user Git redirect makes the normal clone URL resolve locally.
    # A GitHub archive fallback has no matching Git HEAD and fails the audit below.
    bash install.sh 2>&1 | tail -200
'

docker exec "$CONTAINER_ID" bash -c '
    set -euo pipefail
    expected_home="$NVH_HOME"
    expected_config="$NVH_CONFIG"
    # Simulate a fresh shell without inherited workspace/config overrides.
    unset NVH_HOME NVHIVE_HOME NVH_CONFIG HIVE_CONFIG_HOME
    source "$expected_home/nvh-env.sh"
    test "$NVH_HOME" = "$expected_home"
    test "$NVH_CONFIG" = "$expected_config"
    test "$HIVE_CONFIG_HOME" = "$expected_config"
    test -s "$NVH_CONFIG/config.yaml"
    test "$(git -C "$NVH_HOME/repo" rev-parse HEAD)" = "$NVH_TEST_EXPECTED_REV"
    test "$(git -C "$NVH_HOME/repo" rev-parse "HEAD^{tree}")" = "$NVH_TEST_EXPECTED_TREE"
    git -C "$NVH_HOME/repo" diff --quiet HEAD --
    test -f "$NVH_HOME/bin/ollama"
    test -x "$NVH_HOME/bin/ollama"
    test -s "$NVH_HOME/logs/install.log"
    "$NVH_HOME/venv/bin/nvh" version
    "$NVH_HOME/venv/bin/nvh" --help >/dev/null
    "$NVH_HOME/venv/bin/python" -c "import os, pathlib, nvh; from nvh.integrations.workspace.storage import storage_layout; h=pathlib.Path(os.environ[\"NVH_HOME\"]); c=pathlib.Path(os.environ[\"NVH_CONFIG\"]); s=storage_layout(); assert s.home==h and s.config_dir==c; assert pathlib.Path(nvh.__file__).resolve().is_relative_to(h/\"repo\")"
    # A binary alone does not prove the local daemon bound successfully.
    curl --fail --silent --show-error --max-time 10 http://localhost:11434/api/tags > /home/kiosk/ollama-tags.json
    if [ "$NVH_TEST_SKIP_MODEL" = 1 ]; then
        "$NVH_HOME/venv/bin/python" -c "import json; assert not json.load(open(\"/home/kiosk/ollama-tags.json\"))[\"models\"]"
        test -z "$(find "$OLLAMA_MODELS" -type f -print -quit)"
    else
        grep -qE "Downloading .* for AI Wizard|pull_nvwizard_model_cli|nvwizard_fallback_chain|bootstrap_omni_via_hf|model-pull\.log|Switching to a smaller model" "$NVH_HOME/logs/install.log"
    fi
'
echo "PASS: candidate $EXPECTED_REV; CLI, saved workspace/config, and rootless Ollama daemon."
if [ "$NVH_TEST_SKIP_MODEL" = 1 ]; then
    echo 'No-model smoke only: no model download, inference, GPU, or WebUI validation.'
else
    echo 'Model-pull attempt checked; this is not an inference-quality validation.'
fi
