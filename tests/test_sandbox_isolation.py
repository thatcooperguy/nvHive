"""Tests for sandbox isolation labeling and the require-Docker fail-closed mode."""

from __future__ import annotations

import asyncio
import sys

import pytest

from nvh.sandbox.executor import ExecutionResult, SandboxConfig, SandboxExecutor

# -- fixtures -----------------------------------------------------------------


@pytest.fixture
def no_require_docker(monkeypatch):
    monkeypatch.delenv("NVH_SANDBOX_REQUIRE_DOCKER", raising=False)


@pytest.fixture
def docker_unavailable(monkeypatch):
    async def no_docker(self):
        return False

    monkeypatch.setattr(SandboxExecutor, "_check_docker", no_docker)


@pytest.fixture
def portable_python(monkeypatch):
    # The subprocess fallback invokes "python3", which is not runnable on
    # every dev box (e.g. the Windows Store stub); redirect it to the
    # current interpreter so the code still genuinely executes.
    real = asyncio.create_subprocess_exec

    async def patched(program, *args, **kwargs):
        if program == "python3":
            program = sys.executable
        return await real(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", patched)


# -- isolation labeling -------------------------------------------------------


async def test_subprocess_fallback_labels_isolation_and_runs(
    no_require_docker, docker_unavailable, portable_python, tmp_path
):
    marker = tmp_path / "ran.txt"
    executor = SandboxExecutor()
    result = await executor.execute(
        f"open('{marker.as_posix()}', 'w').write('ran'); print('done')"
    )
    assert result.isolation == "subprocess"
    assert result.exit_code == 0
    assert "done" in result.stdout
    assert marker.read_text() == "ran"


async def test_docker_path_labels_isolation(no_require_docker, monkeypatch):
    async def yes_docker(self):
        return True

    async def fake_docker(self, code, language, files):
        return ExecutionResult(
            stdout="ok", stderr="", exit_code=0, execution_time_ms=1
        )

    monkeypatch.setattr(SandboxExecutor, "_check_docker", yes_docker)
    monkeypatch.setattr(SandboxExecutor, "_execute_docker", fake_docker)
    result = await SandboxExecutor().execute("print('hi')")
    assert result.isolation == "docker"
    assert result.stdout == "ok"


async def test_language_not_allowed_leaves_isolation_empty(no_require_docker):
    result = await SandboxExecutor().execute("puts 'hi'", language="ruby")
    assert result.isolation == ""
    assert result.exit_code == 1


# -- require_docker fail-closed -----------------------------------------------


async def test_require_docker_env_refuses_without_executing(
    monkeypatch, docker_unavailable, tmp_path
):
    monkeypatch.setenv("NVH_SANDBOX_REQUIRE_DOCKER", "1")

    async def must_not_run(self, code, language, files):
        pytest.fail("subprocess fallback must not execute when require_docker is set")

    monkeypatch.setattr(SandboxExecutor, "_execute_subprocess", must_not_run)
    marker = tmp_path / "should_not_exist.txt"
    result = await SandboxExecutor().execute(
        f"open('{marker.as_posix()}', 'w').write('leaked')"
    )
    assert result.exit_code == -1
    assert "Docker" in result.error
    assert "NVH_SANDBOX_REQUIRE_DOCKER" in result.error
    assert result.isolation == ""
    assert not marker.exists()


async def test_require_docker_config_flag_refuses(no_require_docker, docker_unavailable):
    executor = SandboxExecutor(SandboxConfig(require_docker=True))
    result = await executor.execute("print('nope')")
    assert result.exit_code == -1
    assert "NVH_SANDBOX_REQUIRE_DOCKER" in result.error


def test_require_docker_env_parsing(monkeypatch):
    monkeypatch.delenv("NVH_SANDBOX_REQUIRE_DOCKER", raising=False)
    assert SandboxConfig().require_docker is False
    monkeypatch.setenv("NVH_SANDBOX_REQUIRE_DOCKER", "true")
    assert SandboxConfig().require_docker is True
    # "yes" works for NVH_SANDBOX (docker_sandbox.sandbox_enabled); a
    # fail-closed flag rejecting it would silently fail open
    monkeypatch.setenv("NVH_SANDBOX_REQUIRE_DOCKER", "yes")
    assert SandboxConfig().require_docker is True
    monkeypatch.setenv("NVH_SANDBOX_REQUIRE_DOCKER", "0")
    assert SandboxConfig().require_docker is False


# -- run_code tool notice -----------------------------------------------------


async def test_run_code_tool_appends_subprocess_notice(
    no_require_docker, docker_unavailable, monkeypatch
):
    async def fake_subprocess(self, code, language, files):
        return ExecutionResult(
            stdout="hello", stderr="", exit_code=0, execution_time_ms=1
        )

    monkeypatch.setattr(SandboxExecutor, "_execute_subprocess", fake_subprocess)
    from nvh.core.tools import ToolRegistry

    tool = ToolRegistry(include_system=False).get("run_code")
    output = await tool.handler(code="print('hello')")
    assert "hello" in output
    assert "[isolation: subprocess" in output


async def test_run_code_tool_no_notice_under_docker(no_require_docker, monkeypatch):
    async def yes_docker(self):
        return True

    async def fake_docker(self, code, language, files):
        return ExecutionResult(
            stdout="hello", stderr="", exit_code=0, execution_time_ms=1
        )

    monkeypatch.setattr(SandboxExecutor, "_check_docker", yes_docker)
    monkeypatch.setattr(SandboxExecutor, "_execute_docker", fake_docker)
    from nvh.core.tools import ToolRegistry

    tool = ToolRegistry(include_system=False).get("run_code")
    output = await tool.handler(code="print('hello')")
    assert "hello" in output
    assert "[isolation:" not in output


# -- workflow shell step ------------------------------------------------------


async def test_workflow_shell_step_fails_on_refusal(docker_unavailable, monkeypatch):
    # A refused execution (require_docker, no Docker) must fail the workflow
    # step, not flow downstream as command output.
    monkeypatch.setenv("NVH_SANDBOX_REQUIRE_DOCKER", "1")
    from nvh.core.workflows import Workflow, WorkflowStep, run_workflow

    wf = Workflow(
        name="wf",
        steps=[WorkflowStep(name="sh", action="shell", prompt="echo hi", save_as="out")],
    )
    result = await run_workflow(wf, engine=None)

    assert result.success is False
    assert "NVH_SANDBOX_REQUIRE_DOCKER" in (result.error or "")
    assert "out" not in result.variables
