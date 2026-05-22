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

mount_point_for_path() {
    df -Pk "$1" 2>/dev/null | awk 'NR==2 {print $6}'
}

fs_type_for_path() {
    df -PT "$1" 2>/dev/null | awk 'NR==2 {print $2}'
}

home_is_persistent_candidate() {
    local base="${1%/}"
    local free_gb="${2:-0}"
    local mount_point fs_type

    [ "$base" = "${HOME%/}" ] || return 1
    [ "$free_gb" -ge 100 ] || return 1

    mount_point="$(mount_point_for_path "$base")"
    fs_type="$(fs_type_for_path "$base")"
    [ -n "$mount_point" ] || return 1
    [ "$mount_point" != "/" ] || return 1

    case "$fs_type" in
        cifs|smb3|nfs|nfs4|sshfs|fuse.sshfs|tmpfs|overlay|squashfs)
            return 1
            ;;
    esac
    return 0
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
    free_gb="$(free_gb_for_path "$base")"
    free_gb="${free_gb:-0}"
    case "$base" in
        "$HOME"|"$HOME/"*)
            if home_is_persistent_candidate "$base" "${free_gb:-0}"; then
                score=$((score + 70))
            else
                score=$((score - 15))
            fi
            ;;
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
    roots+=("$HOME" "/mnt" "/media/${USER:-}" "/workspace" "/data" "/persistent" "/storage")

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
export PATH="$NVH_HOME/runtimes/node/current/bin:$NVH_VENV/bin:$NVH_BIN:\$PATH"
ENVEOF
chmod 600 "$NVH_HOME/nvh-env.sh" 2>/dev/null || true
}

shell_rc_path() {
    if [ -f "$HOME/.zshrc" ]; then
        printf '%s\n' "$HOME/.zshrc"
    else
        printf '%s\n' "$HOME/.bashrc"
    fi
}

install_shell_hook() {
    [ "${NVH_NO_OS_MOD:-0}" = "1" ] && return 0
    [ "$USE_ACTIVE_ENV" = "true" ] && return 0

    local rc tmp
    rc="$(shell_rc_path)"
    mkdir -p "$(dirname "$rc")"
    touch "$rc"
    tmp="$rc.nvhive.tmp"

    awk '
        $0 == "# >>> nvhive rootless env >>>" { skip=1; next }
        $0 == "# <<< nvhive rootless env <<<" { skip=0; next }
        skip { next }
        { print }
    ' "$rc" \
        | grep -vF "source \"$NVH_HOME/nvh-env.sh\"" \
        | grep -vF "export PATH=\"$NVH_VENV/bin:\$PATH\"" \
        | grep -vF "$NVH_BIN/ollama" > "$tmp" || true

    cat >> "$tmp" << RCEOF

# >>> nvhive rootless env >>>
source "$NVH_HOME/nvh-env.sh"
# Auto-start Ollama on shell login if it's not already running AND no
# other shell is mid-spawn. Previously this used a curl probe ONLY,
# which on a slow daemon false-negatived — so every new terminal during
# boot forked an additional \`ollama serve\` racing for :11434. With
# multi-tab terminals this stormed the OS; one won, the others wrote
# "address already in use" to a discarded log and exited. The pgrep
# guard makes this idempotent (real-rig audit 2026-05-22 Agent D).
[ -x "$NVH_BIN/ollama" ] && "$NVH_BIN/ollama" --version >/dev/null 2>&1 && ! pgrep -f "$NVH_BIN/ollama serve" >/dev/null 2>&1 && ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && OLLAMA_MODELS="$OLLAMA_MODELS" "$NVH_BIN/ollama" serve >/dev/null 2>&1 &
# <<< nvhive rootless env <<<
RCEOF
    mv "$tmp" "$rc"
}

install_uninstall_script() {
    local target="$NVH_HOME/uninstall.sh"
    if [ -f "$NVH_REPO/uninstall.sh" ]; then
        cp "$NVH_REPO/uninstall.sh" "$target"
    else
        cat > "$target" <<'UNINSTALLEOF'
#!/bin/bash
echo "nvHive uninstall helper is missing from the local repo."
echo "Run: curl -sSL https://raw.githubusercontent.com/thatcooperguy/nvHive/main/uninstall.sh | bash"
UNINSTALLEOF
    fi
    chmod +x "$target" 2>/dev/null || true
    ln -sf "$target" "$NVH_BIN/nvh-uninstall" 2>/dev/null || true
}

install_command_shims() {
    local local_bin shim
    local_bin="$HOME/.local/bin"
    mkdir -p "$local_bin"
    export PATH="$local_bin:$PATH"

    for shim in nvh nvhive; do
        local target="$local_bin/$shim"
        if [ -e "$target" ] && ! grep -q "nvHive rootless wrapper" "$target" 2>/dev/null; then
            echo -e "${Y}Keeping existing $target; use $NVH_VENV/bin/nvh directly if needed.${N}"
            continue
        fi
        cat > "$target" << SHIMEOF
#!/bin/bash
# nvHive rootless wrapper
NVH_ENV="$NVH_HOME/nvh-env.sh"
if [ -f "\$NVH_ENV" ]; then
    # shellcheck disable=SC1090
    source "\$NVH_ENV"
fi
if [ -x "$NVH_VENV/bin/nvh" ]; then
    exec "$NVH_VENV/bin/nvh" "\$@"
fi
exec python -m nvh.cli.main "\$@"
SHIMEOF
        chmod +x "$target" 2>/dev/null || true
    done
}

should_launch_webui() {
    case "${NVH_INSTALL_LAUNCH:-auto}" in
        0|false|False|no|No|off|Off) return 1 ;;
        1|true|True|yes|Yes|on|On) return 0 ;;
    esac
    [ -n "${CI:-}" ] && return 1
    [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}${XDG_CURRENT_DESKTOP:-}" ] || return 1
    return 0
}

launch_webui_after_install() {
    should_launch_webui || return 0
    local nvh_cmd="$NVH_VENV/bin/nvh"
    [ -x "$nvh_cmd" ] || nvh_cmd="$(command -v nvh || true)"
    [ -n "$nvh_cmd" ] || return 0

    echo ""
    echo -e "${G}Verifying all services up before opening the browser...${N}"
    echo -e "${D}Set NVH_INSTALL_LAUNCH=0 before install to skip auto-launch.${N}"
    echo ""
    # Use `nvh services start` (not `workstation --launch`) so the user
    # gets a live CLI table showing Ollama → API → WebUI each transition
    # from waiting → starting → healthy. The browser opens ONLY after
    # every service is verified healthy. Real-rig 2026-05-22 photos
    # showed users landing on the WebUI before the API engine was
    # initialized, seeing a red banner, and having to wait for auto-
    # recovery. The bring-up-verified-first contract eliminates that
    # window entirely.
    #
    # The previous `workstation --launch -y` path is preserved for the
    # explicit `nvh workstation` invocation; it just isn't what the
    # install runs.
    "$nvh_cmd" services start --open || {
        echo -e "${Y}Service bring-up failed. The install completed but one or more${N}"
        echo -e "${Y}services didn't reach a healthy state. Inspect $NVH_LOGS, then:${N}"
        echo -e "${G}  $nvh_cmd services start${N}    ${D}# retry the bring-up${N}"
        echo -e "${G}  $nvh_cmd services status${N}   ${D}# see the current per-service status${N}"
    }
}

