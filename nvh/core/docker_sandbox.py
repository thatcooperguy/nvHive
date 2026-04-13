"""Docker sandbox for agent tool execution.

Wraps shell/command tool calls in a Docker container for isolation.
Falls back gracefully to local execution if Docker is not available.

Usage:
    from nvh.core.docker_sandbox import run_in_sandbox, is_docker_available

    output = await run_in_sandbox("python -c 'print(1+1)'", working_dir="/tmp/project")

Enable via:
    nvh agent "task" --sandbox
    or set NVH_SANDBOX=1 in environment
    or set sandbox: true in nvh config
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Default Docker image — lightweight Python with common tools
DEFAULT_IMAGE = "python:3.11-slim"

# Default timeout in seconds
DEFAULT_TIMEOUT = 60


def is_docker_available() -> bool:
    """Check whether Docker is installed and the daemon is reachable."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


async def run_in_sandbox(
    command: str,
    working_dir: str,
    timeout: int = DEFAULT_TIMEOUT,
    image: str = DEFAULT_IMAGE,
    network: bool = False,
    memory_limit: str = "512m",
    read_only_mount: bool = False,
) -> str:
    """Run a command inside a Docker container.

    Args:
        command: Shell command to execute.
        working_dir: Host directory to mount into the container at /workspace.
        timeout: Maximum execution time in seconds (default 60).
        image: Docker image to use (default python:3.11-slim).
        network: Whether to allow network access (default False).
        memory_limit: Container memory limit (default 512m).
        read_only_mount: Mount working_dir as read-only (default False, i.e. read-write).

    Returns:
        Combined stdout + stderr output from the command.

    Falls back to local subprocess execution with a warning if Docker
    is not available.
    """
    if not is_docker_available():
        logger.warning(
            "Docker not available — running command locally without sandbox isolation. "
            "Install Docker for safer agent execution."
        )
        return await _run_locally(command, working_dir, timeout)

    return await _run_in_docker(
        command, working_dir, timeout, image, network, memory_limit, read_only_mount,
    )


async def _run_in_docker(
    command: str,
    working_dir: str,
    timeout: int,
    image: str,
    network: bool,
    memory_limit: str,
    read_only_mount: bool,
) -> str:
    """Execute a command inside a Docker container."""
    # Resolve to absolute path for volume mount
    host_dir = os.path.abspath(working_dir)

    mount_flag = f"{host_dir}:/workspace"
    if read_only_mount:
        mount_flag += ":ro"

    docker_cmd = [
        "docker", "run",
        "--rm",                          # auto-remove container
        "-v", mount_flag,                # mount working directory
        "-w", "/workspace",              # set working directory inside container
        "--memory", memory_limit,        # memory limit
        "--cpus", "2",                   # CPU limit
        "--pids-limit", "256",           # prevent fork bombs
        "--user", "1000:1000",           # run as non-root
    ]

    if not network:
        docker_cmd.extend(["--network", "none"])

    docker_cmd.extend([image, "bash", "-c", command])

    try:
        process = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return f"[TIMEOUT] Command exceeded {timeout}s limit and was killed."

        output = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        if err:
            output = output + "\n" + err if output else err

        if process.returncode != 0 and not output.strip():
            output = f"[EXIT {process.returncode}] Command failed with no output."

        return output.strip()

    except FileNotFoundError:
        logger.warning("Docker binary disappeared — falling back to local execution.")
        return await _run_locally(command, working_dir, timeout)
    except Exception as e:
        logger.error("Docker execution failed: %s", e)
        return f"[ERROR] Docker execution failed: {e}"


async def _run_locally(
    command: str,
    working_dir: str,
    timeout: int,
) -> str:
    """Fallback: run command locally via subprocess."""
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return f"[TIMEOUT] Command exceeded {timeout}s limit and was killed."

        output = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        if err:
            output = output + "\n" + err if output else err

        return output.strip()

    except Exception as e:
        return f"[ERROR] Local execution failed: {e}"


def sandbox_enabled() -> bool:
    """Check if sandbox mode is enabled via environment or config.

    Sandbox is enabled when any of:
    - NVH_SANDBOX=1 environment variable is set
    - --sandbox flag was passed (caller sets NVH_SANDBOX=1)
    """
    return os.environ.get("NVH_SANDBOX", "").strip() in ("1", "true", "yes")
