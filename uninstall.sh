#!/bin/bash
# Rootless nvHive uninstall/reset helper.
#
# Default mode removes the application runtime and launch hooks but keeps large
# user data such as models, ComfyUI workspaces, projects, outputs, and config.
# Use --purge to delete the entire NVH_HOME folder for a true from-scratch test.

set -euo pipefail

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; D='\033[0;90m'; N='\033[0m'

ASSUME_YES=false
PURGE=false
DRY_RUN=false
FORCE=false
REQUESTED_HOME="${NVH_HOME:-}"

usage() {
    cat <<EOF
nvHive rootless uninstall

Usage:
  bash uninstall.sh [--home PATH] [--purge] [-y] [--dry-run]

Options:
  --home PATH   Uninstall a specific NVH_HOME.
  --purge       Delete all nvHive data under NVH_HOME. Use for a clean reinstall.
  -y, --yes     Do not prompt.
  --dry-run     Show what would be removed.
  --force       Allow removal even if nvHive markers are missing.
  -h, --help    Show this help.

Examples:
  bash "\$HOME/nvhive/uninstall.sh" --purge -y
  curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/uninstall.sh | bash -s -- --purge -y
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --home)
            [ "$#" -ge 2 ] || { echo "Missing value for --home" >&2; exit 2; }
            REQUESTED_HOME="$2"
            shift 2
            ;;
        --purge)
            PURGE=true
            shift
            ;;
        -y|--yes)
            ASSUME_YES=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --force)
            FORCE=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 2
            ;;
    esac
done

expand_path() {
    local path="$1"
    path="${path/#\~/$HOME}"
    printf '%s\n' "$path"
}

candidate_has_nvhive() {
    local path="$1"
    [ -f "$path/nvh-env.sh" ] || [ -d "$path/repo" ] || [ -d "$path/config" ] || [ -d "$path/models" ]
}

