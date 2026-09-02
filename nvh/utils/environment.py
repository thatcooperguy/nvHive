"""Deployment environment detection for Council.

Detects whether Council is running locally, in Docker, or on a cloud GPU instance,
and reports GPU accessibility and root status. Used by `council doctor` and
cloud-aware startup paths.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nvh.utils.hw_ids import detect_machine, is_gb10_name
from nvh.utils.hw_ids import normalize_arch as _normalize_arch

__all__ = [
    "DGX_SPARK_RE",
    "EnvironmentInfo",
    "detect_cloud_provider",
    "detect_environment",
    "detect_machine",
    "get_environment_summary",
    "is_dgx_hardware",
    "is_gb10_name",
    "is_nvidia_cloud_desktop_dmi",
    "is_virtual_machine",
    "normalize_arch",
]

# 32-bit x86 spellings this module has always collapsed to "x86".
_X86_32_ALIASES = frozenset({"i386", "i686", "x86"})

# The "DGX" token with an explicit non-alphanumeric left boundary and any run
# of space / underscore / hyphen before the model word. Boundaries are explicit,
# not ``\b``: some firmware joins the tokens with underscores
# ("NVIDIA_DGX_Spark"), and ``_`` is a word character, so ``\bDGX`` never
# matched there.
_DGX_TOKEN = r"(?<![A-Za-z0-9])DGX[\s_-]*"

# The DGX Spark product name in any separator spelling — "DGX Spark",
# "DGX_Spark", "DGX-Spark" — and nothing else: "DGX Station" and "DGX Sparkle"
# do not match. This is the one Spark-by-DMI predicate; platform_facts
# classification rule 1 uses it so an underscore-joined Spark can never again
# miss a literal "DGX SPARK" substring test and fall through to plain ``dgx``.
DGX_SPARK_RE = re.compile(_DGX_TOKEN + r"SPARK(?![A-Za-z0-9])", re.IGNORECASE)

# DMI strings that identify DGX *hardware*: the GB10 superchip, the named
# desk-side products, or "DGX" followed by a model token that carries a digit
# (H100, A100, B200, GB200, -1, -2, "DGXA100 920-..."). A bare "DGX" — as in
# a DGX-branded cloud image or "DGX Cloud" — is deliberately NOT enough.
_DGX_MODEL_RE = re.compile(
    _DGX_TOKEN + r"(?:SPARK|STATION|[A-Z]{0,2}\d{1,4}[A-Z]?(?![A-Za-z0-9]))",
    re.IGNORECASE,
)

# sys_vendor / product_name fragments that mean "this is a virtual machine".
# Microsoft is handled separately: the vendor string alone also appears on
# Surface hardware, so it needs the "Virtual Machine" product to count.
_HYPERVISOR_MARKERS = (
    "qemu",
    "kvm",
    "vmware",
    "xen",
    "amazon ec2",
    "google compute engine",
    "openstack",
    "parallels",
    "virtualbox",
    "bochs",
)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EnvironmentInfo:
    """Full description of the deployment environment."""

    # OS platform
    platform: str = "unknown"          # "linux", "macos", "windows"
    machine: str = ""                  # hw_ids.detect_machine(): platform.machine() with the
                                       # Windows-on-Arm x64-emulation lie corrected ("ARM64")
    arch: str = ""                     # normalized: "x86_64" | "arm64" | other

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
            f"Platform:       {self.platform}" + (f" ({self.arch})" if self.arch else ""),
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


def normalize_arch(machine: str | None) -> str:
    """:func:`nvh.utils.hw_ids.normalize_arch` with this module's legacy contract.

    Delegates the alias table to the shared helper (``AMD64``/``x64`` →
    ``x86_64``; ``aarch64``/``ARM64`` → ``arm64``) and keeps the two promises
    callers of this module rely on: 32-bit x86 spellings collapse to
    ``"x86"`` and an empty machine string is ``"unknown"`` rather than ``""``.
    Kept exported from this module for its existing importers and tests.
    """
    value = _normalize_arch(machine)
    if value in _X86_32_ALIASES:
        return "x86"
    return value or "unknown"


def _read_dmi(key: str) -> str:
    """Read one ``/sys/class/dmi/id/<key>`` value; empty string off-Linux or on error."""
    path = Path("/sys/class/dmi/id") / key
    try:
        if not path.exists():
            return ""
        return path.read_text(errors="ignore").strip()
    except OSError:
        return ""


def is_virtual_machine() -> bool:
    """True when DMI ``sys_vendor`` / ``product_name`` name a hypervisor.

    QEMU/KVM, VMware, Xen, Hyper-V ("Microsoft Corporation" + "Virtual
    Machine"), Amazon EC2, Google Compute Engine, OpenStack, Parallels and
    VirtualBox. A VM is never DGX hardware, whatever product string the image
    was given — that is what keeps a DGX-branded cloud desktop classified as
    cloud.
    """
    vendor = _read_dmi("sys_vendor").lower()
    product = _read_dmi("product_name").lower()
    blob = f"{vendor} {product}"
    if any(marker in blob for marker in _HYPERVISOR_MARKERS):
        return True
    return "microsoft corporation" in vendor and "virtual machine" in product


def is_dgx_hardware() -> bool:
    """True when DMI identifies physical NVIDIA DGX hardware, DGX Spark included.

    DGX systems ship with NVIDIA as the board vendor, exactly like NVIDIA's
    cloud Linux Desktop vGPU images do — so the cloud heuristic below must
    check this first or a DGX Spark on a desk would be mistaken for a rented
    cloud instance. The match is deliberately narrow: the GB10 superchip, or
    ``DGX Spark`` / ``DGX Station`` / ``DGX <model>`` (``DGX H100``,
    ``DGXA100 ...``, ``DGX-2``). A bare ``DGX`` substring — a DGX-branded
    cloud image, "DGX Cloud" — does not count, and a machine that reports a
    hypervisor in DMI is virtual, never DGX hardware.
    """
    if is_virtual_machine():
        return False
    blob = " ".join(_read_dmi(k) for k in ("product_name", "board_name", "product_family"))
    return is_gb10_name(blob) or bool(_DGX_MODEL_RE.search(blob))


def is_nvidia_cloud_desktop_dmi(
    board_vendor: str | None = None, *, dgx_hardware: bool | None = None,
) -> bool:
    """NVIDIA as the DMI board vendor on something that is *not* DGX hardware.

    That combination is NVIDIA's cloud Linux desktop vGPU image. DGX and DGX
    Spark systems report NVIDIA as the board vendor too, so the DGX check has
    to win or a Spark on a desk is mistaken for a rented instance. This is the
    one implementation of that heuristic: the network-bound cloud probe here
    and the local-only one in :mod:`nvh.utils.platform_facts` both call it.
    Pass ``board_vendor`` / ``dgx_hardware`` when they are already known to
    avoid re-reading DMI; either defaults to being probed.
    """
    vendor = _read_dmi("board_vendor") if board_vendor is None else board_vendor
    if "nvidia" not in (vendor or "").lower():
        return False
    if dgx_hardware is None:
        dgx_hardware = is_dgx_hardware()
    return not dgx_hardware


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

    # NVIDIA vGPU (Linux Desktop and similar) — shared with platform_facts so
    # the two cloud paths cannot drift; see is_nvidia_cloud_desktop_dmi().
    if is_nvidia_cloud_desktop_dmi():
        pip = _curl_metadata("https://api.ipify.org", timeout=3)
        return True, "cloud_desktop", "unknown", pip

    return False, "unknown", "unknown", ""


def detect_cloud_provider() -> tuple[bool, str, str, str]:
    """Public cloud probe: ``(is_cloud, provider, instance_type, public_ip)``.

    Only meaningful on Linux (metadata endpoints, DMI, hostnames); other
    platforms return the not-cloud tuple immediately without network calls.
    """
    if _detect_platform() != "linux":
        return False, "unknown", "unknown", ""
    try:
        return _detect_cloud_provider()
    except Exception:
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

    # Platform. detect_machine() is the WOW64-aware probe platform_facts uses
    # too, so an x64 Python on an Arm Windows box reports ARM64 in both places.
    info.platform = _detect_platform()
    try:
        info.machine = detect_machine() or ""
    except Exception:
        info.machine = ""
    info.arch = normalize_arch(info.machine)

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
