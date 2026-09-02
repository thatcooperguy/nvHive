"""First-run guided setup menu.

Triggered automatically on first run (no config file + no API keys).
Detects GPU tier, shows provider status, collects API keys, and
optionally pulls recommended Ollama models. Skippable with --skip-setup
or by pressing Enter through prompts.

Uses Rich for terminal UI (consistent with the rest of the CLI).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from nvh.config.settings import DEFAULT_CONFIG_DIR
from nvh.providers.registry import RETIRED_PROVIDERS, resolve_provider_key
from nvh.providers.specs import PROVIDER_SPECS
from nvh.utils.ollama import ollama_base_url

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
# Retired model IDs, keyed provider -> {old_id: new_id} — verified against
# provider catalogs 2026-09-01 (0.41.1). A bare ID only means the retired
# model inside its own provider's block (llm7 also served "gpt-4o-mini").
# `nvh config migrate` rewrites these in the user's config.yaml; `nvh status
# --deep` suggests it when a configured model is in this table.
# ---------------------------------------------------------------------------

RETIRED_MODEL_RENAMES: dict[str, dict[str, str]] = {
    "openai": {
        "gpt-4o": "gpt-5.6-terra",
        "gpt-4o-mini": "gpt-5.6-luna",
    },
    "google": {
        "gemini/gemini-2.0-flash": "gemini/gemini-3.7-flash",
        "gemini/gemini-2.0-flash-lite": "gemini/gemini-3.5-flash-lite",
    },
    "groq": {
        "groq/llama-3.3-70b-versatile": "groq/openai/gpt-oss-120b",
        "groq/llama-3.1-8b-instant": "groq/openai/gpt-oss-20b",
    },
    "grok": {
        "xai/grok-2": "xai/grok-4.6",
    },
    "cohere": {
        "command-r-plus": "command-a-03-2025",
        "command-r": "command-r-08-2024",
    },
    "deepseek": {
        "deepseek/deepseek-chat": "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-reasoner": "deepseek/deepseek-v4-pro",
    },
    "perplexity": {
        "perplexity/llama-3.1-sonar-large-128k-online": "perplexity/sonar-pro",
        "perplexity/llama-3.1-sonar-small-128k-online": "perplexity/sonar",
    },
    "together": {
        "together_ai/meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo": "together_ai/openai/gpt-oss-120b",
        "together_ai/meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo": "together_ai/openai/gpt-oss-20b",
    },
    "fireworks": {
        "fireworks_ai/accounts/fireworks/models/llama-v3p1-70b-instruct":
            "fireworks_ai/accounts/fireworks/models/gpt-oss-120b",
        "fireworks_ai/accounts/fireworks/models/llama-v3p1-8b-instruct":
            "fireworks_ai/accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
    },
    "openrouter": {
        "openrouter/meta-llama/llama-3.1-70b-instruct": "openrouter/openai/gpt-oss-120b",
        "openrouter/meta-llama/llama-3.1-8b-instruct": "openrouter/openai/gpt-oss-20b",
    },
    "cerebras": {
        "cerebras/llama3.1-70b": "cerebras/gpt-oss-120b",
        "cerebras/llama3.1-8b": "cerebras/gpt-oss-120b",
    },
    "sambanova": {
        "sambanova/Meta-Llama-3.1-70B-Instruct": "sambanova/Meta-Llama-3.3-70B-Instruct",
        "sambanova/Meta-Llama-3.1-8B-Instruct": "sambanova/gpt-oss-120b",
    },
    "huggingface": {
        "huggingface/meta-llama/Meta-Llama-3-8B-Instruct": "huggingface/openai/gpt-oss-120b",
        "huggingface/mistralai/Mistral-7B-Instruct-v0.3": "huggingface/openai/gpt-oss-20b",
    },
    "ai21": {
        "jamba-1.5-large": "ai21_chat/jamba-large-1.7",
        "jamba-1.5-mini": "ai21_chat/jamba-mini-2",
    },
    "nvidia": {
        "meta/llama-3.1-70b-instruct": "nvidia_nim/meta/llama-3.3-70b-instruct",
        "meta/llama-3.1-8b-instruct": "nvidia_nim/meta/llama-3.1-8b-instruct",
    },
    "llm7": {
        "gpt-4o": "gpt-oss",
        "gpt-4o-mini": "gpt-oss",
        "llama-3.3-70b": "minimax-m2.7",
        "deepseek-r1-0528": "gpt-oss",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _layout_config_dir() -> Path:
    # Mirrors nvh.integrations.workspace.storage.storage_layout().config_dir
    # (HIVE_CONFIG_HOME, else <NVH_HOME | NVHIVE_HOME | ~/.nvh>/config) without
    # importing nvh.integrations, which costs every CLI invocation ~160 ms.
    explicit = os.environ.get("HIVE_CONFIG_HOME")
    if explicit:
        return Path(os.path.expandvars(explicit)).expanduser()
    home = os.environ.get("NVH_HOME") or os.environ.get("NVHIVE_HOME")
    if home:
        return Path(os.path.expandvars(home)).expanduser() / "config"
    return Path.home() / ".nvh" / "config"


def _env_key_files() -> list[Path]:
    """``.env`` files to load, in load order.

    ``DEFAULT_CONFIG_DIR/.env`` (~/.hive or HIVE_CONFIG_HOME) is where
    ``nvh setup`` writes; the storage layout's ``config_dir/.env`` is where the
    web wizard's save-key path writes. They are the same file only when
    HIVE_CONFIG_HOME is exported, so both are read.
    """
    files = [DEFAULT_CONFIG_DIR / ".env"]
    try:
        layout_env = _layout_config_dir() / ".env"
        if layout_env.resolve() != files[0].resolve():
            files.append(layout_env)
    except Exception:
        pass
    return files


def provider_config_files() -> list[Path]:
    """``config.yaml`` files a provider stanza may live in.

    ``nvh setup`` writes DEFAULT_CONFIG_PATH; the web wizard's save-key path
    (and the API server generally) writes the storage layout's
    ``config_dir/config.yaml``, appended when it exists as a distinct file.
    """
    from nvh.config.settings import DEFAULT_CONFIG_PATH

    files = [DEFAULT_CONFIG_PATH]
    try:
        layout_cfg = _layout_config_dir() / "config.yaml"
        if layout_cfg.exists() and layout_cfg.resolve() != DEFAULT_CONFIG_PATH.resolve():
            files.append(layout_cfg)
    except Exception:
        pass
    return files


def _load_env_file(env_file: Path) -> None:
    """Merge ``KEY=VALUE`` lines into os.environ without overriding set vars."""
    try:
        if not env_file.exists():
            return
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            var, _, val = line.partition("=")
            var = var.strip()
            val = val.strip()
            # Don't overwrite existing env vars (keyring or an earlier file may have set them)
            if var and val and not os.environ.get(var):
                os.environ[var] = val
    except Exception:
        pass


def load_env_keys(use_keyring: bool = True) -> None:
    """Load API keys from keyring and the ``.env`` files into os.environ.

    Checks keyring first (primary storage), then falls back to the .env
    files (headless fallback + web wizard save-key). Keys are set in
    os.environ so that config YAML ``${VAR}`` interpolation can resolve
    them without warnings. ``use_keyring=False`` skips the four synchronous
    keyring round-trips (the API lifespan keeps keyring opt-in).
    """
    # --- Keyring: load all known provider keys into os.environ -----------
    _KEYRING_KEYS = [
        ("groq", "GROQ_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ]
    if use_keyring:
        try:
            import keyring
            for name, env_var in _KEYRING_KEYS:
                if not os.environ.get(env_var):
                    val = keyring.get_password("nvhive", f"{name}_api_key")
                    if val:
                        os.environ[env_var] = val
        except Exception:
            pass

    # --- .env files: fallback for headless servers without keyring -------
    for env_file in _env_key_files():
        _load_env_file(env_file)


def _check_provider_key(name: str, env_var: str) -> str | None:
    """Return the API key if configured (env or keyring), else None."""
    val = resolve_provider_key(name)[0]
    if val:
        return val
    # Unconditional keyring read: setup is interactive and must see a key it
    # just stored even when NVH_USE_KEYRING is unset.
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

    # Fallback: write to HIVE_CONFIG_HOME/.env (works on headless servers with no keyring)
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


def provider_env_vars(name: str) -> list[str]:
    """Env var names a provider's key may be stored under (primary first)."""
    spec = PROVIDER_SPECS.get(name)
    names = [f"{name.upper()}_API_KEY", *(spec.env_keys if spec else ())]
    return list(dict.fromkeys(names))


