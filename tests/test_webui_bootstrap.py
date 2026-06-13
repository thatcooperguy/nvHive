from __future__ import annotations

import os
import subprocess
import time
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

    import nvh.integrations.workspace.storage as storage_mod
    from nvh.integrations import node_runtime

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


def test_webui_npm_env_includes_bootstrapped_node_path(tmp_path, monkeypatch):
    import shutil

    import nvh.integrations.workspace.storage as storage_mod

    home = tmp_path / "nvhive"
    node_bin = home / "runtimes" / "node" / "current" / "bin"
    node_bin.mkdir(parents=True)
    (node_bin / "node").write_text("", encoding="utf-8")
    npm_name = "npm.cmd" if os.name == "nt" else "npm"
    (node_bin / npm_name).write_text("", encoding="utf-8")
    web_dir = tmp_path / "pkg" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "package.json").write_text('{"scripts":{"start":"next start"}}', encoding="utf-8")
    (web_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    (web_dir / ".next").mkdir()
    (web_dir / ".next" / "BUILD_ID").write_text("test", encoding="utf-8")

    layout = SimpleNamespace(
        home=home,
        webui_dir=home / "webui",
        cache_dir=home / "cache",
        logs_dir=home / "logs",
        runtime_dir=home / "runtimes",
        env=lambda: {"NVH_HOME": str(home)},
    )
    captured_envs: list[dict[str, str]] = []

    def fake_which(name: str, path: str | None = None) -> str | None:
        if path and str(node_bin) in path.split(os.pathsep) and name in {"node", "npm", "npm.cmd"}:
            return str(node_bin / name)
        return None

    def fake_run(*_args, **kwargs):
        captured_envs.append(kwargs["env"])
        return subprocess.CompletedProcess(_args[0], 0, "", "")

    monkeypatch.setattr(storage_mod, "storage_layout", lambda: layout)
    monkeypatch.setattr(cli_main, "__file__", str(tmp_path / "pkg" / "nvh" / "cli" / "main.py"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(
        cli_main,
        "_try_install_node_no_root",
        lambda *_args, **_kwargs: (str(node_bin / "node"), str(node_bin / "npm")),
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    cli_main.webui(
        install_only=True,
        port=3000,
        uninstall=False,
        clean=False,
        yes=True,
        no_api=True,
        api_port=8000,
        dev=False,
        open_browser=False,
        verbose=False,
    )

    assert captured_envs
    assert str(node_bin) in captured_envs[0]["PATH"].split(os.pathsep)


def test_webui_launch_opens_setup_in_browser(tmp_path, monkeypatch):
    import shutil
    import socket

    import nvh.integrations.workspace.storage as storage_mod

    home = tmp_path / "nvhive"
    node_bin = home / "runtimes" / "node" / "current" / "bin"
    node_bin.mkdir(parents=True)
    (node_bin / "node").write_text("", encoding="utf-8")
    npm_name = "npm.cmd" if os.name == "nt" else "npm"
    (node_bin / npm_name).write_text("", encoding="utf-8")
    web_dir = tmp_path / "pkg" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "package.json").write_text('{"scripts":{"start":"next start"}}', encoding="utf-8")
    (web_dir / "node_modules").mkdir()
    (web_dir / ".next").mkdir()
    (web_dir / ".next" / "BUILD_ID").write_text("test", encoding="utf-8")

    layout = SimpleNamespace(
        home=home,
        webui_dir=home / "webui",
        cache_dir=home / "cache",
        logs_dir=home / "logs",
        runtime_dir=home / "runtimes",
        env=lambda: {"NVH_HOME": str(home)},
    )
    popen_calls: list[list[str]] = []

    def fake_which(name: str, path: str | None = None) -> str | None:
        if path and str(node_bin) in path.split(os.pathsep) and name in {"node", "npm", "npm.cmd"}:
            return str(node_bin / name)
        if name == "xdg-open":
            return "/usr/bin/xdg-open"
        return None

    class _FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def close(self):
            return None

        def __getattr__(self, name):
            # _api_reachable only used the connection as a context
            # manager, but post-#65 the same monkeypatched
            # `socket.create_connection` is reached by `_api_healthy`'s
            # urlopen → http.client, which calls setsockopt/settimeout/
            # sendall/recv. Make any unknown socket method raise OSError
            # so urlopen aborts cleanly and `_api_healthy` catches it as
            # `unreachable` — the kill+restart path that follows is
            # mocked downstream (subprocess.run is no-op).
            def _raise(*_a, **_kw):
                raise OSError("test fixture _FakeConnection has no real socket")
            return _raise

    def fake_create_connection(*_args, **_kwargs):
        return _FakeConnection()

    # 2026-06-10: webui() now port-availability-checks an EXPLICIT --port
    # via socket.socket().bind() (the cascade only triggers on the 0
    # sentinel). Make that bind a deterministic no-op so the launch-path
    # assertions below don't depend on whether :3000 happens to be free
    # on the machine running the suite (a leftover dev server would
    # otherwise make webui() raise typer.Exit(1) before opening the
    # browser). socket.socket is used nowhere else in this code path.
    class _FreePortSocket:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def bind(self, *_a, **_k):
            return None

        def setsockopt(self, *_a, **_k):
            return None

    class _FakePopen:
        def __init__(self, cmd, **_kwargs):
            popen_calls.append(list(cmd))

        def poll(self):
            return None

    monkeypatch.setattr(storage_mod, "storage_layout", lambda: layout)
    monkeypatch.setattr(cli_main, "__file__", str(tmp_path / "pkg" / "nvh" / "cli" / "main.py"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("NVH_FIREFOX_AUTO_INSTALL", "0")
    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(socket, "socket", lambda *_a, **_k: _FreePortSocket())
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args[0], 0, "", ""),
    )

    cli_main.webui(
        install_only=False,
        port=3000,
        uninstall=False,
        clean=False,
        yes=True,
        no_api=True,
        api_port=8000,
        dev=False,
        open_browser=True,
        verbose=False,
    )

    for _ in range(20):
        if popen_calls:
            break
        time.sleep(0.05)

    assert any(call[0] == "/usr/bin/xdg-open" and call[1].endswith("/setup") for call in popen_calls)


def test_webui_launch_prefers_rootless_firefox(tmp_path, monkeypatch):
    import shutil
    import socket

    import nvh.integrations.workspace.storage as storage_mod

    home = tmp_path / "nvhive"
    node_bin = home / "runtimes" / "node" / "current" / "bin"
    node_bin.mkdir(parents=True)
    (node_bin / "node").write_text("", encoding="utf-8")
    npm_name = "npm.cmd" if os.name == "nt" else "npm"
    (node_bin / npm_name).write_text("", encoding="utf-8")
    firefox_bin = home / "apps" / "firefox" / "firefox"
    firefox_bin.parent.mkdir(parents=True)
    firefox_bin.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    firefox_bin.chmod(0o755)
    web_dir = tmp_path / "pkg" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "package.json").write_text('{"scripts":{"start":"next start"}}', encoding="utf-8")
    (web_dir / "node_modules").mkdir()
    (web_dir / ".next").mkdir()
    (web_dir / ".next" / "BUILD_ID").write_text("test", encoding="utf-8")

    layout = SimpleNamespace(
        home=home,
        webui_dir=home / "webui",
        cache_dir=home / "cache",
        logs_dir=home / "logs",
        runtime_dir=home / "runtimes",
        apps_dir=home / "apps",
        tmp_dir=home / "cache" / "tmp",
        env=lambda: {"NVH_HOME": str(home)},
    )
    popen_calls: list[list[str]] = []

    def fake_which(name: str, path: str | None = None) -> str | None:
        if path and str(node_bin) in path.split(os.pathsep) and name in {"node", "npm", "npm.cmd"}:
            return str(node_bin / name)
        if name == "xdg-open":
            return "/usr/bin/xdg-open"
        return None

    class _FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def close(self):
            return None

        def __getattr__(self, name):
            # _api_reachable only used the connection as a context
            # manager, but post-#65 the same monkeypatched
            # `socket.create_connection` is reached by `_api_healthy`'s
            # urlopen → http.client, which calls setsockopt/settimeout/
            # sendall/recv. Make any unknown socket method raise OSError
            # so urlopen aborts cleanly and `_api_healthy` catches it as
            # `unreachable` — the kill+restart path that follows is
            # mocked downstream (subprocess.run is no-op).
            def _raise(*_a, **_kw):
                raise OSError("test fixture _FakeConnection has no real socket")
            return _raise

    def fake_create_connection(*_args, **_kwargs):
        return _FakeConnection()

    # See the matching note in test_webui_launch_opens_setup_in_browser:
    # make the explicit-port bind check a deterministic no-op so this
    # launch-path test doesn't depend on :3000 being free on the host.
    class _FreePortSocket:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def bind(self, *_a, **_k):
            return None

        def setsockopt(self, *_a, **_k):
            return None

    class _FakePopen:
        def __init__(self, cmd, **_kwargs):
            popen_calls.append(list(cmd))

        def poll(self):
            return None

    monkeypatch.setattr(storage_mod, "storage_layout", lambda: layout)
    monkeypatch.setattr(cli_main, "__file__", str(tmp_path / "pkg" / "nvh" / "cli" / "main.py"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(socket, "socket", lambda *_a, **_k: _FreePortSocket())
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args[0], 0, "", ""),
    )

    cli_main.webui(
        install_only=False,
        port=3000,
        uninstall=False,
        clean=False,
        yes=True,
        no_api=True,
        api_port=8000,
        dev=False,
        open_browser=True,
        verbose=False,
    )

    for _ in range(20):
        if popen_calls:
            break
        time.sleep(0.05)

    firefox_calls = [call for call in popen_calls if call[0] == str(firefox_bin)]
    assert firefox_calls
    assert firefox_calls[0][1:4] == ["--new-instance", "--no-remote", "--profile"]
    assert firefox_calls[0][4] == str(home / "state" / "browser-profiles" / "rootless-firefox")
    assert firefox_calls[0][-2] == "--new-window"
    assert firefox_calls[0][-1].endswith("/setup")
    assert not any(call[0] == "/usr/bin/xdg-open" for call in popen_calls)