home_from_shell_rc() {
    local rc line env_path
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        [ -f "$rc" ] || continue
        line="$(grep -E 'source "?.*nvh-env\.sh' "$rc" 2>/dev/null | tail -1 || true)"
        [ -n "$line" ] || continue
        env_path="$(printf '%s\n' "$line" | sed -E 's/.*source "?([^"]*nvh-env\.sh)"?.*/\1/')"
        env_path="$(expand_path "$env_path")"
        [ -n "$env_path" ] && dirname "$env_path" && return 0
    done
    return 1
}

detect_nvh_home() {
    local path
    if [ -n "$REQUESTED_HOME" ]; then
        expand_path "$REQUESTED_HOME"
        return 0
    fi
    for path in "$HOME/nvhive" "$HOME/.nvh" "$HOME/nvh"; do
        if candidate_has_nvhive "$path"; then
            printf '%s\n' "$path"
            return 0
        fi
    done
    home_from_shell_rc && return 0
    return 1
}

NVH_HOME="$(detect_nvh_home)" || {
    echo -e "${R}Could not find an nvHive install.${N}"
    echo "Set NVH_HOME or pass --home PATH."
    exit 1
}
NVH_HOME="${NVH_HOME%/}"
NVH_BIN="${NVH_BIN:-$NVH_HOME/bin}"

safe_to_remove_home() {
    local path="$1"
    [ -n "$path" ] || return 1
    [ "$path" != "/" ] || return 1
    [ "$path" != "$HOME" ] || return 1
    [ "$path" != "$HOME/" ] || return 1
    case "$path" in
        "$HOME"/.nvh|"$HOME"/nvh|"$HOME"/nvhive|/mnt/*|/media/*|/workspace*|/data*|/persistent*|/storage*)
            return 0
            ;;
    esac
    candidate_has_nvhive "$path"
}

if ! safe_to_remove_home "$NVH_HOME"; then
    if [ "$FORCE" != "true" ]; then
        echo -e "${R}Refusing to remove unexpected path: $NVH_HOME${N}"
        echo "Pass --force only if this is definitely your nvHive install directory."
        exit 1
    fi
fi

run_or_print() {
    if [ "$DRY_RUN" = "true" ]; then
        printf 'dry-run: %s\n' "$*"
    else
        "$@"
    fi
}

remove_path() {
    local path="$1"
    [ -e "$path" ] || [ -L "$path" ] || return 0
    echo -e "${D}Removing $path${N}"
    run_or_print rm -rf -- "$path"
}

remove_owned_file() {
    local path="$1"
    local target=""
    [ -e "$path" ] || [ -L "$path" ] || return 0
    if [ -L "$path" ]; then
        target="$(readlink "$path" 2>/dev/null || true)"
        case "$target" in
            "$NVH_HOME"/*|"$NVH_BIN"/*) remove_path "$path"; return 0 ;;
        esac
    fi
    if grep -qF "$NVH_HOME" "$path" 2>/dev/null; then
        remove_path "$path"
    fi
}

clean_shell_rc() {
    local rc tmp
    for rc in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
        [ -f "$rc" ] || continue
        tmp="$rc.nvhive-uninstall.tmp"
        awk -v home="$NVH_HOME" -v bin="$NVH_BIN" '
            $0 == "# >>> nvhive rootless env >>>" { skip=1; changed=1; next }
            $0 == "# <<< nvhive rootless env <<<" { skip=0; next }
            index($0, home "/nvh-env.sh") { changed=1; next }
            index($0, home "/venv/bin") { changed=1; next }
            index($0, bin "/ollama") { changed=1; next }
            skip { changed=1; next }
            { print }
            END { if (changed) exit 0; exit 0 }
        ' "$rc" > "$tmp"
        if cmp -s "$rc" "$tmp"; then
            rm -f "$tmp"
        else
            echo -e "${D}Cleaning shell hook in $rc${N}"
            if [ "$DRY_RUN" = "true" ]; then
                rm -f "$tmp"
            else
                mv "$tmp" "$rc"
            fi
        fi
    done
}

echo ""
echo -e "${G}nvHive rootless uninstall${N}"
echo -e "  Install dir: ${G}$NVH_HOME${N}"
if [ "$PURGE" = "true" ]; then
    echo -e "  Mode:        ${Y}purge everything under NVH_HOME${N}"
else
    echo -e "  Mode:        remove app runtime, keep models/config/projects"
fi
echo ""

if [ "$ASSUME_YES" != "true" ] && [ "$DRY_RUN" != "true" ]; then
    if [ ! -t 0 ]; then
        echo "Non-interactive uninstall requires -y or --dry-run."
        exit 2
    fi
    read -r -p "Continue? [y/N] " ANSWER
    case "${ANSWER:-N}" in
        y|Y|yes|YES) ;;
        *) echo "Cancelled."; exit 0 ;;
    esac
fi

if [ -x "$NVH_BIN/ollama" ] && command -v pkill >/dev/null 2>&1; then
    run_or_print pkill -f "$NVH_BIN/ollama serve" 2>/dev/null || true
fi

clean_shell_rc
remove_owned_file "$HOME/.local/bin/nvh"
remove_owned_file "$HOME/.local/bin/nvhive"
remove_owned_file "$HOME/.local/bin/nvh-uninstall"
remove_owned_file "$HOME/Desktop/NVHive AI Studio.desktop"
remove_owned_file "$HOME/Desktop/nvHive.desktop"
remove_owned_file "$HOME/.local/share/applications/nvhive-ai-studio.desktop"
remove_owned_file "$HOME/.local/share/applications/nvHive.desktop"

if [ "$PURGE" = "true" ]; then
    remove_path "$NVH_HOME"
else
    for rel in repo venv bin webui "runtimes/conda/nvhive" "runtimes/node" "cache/pip" "cache/uv" "cache/tmp" nvh-env.sh; do
        remove_path "$NVH_HOME/$rel"
    done
    echo ""
    echo -e "${Y}Kept:${N} models, config, ComfyUI, Studio apps, projects, outputs, logs, and support snapshots."
    echo -e "${D}For a true clean reinstall, run: bash \"$NVH_HOME/uninstall.sh\" --purge -y${N}"
fi

echo ""
echo -e "${G}nvHive uninstall complete.${N}"
echo -e "${D}Fresh install:${N} curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/install.sh | bash"