# ---------------------------------------------------------------------------
# Port-conflict detection
#
# The owner explicitly asked: "There should be a deep dive to prevent service
# or port conflicts." Today we ship two narrow fixes for this — `_api_healthy`
# / `_kill_stale_api` in nvh/cli/main.py (port 8000, #65) and
# `start_ollama_with_health_wait` here in install.sh (port 11434, #66). The
# gap they leave: install.sh doesn't probe the SET of ports the stack needs
# (3000/3001/3002/8000/11434) BEFORE starting services. If 3000 is held by
# some other tool the user is running, the WebUI silently falls back to 8080
# (Next.js's cascade) and the user gets confused that the URL doesn't match
# the docs. If 8000 is held by a non-nvHive process, the API never binds and
# the WebUI shows the red banner indefinitely.
#
# This helper runs near the start of install.sh — BEFORE any service work —
# and classifies each port:
#
#   OK       — nothing listening, or it's our healthy service (leave it).
#   STALE    — process name matches one of ours but the service isn't
#              healthy (kill it with fuser / lsof+kill; same pattern as
#              _kill_stale_api in nvh/cli/main.py).
#   FOREIGN  — somebody else owns the port. Aborts with exit 2 unless
#              NVH_PORT_CONFLICT_KILL_FOREIGN=1.
#
# Health-probe contract mirrors _api_healthy in nvh/cli/main.py:
#   8000           → GET /v1/health, HTTP 200, status: success.
#   11434          → GET /api/tags, HTTP 200.
#   3000/3001/3002 → any HTTP response (Next.js dev/start binds these and
#                    serves something even before the page mounts).
# ---------------------------------------------------------------------------
NVH_PORTS_TO_CHECK="3000 3001 3002 8000 11434"

_port_listener_pid() {
    # Echo the PID listening on $1, or empty if none.
    local port="$1" pid=""
    if command -v lsof >/dev/null 2>&1; then
        # -t = terse (PID only). Works on Linux + macOS. Match LISTEN state
        # so we don't false-positive on a transient client connection.
        pid="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n1 || true)"
    fi
    if [ -z "$pid" ] && command -v fuser >/dev/null 2>&1; then
        pid="$(fuser "$port"/tcp 2>/dev/null | awk '{print $1}' || true)"
    fi
    if [ -z "$pid" ] && command -v ss >/dev/null 2>&1; then
        # ss -tlnp: state-tcp listen-only numeric process. Parse `pid=NNNN`
        # out of the `users:(("name",pid=12345,fd=…))` column.
        pid="$(ss -tlnp 2>/dev/null \
            | awk -v p=":$port\$" '$4 ~ p {print $0}' \
            | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
            | head -n1 || true)"
    fi
    printf '%s' "$pid"
}

_pid_cmdline() {
    # Echo the process name + args for a PID, or empty if gone.
    local pid="$1"
    [ -n "$pid" ] || return 0
    if command -v ps >/dev/null 2>&1; then
        ps -o command= -p "$pid" 2>/dev/null || ps -p "$pid" -o args= 2>/dev/null || true
    fi
}

_pid_looks_like_ours() {
    # Return 0 if the PID's cmdline matches a known nvHive-stack process.
    # Conservative: only matches things we control. If a user happens to be
    # running their own `next start` on 3000 we'd false-positive — but that's
    # the same risk _kill_stale_api carries, and the alternative (never kill
    # stale UI processes) is worse.
    local pid="$1" cmd
    cmd="$(_pid_cmdline "$pid")"
    case "$cmd" in
        *nvh\ serve*|*nvh.cli*|*nvh\ webui*|*nvh\ workstation*) return 0 ;;
        *ollama\ serve*|*"$NVH_BIN/ollama"*) return 0 ;;
        *next\ start*|*next\ dev*|*node*next*) return 0 ;;
        *) return 1 ;;
    esac
}

_port_is_healthy_nvh_service() {
    # Mirror _api_healthy() in nvh/cli/main.py — pure-bash version.
    # Returns 0 if the listener at $1 looks like one of OUR services AND
    # passes the per-port health probe.
    local port="$1" pid="${2:-}" body=""
    case "$port" in
        8000)
            body="$(curl -sf --max-time 2 "http://127.0.0.1:${port}/v1/health" 2>/dev/null || true)"
            [ -n "$body" ] || return 1
            # The API answers /v1/health with `{"status":"success",...}` when
            # the engine initialized cleanly. Same invariant the WebUI's
            # ApiHealthBanner uses, and the same invariant _api_healthy
            # checks in nvh/cli/main.py.
            case "$body" in
                *'"status"'*'"success"'*) return 0 ;;
                *'"status": "success"'*) return 0 ;;
                *) return 1 ;;
            esac
            ;;
        11434)
            # Ollama answers /api/tags whether it's our binary or one the
            # user installed system-wide — either way "leave it alone" is
            # the right call since we'd be talking to the same daemon.
            curl -sf --max-time 2 "http://127.0.0.1:${port}/api/tags" >/dev/null 2>&1
            ;;
        3000|3001|3002)
            # Next.js binds and serves something even before the React tree
            # mounts. Per the spec, any HTTP response is treated as "our
            # WebUI is running" — BUT we also require the process name to
            # look like one of ours, otherwise an unrelated dev server
            # (Grafana, Storybook, etc.) on 3000 would silently get
            # classified OK and the install would proceed without ever
            # binding the WebUI.
            [ -n "$pid" ] || return 1
            _pid_looks_like_ours "$pid" || return 1
            curl -s --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/" 2>/dev/null \
                | grep -qE '^[1-9][0-9][0-9]$'
            ;;
        *)
            return 1
            ;;
    esac
}

_kill_port_listener() {
    # Free port $1. Mirror _kill_stale_api in nvh/cli/main.py: fuser -k
    # (Linux) then lsof + kill -TERM (macOS / fallback). Best-effort.
    local port="$1"
    if command -v fuser >/dev/null 2>&1; then
        fuser -k "$port"/tcp >/dev/null 2>&1 || true
    fi
    if command -v lsof >/dev/null 2>&1; then
        local pid
        for pid in $(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null); do
            kill -TERM "$pid" 2>/dev/null || true
        done
    fi
    sleep 1
    # Last-resort SIGKILL if the process didn't exit cleanly.
    if command -v lsof >/dev/null 2>&1; then
        local pid
        for pid in $(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null); do
            kill -KILL "$pid" 2>/dev/null || true
        done
    fi
}

