"""Tests for shell completion generation, installation, and CLI dispatch.

nvh/cli/completions.py must target the real `nvh` console script and
Click's env var convention (_NVH_COMPLETE) — earlier versions still
shelled out to the pre-rename `hive` binary and `council.cli.main`,
which no longer exist. The subprocess path is monkeypatched so these
tests never depend on `nvh` being on PATH.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nvh.cli import completions


class TestGetCompletionScript:
    def test_unsupported_shell_raises(self):
        with pytest.raises(ValueError, match="Unsupported shell"):
            completions.get_completion_script("powershell")

    def test_invokes_nvh_with_click_env_var(self, monkeypatch):
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs["env"]

            class Result:
                stdout = "click-generated script"

            return Result()

        monkeypatch.setattr(subprocess, "run", fake_run)
        script = completions.get_completion_script("bash")
        assert script == "click-generated script"
        assert captured["cmd"] == ["nvh"]
        assert captured["env"]["_NVH_COMPLETE"] == "bash_source"

    @pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
    def test_static_fallback_when_nothing_else_works(self, shell, monkeypatch):
        def fake_run(*args, **kwargs):
            raise FileNotFoundError("nvh not on PATH")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(completions, "_generate_via_click", lambda _shell: "")
        script = completions.get_completion_script(shell)
        assert f"_NVH_COMPLETE={shell}_source" in script
        assert "nvh" in script
        assert "hive " not in script
        assert "_HIVE_COMPLETE" not in script
        assert "council.cli.main" not in script

    @pytest.mark.parametrize("exc", [FileNotFoundError, PermissionError])
    def test_in_process_generation_when_nvh_subprocess_fails(self, exc, monkeypatch):
        # A broken shim raises PermissionError, not just FileNotFoundError —
        # both must fall through to the in-process Click generator, which
        # produces a real completion script without the console script.
        def fake_run(*args, **kwargs):
            raise exc("nvh unusable")

        monkeypatch.setattr(subprocess, "run", fake_run)
        script = completions.get_completion_script("bash")
        assert script
        assert "_NVH_COMPLETE" in script
        assert "_HIVE_COMPLETE" not in script
        assert "council.cli.main" not in script


class TestInstallCompletion:
    @pytest.fixture(autouse=True)
    def fake_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        return tmp_path

    @pytest.mark.parametrize(
        ("shell", "rc_file"), [("bash", ".bashrc"), ("zsh", ".zshrc")]
    )
    def test_rc_append_uses_nvh_marker(self, shell, rc_file, fake_home):
        success, message = completions.install_completion(shell, "SCRIPT")
        assert success
        content = (fake_home / rc_file).read_text()
        assert "# nvh completion" in content
        assert "SCRIPT" in content

    def test_rc_append_is_idempotent(self, fake_home):
        completions.install_completion("bash", "SCRIPT")
        success, message = completions.install_completion("bash", "SCRIPT")
        assert success
        assert "skipped" in message
        assert (fake_home / ".bashrc").read_text().count("SCRIPT") == 1

    def test_fish_writes_nvh_fish(self, fake_home):
        success, message = completions.install_completion("fish", "SCRIPT")
        assert success
        target = fake_home / ".config" / "fish" / "completions" / "nvh.fish"
        assert message == str(target)
        assert target.read_text() == "SCRIPT"

    def test_unsupported_shell(self):
        success, message = completions.install_completion("powershell", "SCRIPT")
        assert not success
        assert "Unsupported shell" in message


class TestMainCompletionDispatch:
    """`nvh` invoked with _NVH_COMPLETE set and no argv must hand off to
    the Typer app (where Click serves the completion) instead of falling
    through to the REPL or guided setup."""

    def test_nvh_complete_routes_to_app(self, monkeypatch):
        import nvh.cli.main as cli_main

        calls: list[str] = []
        monkeypatch.setattr(cli_main, "app", lambda: calls.append("app"))
        monkeypatch.setattr(
            cli_main, "_launch_default_repl",
            lambda: pytest.fail("REPL launched during completion request"),
        )
        monkeypatch.setattr(
            cli_main, "_run",
            lambda coro: pytest.fail("REPL launched during completion request"),
        )
        monkeypatch.setattr(cli_main.sys, "argv", ["nvh"])
        monkeypatch.setenv("_NVH_COMPLETE", "bash_source")

        cli_main.main()
        assert calls == ["app"]
