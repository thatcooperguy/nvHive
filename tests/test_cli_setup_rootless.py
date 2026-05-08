"""Tests for rootless first-run setup helpers."""

from __future__ import annotations

import io
import platform

from rich.console import Console

from nvh.cli import setup as cli_setup
from nvh.integrations import studio_packs


def test_first_run_ollama_installer_uses_shared_rootless_candidates(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(studio_packs, "_ollama_download_candidates", lambda arch: calls.append(arch) or [])

    output = io.StringIO()
    result = cli_setup._install_ollama(Console(file=output, color_system=None))

    assert result is None
    assert calls == ["amd64"]
    assert "sudo" not in output.getvalue().lower()
    assert "install.sh | sh" not in output.getvalue()