detect_port_conflicts() {
    # Print a small Markdown-ish table summarizing the state of each port,
    # and abort install if any FOREIGN process is in the way (unless the
    # NVH_PORT_CONFLICT_KILL_FOREIGN=1 override is set).
    #
    # Exit codes:
    #   0 — all ports are clear or owned by healthy/just-cleaned nvHive
    #       services. Install can proceed.
    #   2 — at least one port is held by a foreign process and the kill
    #       override is OFF.
    local any_conflict=0
    local override="${NVH_PORT_CONFLICT_KILL_FOREIGN:-0}"

    echo -e "${B}Checking ports for conflicts before starting services...${N}"
    printf '  %-6s %-9s %-30s %s\n' "Port" "Status" "Owner" "Action"
    printf '  %-6s %-9s %-30s %s\n' "----" "------" "-----" "------"

    local port pid cmd owner status action short
    for port in $NVH_PORTS_TO_CHECK; do
        pid="$(_port_listener_pid "$port")"
        if [ -z "$pid" ]; then
            printf "  %-6s ${G}%-9s${N} %-30s %s\n" "$port" "OK" "(empty)" "available"
            continue
        fi

        cmd="$(_pid_cmdline "$pid")"
        # Trim cmdline to something printable in the Owner column.
        short="$(printf '%s' "$cmd" | awk '{print $1}' | sed 's#.*/##')"
        [ -n "$short" ] || short="pid $pid"
        owner="$short (pid $pid)"

        if _port_is_healthy_nvh_service "$port" "$pid"; then
            printf "  %-6s ${G}%-9s${N} %-30s %s\n" \
                "$port" "OK" "$owner" "keeping (healthy nvHive)"
            continue
        fi

        if _pid_looks_like_ours "$pid"; then
            printf "  %-6s ${Y}%-9s${N} %-30s %s\n" \
                "$port" "STALE" "$owner" "killing + will restart"
            _kill_port_listener "$port"
            pid="$(_port_listener_pid "$port")"
            if [ -z "$pid" ]; then
                printf "  %-6s ${G}%-9s${N} %-30s %s\n" \
                    "$port" "CLEARED" "(killed)" "available"
            else
                printf "  %-6s ${R}%-9s${N} %-30s %s\n" \
                    "$port" "STUCK" "pid $pid" "ABORTING — kill failed"
                any_conflict=1
            fi
            continue
        fi

        # Foreign process. Either abort or kill it.
        case "$override" in
            1|true|True|yes|Yes|on|On)
                printf "  %-6s ${Y}%-9s${N} %-30s %s\n" \
                    "$port" "FOREIGN" "$owner" "killing (override on)"
                _kill_port_listener "$port"
                ;;
            *)
                printf "  %-6s ${R}%-9s${N} %-30s %s\n" \
                    "$port" "FOREIGN" "$owner" "ABORTING — see hint below"
                any_conflict=1
                ;;
        esac
    done

    if [ "$any_conflict" -ne 0 ]; then
        echo ""
        echo -e "${R}Port conflict: one or more required ports are held by a process we don't own.${N}"
        echo -e "${D}nvHive uses 3000/3001/3002 (WebUI), 8000 (API), 11434 (Ollama).${N}"
        echo -e "${D}Free the listed processes, then re-run install — or override with:${N}"
        echo -e "${D}  NVH_PORT_CONFLICT_KILL_FOREIGN=1 bash install.sh${N}"
        echo -e "${D}(The override kills foreign processes too. Use with care.)${N}"
        return 2
    fi

    echo -e "${D}All required ports are clear.${N}"
    echo ""
    return 0
}

mkdir -p "$NVH_BIN" "$NVH_MODELS" "$OLLAMA_MODELS" "$NVH_CACHE" "$NVH_LOGS" "$NVH_STUDIO_HOME" "$COMFYUI_HOME" "$HIVE_CONFIG_HOME" "$TMPDIR"
write_nvh_env
export PATH="$NVH_HOME/runtimes/node/current/bin:$NVH_VENV/bin:$NVH_BIN:$PATH"

# Tee the install run to a single install.log so the WebUI's SystemConsole
# (web/components/SystemConsole.tsx → /api/logs?source=install) can surface
# "what did the install do" to users without terminal access. Mirrors the
# per-service logs (api-server.log, ollama.log, webui-bootstrap.log) the
# console already tails. Process-substitution + `tee -a` keeps the user's
# terminal output unchanged AND captures everything to disk in one pass.
INSTALL_LOG_TS="$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || date)"
exec > >(tee -a "$NVH_LOGS/install.log") 2>&1
echo ""
echo "=== nvHive install starting at $INSTALL_LOG_TS ==="

echo ""
echo -e "${G}╔══════════════════════════════════════╗${N}"
echo -e "${G}║       NVHive — Quick Install         ║${N}"
echo -e "${G}║  No root. Installs to NVH_HOME     ║${N}"
echo -e "${G}╚══════════════════════════════════════╝${N}"
echo ""

# Probe the set of ports the stack needs BEFORE doing any other service
# work — composes with #65 (per-port API restart) and #66 (Ollama health
# wait), which are per-service. See detect_port_conflicts() above for the
# four classifications + NVH_PORT_CONFLICT_KILL_FOREIGN override.
detect_port_conflicts

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
[ -z "$GPU_NAME" ] && echo -e "${Y}No NVIDIA GPU detected - CPU mode${N}"
if [ -n "$GPU_NAME" ]; then
    # Default the AI Wizard to an NVIDIA multimodal model so it can see
    # screenshots, images, and documents — not just read text. Downgrade
    # by VRAM tier; smallest tier still gets a vision-capable model so
    # the Wizard's multimodal tools stay functional.
    if   [ "$VRAM_GB" -ge 40 ]; then DEFAULT_OLLAMA_MODEL="nemotron-omni"          # NVIDIA Nemotron Omni — full multimodal flagship
    elif [ "$VRAM_GB" -ge 24 ]; then DEFAULT_OLLAMA_MODEL="nemotron-3-nano-omni"   # Nemotron 3 Nano Omni (30B MoE, 3B active) — multimodal
    elif [ "$VRAM_GB" -ge 16 ]; then DEFAULT_OLLAMA_MODEL="llama3.2-vision"        # Llama 3.2 11B Vision — multimodal
    elif [ "$VRAM_GB" -ge 12 ]; then DEFAULT_OLLAMA_MODEL="minicpm-v"              # MiniCPM-V — small multimodal
    elif [ "$VRAM_GB" -ge 6  ]; then DEFAULT_OLLAMA_MODEL="moondream"              # Moondream — tiny multimodal (~2 GB)
    else DEFAULT_OLLAMA_MODEL="moondream"; fi
else
    # CPU-only fallback. moondream is still small enough to run usefully
    # on CPU for the multimodal tools.
    DEFAULT_OLLAMA_MODEL="moondream"
fi
echo -e "${D}Recommended local model: $DEFAULT_OLLAMA_MODEL${N}"

# ---------------------------------------------------------------------------
# GPU capability advisor + opt-in auto-staging
# ---------------------------------------------------------------------------
# The DEFAULT_OLLAMA_MODEL picker above handles the AI Wizard's chat +
# vision tier. But nvHive supports OTHER capabilities — image generation
# (ComfyUI), video generation, speech (WhisperX), music (ACE-Step) — that
# are also VRAM-gated and that big-GPU users almost always want.
#
# Historically the user had to dig through `nvh studio --install ...`
# menus to enable them. That made nvHive feel like a chat-only product
# even on a 48 GB rig that could run everything.
#
# This block does two things:
#
#   1. ALWAYS print what the current GPU could run (educational; no
#      downloads). Source of truth for the table: docs/GPU_TIER_MATRIX.md
#      and the recommended_vram_gb fields on STUDIO_MODELS / STUDIO_PACKS
#      / TRENDING_COMFYUI_EXAMPLES.
#   2. When NVH_INSTALL_FULL_CAPABILITY=1, write a marker file under
#      $NVH_HOME/state/ telling the WebUI / Wizard / `nvh studio` to
#      auto-offer the appropriate packs on first launch — and, if
#      NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD=1 is ALSO set, force-pull
#      them inline here.
#
# Default: opt-in. We don't surprise a school Wi-Fi student with a
# 60 GB pull on the back of running install.sh.