def remove_key(name: str) -> dict[str, Any]:
    """Delete a provider key everywhere _store_key or the web wizard may have put it.

    Returns ``{"keyring": bool, "env_file": [vars removed], "env_paths": [files changed]}``.
    """
    result: dict[str, Any] = {"keyring": False, "env_file": [], "env_paths": []}
    try:
        import keyring
        keyring.delete_password("nvhive", f"{name}_api_key")
        result["keyring"] = True
    except Exception:
        pass

    env_vars = provider_env_vars(name)
    for env_file in _env_key_files():
        try:
            if not env_file.exists():
                continue
            kept: list[str] = []
            removed: list[str] = []
            for line in env_file.read_text().splitlines():
                var = line.partition("=")[0].strip()
                if var in env_vars:
                    removed.append(var)
                    continue
                kept.append(line)
            if removed:
                env_file.write_text("\n".join(kept) + ("\n" if kept else ""))
                result["env_file"] += [v for v in removed if v not in result["env_file"]]
                result["env_paths"].append(env_file)
        except OSError:
            pass

    for var in env_vars:
        os.environ.pop(var, None)
    return result


def _provider_sections(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(dotted path, block map) for every advisors/providers mapping in a raw config."""
    sections: list[tuple[str, dict[str, Any]]] = []
    for key in ("advisors", "providers"):
        if isinstance(data.get(key), dict):
            sections.append((key, data[key]))
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        for pname, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            for key in ("advisors", "providers"):
                if isinstance(profile.get(key), dict):
                    sections.append((f"profiles.{pname}.{key}", profile[key]))
    return sections


def disable_provider_in_config(path: Path, name: str) -> bool:
    """Set ``enabled: false`` and drop ``api_key`` for *name* in a raw config.yaml.

    Works on the raw YAML (no ``${VAR}`` interpolation) so secrets never get
    written back in plain text. Returns True if the file changed (the previous
    contents are kept in ``.yaml.bak``).
    """
    import shutil

    import yaml

    if not path.exists():
        return False
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return False
    if not isinstance(data, dict):
        return False

    changed = False
    for _section_path, blocks in _provider_sections(data):
        block = blocks.get(name)
        if not isinstance(block, dict):
            continue
        if block.get("enabled", True) is not False:
            block["enabled"] = False
            changed = True
        if "api_key" in block:
            block.pop("api_key")
            changed = True
    if changed:
        shutil.copy2(path, path.with_suffix(".yaml.bak"))
        path.write_text(yaml.safe_dump(data, default_flow_style=False, sort_keys=False))
    return changed


def rename_retired_model(provider: str, model: str) -> str | None:
    """Replacement ID for a retired *model* configured under *provider*, else None."""
    return RETIRED_MODEL_RENAMES.get(provider, {}).get(model)


def _retired_provider_note(name: str) -> str:
    return f"{name} provider retired {RETIRED_PROVIDERS[name]} — removed"


def migrate_config_data(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy of a raw config mapping plus human-readable changes.

    Rewrites retired model IDs per provider block (top-level and per-profile),
    drops retired provider stanzas, scrubs references to them from
    ``defaults`` and ``council``, and drops the top-level ``hooks:`` key
    (its consumer was deleted in 0.42). Input is never mutated.
    """
    import copy

    out = copy.deepcopy(data)
    changes: list[str] = []

    if "hooks" in out:
        del out["hooks"]
        changes.append("hooks: removed (nvh.core.hooks was deleted in 0.42)")

    for section_path, blocks in _provider_sections(out):
        for name in list(blocks):
            if name in RETIRED_PROVIDERS:
                del blocks[name]
                changes.append(f"{section_path}.{name}: {_retired_provider_note(name)}")
                continue
            block = blocks[name]
            if not isinstance(block, dict):
                continue
            for field in ("default_model", "fallback_model"):
                old = block.get(field)
                if not isinstance(old, str):
                    continue
                new = rename_retired_model(name, old)
                if new:
                    block[field] = new
                    changes.append(f"{section_path}.{name}.{field}: {old} → {new}")

    defaults = out.get("defaults")
    if isinstance(defaults, dict):
        provider = defaults.get("provider") or ""
        if provider in RETIRED_PROVIDERS:
            defaults["provider"] = ""
            changes.append(f"defaults.provider: {_retired_provider_note(provider)}")
            provider = ""
        model = defaults.get("model")
        if isinstance(model, str) and model:
            new = rename_retired_model(provider, model)
            if new:
                defaults["model"] = new
                changes.append(f"defaults.model: {model} → {new}")

    council = out.get("council")
    if isinstance(council, dict):
        weights = council.get("default_weights")
        if isinstance(weights, dict):
            for retired in [p for p in weights if p in RETIRED_PROVIDERS]:
                del weights[retired]
                changes.append(f"council.default_weights.{retired}: removed")
        order = council.get("fallback_order")
        if isinstance(order, list):
            dropped = [p for p in order if p in RETIRED_PROVIDERS]
            if dropped:
                council["fallback_order"] = [p for p in order if p not in RETIRED_PROVIDERS]
                changes.append(f"council.fallback_order: removed {', '.join(dropped)}")

    return out, changes


def stale_default_models(providers: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """(provider, field, model) for enabled providers whose configured model is retired.

    Accepts ProviderConfig objects or raw dict blocks. An enabled stanza for a
    retired provider is reported as ``(name, "provider", name)``.
    """
    def _get(block: Any, key: str, default: Any = "") -> Any:
        if isinstance(block, Mapping):
            return block.get(key, default)
        return getattr(block, key, default)

    stale: list[tuple[str, str, str]] = []
    for name, block in providers.items():
        if not _get(block, "enabled", True):
            continue
        if name in RETIRED_PROVIDERS:
            stale.append((name, "provider", name))
            continue
        for field in ("default_model", "fallback_model"):
            model = _get(block, field, "") or ""
            if model and rename_retired_model(name, model):
                stale.append((name, field, model))
    return stale


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
        resp = httpx.get(f"{ollama_base_url()}/api/tags", timeout=3)
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

    from nvh.integrations.workspace.storage import storage_layout

    # Check PATH first (system install)
    which = shutil.which("ollama")
    if which:
        return which

    # Check nvhive-local install locations (new layout: bin/ollama, legacy: ollama)
    layout = storage_layout()
    legacy_home = Path.home() / ".nvh"
    for candidate in [
        layout.bin_dir / "ollama",
        layout.home / "ollama",
        legacy_home / "bin" / "ollama",
        legacy_home / "ollama",
    ]:
        if candidate.exists() and os.access(str(candidate), os.X_OK):
            return str(candidate)

    return None


def _install_ollama_rootless_bundle(console: Console) -> str | None:
    """Install Ollama into NVH_HOME using the shared rootless download policy."""
    import platform
    import subprocess

    from nvh.integrations.installs.studio_packs import (
        _extract_ollama_archive,
        _ollama_download_candidates,
        _ollama_validation_error,
        _platform_arch,
    )
    from nvh.integrations.workspace.storage import storage_layout

    if platform.system() != "Linux":
        console.print(
            "  [yellow]Auto-install is only supported on Linux.[/yellow]\n"
            "  [dim]Install manually: https://ollama.com/download[/dim]"
        )
        return None

    try:
        arch = _platform_arch()
    except RuntimeError as exc:
        console.print(f"  [red]{exc}[/red]")
        return None

    layout = storage_layout()
    nvh_home = layout.home
    nvh_home.mkdir(parents=True, exist_ok=True)
    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    layout.cache_dir.mkdir(parents=True, exist_ok=True)
    ollama_bin = layout.bin_dir / "ollama"

    console.print("  Downloading latest compatible Ollama bundle (this can be large)...")
    archive_path: Path | None = None
    archive_type = ""
    last_error = ""

    for url, candidate_type in _ollama_download_candidates(arch):
        candidate_path = layout.cache_dir / f"ollama-linux-{arch}.{candidate_type}"
        candidate_path.unlink(missing_ok=True)
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
                    with candidate_path.open("wb") as output:
                        for chunk in resp.iter_bytes(chunk_size=131072):
                            output.write(chunk)
                            progress.update(task, advance=len(chunk))
            downloaded = candidate_path.exists() and candidate_path.stat().st_size > 1_000_000
        except Exception as exc:
            last_error = str(exc)

        if not downloaded:
            try:
                result = subprocess.run(
                    ["curl", "-fSL", "--progress-bar", url, "-o", str(candidate_path)],
                    timeout=600,
                )
                downloaded = result.returncode == 0 and candidate_path.exists() and candidate_path.stat().st_size > 1_000_000
                if result.returncode != 0:
                    last_error = f"curl exited {result.returncode}"
            except Exception as exc:
                last_error = str(exc)

        if downloaded:
            archive_path = candidate_path
            archive_type = candidate_type
            break
        candidate_path.unlink(missing_ok=True)

    if archive_path is None:
        console.print(
            "  [red]Download failed.[/red] Use Setup > Install Runtime or rerun "
            "`nvh studio --install rootless-ollama -y`.\n"
            f"  [dim]Last error: {last_error or 'no candidate completed'}[/dim]"
        )
        return None

    console.print("  Extracting Ollama into NVH_HOME...")
    try:
        _extract_ollama_archive(archive_path, archive_type, nvh_home)
    except Exception as exc:
        archive_path.unlink(missing_ok=True)
        console.print(f"  [red]Extraction failed: {exc}[/red]")
        return None
    finally:
        archive_path.unlink(missing_ok=True)

    validation_error = _ollama_validation_error(ollama_bin) if ollama_bin.exists() else "binary missing after extraction"
    if validation_error:
        console.print(f"  [red]Ollama binary is not usable: {validation_error}[/red]")
        return None

    ollama_bin.chmod(0o755)
    console.print(f"  [green]Installed Ollama to {ollama_bin}[/green]")
    return str(ollama_bin)


def _install_ollama(console: Console) -> str | None:
    """Download and install Ollama to NVH_HOME. Returns binary path or None."""
    return _install_ollama_rootless_bundle(console)


def _start_ollama(console: Console, ollama_bin: str) -> bool:
    """Start 'ollama serve' in the background. Returns True if it comes up."""
    import subprocess
    import time

    from nvh.integrations.workspace.storage import storage_layout

    layout = storage_layout()
    models_dir = layout.ollama_models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    log_path = layout.logs_dir / "ollama.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(layout.env())
    env["OLLAMA_MODELS"] = str(models_dir)
    env["OLLAMA_HOST"] = "127.0.0.1:11434"  # bind to localhost only

    # Add CUDA libs from local install (tar.zst extracts lib/ollama/)
    lib_dir = layout.home / "lib" / "ollama"
    if lib_dir.is_dir():
        existing_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{lib_dir}:{existing_ld}" if existing_ld else str(lib_dir)

    console.print("  Starting Ollama...")
    try:
        log_file = open(log_path, "w")
        # Fully detach from the parent terminal so Ollama survives when the
        # setup CLI exits (and when the user's SSH session disconnects):
        #  - start_new_session=True  → new process group, shields from SIGHUP
        #  - stdin=DEVNULL           → no inherited TTY, no hang on close
        #  - close_fds=True          → don't leak inherited descriptors
        proc = subprocess.Popen(
            [ollama_bin, "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            env=env,
            start_new_session=True,
            close_fds=True,
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


def _model_exists_on_registry(model: str) -> bool | None:
    """Cheap manifest HEAD against the Ollama registry.

    Returns True/False if we got a clear answer, or None if the probe
    failed (network issue, DNS, etc). Callers should treat None as
    "can't confirm — try the pull anyway", not as failure.

    We use this to catch invented/typo'd model names before kicking off a
    progress bar against a pull that will eventually 404.
    """
    try:
        import httpx
        base, _, tag = model.partition(":")
        tag = tag or "latest"
        url = f"https://registry.ollama.ai/v2/library/{base}/manifests/{tag}"
        resp = httpx.head(url, timeout=3, follow_redirects=True)
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        return None  # some other status — don't block the pull
    except Exception:
        return None


def _pull_model(console: Console, model: str, ollama_bin: str) -> bool:
    """Pull an Ollama model with a Rich progress bar.

    Parses ``ollama pull`` stderr/stdout which emits lines like:
        pulling abc123... 45% |██      | 1.2 GB/2.7 GB
    Falls back to a plain spinner if parsing fails.
    """
    import subprocess

    # Short-circuit obviously-nonexistent tags so the user gets a clear
    # error up front instead of a progress bar that fails and cascades
    # into "Ollama is not running" on the next query. We only block on
    # a confirmed 404 — network failures fall through to the real pull.
    registry_state = _model_exists_on_registry(model)
    if registry_state is False:
        console.print(
            f"  [red]Model [bold]{model}[/bold] does not exist on the"
            " Ollama registry (404).[/red]\n"
            f"  [dim]Check the name at https://ollama.com/library, or run"
            " [bold]nvh status --deep[/bold] to see what's configured.[/dim]"
        )
        return False

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

        base = ollama_base_url()
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
        from nvh.integrations.workspace.storage import storage_layout
        env = os.environ.copy()
        env.update(storage_layout().env())
        result = subprocess.run(
            [ollama_bin, "pull", model],
            capture_output=False,
            env=env,
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


# Known vision-capable Ollama model tags. Used for pull ordering so the
# desktop-agent screenshot assist is available during API-key setup even
# if the large text models are still downloading.
_VISION_MODEL_TAGS = {
    "nemotron-3-nano-omni",
    "nemotron-omni",
    "moondream",
    "minicpm-v",
    "llama3.2-vision",
    "llama3.2-vision:11b",
    "llama3.2-vision:90b",
    "llava",
    "llava:7b",
    "llava:13b",
    "llava:34b",
    "bakllava",
}

_MODEL_PULL_PREFERENCE = [
    "nemotron-3-nano-omni",
    "nemotron-omni",
    "nemotron",
    "nemotron:70b",
    "llama3.3:70b",
    "qwen2.5-coder:32b",
    "llama3.2-vision",
    "qwen3:8b",
    "qwen2.5-coder:7b",
    "llama3.1:8b",
    "minicpm-v",
    "llava:7b",
    "gemma3:4b",
    "moondream",
    "nemotron-mini",
]


def _is_vision_model(tag: str) -> bool:
    """Return True if the tag refers to a vision-capable model.

    Matches exact known tags and any tag whose base (before ':') is known.
    """
    if tag in _VISION_MODEL_TAGS:
        return True
    base = tag.split(":", 1)[0]
    return base in _VISION_MODEL_TAGS or "-vision" in base or "llava" in base


def _reorder_vision_first(models: list[str]) -> list[str]:
    """Pull order: vision models first, then text models.

    Preserves relative order within each group.
    """
    vision = [m for m in models if _is_vision_model(m)]
    text = [m for m in models if not _is_vision_model(m)]
    return vision + text


def _prefer_largest_fitting_models(models: list[str]) -> list[str]:
    """Order model tags by nvHive's strongest-first local preference."""
    preference = {model: i for i, model in enumerate(_MODEL_PULL_PREFERENCE)}
    unique_models = list(dict.fromkeys(models))
    return sorted(
        unique_models,
        key=lambda model: preference.get(model, len(preference) + unique_models.index(model)),
    )


def _get_recommended_models(total_vram: float) -> list[str]:
    """Return recommended Ollama model tags for the detected VRAM."""
    try:
        from nvh.utils.gpu import detect_gpus, recommend_models
        gpus = detect_gpus()
        recs = recommend_models(gpus) if gpus else []
        models = [r.model for r in recs]
        if total_vram >= 4 and "gemma3:4b" not in models:
            models.append("gemma3:4b")
        return _prefer_largest_fitting_models(models)
    except Exception:
        pass

    # Fallback: manual recommendations by VRAM, all names verified against
    # Ollama's registry. Each tier fits text + vision
    # model concurrently:
    #   llama3.2-vision (~7GB) — best spatial grounding for desktop agent
    #   minicpm-v (~5GB) — good vision, smaller footprint
    #   moondream (~2GB) — basic vision for very tight VRAM
    if total_vram >= 128:
        return ["nemotron", "llama3.3:70b", "qwen2.5-coder:32b", "llama3.2-vision", "gemma3:4b"]
    if total_vram >= 96:
        return ["nemotron", "llama3.3:70b", "qwen2.5-coder:32b", "llama3.2-vision", "gemma3:4b"]
    if total_vram >= 48:
        return ["nemotron", "llama3.3:70b", "llama3.2-vision", "qwen3:8b", "gemma3:4b"]
    if total_vram >= 40:
        return ["nemotron", "llama3.2-vision", "qwen3:8b", "gemma3:4b"]
    if total_vram >= 24:
        return ["llama3.2-vision", "qwen3:8b", "qwen2.5-coder:7b", "gemma3:4b"]
    if total_vram >= 16:
        return ["minicpm-v", "qwen2.5-coder:7b", "qwen3:8b", "gemma3:4b"]
    if total_vram >= 12:
        return ["minicpm-v", "qwen2.5-coder:7b", "gemma3:4b"]
    if total_vram >= 8:
        return ["qwen3:8b", "llama3.1:8b", "gemma3:4b", "llava:7b"]
    if total_vram >= 4:
        return ["gemma3:4b", "moondream"]
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


def _write_config(
    configured_providers: dict[str, str],
    ollama_enabled: bool = False,
) -> Path:
    """Write a minimal config.yaml enabling the configured providers.

    Args:
        configured_providers: Map of provider name → API key for providers the
            user configured during setup.
        ollama_enabled: True only if Ollama is actually installed and running.
            When False, the ollama advisor is emitted with enabled=false so the
            REPL doesn't try to talk to a port where nothing is listening.

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
            "model": "groq/openai/gpt-oss-120b",
            "fallback": "groq/openai/gpt-oss-20b",
        },
        "openai": {
            "env": "OPENAI_API_KEY",
            "model": "gpt-5.6-terra",
            "fallback": "gpt-5.6-luna",
        },
        "anthropic": {
            "env": "ANTHROPIC_API_KEY",
            "model": "claude-sonnet-5",
            "fallback": "claude-haiku-4-5-20251001",
        },
        "google": {
            "env": "GOOGLE_API_KEY",
            "model": "gemini/gemini-3.7-flash",
            "fallback": "gemini/gemini-3.5-flash-lite",
        },
        "ollama": {
            "env": None,
            # Populated per-machine below based on detected VRAM tier so the
            # config points at a model that actually exists on the Ollama
            # registry AND fits the user's hardware.
            "model": None,
            "fallback": None,
            "base_url": "http://localhost:11434",
        },
    }

    # Pick the Ollama default/fallback for THIS machine — was hardcoded to
    # recommender ensures the config always references real models.
    try:
        from nvh.utils.gpu import detect_gpus, recommend_models
        recs = recommend_models(detect_gpus())
        text_recs = [r.model for r in recs if not r.tier.startswith("vision")]
        if text_recs:
            advisor_defs["ollama"]["model"] = f"ollama/{text_recs[0]}"
            if len(text_recs) > 1:
                advisor_defs["ollama"]["fallback"] = f"ollama/{text_recs[1]}"
    except Exception:
        pass

    # Sensible default if the recommender fails or returns nothing — use
    # the first-run bootstrap model that install.sh pulls by default.
    if not advisor_defs["ollama"]["model"]:
        advisor_defs["ollama"]["model"] = "ollama/gemma3:4b"
        advisor_defs["ollama"]["fallback"] = "ollama/gemma3:4b"

    for name, info in advisor_defs.items():
        if name == "ollama":
            enabled = ollama_enabled
        else:
            enabled = name in configured_providers
        lines.append(f"  {name}:")
        # Only emit the api_key interpolation for providers we've actually
        # configured — otherwise the YAML loader warns about unset env vars
        # on every nvh invocation.
        if info.get("env") and enabled:
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

    # Load any keys previously saved to HIVE_CONFIG_HOME/.env (headless fallback)
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

            # Ask to pull missing models. Vision model goes first so the
            # desktop-agent assist in step 3 is ready even if the big text
            # model pull is still running or fails.
            missing = [
                m for m in recommended
                if not any(m in existing for existing in ollama_models)
            ]
            missing = _reorder_vision_first(missing)
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
    # Save config — re-check Ollama status right before writing, so we don't
    # enable a provider whose daemon has since exited (prevents the REPL
    # error "Ollama is not running at http://localhost:11434").
    # ------------------------------------------------------------------
    console.print()
    final_ollama_up, _ = _ollama_running()
    config_path = _write_config(configured_providers, ollama_enabled=final_ollama_up)
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

    # ------------------------------------------------------------------
    # PATH check — warn if the `nvh` binary isn't reachable from the shell.
    # Give env-appropriate advice: activate the conda/mamba/venv env if
    # detected, otherwise suggest editing the shell rc.
    # ------------------------------------------------------------------
    nvh_cmd = "nvh"
    path_hint = _check_nvh_on_path()
    if path_hint is not None:
        nvh_cmd = path_hint["full_path"]
        console.print()
        kind = path_hint["env_kind"]
        if kind in ("conda", "mamba", "venv") and path_hint["activate_cmd"]:
            label = {"conda": "conda", "mamba": "micromamba", "venv": "venv"}[kind]
            console.print(Panel(
                f"[yellow]The [bold]nvh[/bold] command is not on your PATH.[/yellow]\n\n"
                f"Installed in {label} env [bold]{path_hint['env_name']}[/bold] at:\n"
                f"  {path_hint['full_path']}\n\n"
                f"Activate the env to put [bold]nvh[/bold] on PATH:\n"
                f"  [dim]$[/dim] [bold]{path_hint['activate_cmd']}[/bold]\n\n"
                f"Or run by full path (e.g. [bold]{path_hint['full_path']}[/bold]).",
                border_style="yellow",
            ))
        else:
            console.print(Panel(
                f"[yellow]The [bold]nvh[/bold] command is not on your PATH.[/yellow]\n\n"
                f"Installed at: [bold]{path_hint['full_path']}[/bold]\n\n"
                f"To use [bold]nvh[/bold] directly, add its directory to PATH:\n"
                f"  [dim]$[/dim] echo 'export PATH=\"{path_hint['bin_dir']}:$PATH\"'"
                f" >> {path_hint['shell_rc']}\n"
                f"  [dim]$[/dim] source {path_hint['shell_rc']}\n\n"
                f"Or run commands by full path (e.g. [bold]{path_hint['full_path']}[/bold]).",
                border_style="yellow",
            ))

    console.print()
    console.print("  [bold]Next steps:[/bold]")
    console.print(f'    Try a query:             [bold]{nvh_cmd} "What is the meaning of life?"[/bold]')
    console.print(f"    Launch interactive chat:  [bold]{nvh_cmd}[/bold]")
    if has_vision_model:
        console.print(f"    Desktop agent:           [bold]{nvh_cmd} \"take a screenshot\"[/bold]")
    console.print(f"    Edit config:              [bold]{nvh_cmd} config edit[/bold]")
    console.print()


def _check_nvh_on_path() -> dict[str, str] | None:
    """Check whether the `nvh` binary is reachable via PATH.

    Returns None if everything is fine. Otherwise returns a dict with:
      - full_path: absolute path to the nvh entry-point script
      - bin_dir: directory containing the script
      - env_kind: one of 'conda', 'mamba', 'venv', 'system'
      - env_name: active conda/mamba env name, or '' for venv/system
      - shell_rc: best-guess shell init file to append an export to
      - activate_cmd: idiomatic activation command for the detected env type
    """
    import shutil
    import sys

    if shutil.which("nvh") is not None:
        return None

    # Derive the nvh script location from sys.executable's bin dir
    py_bin_dir = Path(sys.executable).parent
    nvh_path = py_bin_dir / ("nvh.exe" if sys.platform == "win32" else "nvh")
    if not nvh_path.exists():
        # Fall back to sys.prefix/bin (venv/virtualenv layout)
        alt = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin") / (
            "nvh.exe" if sys.platform == "win32" else "nvh"
        )
        if alt.exists():
            nvh_path = alt
        else:
            return None  # can't locate it, nothing useful to say

    # Detect what kind of Python environment we're in, so we can give the
    # right "how to put nvh on PATH" advice. Conda/mamba users should
    # activate their env — editing .bashrc would break when they switch envs.
    env_kind = "system"
    env_name = ""
    activate_cmd = ""

    conda_env = os.environ.get("CONDA_DEFAULT_ENV", "")
    conda_prefix = os.environ.get("CONDA_PREFIX", "")
    mamba_root = os.environ.get("MAMBA_ROOT_PREFIX", "")
    virtual_env = os.environ.get("VIRTUAL_ENV", "")

    if mamba_root or (conda_prefix and "micromamba" in conda_prefix.lower()):
        env_kind = "mamba"
        env_name = conda_env or Path(conda_prefix).name if conda_prefix else ""
        activate_cmd = f"micromamba activate {env_name}" if env_name else "micromamba activate <env>"
    elif conda_prefix:
        env_kind = "conda"
        env_name = conda_env or Path(conda_prefix).name
        activate_cmd = f"conda activate {env_name}" if env_name else "conda activate <env>"
    elif virtual_env:
        env_kind = "venv"
        env_name = Path(virtual_env).name
        if sys.platform == "win32":
            activate_cmd = f"{virtual_env}\\Scripts\\activate"
        else:
            activate_cmd = f"source {virtual_env}/bin/activate"

    # Pick the shell rc to suggest (zsh takes precedence if present)
    home = Path.home()
    if sys.platform == "win32":
        shell_rc = "your PowerShell profile"
    elif (home / ".zshrc").exists():
        shell_rc = "~/.zshrc"
    else:
        shell_rc = "~/.bashrc"

    return {
        "full_path": str(nvh_path),
        "bin_dir": str(nvh_path.parent),
        "env_kind": env_kind,
        "env_name": env_name,
        "shell_rc": shell_rc,
        "activate_cmd": activate_cmd,
    }


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
