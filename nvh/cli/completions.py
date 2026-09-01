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

    try:
        result = subprocess.run(
            ["nvh"],
            env={**_get_clean_env(), "_NVH_COMPLETE": f"{shell}_source"},
            capture_output=True,
            text=True,
        )
        if result.stdout:
            return result.stdout
    except OSError:
        # FileNotFoundError (not on PATH), PermissionError (broken shim /
        # noexec mount) — the in-process path below still works
        pass

    script = _generate_via_click(shell)
    if script:
        return script

    # Final fallback: return a minimal working completion snippet
    return _fallback_completion_script(shell)


def _get_clean_env() -> dict[str, str]:
    """Return the current environment for subprocess calls."""
    import os
    return dict(os.environ)


def _generate_via_click(shell: str) -> str:
    """Generate the completion script in-process via Click.

    Covers installs where the `nvh` console script is not on PATH: a
    `python -m nvh.cli.main` subprocess can't serve completion because
    Click derives the env-var name from argv[0], so build the script
    directly with the real prog name instead.
    """
    try:
        import typer
        from click.shell_completion import get_completion_class

        from nvh.cli.main import app

        cls = get_completion_class(shell)
        if cls is None:
            return ""
        command = typer.main.get_command(app)
        return cls(command, {}, "nvh", "_NVH_COMPLETE").source()
    except Exception:
        return ""


def _fallback_completion_script(shell: str) -> str:
    """Return a minimal completion script when auto-generation fails."""
    if shell == "bash":
        return (
            '# nvh bash completion\n'
            'eval "$(_NVH_COMPLETE=bash_source nvh 2>/dev/null || true)"\n'
        )
    elif shell == "zsh":
        return (
            '# nvh zsh completion\n'
            'eval "$(_NVH_COMPLETE=zsh_source nvh 2>/dev/null || true)"\n'
        )
    elif shell == "fish":
        return (
            '# nvh fish completion\n'
            '_NVH_COMPLETE=fish_source nvh 2>/dev/null | source\n'
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
        marker = "# nvh completion"
        snippet = f"\n{marker}\n{script}\n"
        return _append_if_absent(target, snippet, marker)

    elif shell == "zsh":
        target = home / ".zshrc"
        marker = "# nvh completion"
        snippet = f"\n{marker}\n{script}\n"
        return _append_if_absent(target, snippet, marker)

    elif shell == "fish":
        fish_dir = home / ".config" / "fish" / "completions"
        fish_dir.mkdir(parents=True, exist_ok=True)
        target = fish_dir / "nvh.fish"
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
            return True, f"{path} already contains nvh completion (skipped)"
        with open(path, "a") as f:
            f.write(snippet)
        return True, str(path)
    except OSError as e:
        return False, str(e)