nvh_capability_tiers_for_vram() {
    # Echo a space-separated list of capability tokens the given VRAM
    # tier can run. Tokens are stable strings; the WebUI maps them to
    # human labels.
    local vram="${1:-0}"
    local tokens="vision-chat"  # always: every tier gets a vision-capable chat model
    if [ "$vram" -ge 8  ]; then tokens="$tokens image-gen-starter"; fi
    if [ "$vram" -ge 12 ]; then tokens="$tokens image-edit"; fi
    if [ "$vram" -ge 16 ]; then tokens="$tokens image-control"; fi
    if [ "$vram" -ge 24 ]; then tokens="$tokens video-gen speech-lab music-gen"; fi
    if [ "$vram" -ge 40 ]; then tokens="$tokens video-gen-pro"; fi
    printf '%s' "$tokens"
}

nvh_capability_human_label() {
    case "$1" in
        vision-chat)        echo "Wizard chat + vision (image understanding)" ;;
        image-gen-starter)  echo "Image generation - starter (ComfyUI + Z-Image-Turbo)" ;;
        image-edit)         echo "Image editing (ComfyUI + Qwen Image Edit 2509)" ;;
        image-control)      echo "Controlled image generation (ComfyUI + FLUX.1 ControlNet)" ;;
        video-gen)          echo "Video generation (ComfyUI + Wan 2.2 5B)" ;;
        video-gen-pro)      echo "Video generation - pro (ComfyUI + Wan 2.2 14B i2v)" ;;
        speech-lab)         echo "Local speech (WhisperX, faster-whisper)" ;;
        music-gen)          echo "Music generation (ACE-Step)" ;;
        *)                  echo "$1" ;;
    esac
}

# Map capability tokens to studio pack ids the Wizard / WebUI will
# auto-offer. The mapping is intentionally conservative — we only
# stage packs that are real and tested in studio_packs.py.
nvh_capability_to_pack_ids() {
    case "$1" in
        image-gen-starter|image-edit|image-control|video-gen|video-gen-pro)
            echo "comfyui-power-nodes" ;;
        speech-lab|music-gen)
            echo "music-producer-lab ace-step-music" ;;
        *) ;;
    esac
}

print_capability_summary() {
    [ -n "$GPU_NAME" ] || return 0
    local tier_tokens token
    tier_tokens="$(nvh_capability_tiers_for_vram "$VRAM_GB")"
    echo -e "${G}GPU capabilities at ${VRAM_GB} GB VRAM:${N}"
    for token in $tier_tokens; do
        echo -e "  ${D}-${N} $(nvh_capability_human_label "$token")"
    done
    case "${NVH_INSTALL_FULL_CAPABILITY:-0}" in
        1|true|True|yes|Yes|on|On)
            echo -e "${B}NVH_INSTALL_FULL_CAPABILITY=1 set; staging the matching packs.${N}"
            ;;
        *)
            echo -e "${D}  (Set NVH_INSTALL_FULL_CAPABILITY=1 to auto-stage the matching packs,${N}"
            echo -e "${D}   or enable any of these later from the WebUI or 'nvh studio'.)${N}"
            ;;
    esac
}

# Write the marker file the WebUI / Wizard reads to auto-offer matching
# packs. Idempotent: replaces any prior marker. Living under $NVH_HOME
# keeps it on the persistent block volume so the surface remembers
# after reboot.
stage_full_capability_for_vram_tier() {
    local vram="${1:-$VRAM_GB}"
    case "${NVH_INSTALL_FULL_CAPABILITY:-0}" in
        1|true|True|yes|Yes|on|On) ;;
        *) return 0 ;;
    esac

    local marker_dir="${NVH_STATE:-$NVH_HOME/state}/capability"
    mkdir -p "$marker_dir"
    local marker="$marker_dir/auto-enable.json"
    local tier_tokens
    tier_tokens="$(nvh_capability_tiers_for_vram "$vram")"

    # Collect the pack ids dedup'd, preserving order.
    local pack_ids="" token packs id
    for token in $tier_tokens; do
        packs="$(nvh_capability_to_pack_ids "$token")"
        for id in $packs; do
            case " $pack_ids " in
                *" $id "*) ;;
                *) pack_ids="$pack_ids $id" ;;
            esac
        done
    done
    pack_ids="${pack_ids# }"

    # Write the marker as plain JSON without depending on Python (this
    # runs before the venv may be ready on some paths).
    {
        printf '{'
        printf '"vram_gb": %s, ' "$vram"
        printf '"capabilities": ['
        local first=1
        for token in $tier_tokens; do
            if [ "$first" -eq 1 ]; then first=0; else printf ', '; fi
            printf '"%s"' "$token"
        done
        printf '], '
        printf '"auto_offer_pack_ids": ['
        first=1
        for id in $pack_ids; do
            if [ "$first" -eq 1 ]; then first=0; else printf ', '; fi
            printf '"%s"' "$id"
        done
        printf ']'
        printf '}\n'
    } >"$marker"
    echo -e "${G}Capability marker written: $marker${N}"

    # Optional: force-pull packs inline. Only useful for headless cloud
    # images that won't have a browser to drive the WebUI install.
    case "${NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD:-0}" in
        1|true|True|yes|Yes|on|On)
            if [ -n "$pack_ids" ] && [ -x "$NVH_VENV/bin/nvh" ]; then
                echo -e "${B}NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD=1: pulling $pack_ids inline...${N}"
                # shellcheck disable=SC2086
                "$NVH_VENV/bin/nvh" studio --install $pack_ids -y || \
                    echo -e "${Y}Inline pack install reported errors; check 'nvh studio status'.${N}"
            else
                echo -e "${Y}NVH_INSTALL_FULL_CAPABILITY_DOWNLOAD=1 set but 'nvh' is not on PATH yet — staging only.${N}"
            fi
            ;;
    esac
}

set_config_ollama_model() {
    local cfg="$1"
    local model="$2"
    CFG="$cfg" MODEL="$model" "$PYTHON" - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ["CFG"])
model = os.environ["MODEL"]
text = path.read_text(encoding="utf-8")
updated = text.replace("__NVH_DEFAULT_OLLAMA_MODEL__", model)
# Replace the entire `default_model:` line for ollama/* values rather
# than substring-matching. The previous partial-match regex (#63) fixed
# the FORWARD case where the existing value was a clean model name, but
# couldn't repair files that had ALREADY been corrupted by the pre-#63
# version of this code — e.g. lines like
#     default_model: "ollama/nemotron-omni"-omni"
# where the orphan `-omni"` trailer sits beyond what any "match a known
# model name" regex can see. Replacing the whole line is robust to any
# past corruption pattern AND any future quoting style the writer
# happens to produce.
import re as _re
default_re = _re.compile(r'^(\s*)default_model:\s*[\'"]?ollama/')
out_lines = []
for line in updated.split("\n"):
    m = default_re.match(line)
    if m:
        out_lines.append(f'{m.group(1)}default_model: "ollama/{model}"')
    else:
        out_lines.append(line)
updated = "\n".join(out_lines)
path.write_text(updated, encoding="utf-8")
PY
}

sync_ollama_default_model_config() {
    local cfg="$HIVE_CONFIG_HOME/config.yaml"
    [ -n "$GPU_NAME" ] || return 0
    [ -f "$cfg" ] || return 0
    # Trigger sync if ANY ollama/* default_model line is present, including
    # corruption-recovery cases like the pre-#63 `ollama/nemotron-omni"-omni"`
    # trailer. The set_config_ollama_model function now does whole-line
    # replacement so it's safe to invoke on any malformed value.
    if grep -Eq "default_model:[[:space:]]*[\"']?ollama/" "$cfg"; then
        set_config_ollama_model "$cfg" "$DEFAULT_OLLAMA_MODEL"
        echo -e "${G}Ollama config aligned to GPU recommendation: $DEFAULT_OLLAMA_MODEL${N}"
    fi
}

