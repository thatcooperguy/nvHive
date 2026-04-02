"""Deployment environment detection for Council.

Detects whether Council is running locally, in Docker, or on a cloud GPU instance,
and reports GPU accessibility and root status. Used by `council doctor` and
cloud-aware startup paths.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EnvironmentInfo:
    """Full description of the deployment environment."""

    # OS platform
    platform: str = "unknown"          # "linux", "macos", "windows"

    # Container
    is_docker: bool = False            # running inside a Docker container

    # Cloud
    is_cloud: bool = False             # running on a cloud instance
    cloud_provider: str = "unknown"    # "aws" | "gcp" | "azure" | "lambda" |
                                       # "coreweave" | "cloud_desktop" | "unknown"
    instance_type: str = "unknown"     # e.g. "g5.xlarge"
    public_ip: str = ""                # instance public IP, if any

    # GPU
    has_gpu: bool = False              # nvidia-smi reports at least one GPU
    gpu_accessible: bool = False       # GPU is accessible from this process
    gpu_names: list[str] = field(default_factory=list)
    gpu_count: int = 0
    gpu_vram_gb: float = 0.0           # VRAM of first GPU in GB

    # Privileges
    has_root: bool = False             # uid 0 or sudo available

    def __str__(self) -> str:  # pragma: no cover
        lines = [
            f"Platform:       {self.platform}",
            f"In Docker:      {self.is_docker}",
            f"Cloud:          {self.is_cloud} ({self.cloud_provider})",
            f"Instance type:  {self.instance_type}",
        ]
        if self.public_ip:
            lines.append(f"Public IP:      {self.public_ip}")
        lines += [
            f"Has GPU:        {self.has_gpu}",
            f"GPU accessible: {self.gpu_accessible}",
        ]
        if self.gpu_names:
            lines.append(f"GPUs:           {', '.join(self.gpu_names)} x{self.gpu_count}")
        if self.gpu_vram_gb:
            lines.append(f"VRAM (GPU 0):   {self.gpu_vram_gb:.1f} GB")
        lines.append(f"Has root:       {self.has_root}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _curl_metadata(url: str, headers: dict[str, str] | None = None,
                   timeout: float = 1.5) -> str:
    """Fetch a metadata URL, returning the response body or empty string on failure.

    Uses the `curl` binary rather than an httpx/requests import so this module
    has zero extra dependencies and can be imported early in the CLI.
    """
    if not shutil.which("curl"):
        return ""
    cmd = ["curl", "-s", "--max-time", str(timeout)]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 0.5)
        return result.stdout.strip()
    except Exception:
        return ""


def _curl_metadata_put(url: str, put_headers: dict[str, str],
                       timeout: float = 1.5) -> str:
    """HTTP PUT for AWS IMDSv2 token request."""
    if not shutil.which("curl"):
        return ""
    cmd = ["curl", "-s", "--max-time", str(timeout), "-X", "PUT"]
    for k, v in put_headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 0.5)
        return result.stdout.strip()
    except Exception:
        return ""


def _detect_platform() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return sys.platform


def _detect_docker() -> bool:
    """Return True if running inside a Docker container."""
    # Standard indicator
    if Path("/.dockerenv").exists():
        return True
    # Check cgroup for docker/containerd/kubernetes markers
    cgroup = Path("/proc/1/cgroup")
    if cgroup.exists():
        try:
            content = cgroup.read_text(errors="ignore")
            if any(kw in content for kw in ("docker", "containerd", "kubepods", "lxc")):
                return True
        except OSError:
            pass
    return False


def _detect_aws() -> tuple[bool, str, str]:
    """Return (is_aws, instance_type, public_ip)."""
    # IMDSv2: first get a session token
    token = _curl_metadata_put(
        "http://169.254.169.254/latest/api/token",
        {"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
    )
    if not token:
        return False, "unknown", ""

    instance_type = _curl_metadata(
        "http://169.254.169.254/latest/meta-data/instance-type",
        {"X-aws-ec2-metadata-token": token},
    )
    if not instance_type:
        return False, "unknown", ""

    public_ip = _curl_metadata(
        "http://169.254.169.254/latest/meta-data/public-ipv4",
        {"X-aws-ec2-metadata-token": token},
    )
    return True, instance_type, public_ip


def _detect_gcp() -> tuple[bool, str, str]:
    """Return (is_gcp, machine_type, public_ip)."""
    machine_type = _curl_metadata(
        "http://metadata.google.internal/computeMetadata/v1/instance/machine-type",
        {"Metadata-Flavor": "Google"},
    )
    if not machine_type:
        return False, "unknown", ""

    # machine_type is a full resource path like "zones/us-central1-a/machineTypes/a2-highgpu-1g"
    short_type = machine_type.split("/")[-1]

    public_ip = _curl_metadata(
        "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/"
        "access-configs/0/externalIp",
        {"Metadata-Flavor": "Google"},
    )
    return True, short_type, public_ip


def _detect_azure() -> tuple[bool, str, str]:
    """Return (is_azure, vm_size, public_ip)."""
    meta = _curl_metadata(
        "http://169.254.169.254/metadata/instance?api-version=2021-02-01",
        {"Metadata": "true"},
    )
    if '"azEnvironment"' not in meta:
        return False, "unknown", ""

    # Extract vmSize from JSON without importing json (keep it import-free)
    import json  # only import if we have a response
    try:
        data = json.loads(meta)
        vm_size = data.get("compute", {}).get("vmSize", "unknown")
    except Exception:
        vm_size = "unknown"

    public_ip = _curl_metadata("https://api.ipify.org", timeout=3)
    return True, vm_size, public_ip


def _detect_cloud_provider() -> tuple[bool, str, str, str]:
    """Return (is_cloud, provider, instance_type, public_ip)."""
    # AWS
    is_aws, itype, pip = _detect_aws()
    if is_aws:
        return True, "aws", itype, pip

    # GCP
    is_gcp, itype, pip = _detect_gcp()
    if is_gcp:
        return True, "gcp", itype, pip

    # Azure
    is_azure, itype, pip = _detect_azure()
    if is_azure:
        return True, "azure", itype, pip

    # Lambda Labs / CoreWeave / RunPod / Vast / Paperspace — hostname heuristic
    try:
        hostname = subprocess.run(
            ["hostname", "-f"], capture_output=True, text=True, timeout=2
        ).stdout.strip().lower()
    except Exception:
        hostname = ""

    gpu_cloud_keywords = {
        "lambda": "lambda",
        "coreweave": "coreweave",
        "vast": "vast_ai",
        "runpod": "runpod",
        "paperspace": "paperspace",
        "tensordock": "tensordock",
    }
    for kw, provider in gpu_cloud_keywords.items():
        if kw in hostname:
            pip = _curl_metadata("https://api.ipify.org", timeout=3)
            return True, provider, "unknown", pip

    # NVIDIA vGPU (Linux Desktop and similar)
    board_vendor = Path("/sys/class/dmi/id/board_vendor")
    if board_vendor.exists():
        try:
            if "nvidia" in board_vendor.read_text().lower():
                pip = _curl_metadata("https://api.ipify.org", timeout=3)
                return True, "cloud_desktop", "unknown", pip
        except OSError:
            pass

    return False, "unknown", "unknown", ""


def _detect_gpu() -> tuple[bool, bool, list[str], int, float]:
    """Return (has_gpu, gpu_accessible, gpu_names, gpu_count, vram_gb_first)."""
    if not shutil.which("nvidia-smi"):
        return False, False, [], 0, 0.0

    # nvidia-smi is present — try to query it
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return True, False, [], 0, 0.0  # has driver but can't access GPU

        lines = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
        if not lines:
            return True, False, [], 0, 0.0

        names: list[str] = []
        vram_mib_first: float = 0.0
        for i, line in enumerate(lines):
            parts = [p.strip() for p in line.split(",")]
            names.append(parts[0] if parts else "unknown")
            if i == 0 and len(parts) >= 2:
                try:
                    vram_mib_first = float(parts[1].split()[0])
                except (ValueError, IndexError):
                    vram_mib_first = 0.0

        vram_gb = round(vram_mib_first / 1024, 1)
        return True, True, names, len(names), vram_gb

    except Exception:
        return True, False, [], 0, 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_environment() -> EnvironmentInfo:
    """Detect the current deployment environment.

    This function makes network calls (cloud metadata endpoints) with short
    timeouts so it completes quickly in non-cloud environments. All failures
    are handled gracefully.
    """
    info = EnvironmentInfo()

    # Platform
    info.platform = _detect_platform()

    # Docker
    info.is_docker = _detect_docker()

    # Cloud (only makes sense on Linux; skip on macOS/Windows to avoid delay)
    if info.platform == "linux":
        is_cloud, provider, itype, pip = _detect_cloud_provider()
        info.is_cloud = is_cloud
        info.cloud_provider = provider
        info.instance_type = itype
        info.public_ip = pip

    # GPU
    has_gpu, accessible, names, count, vram = _detect_gpu()
    info.has_gpu = has_gpu
    info.gpu_accessible = accessible
    info.gpu_names = names
    info.gpu_count = count
    info.gpu_vram_gb = vram

    # Root / sudo
    info.has_root = (os.getuid() == 0) if hasattr(os, "getuid") else False

    return info


def get_environment_summary(info: EnvironmentInfo | None = None) -> str:
    """Return a one-line summary string for use in logs and CLI output."""
    if info is None:
        info = detect_environment()

    parts = [info.platform]
    if info.is_docker:
        parts.append("docker")
    if info.is_cloud:
        parts.append(info.cloud_provider)
        if info.instance_type != "unknown":
            parts.append(info.instance_type)
    if info.gpu_accessible:
        gpu_label = info.gpu_names[0] if info.gpu_names else "GPU"
        parts.append(f"{gpu_label} x{info.gpu_count}")
    elif info.has_gpu:
        parts.append("GPU(inaccessible)")
    else:
        parts.append("CPU-only")
    return " | ".join(parts)
