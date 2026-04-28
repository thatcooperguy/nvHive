#!/usr/bin/env bash
# NVHive Linux GPU desktop launcher.
# Rootless, mount-aware, and safe to run from a GitHub download link.

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; D='\033[0;90m'; R='\033[0;31m'; N='\033[0m'
REPO_RAW="${NVH_REPO_RAW:-https://raw.githubusercontent.com/thatcooperguy/nvHive/main}"
LINUX_BINARY_URL="${NVH_BINARY_URL:-https://github.com/thatcooperguy/nvHive/releases/latest/download/nvh-linux-x86_64}"

free_gb_for_path() {
    df -Pk "$1" 2>/dev/null | awk 'NR==2 {printf "%d", $4 / 1048576}'
}

score_candidate() {
    local base="${1%/}"
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
        *persist*|*Persist*|*workspace*|*Workspace*|*project*|*Project*|*data*|*Data*) score=$((score + 20)) ;;
        *tmp*|*cache*|*Cache*) score=$((score - 40)) ;;
    esac
    free_gb="$(free_gb_for_path "$base")"
    free_gb="${free_gb:-0}"
    if [ "$free_gb" -ge 100 ]; then
        score=$((score + 35))
    elif [ "$free_gb" -ge 50 ]; then
        score=$((score + 30))
    elif [ "$free_gb" -ge 20 ]; then
        score=$((score + 20))
    elif [ "$free_gb" -lt 10 ]; then
        score=$((score - 15))
    fi
    if [ -f "$home/nvh-env.sh" ] || [ -d "$home/repo" ] || [ -d "$home/models" ]; then
        score=$((score + 30))
    fi
    printf '%s|%s\n' "$score" "$home"
}

detect_home() {
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
        if scored="$(score_candidate "$root")"; then
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
            if scored="$(score_candidate "$child")"; then
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

find_python() {
    for py in python3.12 python3.11 python3.10 python3; do
        if command -v "$py" >/dev/null 2>&1; then
            echo "$py"
            return 0
        fi
    done
    return 1
}

install_binary() {
    mkdir -p "$NVH_HOME/bin"
    echo -e "${Y}Python was not found, so nvHive is using the single-file Linux binary.${N}"
    echo -e "${D}Downloading: $LINUX_BINARY_URL${N}"
    if command -v curl >/dev/null 2>&1; then
        curl -fL "$LINUX_BINARY_URL" -o "$NVH_HOME/bin/nvh"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$NVH_HOME/bin/nvh" "$LINUX_BINARY_URL"
    else
        echo -e "${R}Need curl or wget to download the nvHive binary.${N}"
        exit 1
    fi
    chmod +x "$NVH_HOME/bin/nvh"
}

if [ -z "${NVH_HOME:-}" ]; then
    if NVH_HOME="$(detect_home)"; then
        echo -e "${G}Mount autopilot selected ${NVH_HOME}${N}"
    else
        NVH_HOME="$HOME/.nvh"
        echo -e "${Y}No persistent mount was obvious; using ${NVH_HOME}.${N}"
    fi
fi
export NVH_HOME
export NVH_BIN="$NVH_HOME/bin"
export PATH="$NVH_BIN:$PATH"

echo ""
echo -e "${G}NVHive Linux Launch${N}"
echo -e "${D}Persistent home: $NVH_HOME${N}"

if [ -f "$NVH_HOME/nvh-env.sh" ]; then
    # shellcheck disable=SC1091
    source "$NVH_HOME/nvh-env.sh"
fi

if ! command -v nvh >/dev/null 2>&1; then
    if [ -x "$NVH_HOME/venv/bin/nvh" ]; then
        export PATH="$NVH_HOME/venv/bin:$PATH"
    elif [ "${NVH_USE_BINARY:-0}" = "1" ] || ! find_python >/dev/null; then
        install_binary
    else
        echo -e "${G}Installing nvHive rootlessly into ${NVH_HOME}${N}"
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL "$REPO_RAW/install.sh" | bash
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- "$REPO_RAW/install.sh" | bash
        else
            echo -e "${R}Need curl or wget to install nvHive.${N}"
            exit 1
        fi
        [ -f "$NVH_HOME/nvh-env.sh" ] && source "$NVH_HOME/nvh-env.sh"
        [ -x "$NVH_HOME/venv/bin/nvh" ] && export PATH="$NVH_HOME/venv/bin:$PATH"
    fi
fi

if ! command -v nvh >/dev/null 2>&1; then
    echo -e "${R}nvh is still not on PATH after install.${N}"
    echo -e "${D}Try: source \"$NVH_HOME/nvh-env.sh\"${N}"
    exit 1
fi

echo -e "${G}Launching nvHive workstation and WebUI.${N}"
nvh workstation --home-dir "$NVH_HOME" --launch -y