# Start the Ollama daemon with a real health gate. The previous code
# was `ollama serve &>/dev/null & sleep 2/3` — three real bugs in one
# line:
#   1. `&>/dev/null` swallows every Ollama startup error, so the user
#      can't tell why the daemon didn't bind.
#   2. The fixed `sleep 2/3` races against cold-start GPU rigs where
#      Ollama needs 5-15 seconds before it accepts connections; the
#      install moves on, the WebUI later shows "Ollama not running on
#      11434 — falling back to cloud providers", and the user thinks
#      the install is broken.
#   3. `&` alone doesn't survive install.sh's exit — the orphaned
#      process can be killed when the script returns. `disown` plus a
#      log redirect makes the daemon truly background.
#
# This helper writes the daemon's stdout+stderr to $NVH_LOGS/ollama.log
# (so the install can show the last 5 lines on timeout) and polls the
# /api/tags endpoint until it binds — up to OLLAMA_BOOT_TIMEOUT_S (15s
# by default; override via NVH_OLLAMA_BOOT_TIMEOUT). Echoes the live
# status so the user sees "Waiting for Ollama..." instead of staring
# at a frozen line.
start_ollama_with_health_wait() {
    local ollama_bin="$1"
    local models_dir="$2"
    local log_file="${NVH_LOGS:-/tmp}/ollama.log"
    local timeout_s="${NVH_OLLAMA_BOOT_TIMEOUT:-15}"

    mkdir -p "$models_dir" "${NVH_LOGS:-/tmp}"
    : >"$log_file"  # truncate so we capture this run only

    echo -e "${B}Starting Ollama...${N}"
    OLLAMA_MODELS="$models_dir" nohup "$ollama_bin" serve >"$log_file" 2>&1 &
    local ollama_pid=$!
    disown "$ollama_pid" 2>/dev/null || true

    local waited=0
    while [ "$waited" -lt "$timeout_s" ]; do
        if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
            echo -e "${G}Ollama ready on :11434 (pid $ollama_pid, took ${waited}s).${N}"
            echo -e "${D}Daemon log: $log_file${N}"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    echo -e "${Y}Ollama did not bind :11434 within ${timeout_s}s. Last log lines:${N}"
    tail -5 "$log_file" 2>/dev/null | sed "s/^/  /" || true
    echo -e "${D}Override the timeout with NVH_OLLAMA_BOOT_TIMEOUT=30 if your rig is slow.${N}"
    return 1
}

ollama_binary_valid() {
    local bin="${1:-}"
    [ -n "$bin" ] || return 1
    [ -x "$bin" ] || return 1
    "$bin" --version >/dev/null 2>&1
}

ollama_arch() {
    case "$(uname -m 2>/dev/null || printf unknown)" in
        x86_64|amd64) printf 'amd64' ;;
        aarch64|arm64) printf 'arm64' ;;
        *) return 1 ;;
    esac
}

ollama_download_candidates() {
    local arch="$1"
    local custom_url base version version_param github_tag github_base
    custom_url="${NVH_OLLAMA_URL:-}"
    if [ -n "$custom_url" ]; then
        case "${custom_url%%\?*}" in
            *.tar.zst) printf 'tar.zst|%s\n' "$custom_url" ;;
            *) printf 'tgz|%s\n' "$custom_url" ;;
        esac
        return 0
    fi

    base="${NVH_OLLAMA_DOWNLOAD_BASE:-https://ollama.com/download}"
    base="${base%/}"
    version="${NVH_OLLAMA_VERSION:-}"
    version_param=""
    [ -n "$version" ] && version_param="?version=$version"
    if [ -n "$version" ]; then
        case "$version" in
            v*) github_tag="$version" ;;
            *) github_tag="v$version" ;;
        esac
        github_base="https://github.com/ollama/ollama/releases/download/$github_tag"
    else
        github_base="https://github.com/ollama/ollama/releases/latest/download"
    fi

    printf 'tar.zst|%s/ollama-linux-%s.tar.zst%s\n' "$base" "$arch" "$version_param"
    printf 'tar.zst|%s/ollama-linux-%s.tar.zst\n' "$github_base" "$arch"
    printf 'tgz|%s/ollama-linux-%s.tgz%s\n' "$base" "$arch" "$version_param"
    printf 'tgz|%s/ollama-linux-%s.tgz\n' "$github_base" "$arch"
}

extract_rootless_ollama_archive() {
    local archive="$1"
    local archive_type="$2"
    local env_python
    env_python="$(env_python_path)" || env_python="$PYTHON"

    if [ "$archive_type" = "tar.zst" ] && ! "$env_python" -c "import zstandard" >/dev/null 2>&1; then
        echo "Installing Python zstandard extractor..." >>"$NVH_LOGS/ollama-install.log"
        "$env_python" -m pip install -q "zstandard>=0.20" >>"$NVH_LOGS/ollama-install.log" 2>&1 || true
    fi

    ARCHIVE="$archive" ARCHIVE_TYPE="$archive_type" TARGET="$NVH_HOME" "$env_python" - <<'PY' >>"$NVH_LOGS/ollama-install.log" 2>&1
import os
from pathlib import Path

from nvh.integrations.studio_packs import _extract_ollama_archive

_extract_ollama_archive(
    Path(os.environ["ARCHIVE"]),
    os.environ["ARCHIVE_TYPE"],
    Path(os.environ["TARGET"]),
)
PY
}

install_rootless_ollama_binary() {
    local arch stage archive archive_type candidate_type candidate_url downloaded
    OLLAMA_BIN="$NVH_BIN/ollama"
    if ollama_binary_valid "$OLLAMA_BIN"; then
        return 0
    fi

    if [ -e "$OLLAMA_BIN" ]; then
        echo -e "${Y}Existing Ollama binary is not runnable; replacing it rootlessly.${N}"
    else
        echo -e "${B}Installing Ollama (local AI)...${N}"
    fi

    if ! command -v curl >/dev/null 2>&1; then
        echo -e "${R}curl is required to install Ollama without root.${N}"
        return 1
    fi
    if ! arch="$(ollama_arch)"; then
        echo -e "${R}Unsupported Ollama Linux architecture: $(uname -m 2>/dev/null || printf unknown)${N}"
        return 1
    fi

    stage="$NVH_CACHE/bootstrap/ollama-${arch}"
    rm -rf "$stage"
    mkdir -p "$stage" "$NVH_BIN" "$NVH_HOME/lib"
    : >"$NVH_LOGS/ollama-install.log"

    downloaded=false
    while IFS='|' read -r candidate_type candidate_url; do
        [ -n "$candidate_type" ] || continue
        archive="$stage/ollama-linux-${arch}.${candidate_type}"
        echo "Downloading Ollama ${candidate_type}: $candidate_url" >>"$NVH_LOGS/ollama-install.log"
        if curl -fL --retry 2 --connect-timeout 20 --show-error "$candidate_url" -o "$archive" >>"$NVH_LOGS/ollama-install.log" 2>&1 && [ -s "$archive" ]; then
            archive_type="$candidate_type"
            downloaded=true
            break
        fi
        echo "Ollama candidate unavailable (${candidate_type}); trying fallback." >>"$NVH_LOGS/ollama-install.log"
    done < <(ollama_download_candidates "$arch")

    if [ "$downloaded" != "true" ]; then
        echo -e "${R}Ollama download failed.${N} ${D}Log: $NVH_LOGS/ollama-install.log${N}"
        return 1
    fi
    if ! extract_rootless_ollama_archive "$archive" "$archive_type"; then
        echo -e "${R}Ollama extraction failed.${N} ${D}Log: $NVH_LOGS/ollama-install.log${N}"
        return 1
    fi
    chmod +x "$OLLAMA_BIN" 2>/dev/null || true
    if ! ollama_binary_valid "$OLLAMA_BIN"; then
        echo -e "${R}Ollama installed but did not pass its Linux binary check.${N}"
        echo -e "${D}Binary: $OLLAMA_BIN${N}"
        echo -e "${D}Log: $NVH_LOGS/ollama-install.log${N}"
        return 1
    fi
    echo -e "${G}Ollama runtime ready: $OLLAMA_BIN${N}"
    return 0
}

