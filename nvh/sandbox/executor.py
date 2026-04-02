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
"""

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    execution_time_ms: int
    files_created: list[str] = field(default_factory=list)
    timed_out: bool = False
    error: str = ""

@dataclass
class SandboxConfig:
    timeout_seconds: int = 30
    memory_limit_mb: int = 512
    network_enabled: bool = False
    max_output_bytes: int = 1_000_000  # 1MB output limit
    allowed_languages: list[str] = field(default_factory=lambda: ["python", "javascript", "bash"])

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

        if await self._check_docker():
            return await self._execute_docker(code, language, files)
        else:
            import logging
            logging.getLogger(__name__).warning(
                "Docker unavailable — using subprocess fallback "
                "(no network/memory/user isolation)"
            )
            return await self._execute_subprocess(code, language, files)

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

            # Docker image per language
            images = {
                "python": "python:3.12-slim",
                "javascript": "node:22-slim",
                "bash": "ubuntu:24.04",
            }

            # Build docker run command
            cmd = [
                "docker", "run", "--rm",
                "--user", "1000:1000",
                "--memory", f"{self.config.memory_limit_mb}m",
                "--cpus", "1",
                "--pids-limit", "64",
                "--read-only",
                "--tmpfs", "/tmp:rw,size=64m",
                "-v", f"{tmpdir}:/workspace:ro",
                "-w", "/workspace",
            ]

            if not self.config.network_enabled:
                cmd.extend(["--network", "none"])

            cmd.append(images[language])

            # Execution command per language
            if language == "python":
                cmd.extend(["python", f"/workspace/main{ext}"])
            elif language == "javascript":
                cmd.extend(["node", f"/workspace/main{ext}"])
            elif language == "bash":
                cmd.extend(["bash", f"/workspace/main{ext}"])

            import time
            start = time.monotonic()

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
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
                        timed_out=True, error=f"Timed out after {self.config.timeout_seconds}s"
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
                    exit_code=-1, execution_time_ms=elapsed,
                    error=str(e)
                )

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

            interpreters = {
                "python": ["python3", str(code_file)],
                "javascript": ["node", str(code_file)],
                "bash": ["bash", str(code_file)],
            }

            import time
            start = time.monotonic()

            try:
                proc = await asyncio.create_subprocess_exec(
                    *interpreters[language],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tmpdir,
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
                        timed_out=True,
                    )

                elapsed = int((time.monotonic() - start) * 1000)
                return ExecutionResult(
                    stdout=stdout.decode(errors="replace")[:self.config.max_output_bytes],
                    stderr=stderr.decode(errors="replace")[:self.config.max_output_bytes],
                    exit_code=proc.returncode or 0,
                    execution_time_ms=elapsed,
                )
            except Exception as e:
                return ExecutionResult(
                    stdout="", stderr=str(e),
                    exit_code=-1, execution_time_ms=0, error=str(e)
                )
