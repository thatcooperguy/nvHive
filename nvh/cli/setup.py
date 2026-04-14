"""First-run guided setup menu.

Triggered automatically on first run (no config file + no API keys).
Detects GPU tier, shows provider status, collects API keys, and
optionally pulls recommended Ollama models. Skippable with --skip-setup
or by pressing Enter through prompts.

Uses Rich for terminal UI (consistent with the rest of the CLI).
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from nvh.config.settings import DEFAULT_CONFIG_DIR

# ---------------------------------------------------------------------------
# Provider definitions — the four core providers plus Ollama
# ---------------------------------------------------------------------------

# (name, display_name, env_var, signup_url)
CORE_PROVIDERS = [
    ("groq", "Groq", "GROQ_API_KEY", "https://console.groq.com/keys"),
    ("openai", "OpenAI", "OPENAI_API_KEY", "https://platform.openai.com/api-keys"),
    ("anthropic", "Anthropic", "ANTHROPIC_API_KEY", "https://console.anthropic.com/settings/keys"),
    ("google", "Google Gemini", "GOOGLE_API_KEY", "https://aistudio.google.com/apikey"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_env_keys() -> None:
    """Load API keys from keyring and ~/.hive/.env into os.environ.

    Checks keyring first (primary storage), then falls back to .env file
    (headless fallback). Keys are set in os.environ so that config YAML
    ``${VAR}`` interpolation can resolve them without warnings.
    """
    # --- Keyring: load all known provider keys into os.environ -----------
    _KEYRING_KEYS = [
        ("groq", "GROQ_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ]
    try:
        import keyring
        for name, env_var in _KEYRING_KEYS:
            if not os.environ.get(env_var):
                val = keyring.get_password("nvhive", f"{name}_api_key")
                if val:
                    os.environ[env_var] = val
    except Exception:
        pass

    # --- .env file: fallback for headless servers without keyring --------
    try:
        env_file = DEFAULT_CONFIG_DIR / ".env"
        if not env_file.exists():
            return
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            var, _, val = line.partition("=")
            var = var.strip()
            val = val.strip()
            # Don't overwrite existing env vars (keyring may have set them above)
            if var and val and not os.environ.get(var):
                os.environ[var] = val
    except Exception:
        pass


def _check_provider_key(name: str, env_var: str) -> str | None:
    """Return the API key if configured (env or keyring), else None."""
    # Check environment variable
    val = os.environ.get(env_var)
    if val:
        return val
    # Check keyring
    try:
        import keyring
        val = keyring.get_password("nvhive", f"{name}_api_key")
        if val:
            return val
    except Exception:
        pass
    return None


def _store_key(name: str, env_var: str, key: str) -> bool:
    """Store an API key via keyring or .env fallback. Returns True on success."""
    # Try keyring first
    try:
        import keyring
        keyring.set_password("nvhive", f"{name}_api_key", key)
        return True
    except Exception:
        pass

    # Fallback: write to ~/.hive/.env (works on headless servers with no keyring)
    try:
        env_file = DEFAULT_CONFIG_DIR / ".env"
        DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        # Read existing lines, update or append
        existing_lines: list[str] = []
        if env_file.exists():
            existing_lines = env_file.read_text().splitlines()

        updated = False
        for i, line in enumerate(existing_lines):
            if line.startswith(f"{env_var}="):
                existing_lines[i] = f"{env_var}={key}"
                updated = True
                break
        if not updated:
            existing_lines.append(f"{env_var}={key}")

        env_file.write_text("\n".join(existing_lines) + "\n")
        # Restrict permissions on Unix (best-effort)
        try:
            env_file.chmod(0o600)
        except OSError:
            pass
        return True
    except Exception:
        pass
    return False


def _detect_gpu_info() -> tuple[list, float, str, str]:
    """Detect GPUs and return (gpu_list, total_vram, tier_name, tier_desc).

    Returns safe defaults if detection fails.
    """
    try:
        from nvh.core.agentic import (
            TIER_DESCRIPTIONS,
            detect_agent_tier,
        )
        from nvh.utils.gpu import detect_gpus

        gpus = detect_gpus()
        total_vram = sum(g.vram_gb for g in gpus) if gpus else 0.0
        tier = detect_agent_tier(total_vram)
        tier_desc = TIER_DESCRIPTIONS.get(tier, "Unknown")
        return gpus, total_vram, tier.value, tier_desc
    except Exception:
        return [], 0.0, "tier_0", "Fully cloud (no local GPU)"


def _ollama_running() -> tuple[bool, list[str]]:
    """Check if Ollama is running and return (running, installed_models)."""
    try:
        import httpx
        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        resp = httpx.get(f"{base}/api/tags", timeout=3)
        if resp.status_code == 200:
            models = [
                m.get("name", "")
                for m in resp.json().get("models", [])
            ]
            return True, models
    except Exception:
        pass
    return False, []


def _find_ollama_binary() -> str | None:
    """Return path to an existing Ollama binary, or None."""
    import shutil

    # Check PATH first (system install)
    which = shutil.which("ollama")
    if which:
        return which

    # Check nvhive-local install locations (new layout: bin/ollama, legacy: ollama)
    nvh_home = Path.home() / ".nvh"
    for candidate in [nvh_home / "bin" / "ollama", nvh_home / "ollama"]:
        if candidate.exists() and os.access(str(candidate), os.X_OK):
            return str(candidate)

    return None


def _install_ollama(console: Console) -> str | None:
    """Download and install Ollama to ~/.nvh/. Returns binary path or None.

    Ollama ships as a .tar.zst archive containing bin/ollama plus CUDA libs.
    We stream-download with a Rich progress bar, then decompress and extract
    using the ``zstandard`` Python package — no root or system tools needed.
    """
    import platform
    import subprocess

    if platform.system() != "Linux":
        console.print(
            "  [yellow]Auto-install is only supported on Linux.[/yellow]\n"
            "  [dim]Install manually: https://ollama.com/download[/dim]"
        )
        return None

    # Detect architecture
    arch = "arm64" if platform.machine() in ("aarch64", "arm64") else "amd64"

    nvh_home = Path.home() / ".nvh"
    nvh_home.mkdir(parents=True, exist_ok=True)
    ollama_bin = nvh_home / "bin" / "ollama"

    url = f"https://ollama.com/download/ollama-linux-{arch}.tar.zst"
    archive_path = nvh_home / f"ollama-linux-{arch}.tar.zst"

    # --- Download with Rich progress bar ---
    console.print("  Downloading Ollama (this is ~2 GB — includes CUDA libraries)...")
    downloaded = False

    try:
        import httpx
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )

        with httpx.stream("GET", url, follow_redirects=True, timeout=600) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))

            with Progress(
                TextColumn("  "),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Ollama", total=total or None)
                with open(archive_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=131072):
                        f.write(chunk)
                        progress.update(task, advance=len(chunk))

        if archive_path.exists() and archive_path.stat().st_size > 1_000_000:
            downloaded = True
    except Exception:
        pass

    # Fallback: try curl with progress bar
    if not downloaded:
        try:
            result = subprocess.run(
                ["curl", "-fSL", "--progress-bar", url, "-o", str(archive_path)],
                timeout=600,
            )
            if result.returncode == 0 and archive_path.exists() and archive_path.stat().st_size > 1_000_000:
                downloaded = True
        except Exception:
            pass

    if not downloaded:
        archive_path.unlink(missing_ok=True)
        console.print(
            "  [red]Download failed.[/red] Install manually:\n"
            "    curl -fsSL https://ollama.com/install.sh | sh"
        )
        return None

    # --- Extract tar.zst using Python (no system zstd needed) ---
    console.print("  Extracting Ollama (this may take a moment)...")
    try:
        import shutil
        import tarfile

        import zstandard

        dctx = zstandard.ZstdDecompressor()
        with open(archive_path, "rb") as compressed:
            with dctx.stream_reader(compressed) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    # Extract only bin/ and lib/ — skip anything unexpected
                    for member in tar:
                        # Security: prevent path traversal
                        if member.name.startswith("/") or ".." in member.name:
                            continue
                        tar.extract(member, path=str(nvh_home))

        console.print("  [green]Extraction complete.[/green]")
    except Exception as exc:
        console.print(f"  [red]Extraction failed: {exc}[/red]")

        # Fallback: try system zstd + tar if Python extraction failed
        if shutil.which("zstd"):
            console.print("  Retrying with system zstd...")
            try:
                result = subprocess.run(
                    ["bash", "-c", f"zstd -d < '{archive_path}' | tar xf - -C '{nvh_home}'"],
                    capture_output=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    console.print("  [green]Extraction complete.[/green]")
                else:
                    archive_path.unlink(missing_ok=True)
                    return None
            except Exception:
                archive_path.unlink(missing_ok=True)
                return None
        else:
            archive_path.unlink(missing_ok=True)
            return None

    # Clean up archive to free disk space
    archive_path.unlink(missing_ok=True)

    if ollama_bin.exists():
        ollama_bin.chmod(0o755)
        console.print(f"  [green]Installed Ollama to {ollama_bin}[/green]")
        return str(ollama_bin)

    console.print("  [red]Binary not found after extraction.[/red]")
    return None


def _start_ollama(console: Console, ollama_bin: str) -> bool:
    """Start 'ollama serve' in the background. Returns True if it comes up."""
    import subprocess
    import time

    nvh_home = Path.home() / ".nvh"
    models_dir = nvh_home / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    log_path = nvh_home / "ollama.log"

    env = os.environ.copy()
    env["OLLAMA_MODELS"] = str(models_dir)
    env["OLLAMA_HOST"] = "127.0.0.1:11434"  # bind to localhost only

    # Add CUDA libs from local install (tar.zst extracts lib/ollama/)
    lib_dir = nvh_home / "lib" / "ollama"
    if lib_dir.is_dir():
        existing_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{lib_dir}:{existing_ld}" if existing_ld else str(lib_dir)

    console.print("  Starting Ollama...")
    try:
        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            [ollama_bin, "serve"],
            stdout=log_file,
            stderr=log_file,
            env=env,
            start_new_session=True,
        )
        log_file.close()  # child inherited the fd, parent can close
    except Exception as exc:
        console.print(f"  [red]Failed to start Ollama: {exc}[/red]")
        return False

    # Wait up to 15 seconds for Ollama to be ready
    for i in range(15):
        time.sleep(1)
        # Check if process died
        if proc.poll() is not None:
            log_file.close()
            console.print(f"  [red]Ollama exited with code {proc.returncode}.[/red]")
            try:
                tail = log_path.read_text().strip().splitlines()[-5:]
                for line in tail:
                    console.print(f"  [dim]{line}[/dim]")
            except Exception:
                pass
            console.print(f"  [dim]Full log: {log_path}[/dim]")
            return False
        running, _ = _ollama_running()
        if running:
            console.print("  [green]Ollama is running.[/green]")
            console.print(f"  [dim]Log: {log_path}[/dim]")
            return True

    console.print("  [yellow]Ollama started but not responding yet. It may need more time.[/yellow]")
    console.print(f"  [dim]Check log: {log_path}[/dim]")
    return False


def _pull_model(console: Console, model: str, ollama_bin: str) -> bool:
    """Pull an Ollama model with a Rich progress bar.

    Parses ``ollama pull`` stderr/stdout which emits lines like:
        pulling abc123... 45% |██      | 1.2 GB/2.7 GB
    Falls back to a plain spinner if parsing fails.
    """
    import subprocess

    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    # Use the Ollama HTTP API for pull — it streams JSON progress
    try:
        import json

        import httpx

        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        with Progress(
            TextColumn("  "),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(model, total=None)

            with httpx.stream(
                "POST",
                f"{base}/api/pull",
                json={"name": model},
                timeout=None,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    status = data.get("status", "")
                    total = data.get("total")
                    completed = data.get("completed")

                    if total and total > 0:
                        progress.update(task, total=total, completed=completed or 0)
                    progress.update(task, description=f"{model}: {status}")

        console.print(f"  [green]Pulled {model}.[/green]")
        return True

    except Exception:
        pass

    # Fallback: plain subprocess call
    console.print(f"  Pulling {model} (this may take a while)...")
    try:
        result = subprocess.run(
            [ollama_bin, "pull", model],
            capture_output=False,
            timeout=1800,
        )
        if result.returncode == 0:
            console.print(f"  [green]Pulled {model}.[/green]")
            return True
    except Exception:
        pass

    console.print(f"  [yellow]Failed to pull {model}.[/yellow]")
    return False


def _ensure_ollama(console: Console) -> tuple[bool, list[str]]:
    """Ensure Ollama is installed and running. Returns (running, models)."""
    # Already running?
    running, models = _ollama_running()
    if running:
        return running, models

    # Find or install the binary
    ollama_bin = _find_ollama_binary()
    if not ollama_bin:
        try:
            answer = console.input(
                "  Ollama is not installed. Download it now? [Y/n] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("n", "no"):
            return False, []
        ollama_bin = _install_ollama(console)
        if not ollama_bin:
            return False, []

    # Binary exists but not running — start it
    console.print()
    started = _start_ollama(console, ollama_bin)
    if started:
        return _ollama_running()
    return False, []


def _get_recommended_models(total_vram: float) -> list[str]:
    """Return recommended Ollama model tags for the detected VRAM."""
    try:
        from nvh.utils.gpu import detect_gpus, recommend_models
        gpus = detect_gpus()
        recs = recommend_models(gpus) if gpus else []
        return [r.model for r in recs]
    except Exception:
        pass

    # Fallback: manual recommendations by VRAM
    # llama3.2-vision (~7GB) included for desktop agent — best spatial grounding
    # minicpm-v (~5GB) for tighter VRAM budgets
    if total_vram >= 128:
        return ["nemotron:70b", "llama3.3:70b", "qwen2.5-coder:32b", "llama3.2-vision"]
    if total_vram >= 96:
        return ["llama3.3:70b", "qwen2.5-coder:32b", "llama3.2-vision"]
    if total_vram >= 48:
        return ["llama3.3:70b", "llama3.2-vision"]
    if total_vram >= 24:
        return ["gemma2:27b", "llama3.2-vision"]
    if total_vram >= 16:
        return ["qwen2.5-coder:7b", "minicpm-v"]
    if total_vram >= 8:
        return ["minicpm-v"]
    return []


def _open_in_browser(url: str) -> bool:
    """Open a URL in the default browser. Returns True on success."""
    try:
        import subprocess
        import sys

        if sys.platform == "darwin":
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "win32":
            os.startfile(url)
        else:
            # Linux — try xdg-open, then common browsers
            for cmd in ["xdg-open", "firefox", "google-chrome", "chromium-browser"]:
                try:
                    subprocess.Popen(
                        [cmd, url],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    return True
                except FileNotFoundError:
                    continue
            return False
        return True
    except Exception:
        return False


def _get_clipboard() -> str:
    """Read the system clipboard. Returns empty string on failure."""
    try:
        import subprocess
        import sys

        if sys.platform == "darwin":
            result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2)
            return result.stdout.strip()
        elif sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-Command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=2,
            )
            return result.stdout.strip()
        else:
            # Linux — try xclip, then xsel
            for cmd in [["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]:
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        return result.stdout.strip()
                except FileNotFoundError:
                    continue
    except Exception:
        pass
    return ""


# API key patterns — prefix → min length
_KEY_PATTERNS = {
    "groq": (["gsk_"], 20),
    "openai": (["sk-"], 20),
    "anthropic": (["sk-ant-"], 20),
    "google": (["AIza"], 20),
}


def _looks_like_api_key(text: str, provider: str) -> bool:
    """Check if text looks like an API key for the given provider."""
    text = text.strip()
    prefixes, min_len = _KEY_PATTERNS.get(provider, ([], 20))
    if len(text) < min_len:
        return False
    if prefixes:
        return any(text.startswith(p) for p in prefixes)
    # Generic: long alphanumeric string
    return len(text) >= 20 and text.isascii()


def _watch_clipboard_for_key(
    console: Console, provider: str, timeout_seconds: int = 60,
) -> str | None:
    """Watch the clipboard for an API key. Returns key or None on timeout."""
    import time

    initial_clipboard = _get_clipboard()
    console.print(
        f"  [dim]Watching clipboard for {timeout_seconds}s — "
        f"copy your API key from the browser...[/dim]"
    )

    start = time.monotonic()
    last_dot = start
    while time.monotonic() - start < timeout_seconds:
        time.sleep(0.5)
        current = _get_clipboard()

        # Check if clipboard changed and contains a key
        if current and current != initial_clipboard:
            if _looks_like_api_key(current, provider):
                return current

        # Print a dot every 5 seconds to show we're still watching
        if time.monotonic() - last_dot >= 5:
            console.print("  [dim].[/dim]", end="")
            last_dot = time.monotonic()

    console.print()
    return None


def _write_config(configured_providers: dict[str, str]) -> Path:
    """Write a minimal config.yaml enabling the configured providers.

    Returns the path to the written file.
    """
    from nvh.config.settings import DEFAULT_CONFIG_PATH, get_config_dir

    get_config_dir()

    lines = [
        'version: "1"',
        "",
        "defaults:",
        '  provider: ""',
        "  output: text",
        "  stream: true",
        "  timeout: 30",
        "  max_tokens: 4096",
        "  temperature: 1.0",
        "  show_metadata: true",
        "",
        "advisors:",
    ]

    # Provider configs — enable those we have keys for
    advisor_defs = {
        "groq": {
            "env": "GROQ_API_KEY",
            "model": "groq/llama-3.3-70b-versatile",
            "fallback": "groq/llama-3.1-8b-instant",
        },
        "openai": {
            "env": "OPENAI_API_KEY",
            "model": "gpt-4o",
            "fallback": "gpt-4o-mini",
        },
        "anthropic": {
            "env": "ANTHROPIC_API_KEY",
            "model": "claude-sonnet-4-6",
            "fallback": "claude-haiku-4-5-20251001",
        },
        "google": {
            "env": "GOOGLE_API_KEY",
            "model": "gemini/gemini-2.0-flash",
            "fallback": "gemini/gemini-2.0-flash",
        },
        "ollama": {
            "env": None,
            "model": "ollama/nemotron-small",
            "base_url": "http://localhost:11434",
        },
    }

    for name, info in advisor_defs.items():
        enabled = name in configured_providers or name == "ollama"
        lines.append(f"  {name}:")
        if info.get("env"):
            lines.append(f"    api_key: ${{{info['env']}}}")
        if info.get("base_url"):
            lines.append(f"    base_url: {info['base_url']}")
            lines.append("    type: ollama")
        lines.append(f"    default_model: {info['model']}")
        if info.get("fallback"):
            lines.append(f"    fallback_model: {info['fallback']}")
        lines.append(f"    enabled: {str(enabled).lower()}")
        lines.append("")

    DEFAULT_CONFIG_PATH.write_text("\n".join(lines))
    return DEFAULT_CONFIG_PATH


# ---------------------------------------------------------------------------
# Main guided setup
# ---------------------------------------------------------------------------

def guided_setup(console: Console | None = None) -> None:
    """Run the first-run guided setup menu.

    Steps:
    1. Detect GPU + install Ollama + pull local models (combined hardware step)
    2. Show provider status (Ollama now running, shows accurate picture)
    3. Configure API keys (with desktop agent assist if vision model available)

    The entire setup is skippable by pressing Enter at each prompt.
    """
    if console is None:
        console = Console()

    # Load any keys previously saved to ~/.hive/.env (headless fallback)
    load_env_keys()

    console.print()
    console.print(
        Panel(
            "[bold]Welcome to nvHive[/bold]\n\n"
            "This one-time setup detects your hardware, sets up local AI,\n"
            "and configures cloud providers. Press [bold]Enter[/bold] to skip any step.",
            border_style="green",
            padding=(1, 2),
        )
    )

    # ------------------------------------------------------------------
    # Step 1: Hardware + Local AI (combined)
    # ------------------------------------------------------------------
    console.print()
    console.print("[bold green]Step 1/3:[/bold green] Hardware + Local AI\n")

    gpus, total_vram, tier_name, tier_desc = _detect_gpu_info()

    gpu_table = Table(show_header=False, box=None, padding=(0, 2))
    gpu_table.add_column("Label", style="dim")
    gpu_table.add_column("Value")

    if gpus:
        for gpu in gpus:
            gpu_table.add_row("GPU", f"{gpu.name} ({gpu.vram_gb:.0f} GB VRAM)")
        gpu_table.add_row("Total VRAM", f"{total_vram:.0f} GB")
    else:
        gpu_table.add_row("GPU", "None detected (CPU only)")

    gpu_table.add_row("Agent Tier", f"{tier_name} - {tier_desc}")
    console.print(gpu_table)

    has_vision_model = False
    ollama_up = False
    ollama_models: list[str] = []

    if total_vram > 0:
        console.print()
        # Ensure Ollama is installed and running
        ollama_up, ollama_models = _ollama_running()
        if not ollama_up:
            ollama_up, ollama_models = _ensure_ollama(console)
            console.print()

        recommended = _get_recommended_models(total_vram)
        if recommended and ollama_up:
            console.print(
                f"  Recommended models for your GPU ({total_vram:.0f} GB VRAM):\n"
            )
            for model in recommended:
                installed = any(model in m for m in ollama_models)
                if installed:
                    console.print(f"    [green]installed[/green]  {model}")
                else:
                    console.print(f"    [dim]available[/dim]   {model}")

            # Ask to pull missing models
            missing = [
                m for m in recommended
                if not any(m in existing for existing in ollama_models)
            ]
            if missing:
                console.print()
                try:
                    pull = console.input(
                        f"  Pull {len(missing)} recommended model(s)? [Y/n] "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    pull = "n"

                if pull not in ("n", "no"):
                    ollama_bin = _find_ollama_binary() or "ollama"
                    for model in missing:
                        _pull_model(console, model, ollama_bin)
                    # Refresh model list after pulling
                    ollama_up, ollama_models = _ollama_running()
                else:
                    console.print("  [dim]Skipped model pull.[/dim]")
                    if missing:
                        console.print("  [dim]You can pull later with:[/dim]")
                        for m in missing:
                            console.print(f"    [dim]ollama pull {m}[/dim]")
            else:
                console.print("\n  [green]All recommended models already installed.[/green]")

            # Check if a vision model is now available
            from nvh.core.vision_tools import _detect_ollama_vision_model
            has_vision_model = _detect_ollama_vision_model() is not None
            if has_vision_model:
                console.print("  [green]Desktop agent: ready (vision model loaded)[/green]")

        elif recommended and not ollama_up:
            console.print(
                "  [yellow]Could not start Ollama.[/yellow] "
                "You can start it manually later:\n"
            )
            console.print("    ollama serve")
            console.print()
            console.print("  Then pull recommended models:")
            for model in recommended:
                console.print(f"    ollama pull {model}")
        elif not recommended:
            console.print("  [dim]No model recommendations for this GPU tier.[/dim]")
    else:
        console.print(
            "\n  [dim]No GPU detected. nvHive will use cloud providers.[/dim]\n"
            "  [dim]Install Ollama for CPU-based local inference: https://ollama.com[/dim]"
        )

    # ------------------------------------------------------------------
    # Step 2: Show provider status (now accurate — Ollama is running)
    # ------------------------------------------------------------------
    console.print()
    console.print("[bold green]Step 2/3:[/bold green] Provider status\n")

    provider_table = Table(box=None, padding=(0, 2), show_header=True)
    provider_table.add_column("Provider", style="bold")
    provider_table.add_column("Status")
    provider_table.add_column("Signup")

    configured_providers: dict[str, str] = {}

    for name, display, env_var, url in CORE_PROVIDERS:
        key = _check_provider_key(name, env_var)
        if key:
            configured_providers[name] = key
            masked = key[:4] + "..." + key[-4:] if len(key) > 8 else "***"
            provider_table.add_row(display, f"[green]configured[/green] ({masked})", "")
        else:
            provider_table.add_row(display, "[yellow]not configured[/yellow]", f"[dim]{url}[/dim]")

    # Ollama status (now should be running from Step 1)
    if ollama_up:
        provider_table.add_row(
            "Ollama (local)",
            f"[green]running[/green] ({len(ollama_models)} models)",
            "",
        )
    else:
        provider_table.add_row(
            "Ollama (local)",
            "[dim]not running[/dim]",
            "[dim]https://ollama.com[/dim]",
        )

    console.print(provider_table)

    # ------------------------------------------------------------------
    # Step 3: Configure API keys (with desktop agent assist)
    # ------------------------------------------------------------------
    unconfigured = [
        (name, display, env_var, url)
        for name, display, env_var, url in CORE_PROVIDERS
        if name not in configured_providers
    ]

    if unconfigured:
        console.print()
        console.print("[bold green]Step 3/3:[/bold green] Configure API keys\n")

        # Detect if we have a desktop (can open browser + read clipboard)
        has_desktop = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        clipboard_works = bool(_get_clipboard() or has_desktop)

        if has_desktop and clipboard_works and has_vision_model:
            console.print(
                "  [dim]Desktop agent is ready! For each provider I'll:[/dim]\n"
                "  [dim]  1. Open the signup page in your browser[/dim]\n"
                "  [dim]  2. Take a screenshot to verify the page[/dim]\n"
                "  [dim]  3. Watch your clipboard for the API key[/dim]\n"
                "  [dim]Press Enter to skip any provider.[/dim]\n"
            )
        elif has_desktop and clipboard_works:
            console.print(
                "  [dim]For each provider, I'll open the signup page in your browser.[/dim]\n"
                "  [dim]Just copy the API key — I'll detect it from your clipboard.[/dim]\n"
                "  [dim]Press Enter to skip any provider.[/dim]\n"
            )
        else:
            console.print(
                "  [dim]Paste each key and press Enter. Press Enter with no input to skip.[/dim]\n"
            )

        for name, display, env_var, url in unconfigured:
            key = ""

            if has_desktop and clipboard_works:
                # Smart mode: open browser + watch clipboard
                try:
                    answer = console.input(
                        f"  Open {display} signup page in browser? [Y/n] "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n  [dim]Setup interrupted.[/dim]")
                    break

                if answer in ("n", "no"):
                    console.print(f"  [dim]Skipped {display}[/dim]")
                    continue

                opened = _open_in_browser(url)
                if opened:
                    console.print(f"  [dim]Opened {url}[/dim]")

                    # If we have a vision model, take a screenshot to verify
                    if has_vision_model:
                        import time as _time
                        _time.sleep(3)  # wait for browser to load
                        try:
                            import asyncio

                            from nvh.core.vision_tools import (
                                _analyze_with_ollama,
                                _detect_ollama_vision_model,
                                _ensure_display,
                            )
                            _ensure_display()

                            import pyautogui
                            screenshot_path = Path.home() / ".nvh" / "setup_screenshot.png"
                            img = pyautogui.screenshot()
                            img.save(str(screenshot_path))

                            if screenshot_path.exists():
                                import base64
                                with open(screenshot_path, "rb") as f:
                                    img_data = base64.b64encode(f.read()).decode("utf-8")
                                vm = _detect_ollama_vision_model()
                                if vm:
                                    analysis = asyncio.get_event_loop().run_until_complete(
                                        _analyze_with_ollama(
                                            img_data,
                                            f"I opened the {display} API key page. "
                                            f"Describe what you see briefly. Is there a visible "
                                            f"API key on screen? If so, read it exactly.",
                                            vm,
                                        )
                                    )
                                    if analysis:
                                        console.print(f"  [dim]Agent: {analysis[:200]}[/dim]")
                                screenshot_path.unlink(missing_ok=True)
                        except Exception:
                            pass  # vision assist is best-effort

                    detected = _watch_clipboard_for_key(console, name, timeout_seconds=60)
                    if detected:
                        masked = detected[:6] + "..." + detected[-4:]
                        console.print(f"  [green]Detected key: {masked}[/green]")
                        key = detected
                    else:
                        console.print("  [dim]No key detected from clipboard.[/dim]")
                        try:
                            key = console.input(
                                f"  Paste {display} API key manually (or Enter to skip): "
                            ).strip()
                        except (EOFError, KeyboardInterrupt):
                            console.print("\n  [dim]Setup interrupted.[/dim]")
                            break
                else:
                    console.print(f"  [dim]Could not open browser. URL: {url}[/dim]")
                    try:
                        key = console.input(
                            f"  Paste {display} API key: "
                        ).strip()
                    except (EOFError, KeyboardInterrupt):
                        console.print("\n  [dim]Setup interrupted.[/dim]")
                        break
            else:
                # Headless mode: manual paste
                try:
                    key = console.input(
                        f"  {display} API key ([dim]{url}[/dim]): "
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    console.print("\n  [dim]Setup interrupted.[/dim]")
                    break

            if not key:
                console.print(f"  [dim]Skipped {display}[/dim]")
                continue

            if len(key) < 10:
                console.print(
                    f"  [red]Key looks too short ({len(key)} chars) - skipping.[/red]"
                )
                continue

            # Quick validation for providers with known test endpoints
            valid = _validate_key(name, key, console)
            if valid is False:
                continue

            stored = _store_key(name, env_var, key)
            if stored:
                configured_providers[name] = key
                console.print(f"  [green]Saved {display} key.[/green]")
            else:
                os.environ[env_var] = key
                configured_providers[name] = key
                console.print(
                    f"  [yellow]Could not persist key.[/yellow] Key set for this session only.\n"
                    f"  [dim]To persist: export {env_var}=<your-key>  (add to shell profile)[/dim]"
                )
    else:
        console.print()
        console.print(
            "[bold green]Step 3/3:[/bold green] All core providers already configured.\n"
        )

    # ------------------------------------------------------------------
    # Save config
    # ------------------------------------------------------------------
    console.print()
    config_path = _write_config(configured_providers)
    console.print(f"  [green]Config saved to {config_path}[/green]")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    console.print()
    n_configured = len(configured_providers)
    summary = Text()
    summary.append("Setup complete! ", style="bold green")
    summary.append(f"{n_configured} provider(s) configured")
    if total_vram > 0:
        summary.append(f", {total_vram:.0f} GB VRAM detected")
    if has_vision_model:
        summary.append(", desktop agent ready")
    summary.append(".")
    console.print(Panel(summary, border_style="green"))

    console.print()
    console.print("  [bold]Next steps:[/bold]")
    console.print('    Try a query:             [bold]nvh "What is the meaning of life?"[/bold]')
    console.print("    Launch interactive chat:  [bold]nvh[/bold]")
    if has_vision_model:
        console.print("    Desktop agent:           [bold]nvh \"take a screenshot\"[/bold]")
    console.print("    Edit config:              [bold]nvh config edit[/bold]")
    console.print()


def _validate_key(name: str, key: str, console: Console) -> bool | None:
    """Quick-validate an API key. Returns True/None (ok/unknown) or False (rejected)."""
    test_urls = {
        "groq": ("https://api.groq.com/openai/v1/models", "bearer"),
        "openai": ("https://api.openai.com/v1/models", "bearer"),
        "anthropic": ("https://api.anthropic.com/v1/models", "x-api-key"),
        "google": ("https://generativelanguage.googleapis.com/v1/models", "query"),
    }
    spec = test_urls.get(name)
    if not spec:
        return None

    url, auth_type = spec
    try:
        import httpx
        if auth_type == "bearer":
            headers = {"Authorization": f"Bearer {key}"}
            resp = httpx.get(url, headers=headers, timeout=8)
        elif auth_type == "x-api-key":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            resp = httpx.get(url, headers=headers, timeout=8)
        elif auth_type == "query":
            resp = httpx.get(f"{url}?key={key}", timeout=8)
        else:
            return None

        if resp.status_code in (200, 201):
            return True
        if resp.status_code in (401, 403):
            console.print(
                f"  [red]Key rejected by {name} (HTTP {resp.status_code}). Skipping.[/red]"
            )
            return False
        # Other status — probably fine, don't block
        return None
    except Exception:
        return None