ollama_model_installed() {
    local model="$1"
    [ -n "${OLLAMA_BIN:-}" ] || return 1
    [ -x "$OLLAMA_BIN" ] || return 1
    "$OLLAMA_BIN" list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fxq "$model"
}

# Rough size estimates for the AI Wizard models we ship. Sourced from
# the Ollama library + the HF Q4_K_M / Q8 GGUFs the install pulls. Used
# by the countdown copy so users see "this is a ~7.9 GB download" not
# just "downloading model X in 10s". Values are approximate; better to
# slightly over-estimate so users on metered links don't feel ambushed.
nvwizard_model_size_hint() {
    case "$1" in
        nemotron-omni)             echo "~32 GB" ;;
        nemotron-3-nano-omni)      echo "~17 GB" ;;
        llama3.2-vision)           echo "~7.9 GB" ;;
        minicpm-v)                 echo "~5 GB" ;;
        moondream)                 echo "~1.7 GB" ;;
        *)                         echo "size TBD" ;;
    esac
}

nvwizard_model_download_countdown() {
    local model="$1"
    local delay="${NVH_MODEL_DOWNLOAD_DELAY:-10}"
    local size
    size="$(nvwizard_model_size_hint "$model")"
    local key=""

    case "${NVH_INSTALL_MODEL_DOWNLOAD:-auto}" in
        0|false|False|no|No|off|Off)
            echo -e "${D}Skipping AI Wizard model download because NVH_INSTALL_MODEL_DOWNLOAD=0.${N}"
            return 1
            ;;
    esac

    # Pre-roll banner that's visible regardless of TTY state. Tells the
    # user WHAT they're about to download, ROUGHLY how big it is, and
    # HOW to skip — both via key on TTY and via env knob on headless.
    # Real-rig audit 2026-05-22 (Agent C): the previous copy showed
    # only the model name and 10s skip prompt, leaving non-engineer
    # users panic-skipping or sitting through a multi-GB pull blind.
    echo ""
    echo -e "${G}AI Wizard local brain: $model ($size)${N}"
    echo -e "${D}  This is the model the Wizard chats with. Smaller models load fast on CPU;${N}"
    echo -e "${D}  bigger ones are stronger on GPU. You can change it later from the WebUI.${N}"

    if [ -r /dev/tty ] && [ -w /dev/tty ] && [ "${NVH_INSTALL_MODEL_DOWNLOAD:-auto}" = "auto" ]; then
        echo -e "${D}  Press [s] to skip; download starts in 10s (you can grab it later from /setup).${N}"
        while [ "$delay" -gt 0 ]; do
            printf "\r  Starting in %ss... press [s] to skip " "$delay" >/dev/tty
            if IFS= read -r -s -n 1 -t 1 key </dev/tty; then
                case "$key" in
                    s|S|c|C|q|Q)
                        printf "\n" >/dev/tty
                        echo -e "${Y}  Skipped. The Wizard will offer the download from the WebUI later.${N}"
                        return 1
                        ;;
                esac
            fi
            delay=$((delay - 1))
        done
        printf "\n" >/dev/tty
    else
        # Headless / piped install — no /dev/tty available. Surface the
        # opt-out hint inline so users running via `ssh host 'bash -s' <
        # install.sh` or cloud-init userdata don't get hit with a
        # multi-GB pull they didn't expect.
        echo -e "${D}  Headless install — running with no TTY. To skip the download in this${N}"
        echo -e "${D}  mode, re-run with: ${G}NVH_INSTALL_MODEL_DOWNLOAD=0 bash install.sh${N}"
    fi

    return 0
}

# Fallback chain for a preferred Wizard model. NVIDIA's Nemotron Omni
# tags aren't published to the public Ollama library yet (verified
# 2026-05-17: `ollama pull nemotron-omni` 404s) but the Wizard surface
# is designed multimodal-first — so when the preferred tag is one of
# the multimodal targets, fall through progressively smaller vision-
# capable models so the install never breaks. Other tags keep their
# current single-pull behavior.
_nvwizard_fallback_chain() {
    case "$1" in
        nemotron-omni)         echo "nemotron-omni nemotron-3-nano-omni llama3.2-vision minicpm-v moondream" ;;
        nemotron-3-nano-omni)  echo "nemotron-3-nano-omni llama3.2-vision minicpm-v moondream" ;;
        llama3.2-vision*)      echo "$1 llama3.2-vision minicpm-v moondream" ;;
        *)                     echo "$1" ;;
    esac
}

# HuggingFace GGUF source for the Nemotron Omni models. The ggml-org
# org is maintained by the llama.cpp / GGUF team — most authoritative
# community quantization currently published. Quant is picked by VRAM
# tier (Q8_0 on 40 GB+, Q4_K_M on 24-40 GB; smaller GPUs fall through
# to Path 2 fallbacks since the model itself doesn't fit).
_nvwizard_hf_gguf_source() {
    local preferred="$1"
    case "$preferred" in
        nemotron-omni|nemotron-3-nano-omni)
            if [ "${VRAM_GB:-0}" -ge 40 ]; then
                echo "ggml-org/NVIDIA-Nemotron-3-Nano-Omni nemotron-3-nano-omni-ga_v1.0-Q8_0.gguf mmproj-nemotron-3-nano-omni-ga_v1.0.gguf 32"
            elif [ "${VRAM_GB:-0}" -ge 24 ]; then
                echo "ggml-org/NVIDIA-Nemotron-3-Nano-Omni nemotron-3-nano-omni-ga_v1.0-Q4_K_M.gguf mmproj-nemotron-3-nano-omni-ga_v1.0.gguf 24"
            fi
            ;;
    esac
}

