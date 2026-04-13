"""Tests for nvh.core.docker_sandbox — Docker sandbox for agent tool execution."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nvh.core.docker_sandbox import (
    DEFAULT_IMAGE,
    DEFAULT_TIMEOUT,
    _run_in_docker,
    _run_locally,
    is_docker_available,
    run_in_sandbox,
    sandbox_enabled,
)


async def _wait_for_timeout(coro, **kwargs):
    """Replacement for asyncio.wait_for that closes the coroutine, then raises."""
    coro.close()
    raise TimeoutError


def _make_proc_mock(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
    communicate_side_effect=None,
) -> MagicMock:
    """Build a MagicMock that mimics asyncio.subprocess.Process.

    Uses ``spec=asyncio.subprocess.Process`` so that no spurious child
    mocks are auto-created on attribute access, which would otherwise
    produce "coroutine … was never awaited" RuntimeWarnings.

    * ``kill()`` is synchronous (MagicMock) — matches the real API.
    * ``communicate()`` and ``wait()`` are AsyncMock — they are awaited.
    """
    proc = MagicMock(spec=asyncio.subprocess.Process)
    if communicate_side_effect is not None:
        proc.communicate = AsyncMock(side_effect=communicate_side_effect)
    else:
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc

# ---------------------------------------------------------------------------
# is_docker_available
# ---------------------------------------------------------------------------


class TestIsDockerAvailable:
    @patch("nvh.core.docker_sandbox.shutil.which", return_value=None)
    def test_no_docker_binary(self, mock_which):
        assert is_docker_available() is False

    @patch("nvh.core.docker_sandbox.subprocess.run")
    @patch("nvh.core.docker_sandbox.shutil.which", return_value="/usr/bin/docker")
    def test_docker_daemon_running(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert is_docker_available() is True

    @patch("nvh.core.docker_sandbox.subprocess.run")
    @patch("nvh.core.docker_sandbox.shutil.which", return_value="/usr/bin/docker")
    def test_docker_daemon_not_running(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert is_docker_available() is False

    @patch("nvh.core.docker_sandbox.subprocess.run", side_effect=Exception("boom"))
    @patch("nvh.core.docker_sandbox.shutil.which", return_value="/usr/bin/docker")
    def test_docker_info_exception(self, mock_which, mock_run):
        assert is_docker_available() is False


# ---------------------------------------------------------------------------
# sandbox_enabled
# ---------------------------------------------------------------------------


class TestSandboxEnabled:
    def test_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("NVH_SANDBOX", None)
            assert sandbox_enabled() is False

    def test_set_to_1(self):
        with patch.dict(os.environ, {"NVH_SANDBOX": "1"}):
            assert sandbox_enabled() is True

    def test_set_to_true(self):
        with patch.dict(os.environ, {"NVH_SANDBOX": "true"}):
            assert sandbox_enabled() is True

    def test_set_to_yes(self):
        with patch.dict(os.environ, {"NVH_SANDBOX": "yes"}):
            assert sandbox_enabled() is True

    def test_set_to_0(self):
        with patch.dict(os.environ, {"NVH_SANDBOX": "0"}):
            assert sandbox_enabled() is False


# ---------------------------------------------------------------------------
# _run_locally (fallback)
# ---------------------------------------------------------------------------


class TestRunLocally:
    @pytest.mark.asyncio
    async def test_successful_command(self):
        mock_proc = _make_proc_mock(stdout=b"hello world")
        mock_create = AsyncMock(return_value=mock_proc)

        with patch("asyncio.create_subprocess_shell", new=mock_create):
            result = await _run_locally("echo hello", "/tmp", timeout=10)
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_command_with_stderr(self):
        mock_proc = _make_proc_mock(stdout=b"out", stderr=b"warn")
        mock_create = AsyncMock(return_value=mock_proc)

        with patch("asyncio.create_subprocess_shell", new=mock_create):
            result = await _run_locally("cmd", "/tmp", timeout=10)
        assert "out" in result
        assert "warn" in result

    @pytest.mark.asyncio
    async def test_timeout(self):
        mock_proc = _make_proc_mock(communicate_side_effect=asyncio.TimeoutError)
        mock_create = AsyncMock(return_value=mock_proc)

        with patch("asyncio.create_subprocess_shell", new=mock_create):
            with patch("asyncio.wait_for", new=_wait_for_timeout):
                result = await _run_locally("sleep 999", "/tmp", timeout=1)
        assert "[TIMEOUT]" in result

    @pytest.mark.asyncio
    async def test_exception(self):
        async def _raise(*args, **kwargs):
            raise OSError("no shell")

        with patch("asyncio.create_subprocess_shell", new=_raise):
            result = await _run_locally("echo x", "/tmp", timeout=10)
        assert "[ERROR]" in result


# ---------------------------------------------------------------------------
# _run_in_docker
# ---------------------------------------------------------------------------


class TestRunInDocker:
    @pytest.mark.asyncio
    async def test_successful_docker_run(self):
        mock_proc = _make_proc_mock(stdout=b"container output")
        captured_args: list[tuple] = []

        async def _fake_exec(*args, **kwargs):
            captured_args.append(args)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", new=_fake_exec):
            result = await _run_in_docker(
                "python -c 'print(1)'",
                "/tmp/project",
                timeout=60,
                image=DEFAULT_IMAGE,
                network=False,
                memory_limit="512m",
                read_only_mount=False,
            )

        assert result == "container output"

        # Verify docker command was constructed correctly
        call_args = captured_args[0]
        assert call_args[0] == "docker"
        assert "run" in call_args
        assert "--rm" in call_args
        assert "--network" in call_args
        assert "none" in call_args
        assert DEFAULT_IMAGE in call_args

    @pytest.mark.asyncio
    async def test_docker_timeout(self):
        mock_proc = _make_proc_mock()
        mock_exec = AsyncMock(return_value=mock_proc)

        with patch("asyncio.create_subprocess_exec", new=mock_exec):
            with patch("asyncio.wait_for", new=_wait_for_timeout):
                result = await _run_in_docker(
                    "sleep 999", "/tmp", 5, DEFAULT_IMAGE, False, "512m", False,
                )

        assert "[TIMEOUT]" in result
        assert "5s" in result

    @pytest.mark.asyncio
    async def test_docker_with_stderr(self):
        mock_proc = _make_proc_mock(stdout=b"ok", stderr=b"warning: something")
        mock_exec = AsyncMock(return_value=mock_proc)

        with patch("asyncio.create_subprocess_exec", new=mock_exec):
            result = await _run_in_docker(
                "cmd", "/tmp", 60, DEFAULT_IMAGE, False, "512m", False,
            )

        assert "ok" in result
        assert "warning: something" in result

    @pytest.mark.asyncio
    async def test_docker_nonzero_exit_no_output(self):
        mock_proc = _make_proc_mock(returncode=1)
        mock_exec = AsyncMock(return_value=mock_proc)

        with patch("asyncio.create_subprocess_exec", new=mock_exec):
            result = await _run_in_docker(
                "false", "/tmp", 60, DEFAULT_IMAGE, False, "512m", False,
            )

        assert "[EXIT 1]" in result

    @pytest.mark.asyncio
    async def test_docker_network_enabled(self):
        mock_proc = _make_proc_mock(stdout=b"ok")
        captured_args: list[tuple] = []

        async def _fake_exec(*args, **kwargs):
            captured_args.append(args)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", new=_fake_exec):
            await _run_in_docker(
                "curl example.com", "/tmp", 60, DEFAULT_IMAGE,
                network=True, memory_limit="512m", read_only_mount=False,
            )

        call_args = captured_args[0]
        # --network none should NOT be present when network=True
        assert "--network" not in call_args

    @pytest.mark.asyncio
    async def test_docker_read_only_mount(self):
        mock_proc = _make_proc_mock(stdout=b"ok")
        captured_args: list[tuple] = []

        async def _fake_exec(*args, **kwargs):
            captured_args.append(args)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", new=_fake_exec):
            await _run_in_docker(
                "ls", "/tmp/project", 60, DEFAULT_IMAGE,
                network=False, memory_limit="512m", read_only_mount=True,
            )

        call_args = captured_args[0]
        # Find the -v argument and verify :ro suffix
        v_idx = list(call_args).index("-v")
        mount_arg = call_args[v_idx + 1]
        assert mount_arg.endswith(":ro")

    @pytest.mark.asyncio
    async def test_docker_binary_gone_falls_back(self):
        """If docker binary vanishes mid-run, fall back to local."""

        async def _raise(*args, **kwargs):
            raise FileNotFoundError("docker not found")

        mock_local = AsyncMock(return_value="local fallback")
        with patch("asyncio.create_subprocess_exec", new=_raise):
            with patch(
                "nvh.core.docker_sandbox._run_locally",
                new=mock_local,
            ):
                result = await _run_in_docker(
                    "echo hi", "/tmp", 60, DEFAULT_IMAGE, False, "512m", False,
                )

        assert result == "local fallback"
        mock_local.assert_called_once()


# ---------------------------------------------------------------------------
# run_in_sandbox (main entry point)
# ---------------------------------------------------------------------------


class TestRunInSandbox:
    @pytest.mark.asyncio
    async def test_uses_docker_when_available(self):
        mock_docker = AsyncMock(return_value="docker output")
        with patch("nvh.core.docker_sandbox.is_docker_available", return_value=True):
            with patch(
                "nvh.core.docker_sandbox._run_in_docker",
                new=mock_docker,
            ):
                result = await run_in_sandbox("echo hi", "/tmp/project")

        assert result == "docker output"
        mock_docker.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_local(self):
        mock_local = AsyncMock(return_value="local output")
        with patch("nvh.core.docker_sandbox.is_docker_available", return_value=False):
            with patch(
                "nvh.core.docker_sandbox._run_locally",
                new=mock_local,
            ):
                result = await run_in_sandbox("echo hi", "/tmp/project")

        assert result == "local output"
        mock_local.assert_called_once()

    @pytest.mark.asyncio
    async def test_passes_timeout(self):
        mock_docker = AsyncMock(return_value="ok")
        with patch("nvh.core.docker_sandbox.is_docker_available", return_value=True):
            with patch(
                "nvh.core.docker_sandbox._run_in_docker",
                new=mock_docker,
            ):
                await run_in_sandbox("cmd", "/tmp", timeout=120)

        _, kwargs = mock_docker.call_args
        # timeout is a positional-or-keyword arg
        assert mock_docker.call_args[0][2] == 120 or kwargs.get("timeout") == 120

    @pytest.mark.asyncio
    async def test_passes_custom_image(self):
        mock_docker = AsyncMock(return_value="ok")
        with patch("nvh.core.docker_sandbox.is_docker_available", return_value=True):
            with patch(
                "nvh.core.docker_sandbox._run_in_docker",
                new=mock_docker,
            ):
                await run_in_sandbox("cmd", "/tmp", image="node:20-slim")

        call_kwargs = mock_docker.call_args[1]
        call_pos = mock_docker.call_args[0]
        # image should appear in the call
        assert "node:20-slim" in call_pos or call_kwargs.get("image") == "node:20-slim"

    @pytest.mark.asyncio
    async def test_default_timeout_is_60(self):
        assert DEFAULT_TIMEOUT == 60
