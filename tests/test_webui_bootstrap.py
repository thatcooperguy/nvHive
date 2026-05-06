from __future__ import annotations

from types import SimpleNamespace

import nvh.cli.main as cli_main


class _ConsoleSpy:
    def __init__(self) -> None:
        self.input_called = False
        self.messages: list[str] = []

    def input(self, _prompt: str) -> str:
        self.input_called = True
        return "n"

    def print(self, *args: object, **_kwargs: object) -> None:
        self.messages.append(" ".join(str(arg) for arg in args))


def test_rootless_node_install_assume_yes_skips_prompt(monkeypatch, tmp_path):
    import shutil
    import subprocess

    from nvh.integrations import node_runtime
    import nvh.integrations.storage as storage_mod

    layout = SimpleNamespace(
        runtime_dir=tmp_path / "runtimes",
        env=lambda: {},
    )
    console = _ConsoleSpy()

    monkeypatch.setattr(cli_main.sys, "platform", "linux")
    monkeypatch.setattr(storage_mod, "storage_layout", lambda: layout)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr="offline"),
    )
    monkeypatch.setattr(
        node_runtime,
        "install_node_tarball",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    node, npm = cli_main._try_install_node_no_root(console, assume_yes=True)

    assert (node, npm) == (None, None)
    assert console.input_called is False


def test_rootless_node_discovery_supports_fnm_and_direct_layouts(tmp_path, monkeypatch):
    from nvh.integrations import node_runtime

    monkeypatch.setenv("FNM_DIR", str(tmp_path / "fnm"))
    fnm = tmp_path / "fnm" / "bin" / "fnm"
    fnm.parent.mkdir(parents=True)
    fnm.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    assert node_runtime.find_fnm_binary(tmp_path / "missing") == str(fnm)

    direct_bin = tmp_path / "runtimes" / "node" / "node-v22.99.0-linux-x64" / "bin"
    direct_bin.mkdir(parents=True)
    (direct_bin / "node").write_text("", encoding="utf-8")
    npm_name = "npm.cmd" if node_runtime.os.name == "nt" else "npm"
    (direct_bin / npm_name).write_text("", encoding="utf-8")

    assert node_runtime.find_rootless_node_bin(tmp_path / "runtimes") == direct_bin