# Try to bootstrap a Nemotron Omni model into the local Ollama library
# by downloading the GGUF + vision projector from HuggingFace and
# registering them via an Ollama Modelfile. Returns 0 on success, 1 if
# unsupported / disabled / failed (in which case the caller continues
# the Path 2 fallback chain).
bootstrap_omni_via_hf() {
    local preferred="$1"
    local target_tag="$1"
    local spec repo gguf mmproj need_gb
    spec="$(_nvwizard_hf_gguf_source "$preferred")"
    [ -n "$spec" ] || return 1
    # shellcheck disable=SC2086
    set -- $spec
    repo="$1"; gguf="$2"; mmproj="$3"; need_gb="$4"

    # User opt-out via env
    case "${NVH_INSTALL_MODEL_DOWNLOAD:-1}" in
        0|false|False|no|No|off|Off) return 1 ;;
    esac

    local target_dir="$NVH_HOME/models/${target_tag}"
    mkdir -p "$target_dir"

    # Disk-space check (rough: GGUF + mmproj + 5 GB headroom)
    local free_gb=0
    if free_gb="$(df -BG "$target_dir" 2>/dev/null | awk 'NR==2 {gsub("G","",$4); print $4}')"; then
        if [ "${free_gb:-0}" -lt "$((need_gb + 5))" ]; then
            echo -e "${Y}Skipping Omni HuggingFace bootstrap: need ~${need_gb} GB free under $NVH_HOME, found ${free_gb} GB.${N}"
            return 1
        fi
    fi

    local base="https://huggingface.co/${repo}/resolve/main"
    local gguf_local="${target_dir}/${gguf}"
    local mmproj_local="${target_dir}/${mmproj}"

    echo -e "${B}Bootstrapping NVIDIA Nemotron Omni from HuggingFace (${repo})...${N}"
    echo -e "${D}  Will download: ${gguf} (~${need_gb} GB) + ${mmproj} (~1.5 GB)${N}"
    echo -e "${D}  Skip with: NVH_INSTALL_MODEL_DOWNLOAD=0${N}"

    # Resumable downloads (-C - resumes if interrupted). curl --fail
    # treats HTTP 4xx/5xx as errors so partial 404 pages don't pose as
    # complete files.
    if ! curl -L --fail -C - --progress-bar -o "$gguf_local" "${base}/${gguf}"; then
        echo -e "${Y}HuggingFace GGUF download failed for ${repo}/${gguf}.${N}"
        return 1
    fi
    if ! curl -L --fail -C - --progress-bar -o "$mmproj_local" "${base}/${mmproj}"; then
        echo -e "${Y}HuggingFace mmproj download failed for ${repo}/${mmproj}.${N}"
        return 1
    fi

    # Write the Modelfile. Ollama supports multimodal models via two
    # FROM directives (one for the LLM, one for the mmproj projector)
    # — this is the same pattern used by the published llama3.2-vision
    # tag. If a future Ollama version changes the syntax, this falls
    # through to Path 2 cleanly via the surrounding chain.
    local modelfile="${target_dir}/Modelfile"
    cat >"$modelfile" <<EOF
# Generated by nvHive install.sh — registers NVIDIA Nemotron Omni from
# the local GGUF + vision projector downloaded from HuggingFace
# (${repo}). Ollama exposes the result as the tag below; the AI Wizard
# will use it just like a tag pulled from the Ollama library.
FROM ${gguf_local}
FROM ${mmproj_local}
EOF

    echo -e "${B}Registering ${target_tag} in Ollama (this is fast)...${N}"
    if ! OLLAMA_MODELS="$OLLAMA_MODELS" "$OLLAMA_BIN" create "$target_tag" -f "$modelfile" 2>&1 | tee -a "$NVH_LOGS/model-pull.log"; then
        echo -e "${Y}ollama create failed for ${target_tag}. Falling back to the next chain entry.${N}"
        return 1
    fi
    echo -e "${G}NVIDIA Nemotron Omni (${target_tag}) registered locally from HuggingFace.${N}"
    return 0
}

pull_nvwizard_model_cli() {
    local preferred="$1"
    local pull_rc=0
    local model

    [ -n "${OLLAMA_BIN:-}" ] || return 1
    [ -x "$OLLAMA_BIN" ] || return 1
    mkdir -p "$OLLAMA_MODELS" "$NVH_LOGS"

    if ollama_model_installed "$preferred"; then
        echo -e "${G}Model $preferred ready.${N}"
        return 0
    fi

    nvwizard_model_download_countdown "$preferred" || return 0
    : >"$NVH_LOGS/model-pull.log"
    local chain attempt=0
    chain="$(_nvwizard_fallback_chain "$preferred")"
    for model in $chain; do
        attempt=$((attempt + 1))
        if [ "$attempt" -eq 1 ]; then
            echo -e "${B}Downloading $model for AI Wizard. This can take a few minutes on first run.${N}"
        else
            # 2026-05-22 audit (Agent C): the previous copy said "Trying
            # fallback" / "Pull failed" which read like the install was
            # dying. Reframe as a positive swap so first-time users
            # don't panic when the chain walks to a smaller model.
            echo -e "${B}Switching to a smaller model: $model (better fit for this rig)...${N}"
        fi
        # Skip ahead if this fallback is already on disk
        if ollama_model_installed "$model"; then
            echo -e "${G}Model $model already installed; using it for the AI Wizard.${N}"
            return 0
        fi
        set +e
        OLLAMA_MODELS="$OLLAMA_MODELS" "$OLLAMA_BIN" pull "$model" 2>&1 | tee -a "$NVH_LOGS/model-pull.log"
        pull_rc=${PIPESTATUS[0]}
        set -e
        if [ "$pull_rc" -eq 0 ]; then
            if [ "$model" != "$preferred" ]; then
                echo -e "${G}Wizard is using $model. Multimodal tools (vision/reasoning) still work.${N}"
            else
                echo -e "${G}Model $model ready for AI Wizard.${N}"
            fi
            return 0
        fi
        # Internal/log line — kept terse for the model-pull.log tail but
        # not formatted as a scary user-facing failure. The next iteration
        # of the loop will surface the actual user-facing "switching to
        # smaller model" message.
        echo -e "${D}  ($model not available; trying the next option…)${N}"
        # If the failed pull is one of the NVIDIA Omni tags (the Ollama
        # library doesn't publish them yet as of 2026-05-17), try the
        # HuggingFace → Modelfile bootstrap before walking further down
        # the fallback chain. This lands the actual NVIDIA Nemotron
        # Omni model — vision + reasoning — instead of degrading to a
        # generic llama3.2-vision.
        case "$model" in
            nemotron-omni|nemotron-3-nano-omni)
                if bootstrap_omni_via_hf "$model"; then
                    return 0
                fi
                ;;
        esac
    done
    echo -e "${Y}AI Wizard model download did not complete. Log: $NVH_LOGS/model-pull.log${N}"
    return "$pull_rc"
}

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

download_source_archive() {
    local target="$1"
    local archive="$NVH_CACHE/bootstrap/nvhive-main.tar.gz"
    rm -rf "$target"
    mkdir -p "$target" "$(dirname "$archive")"
    download_to_file "https://github.com/thatcooperguy/nvHive/archive/refs/heads/main.tar.gz" "$archive" || return 1
    tar xz -C "$target" --strip-components=1 < "$archive"
}

