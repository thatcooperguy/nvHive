"""Tests for rootless runtime fallback helpers."""

from __future__ import annotations

from nvh.integrations import runtime


def test_runtime_status_uses_persistent_layout(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))

    status = runtime.runtime_status()

    assert status.python_executable
    assert status.strategy in {"python-venv", "micromamba-fallback", "needs-runtime"}
    assert status.micromamba_binary == str(tmp_path / "nvh" / "bin" / "micromamba")
    assert status.micromamba_root_prefix == str(tmp_path / "nvh" / "runtimes" / "micromamba")


def test_micromamba_subdir_known_architectures(monkeypatch) -> None:
    monkeypatch.setattr(runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runtime.platform, "machine", lambda: "x86_64")

    assert runtime.micromamba_subdir() == "linux-64"
