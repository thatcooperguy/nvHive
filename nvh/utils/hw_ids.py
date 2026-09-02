"""Hardware identity helpers shared by the GPU, environment and platform probes.

One home for the facts several modules used to re-derive independently:

* :func:`is_gb10_name` — is this GPU name the GB10 Grace Blackwell superchip
  (DGX Spark / RTX Spark), the one NVIDIA part whose "VRAM" is a unified
  CPU/GPU pool.
* :func:`normalize_arch` — collapse ``platform.machine()`` spellings to
  ``x86_64`` / ``arm64`` / the lower-cased original.
* :func:`detect_machine` — ``platform.machine()`` corrected for Windows on Arm
  under x64 emulation (``PROCESSOR_ARCHITEW6432``), where the raw call lies.

Pure functions, no I/O beyond ``os.environ``; safe to import from anywhere.
"""

from __future__ import annotations

import os
import platform
import re
import sys

# Explicit non-alphanumeric boundaries rather than ``\b``: DMI and driver
# strings are sometimes underscore-joined ("NVIDIA_GB10"), and ``_`` is a word
# character, so ``\bGB10\b`` missed them. GB100 / GB200 must still not match.
GB10_RE = re.compile(r"(?<![A-Za-z0-9])GB10(?![A-Za-z0-9])", re.IGNORECASE)

_ARCH_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv8l": "arm64",
}


def is_gb10_name(name: str | None) -> bool:
    """True when ``name`` is a GB10-class (unified-memory) NVIDIA GPU."""
    return bool(GB10_RE.search(name or ""))


def normalize_arch(machine: str | None) -> str:
    """``AMD64``/``x64`` → ``x86_64``; ``aarch64``/``ARM64`` → ``arm64``; else lower-case."""
    key = (machine or "").strip().lower()
    return _ARCH_ALIASES.get(key, key)


def detect_machine() -> str:
    """Raw machine string, corrected for Windows-on-Arm x64 emulation.

    A Python built for x86-64 running under Prism on an Arm Windows box
    reports ``AMD64`` from :func:`platform.machine`; Windows exposes the real
    CPU in ``PROCESSOR_ARCHITEW6432`` (or ``PROCESSOR_ARCHITECTURE`` for a
    native process). Non-Windows hosts return :func:`platform.machine` as-is.
    """
    if sys.platform == "win32":
        for var in ("PROCESSOR_ARCHITEW6432", "PROCESSOR_ARCHITECTURE"):
            value = os.environ.get(var, "").strip()
            if value:
                return value
    return platform.machine()


def detect_arch() -> str:
    """Normalised architecture of the running host (``x86_64`` / ``arm64`` / other)."""
    return normalize_arch(detect_machine())