refresh_nvh_repo() {
    local tmp_repo
    [ -d "$NVH_REPO" ] || return 1
    echo -e "${B}Updating NVHive source...${N}"
    if [ -d "$NVH_REPO/.git" ] && command -v git &>/dev/null; then
        if (
            cd "$NVH_REPO" || exit 1
            git remote set-url origin https://github.com/thatcooperguy/nvHive.git >/dev/null 2>&1 || true
            git fetch --depth 1 origin main --quiet
            git checkout -q -B main FETCH_HEAD
        ); then
            return 0
        fi
    fi

    tmp_repo="$NVH_HOME/repo.refresh.$$"
    if download_source_archive "$tmp_repo"; then
        rm -rf "$NVH_REPO"
        mv "$tmp_repo" "$NVH_REPO"
        return 0
    fi
    rm -rf "$tmp_repo"
    return 1
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

install_nvhive_package() {
    local env_python
    env_python="$(env_python_path)" || env_python="$PYTHON"
    "$env_python" -m pip install -q -e "$NVH_REPO[serve,nvidia]" 2>"$NVH_LOGS/pip-install.log"
}

activate_nvh_python_env() {
    if [ -f "$NVH_VENV/bin/activate" ]; then
        # shellcheck disable=SC1091
        source "$NVH_VENV/bin/activate"
    else
        export PATH="$NVH_HOME/runtimes/node/current/bin:$NVH_VENV/bin:$PATH"
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
    export PATH="$NVH_HOME/runtimes/node/current/bin:$NVH_VENV/bin:$NVH_BIN:$PATH"
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
    refresh_nvh_repo || echo -e "${Y}Could not refresh source; continuing with local repo.${N}"

    # Existing install found - heal if needed, then activate
    heal_venv
    activate_nvh_python_env

    # Reinstall after source refresh so existing tarball-based installs do not stay stale.
    install_nvhive_package || echo -e "${Y}Package reinstall warning. Log: $NVH_LOGS/pip-install.log${N}"

    # Verify nvh command works
    if command -v nvh &>/dev/null; then
        echo -e "${G}NVHive ready.${N}"
    else
        echo -e "${Y}Reinstalling...${N}"
        install_nvhive_package || {
            echo -e "${R}Reinstall failed. Log: $NVH_LOGS/pip-install.log${N}"
            exit 1
        }
    fi

    # Ensure the rootless local runtime is present and runnable before the UI opens.
    if [ -n "$GPU_NAME" ]; then
        OLLAMA_BIN="$NVH_BIN/ollama"
        install_rootless_ollama_binary || OLLAMA_BIN=""
        if [ -n "$OLLAMA_BIN" ] && ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
            start_ollama_with_health_wait "$OLLAMA_BIN" "$OLLAMA_MODELS"
        fi
    fi

    export PATH="$NVH_HOME/runtimes/node/current/bin:$NVH_VENV/bin:$NVH_BIN:$PATH"

    install_uninstall_script
    install_command_shims
    sync_ollama_default_model_config
    install_shell_hook

    echo ""
    echo -e "  Type ${G}nvh${N} to start chatting"
    echo -e "  Activate manually: ${G}source \"$NVH_HOME/nvh-env.sh\"${N}"
    echo -e "  Uninstall/reset:   ${G}bash \"$NVH_HOME/uninstall.sh\" --help${N}"
    echo ""
    launch_webui_after_install
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
        download_source_archive "$NVH_REPO"
    }
else
    download_source_archive "$NVH_REPO"
fi
install_uninstall_script
install_command_shims

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

export PATH="$NVH_HOME/runtimes/node/current/bin:$NVH_VENV/bin:$NVH_BIN:$PATH"

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
    default_model: "ollama/__NVH_DEFAULT_OLLAMA_MODEL__"
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
    set_config_ollama_model "$HIVE_DIR/config.yaml" "$DEFAULT_OLLAMA_MODEL"
    echo -e "${G}Config created: $HIVE_DIR/config.yaml${N}"
else
    sync_ollama_default_model_config
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
    install_shell_hook
fi

# ---------------------------------------------------------------------------
# Set up Ollama (local AI) — ALWAYS, on every Linux install
# ---------------------------------------------------------------------------
# Previously this whole block was gated on `[ -n "$GPU_NAME" ]`. That gate
# made the install do nothing on rigs where `nvidia-smi` wasn't on PATH —
# which is common on cloud GPU desktops where the GPU is present but the
# NVIDIA userspace tooling isn't installed at OS image-build time, OR
# `nvidia-smi` lives outside the default PATH. Result: real-rig 2026-05-22
# photo showed "Ollama Local AI: MISSING-RUNTIME" on an RTX 4080 rig
# because GPU_NAME ended up empty, so install_rootless_ollama_binary was
# never called.
#
# The rootless Ollama tarball runs CPU-only on hosts without a GPU
# (slow but functional), and the Wizard's multimodal fallback chain
# (moondream at ~2 GB) keeps it usable. Wasting ~600 MB on a no-GPU host
# beats shipping a non-working install on every cloud GPU rig where the
# driver tooling is unusual.
OLLAMA_BIN="$NVH_BIN/ollama"
# 2026-05-22 real-rig regression: install_rootless_ollama_binary failing
# silently meant install.sh continued as if Ollama were installed, then
# the bring-up pipeline aborted at the very end with the confusing
# "ollama binary not found (run install.sh first)" message — but
# install.sh HAD just run. Fix: surface the install-log tail INLINE on
# failure so the user can see exactly what went wrong (download 404,
# extract failure, missing libc, whatever) AND continue with a clear
# warning instead of pretending Ollama is installed.
if ! install_rootless_ollama_binary; then
    OLLAMA_BIN=""
    echo ""
    echo -e "${R}Ollama binary install failed.${N}"
    echo -e "${Y}install.sh will continue (API + WebUI can still come up on cloud providers),${N}"
    echo -e "${Y}but the local Wizard won't work until Ollama is installed.${N}"
    if [ -s "$NVH_LOGS/ollama-install.log" ]; then
        echo -e "${D}--- ollama-install.log tail (last 20 lines) ---${N}"
        tail -n 20 "$NVH_LOGS/ollama-install.log" | sed "s/^/  /"
        echo -e "${D}--- end tail ---${N}"
        echo -e "${D}Full log: $NVH_LOGS/ollama-install.log${N}"
    fi
    echo ""
fi

# Start Ollama
if [ -n "$OLLAMA_BIN" ] && ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
    start_ollama_with_health_wait "$OLLAMA_BIN" "$OLLAMA_MODELS"
fi

# First-run model pull. Previously deferred to the WebUI on launch-mode
# installs ("WebUI will show AI Wizard model download…"), but the WebUI
# launch path is `nvh workstation --launch -y` — which does NOT imply
# `--with-local-ai`, so the deferred pull never happened. Result: fresh
# install ended with no model, Wizard had nothing to route to, falling
# back to cloud providers (which aren't configured).
#
# The fix: always run pull_nvwizard_model_cli on first install. It has
# the HF→Modelfile bootstrap for Omni tags that aren't in the Ollama
# library, the multi-step fallback chain to llama3.2-vision → minicpm-v
# → moondream, the 10s countdown so users on metered connections can
# press 's' to skip, and the NVH_INSTALL_MODEL_DOWNLOAD=0 opt-out.
if [ -n "$OLLAMA_BIN" ] && curl -sf http://localhost:11434/api/tags &>/dev/null; then
    MODEL="$DEFAULT_OLLAMA_MODEL"
    if ollama_model_installed "$MODEL"; then
        echo -e "${G}Model $MODEL ready.${N}"
    else
        pull_nvwizard_model_cli "$MODEL" || true
    fi
fi

# Show the full capability matrix for this GPU, and (if opted in)
# stage the matching ComfyUI / speech / music packs. See
# docs/GPU_TIER_MATRIX.md for the source of truth.
if [ -n "$GPU_NAME" ]; then
    print_capability_summary
    stage_full_capability_for_vram_tier "$VRAM_GB"
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
echo -e "  ${D}Uninstall: bash $NVH_HOME/uninstall.sh${N}"
echo -e "  ${D}Fresh reset: bash $NVH_HOME/uninstall.sh --purge -y${N}"
echo ""
echo -e "  ${G}Start now:${N} ${G}nvh${N} or ${G}$NVH_VENV/bin/nvh${N}"
echo -e "  ${D}The installer also created ~/.local/bin/nvh for future terminals.${N}"
echo ""
launch_webui_after_install
