"""Shell completion script generation and installation helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Shell completion scripts
# ---------------------------------------------------------------------------

def get_completion_script(shell: str) -> str:
    """Generate a shell completion script for the given shell via Typer/Click.

    Returns the script text, or raises ValueError for unsupported shells.
    """
    if shell not in ("bash", "zsh", "fish"):
        raise ValueError(f"Unsupported shell '{shell}'. Choose from: bash, zsh, fish")

    env_var_map = {
        "bash": "_HIVE_COMPLETE=bash_source",
        "zsh": "_HIVE_COMPLETE=zsh_source",
        "fish": "_HIVE_COMPLETE=fish_source",
    }

    env_var = env_var_map[shell]

    try:
        result = subprocess.run(
            ["hive"],
            env={**_get_clean_env(), env_var.split("=")[0]: env_var.split("=")[1]},
            capture_output=True,
            text=True,
        )
        if result.stdout:
            return result.stdout
    except FileNotFoundError:
        pass

    # Fallback: generate via python -m council approach
    try:
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "council.cli.main"],
            env={**_get_clean_env(), env_var.split("=")[0]: env_var.split("=")[1]},
            capture_output=True,
            text=True,
        )
        if result.stdout:
            return result.stdout
    except Exception:
        pass

    # Final fallback: return a minimal working completion snippet
    return _fallback_completion_script(shell)


def _get_clean_env() -> dict[str, str]:
    """Return the current environment for subprocess calls."""
    import os
    return dict(os.environ)


def _fallback_completion_script(shell: str) -> str:
    """Return a minimal completion script when auto-generation fails."""
    if shell == "bash":
        return (
            '# Hive bash completion\n'
            'eval "$(_HIVE_COMPLETE=bash_source hive 2>/dev/null || true)"\n'
        )
    elif shell == "zsh":
        return (
            '# Hive zsh completion\n'
            'eval "$(_HIVE_COMPLETE=zsh_source hive 2>/dev/null || true)"\n'
        )
    elif shell == "fish":
        return (
            '# Hive fish completion\n'
            '_HIVE_COMPLETE=fish_source hive 2>/dev/null | source\n'
        )
    return ""


# ---------------------------------------------------------------------------
# Installation helpers
# ---------------------------------------------------------------------------

def install_completion(shell: str, script: str) -> tuple[bool, str]:
    """Install the completion script into the appropriate shell config file.

    Returns (success, message).
    """
    home = Path.home()

    if shell == "bash":
        target = home / ".bashrc"
        marker = "# hive completion"
        snippet = f"\n{marker}\n{script}\n"
        return _append_if_absent(target, snippet, marker)

    elif shell == "zsh":
        target = home / ".zshrc"
        marker = "# hive completion"
        snippet = f"\n{marker}\n{script}\n"
        return _append_if_absent(target, snippet, marker)

    elif shell == "fish":
        fish_dir = home / ".config" / "fish" / "completions"
        fish_dir.mkdir(parents=True, exist_ok=True)
        target = fish_dir / "hive.fish"
        try:
            target.write_text(script)
            return True, str(target)
        except OSError as e:
            return False, str(e)

    return False, f"Unsupported shell: {shell}"


def _append_if_absent(path: Path, snippet: str, marker: str) -> tuple[bool, str]:
    """Append snippet to path if the marker is not already present."""
    try:
        existing = path.read_text() if path.exists() else ""
        if marker in existing:
            return True, f"{path} already contains hive completion (skipped)"
        with open(path, "a") as f:
            f.write(snippet)
        return True, str(path)
    except OSError as e:
        return False, str(e)
