"""Linux Desktop integration — auto-detect cloud sessions and configure NVHive.

On Linux desktop nodes:
- Detects if running inside a cloud session
- Reads cloud desktop user/session info from environment and local files
- Auto-configures NVHive for the CLOUD GPU tier
- Hooks into CLOUD account for seamless user experience

CLOUD Detection Methods:
1. Environment variables: CLOUD_SESSION_ID, NVIDIA_CLOUD_*, etc.
2. Process check: cloud-specific services running
3. Hardware fingerprint: cloud-specific GPU names (Tesla T10, RTX virtual GPUs)
4. File markers: /etc/nvidia/cloud_session.conf, ~/.nvidia/cloud_session_session

CLOUD Tiers and GPUs:
- Priority: RTX 3060 class (8GB VRAM)
- Performance: RTX 3080 class (10GB VRAM)
- Ultimate: RTX 4080 class (16GB VRAM)
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CLOUDSession:
    """Linux Desktop session information."""
    is_cloud_session: bool = False
    session_id: str = ""
    user_id: str = ""
    user_email: str = ""
    tier: str = ""              # "priority", "performance", "ultimate"
    gpu_class: str = ""         # e.g., "RTX 3060", "RTX 4080"
    session_duration_max: int = 0  # max session length in minutes
    region: str = ""
    persistent_storage: str = ""  # path to persistent user storage


def detect_cloud_session() -> CLOUDSession:
    """Detect if running inside a Linux Desktop session.

    Checks multiple signals to determine if this is a cloud instance.
    """
    session = CLOUDSession()

    # Method 1: Environment variables
    # CLOUD sets various NVIDIA-specific env vars
    cloud_session_env_markers = [
        "CLOUD_SESSION_ID",
        "NVIDIA_CLOUD_SESSION",
        "CLOUD_USER_ID",
        "CLOUD_TIER",
    ]
    for var in cloud_session_env_markers:
        val = os.environ.get(var, "")
        if val:
            session.is_cloud_session = True
            if "SESSION" in var:
                session.session_id = val
            elif "USER" in var:
                session.user_id = val
            elif "TIER" in var:
                session.tier = val.lower()

    # Method 2: Check for CLOUD config files
    cloud_session_paths = [
        Path("/etc/nvidia/cloud_session.conf"),
        Path("/etc/nvidia/grid.conf"),
        Path(os.path.expanduser("~/.nvidia/cloud_session_session")),
        Path("/var/lib/nvidia/cloud_session"),
    ]
    for path in cloud_session_paths:
        if path.exists():
            session.is_cloud_session = True
            try:
                content = path.read_text()
                # Parse key=value format
                for line in content.splitlines():
                    if "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip().lower()
                        value = value.strip().strip('"')
                        if "session" in key:
                            session.session_id = session.session_id or value
                        elif "user" in key or "email" in key:
                            session.user_email = session.user_email or value
                        elif "tier" in key:
                            session.tier = session.tier or value.lower()
                        elif "region" in key:
                            session.region = value
            except Exception:
                pass

    # Method 3: GPU name fingerprint
    # CLOUD uses virtual GPUs with specific naming patterns
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        gpu_name = result.stdout.strip()

        # CLOUD virtual GPU identifiers
        cloud_session_gpu_patterns = [
            "Tesla T10",        # Older CLOUD servers
            "GRID",             # NVIDIA GRID virtual GPU
            "Virtual",          # Generic virtual GPU
        ]
        for pattern in cloud_session_gpu_patterns:
            if pattern.lower() in gpu_name.lower():
                session.is_cloud_session = True
                session.gpu_class = gpu_name
                break

        if not session.gpu_class and gpu_name:
            session.gpu_class = gpu_name

        # Infer tier from GPU if not set
        if session.is_cloud_session and not session.tier:
            if "4080" in gpu_name or "4090" in gpu_name:
                session.tier = "ultimate"
            elif "3080" in gpu_name:
                session.tier = "performance"
            else:
                session.tier = "priority"
    except Exception:
        pass

    # Method 4: Check if home directory is a mount point (persistent storage)
    home = Path.home()
    if os.path.ismount(str(home)):
        session.persistent_storage = str(home)
    # Also check common CLOUD persistent paths
    for mount_path in ["/mnt/user", "/home/user", str(home)]:
        if os.path.isdir(mount_path):
            session.persistent_storage = session.persistent_storage or mount_path

    # Method 5: Process-based detection
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=5,
        )
        cloud_session_processes = ["cloud_session-", "nvidia-streamer", "nvstreamer"]
        for proc in cloud_session_processes:
            if proc in result.stdout.lower():
                session.is_cloud_session = True
                break
    except Exception:
        pass

    # Default session duration by tier
    if session.is_cloud_session and not session.session_duration_max:
        tier_durations = {
            "priority": 360,      # 6 hours
            "performance": 480,   # 8 hours
            "ultimate": 480,      # 8 hours
        }
        session.session_duration_max = tier_durations.get(session.tier, 360)

    return session


def get_cloud_recommended_config(session: CLOUDSession) -> dict[str, Any]:
    """Get NVHive configuration recommendations for this cloud session.

    Returns a dict of config overrides optimized for the instance tier.
    """
    config: dict[str, Any] = {}

    if not session.is_cloud_session:
        return config

    # Model selection based on tier
    tier_models = {
        "priority": {
            "default_model": "ollama/nemotron-mini",
            "recommended_models": ["nemotron-mini"],
        },
        "performance": {
            "default_model": "ollama/nemotron-small",
            "recommended_models": ["nemotron-mini", "nemotron-small"],
        },
        "ultimate": {
            "default_model": "ollama/nemotron-small",
            "recommended_models": ["nemotron-mini", "nemotron-small", "codellama"],
        },
    }

    tier_config = tier_models.get(session.tier, tier_models["priority"])
    config["ollama_default_model"] = tier_config["default_model"]
    config["recommended_models"] = tier_config["recommended_models"]

    # Session-aware settings
    config["auto_save"] = True  # auto-save conversations (session could end)
    config["cache_aggressive"] = True  # cache more to survive session restarts
    config["budget_daily_limit"] = 5.0  # conservative default for students

    # Performance tuning
    if session.tier == "ultimate":
        config["ollama_num_parallel"] = 2
        config["ollama_flash_attention"] = True
    else:
        config["ollama_num_parallel"] = 1
        config["ollama_flash_attention"] = True

    return config


def format_cloud_status(session: CLOUDSession) -> str:
    """Format cloud session info for display."""
    if not session.is_cloud_session:
        return "Not running on Linux Desktop"

    lines = [
        f"Linux Desktop: {session.tier.capitalize()} tier",
        f"GPU: {session.gpu_class}",
    ]
    if session.session_id:
        lines.append(f"Session: {session.session_id[:8]}...")
    if session.session_duration_max:
        hours = session.session_duration_max // 60
        lines.append(f"Max duration: {hours}h")
    if session.region:
        lines.append(f"Region: {session.region}")
    if session.persistent_storage:
        lines.append(f"Storage: {session.persistent_storage}")

    return " | ".join(lines)
