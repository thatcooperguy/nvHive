"""Sandboxed code execution for LLM tool use.

Two execution modes:

**Docker mode** (preferred, full isolation):
- Time-limited (configurable, default 30s)
- Memory-limited (configurable, default 512MB)
- Network-isolated (no outbound access)
- Filesystem-isolated (only a temp directory is shared)
- Non-root (runs as unprivileged user inside container)

**Subprocess fallback** (when Docker is unavailable):
- Time-limited only (via asyncio timeout)
- NO memory limit, NO network isolation, NO user isolation
- Code runs with the same permissions as the nvHive process
- Use with caution — only run trusted code in this mode

Docker mode is strongly recommended for production deployments.
The subprocess fallback is intended for development and trusted
environments where Docker is not available.

ExecutionResult.isolation records which mode actually ran ("docker" or
"subprocess"). Set NVH_SANDBOX_REQUIRE_DOCKER=1 (or
SandboxConfig.require_docker) to fail closed instead of falling back.

``run_shell`` is the agent ``shell`` tool's entry point: the same Docker
flags as ``execute`` plus a read-write mount of ``SandboxConfig.mount_dir``
(the agent workspace) at /workspace, so build/test commands can see the
project. The subprocess fallback runs the command with ``mount_dir`` as cwd.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes")


def _require_docker_default() -> bool:
    # NVH_SANDBOX was the pre-0.42 docker_sandbox opt-in; honoured as a
    # spelling of "require isolation" for one release.
    return any(
        os.environ.get(var, "").strip().lower() in _TRUTHY
        for var in ("NVH_SANDBOX_REQUIRE_DOCKER", "NVH_SANDBOX")
    )


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    execution_time_ms: int
    files_created: list[str] = field(default_factory=list)
    timed_out: bool = False
    error: str = ""
    isolation: str = ""  # "docker", "subprocess", or "" if nothing executed

@dataclass
class SandboxConfig:
    timeout_seconds: int = 30
    memory_limit_mb: int = 512
    network_enabled: bool = False
    max_output_bytes: int = 1_000_000  # 1MB output limit
    allowed_languages: list[str] = field(default_factory=lambda: ["python", "javascript", "bash"])
    # Host directory ``run_shell`` mounts read-write at /workspace (Docker)
    # or uses as cwd (subprocess). None = an empty temp dir, like ``execute``.
    mount_dir: str | Path | None = None
    shell_image: str = "python:3.12-slim"
    # Fail closed instead of falling back to an unisolated subprocess when
    # Docker is unavailable. Off by default: the primary deployment target
    # is rootless Linux boxes without Docker. "yes" is accepted so a flag
    # meant to fail closed never silently fails open.
    require_docker: bool = field(default_factory=_require_docker_default)

class SandboxExecutor:
    """Execute code in a sandboxed environment."""

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()
        self._docker_available: bool | None = None

    async def _check_docker(self) -> bool:
        """Check if Docker is available (rootless or regular)."""
        if self._docker_available is not None:
            return self._docker_available
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            self._docker_available = proc.returncode == 0
        except FileNotFoundError:
            self._docker_available = False
        return self._docker_available

    async def execute(
        self,
        code: str,
        language: str = "python",
        files: dict[str, str] | None = None,
        agent_id: str = "sandbox",
    ) -> ExecutionResult:
        """Execute code in a sandbox.

        Args:
            code: The code to execute
            language: python, javascript, or bash
            files: Optional dict of filename -> content to make available
        """
        if language not in self.config.allowed_languages:
            return ExecutionResult(
                stdout="", stderr=f"Language '{language}' not allowed",
                exit_code=1, execution_time_ms=0, error=f"Language '{language}' not allowed"
            )

        docker, refusal = await self._select_mode()
        if refusal is not None:
            return refusal

        runner = self._execute_docker if docker else self._execute_subprocess
        result = await runner(code, language, files)
        result.isolation = "docker" if docker else "subprocess"
        return result

    async def run_shell(self, command: str) -> ExecutionResult:
        """Run a shell command with the workspace (``config.mount_dir``) visible.

        Docker mode keeps every isolation flag from ``execute`` — non-root,
        memory/pids caps, read-only root fs, no network — and additionally
        mounts ``mount_dir`` read-write at /workspace. Subprocess fallback
        runs the command with ``mount_dir`` as cwd and no isolation; it is
        refused when ``require_docker`` is set, exactly like ``execute``.
        """
        docker, refusal = await self._select_mode()
        if refusal is not None:
            return refusal

        mount = Path(self.config.mount_dir).resolve() if self.config.mount_dir else None
        if docker:
            result = await self._run_shell_docker(command, mount)
        else:
            result = await self._run_shell_subprocess(command, mount)
        result.isolation = "docker" if docker else "subprocess"
        return result

    async def _select_mode(self) -> tuple[bool, ExecutionResult | None]:
        """Return ``(docker_available, refusal)``; refusal is set when the
        fail-closed flag forbids the subprocess fallback."""
        docker = await self._check_docker()
        if not docker and self.config.require_docker:
            msg = (
                "Docker is unavailable and NVH_SANDBOX_REQUIRE_DOCKER is set — "
                "refusing subprocess fallback, nothing was executed"
            )
            return False, ExecutionResult(
                stdout="", stderr=msg,
                exit_code=-1, execution_time_ms=0, error=msg,
            )
        if not docker:
            logger.warning(
                "Docker unavailable — using subprocess fallback "
                "(no network/memory/user isolation)"
            )
        return docker, None

    def _docker_run_flags(self) -> list[str]:
        cmd = [
            "docker", "run", "--rm",
            "--user", "1000:1000",
            "--memory", f"{self.config.memory_limit_mb}m",
            "--cpus", "1",
            "--pids-limit", "64",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m",
        ]
        if not self.config.network_enabled:
            cmd.extend(["--network", "none"])
        return cmd

    async def _run_process(
        self, argv: list[str] | None, *, shell_command: str | None = None,
        cwd: str | None = None,
    ) -> ExecutionResult:
        """Run ``argv`` (exec) or ``shell_command`` (via the system shell)
        under the configured timeout and output cap."""
        start = time.monotonic()
        try:
            if shell_command is not None:
                proc = await asyncio.create_subprocess_shell(
                    shell_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *(argv or []),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.config.timeout_seconds,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                elapsed = int((time.monotonic() - start) * 1000)
                return ExecutionResult(
                    stdout="", stderr="Execution timed out",
                    exit_code=-1, execution_time_ms=elapsed,
                    timed_out=True, error=f"Timed out after {self.config.timeout_seconds}s",
                )
            elapsed = int((time.monotonic() - start) * 1000)
            return ExecutionResult(
                stdout=stdout.decode(errors="replace")[:self.config.max_output_bytes],
                stderr=stderr.decode(errors="replace")[:self.config.max_output_bytes],
                exit_code=proc.returncode or 0,
                execution_time_ms=elapsed,
            )
        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            return ExecutionResult(
                stdout="", stderr=str(e),
                exit_code=-1, execution_time_ms=elapsed, error=str(e),
            )

    async def _run_shell_docker(self, command: str, mount: Path | None) -> ExecutionResult:
        cmd = self._docker_run_flags()
        if mount is not None:
            cmd.extend(["-v", f"{mount}:/workspace:rw", "-w", "/workspace"])
            cmd.extend([self.config.shell_image, "bash", "-c", command])
            return await self._run_process(cmd)
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd.extend(["-v", f"{tmpdir}:/workspace:ro", "-w", "/workspace"])
            cmd.extend([self.config.shell_image, "bash", "-c", command])
            return await self._run_process(cmd)

    async def _run_shell_subprocess(self, command: str, mount: Path | None) -> ExecutionResult:
        if mount is not None:
            return await self._run_process(None, shell_command=command, cwd=str(mount))
        with tempfile.TemporaryDirectory() as tmpdir:
            return await self._run_process(None, shell_command=command, cwd=tmpdir)

    async def _execute_docker(
        self, code: str, language: str, files: dict[str, str] | None
    ) -> ExecutionResult:
        """Execute in a Docker container (preferred, most isolated)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write code to temp file
            ext = {"python": ".py", "javascript": ".js", "bash": ".sh"}[language]
            code_file = Path(tmpdir) / f"main{ext}"
            code_file.write_text(code)

            # Write any additional files
            if files:
                for name, content in files.items():
                    # Prevent path traversal
                    safe_name = Path(name).name
                    (Path(tmpdir) / safe_name).write_text(content)

            images = {
                "python": "python:3.12-slim",
                "javascript": "node:22-slim",
                "bash": "ubuntu:24.04",
            }
            interpreters = {"python": "python", "javascript": "node", "bash": "bash"}
            cmd = self._docker_run_flags()
            cmd.extend(["-v", f"{tmpdir}:/workspace:ro", "-w", "/workspace"])
            cmd.extend([images[language], interpreters[language], f"/workspace/main{ext}"])
            return await self._run_process(cmd)

    async def _execute_subprocess(
        self, code: str, language: str, files: dict[str, str] | None
    ) -> ExecutionResult:
        """Fallback: execute as a subprocess with resource limits (less isolated)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ext = {"python": ".py", "javascript": ".js", "bash": ".sh"}[language]
            code_file = Path(tmpdir) / f"main{ext}"
            code_file.write_text(code)

            if files:
                for name, content in files.items():
                    safe_name = Path(name).name
                    (Path(tmpdir) / safe_name).write_text(content)

            interpreters = {"python": "python3", "javascript": "node", "bash": "bash"}
            return await self._run_process(
                [interpreters[language], str(code_file)], cwd=tmpdir,
            )
