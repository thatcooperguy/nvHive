#!/usr/bin/env bash
# NVHive Linux GPU desktop launcher.
# Rootless, mount-aware, and safe to run from a GitHub download link.

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; D='\033[0;90m'; R='\033[0;31m'; N='\033[0m'
REPO_RAW="${NVH_REPO_RAW:-https://raw.githubusercontent.com/thatcooperguy/nvHive/main}"

# Pick the release binary for this CPU: x86_64 cloud desktops or aarch64
# DGX Spark (GB10, DGX OS). NVH_BINARY_URL overrides the choice entirely.
binary_arch() {
    case "$(uname -m 2>/dev/null)" in
        x86_64|amd64) echo "x86_64" ;;
        aarch64|arm64) echo "arm64" ;;
        *) echo "unsupported" ;;
    esac
}
BINARY_ARCH="$(binary_arch)"
LINUX_BINARY_URL="${NVH_BINARY_URL:-https://github.com/thatcooperguy/nvHive/releases/latest/download/nvh-linux-${BINARY_ARCH}}"

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
    local env_name env_value root child scored score home best_score best_home home_free
    if [ -d "$HOME/nvh/repo" ] && [ ! -d "$HOME/.nvh/repo" ]; then
        printf '%s\n' "$HOME/nvh"
        return 0
    fi
    if [ -f "$HOME/nvhive/nvh-env.sh" ] || [ -d "$HOME/nvhive/repo" ] || [ -d "$HOME/nvhive/models" ]; then
        printf '%s\n' "$HOME/nvhive"
        return 0
    fi
    if [ -n "${HOME:-}" ] && [ -d "$HOME" ] && [ -w "$HOME" ]; then
        home_free="$(free_gb_for_path "$HOME")"
        home_free="${home_free:-0}"
        if [ "$home_free" -ge 100 ]; then
            printf '%s\n' "$HOME/nvhive"
            return 0
        fi
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
    if [ "$BINARY_ARCH" = "unsupported" ] && [ -z "${NVH_BINARY_URL:-}" ]; then
        echo -e "${R}No nvHive binary is published for $(uname -m); install Python 3.11+ or set NVH_BINARY_URL.${N}"
        exit 1
    fi
    mkdir -p "$NVH_HOME/bin"
    echo -e "${Y}Python was not found, so nvHive is using the single-file Linux binary (${BINARY_ARCH}).${N}"
    echo -e "${D}Downloading: $LINUX_BINARY_URL${N}"
    local tmp="$NVH_HOME/bin/.nvh.download"
    if ! fetch_url "$LINUX_BINARY_URL" "$tmp"; then
        rm -f "$tmp"
        echo -e "${R}Could not download the nvHive binary for ${BINARY_ARCH}.${N}"
        if [ "$BINARY_ARCH" = "arm64" ] && [ -z "${NVH_BINARY_URL:-}" ]; then
            echo -e "${Y}arm64 (DGX Spark) binaries ship from nvHive 0.43; the latest release may predate them.${N}"
        fi
        echo -e "${Y}Install Python 3.11+ (DGX OS and Ubuntu ship python3) and re-run, or set NVH_BINARY_URL.${N}"
        exit 1
    fi
    verify_binary_checksum "$tmp" || { rm -f "$tmp"; exit 1; }
    mv -f "$tmp" "$NVH_HOME/bin/nvh"
    chmod +x "$NVH_HOME/bin/nvh"
}

# fetch_url URL DEST — download with curl or wget; non-zero on any failure
# (including HTTP 404 for a release that has no binary for this CPU yet).
fetch_url() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        echo -e "${R}Need curl or wget to download the nvHive binary.${N}"
        return 1
    fi
}

# verify_binary_checksum FILE — check FILE against the release's SHA256SUMS.
# Releases from 0.43 publish that file; when it is absent (older releases or a
# custom NVH_BINARY_URL) the download is used unverified with a loud warning.
verify_binary_checksum() {
    local file="$1" sums="$1.sums" asset expected actual
    asset="$(basename "$LINUX_BINARY_URL")"
    if ! fetch_url "${LINUX_BINARY_URL%/*}/SHA256SUMS" "$sums" 2>/dev/null; then
        rm -f "$sums"
        echo -e "${Y}No SHA256SUMS published for this release; using the binary unverified.${N}"
        return 0
    fi
    expected="$(awk -v a="$asset" '$2 == a || $2 == "*" a {print $1}' "$sums" | head -n1)"
    rm -f "$sums"
    if [ -z "$expected" ]; then
        echo -e "${Y}SHA256SUMS has no entry for ${asset}; using the binary unverified.${N}"
        return 0
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$file" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        actual="$(shasum -a 256 "$file" | awk '{print $1}')"
    else
        echo -e "${Y}No sha256sum/shasum available; using the binary unverified.${N}"
        return 0
    fi
    if [ "$actual" != "$expected" ]; then
        echo -e "${R}Checksum mismatch for ${asset}: expected ${expected}, got ${actual}. Refusing to run it.${N}"
        return 1
    fi
    echo -e "${G}Verified ${asset} against SHA256SUMS.${N}"
}

if [ -z "${NVH_HOME:-}" ]; then
    if NVH_HOME="$(detect_home)"; then
        echo -e "${G}Mount autopilot selected ${NVH_HOME}${N}"
    else
        fallback_home="$HOME/.nvh"
        if [ "${NVH_ALLOW_EPHEMERAL:-0}" != "1" ]; then
            echo -e "${R}No persistent mount was found.${N}" >&2
            echo "" >&2
            echo "nvHive could not locate a persistent storage path (>=20GB free) under" >&2
            echo "any of: /mnt, /media, /workspace, /data, /persistent, /storage, or under" >&2
            echo "\$HOME with at least 100GB free. On cloud GPU desktops the OS disk is" >&2
            echo "usually ephemeral — falling back to ${fallback_home} would mean models," >&2
            echo "configs, and Studio Packs vanish on reconnect." >&2
            echo "" >&2
            echo "Fix one of:" >&2
            echo "  1. Set NVH_HOME=/path/to/persistent/mount and rerun this script." >&2
            echo "  2. Set NVH_ALLOW_EPHEMERAL=1 to acknowledge the risk and use ${fallback_home}." >&2
            echo "  3. Mount a persistent volume at /mnt, /workspace, or /data." >&2
            exit 2
        fi
        NVH_HOME="$fallback_home"
        echo -e "${Y}NVH_ALLOW_EPHEMERAL=1 — using ${NVH_HOME}. Data may be lost on reconnect.${N}"
    fi
fi
export NVH_HOME
export NVH_BIN="$NVH_HOME/bin"
export NVH_NO_OS_MOD="${NVH_NO_OS_MOD:-1}"
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
