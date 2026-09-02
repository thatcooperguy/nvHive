"""``nvh.utils.hw_ids`` — the one home for GB10 / architecture / machine identity.

The GB10 predicate is consumed by the GPU probe, the environment module and
platform_facts; the machine probe by platform_facts and (S9) the diagnostics
fingerprint. These tests pin the token boundaries and that the consumers
agree with each other.
"""

from __future__ import annotations

import pytest

from nvh.integrations.diagnostics import compatibility
from nvh.utils import hw_ids

# ---------------------------------------------------------------------------
# S8: GB10 token boundaries — underscores are word characters, so ``\b`` missed
# underscore-joined firmware/driver strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "NVIDIA GB10",
        "GB10",
        "gb10",
        "NVIDIA_GB10",
        "NVIDIA-GB10",
        "NVIDIA_GB10_SUPERCHIP",
        "GB10 Grace Blackwell",
        "(GB10)",
        "NVIDIA GB10, 128 GB unified",
    ],
)
def test_is_gb10_name_matches_every_token_spelling(name: str) -> None:
    assert hw_ids.is_gb10_name(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "NVIDIA GB100",
        "NVIDIA GB200",
        "GB200 NVL72",
        "NVIDIA_GB100",
        "NVIDIA_GB200_NVL72",
        "GB10X",
        "XGB10",
        "GB1",
        "GB",
        "",
        None,
        "NVIDIA GeForce RTX 4090",
        "NVIDIA H100 80GB HBM3",
    ],
)
def test_is_gb10_name_rejects_gb100_gb200_and_noise(name: str | None) -> None:
    assert hw_ids.is_gb10_name(name) is False


def test_gb10_regex_uses_explicit_boundaries() -> None:
    assert r"\b" not in hw_ids.GB10_RE.pattern


# ---------------------------------------------------------------------------
# normalize_arch / detect_machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("x86_64", "x86_64"),
        ("AMD64", "x86_64"),
        ("x64", "x86_64"),
        ("aarch64", "arm64"),
        ("ARM64", "arm64"),
        ("armv8l", "arm64"),
        ("riscv64", "riscv64"),
        ("  ARM64 ", "arm64"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_arch(raw: str | None, expected: str) -> None:
    assert hw_ids.normalize_arch(raw) == expected


def test_detect_machine_windows_on_arm_under_x64_emulation(monkeypatch) -> None:
    monkeypatch.setattr(hw_ids.sys, "platform", "win32")
    monkeypatch.setattr(hw_ids.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    assert hw_ids.detect_machine() == "ARM64"
    assert hw_ids.detect_arch() == "arm64"


def test_detect_machine_native_windows_reads_processor_architecture(monkeypatch) -> None:
    monkeypatch.setattr(hw_ids.sys, "platform", "win32")
    monkeypatch.setattr(hw_ids.platform, "machine", lambda: "AMD64")
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "ARM64")
    assert hw_ids.detect_machine() == "ARM64"


def test_detect_machine_off_windows_ignores_wow64_variables(monkeypatch) -> None:
    monkeypatch.setattr(hw_ids.sys, "platform", "linux")
    monkeypatch.setattr(hw_ids.platform, "machine", lambda: "aarch64")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    assert hw_ids.detect_machine() == "aarch64"
    assert hw_ids.detect_arch() == "arm64"


# ---------------------------------------------------------------------------
# S9: the diagnostics fingerprint's ``machine`` fact is the shared probe
# ---------------------------------------------------------------------------


def test_compatibility_platform_summary_uses_the_shared_machine_probe(monkeypatch) -> None:
    """``boot_preflight.host_fingerprint`` builds ``machine`` from platform_summary();
    it must be the WOW64-aware string platform_facts reports, not raw platform.machine()."""
    monkeypatch.setattr(hw_ids.sys, "platform", "win32")
    monkeypatch.setattr(hw_ids.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")

    summary = compatibility.platform_summary()

    assert summary["machine"] == "ARM64"
    assert summary["machine"] == hw_ids.detect_machine()
    assert compatibility.platform.machine() == "AMD64"  # the raw call still lies; nobody reads it any more
    assert set(summary) == {"system", "release", "machine", "python"}


def test_compatibility_platform_summary_agrees_with_platform_facts(monkeypatch) -> None:
    from nvh.utils import platform_facts as pf

    monkeypatch.setattr(hw_ids.sys, "platform", "linux")
    monkeypatch.setattr(hw_ids.platform, "machine", lambda: "aarch64")
    assert compatibility.platform_summary()["machine"] == "aarch64" == pf.detect_machine()
