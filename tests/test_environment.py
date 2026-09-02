"""``nvh.utils.environment`` — architecture + DGX-hardware detection.

Pins two review findings from the DGX Spark / RTX Spark work:

* W5 — ``EnvironmentInfo.machine`` / ``arch`` come from the shared
  :mod:`nvh.utils.hw_ids` probe, so an x64 Python under Windows-on-Arm
  emulation agrees with :mod:`nvh.utils.platform_facts` (``ARM64``), and the
  module no longer keeps private copies of the GB10 regex / arch alias table.
* W6 — ``is_dgx_hardware()`` matches only real DGX product strings and never a
  hypervisor, so a DGX-branded NVIDIA cloud-desktop image stays "cloud" while a
  DGX Spark on a desk stays "not cloud".
"""

from __future__ import annotations

import pytest

from nvh.utils import environment as env
from nvh.utils import hw_ids

# ---------------------------------------------------------------------------
# W5: machine / arch come from hw_ids
# ---------------------------------------------------------------------------


def _quiet_probes(monkeypatch) -> None:
    """Keep detect_environment() away from nvidia-smi, cgroups and metadata endpoints."""
    monkeypatch.setattr(env, "_detect_docker", lambda: False)
    monkeypatch.setattr(env, "_detect_gpu", lambda: (False, False, [], 0, 0.0))
    monkeypatch.setattr(env, "_detect_cloud_provider", lambda: (False, "unknown", "unknown", ""))


def test_environment_uses_shared_hw_ids_helpers() -> None:
    assert env.detect_machine is hw_ids.detect_machine
    assert env.is_gb10_name is hw_ids.is_gb10_name
    assert not hasattr(env, "_GB10_RE")


def test_detect_environment_reports_arm64_under_windows_on_arm_emulation(monkeypatch) -> None:
    """x64 Python under Prism: platform.machine() says AMD64; the WOW64 variable tells the truth."""
    _quiet_probes(monkeypatch)
    monkeypatch.setattr(hw_ids.sys, "platform", "win32")
    monkeypatch.setattr(hw_ids.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")

    info = env.detect_environment()

    assert info.platform == "windows"
    assert info.machine == "ARM64"
    assert info.arch == "arm64"
    assert info.arch == hw_ids.detect_arch()  # the same answer platform_facts gets


def test_detect_environment_native_x86_64_is_unchanged(monkeypatch) -> None:
    """Discrete-GPU Linux boxes and cloud desktops keep reporting x86_64; WOW64 vars are ignored off-Windows."""
    _quiet_probes(monkeypatch)
    monkeypatch.setattr(hw_ids.sys, "platform", "linux")
    monkeypatch.setattr(hw_ids.platform, "machine", lambda: "x86_64")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")

    info = env.detect_environment()

    assert info.platform == "linux"
    assert info.machine == "x86_64"
    assert info.arch == "x86_64"


def test_detect_environment_survives_machine_probe_failure(monkeypatch) -> None:
    _quiet_probes(monkeypatch)

    def boom() -> str:
        raise RuntimeError("no uname")

    monkeypatch.setattr(env, "detect_machine", boom)
    info = env.detect_environment()
    assert info.machine == ""
    assert info.arch == "unknown"


@pytest.mark.parametrize("raw", ["aarch64", "ARM64", "AMD64", "x64", "x86_64", "riscv64"])
def test_normalize_arch_delegates_to_hw_ids(raw: str) -> None:
    assert env.normalize_arch(raw) == hw_ids.normalize_arch(raw)


def test_normalize_arch_keeps_legacy_contract_for_callers() -> None:
    # platform_facts (and its tests) rely on these two promises.
    assert env.normalize_arch("") == "unknown"
    assert env.normalize_arch(None) == "unknown"
    assert env.normalize_arch("i686") == "x86"


# ---------------------------------------------------------------------------
# W6: is_dgx_hardware() is narrow and hypervisor-aware
# ---------------------------------------------------------------------------


def _patch_env_cloud(monkeypatch, dmi: dict[str, str]) -> None:
    """Drive _detect_cloud_provider() to the NVIDIA board-vendor heuristic with fake DMI."""
    monkeypatch.setattr(env, "_read_dmi", lambda key: dmi.get(key, ""))
    monkeypatch.setattr(env, "_detect_aws", lambda: (False, "unknown", ""))
    monkeypatch.setattr(env, "_detect_gcp", lambda: (False, "unknown", ""))
    monkeypatch.setattr(env, "_detect_azure", lambda: (False, "unknown", ""))
    monkeypatch.setattr(env, "_curl_metadata", lambda *a, **k: "")

    def _no_hostname(*args, **kwargs):
        raise OSError("hostname unavailable in test")

    monkeypatch.setattr(env.subprocess, "run", _no_hostname)


def test_dgx_branded_cloud_desktop_image_is_still_cloud(monkeypatch) -> None:
    """The review case: an NVIDIA cloud-desktop image with 'DGX' in its product strings."""
    _patch_env_cloud(monkeypatch, {
        "board_vendor": "NVIDIA",
        "sys_vendor": "NVIDIA",
        "product_name": "NVIDIA DGX Cloud Desktop",
        "product_family": "DGX Cloud",
    })
    assert env.is_dgx_hardware() is False
    is_cloud, provider, _itype, _ip = env._detect_cloud_provider()
    assert (is_cloud, provider) == (True, "cloud_desktop")


@pytest.mark.parametrize(
    "sys_vendor, product_name",
    [
        ("QEMU", "Standard PC (Q35 + ICH9, 2009)"),
        ("Red Hat", "KVM"),
        ("VMware, Inc.", "VMware Virtual Platform"),
        ("Xen", "HVM domU"),
        ("Microsoft Corporation", "Virtual Machine"),
        ("Amazon EC2", "g5.xlarge"),
        ("Google", "Google Compute Engine"),
        ("OpenStack Foundation", "OpenStack Nova"),
        ("Parallels International GmbH.", "Parallels Virtual Platform"),
        ("innotek GmbH", "VirtualBox"),
        ("QEMU", "NVIDIA DGX Spark"),  # a DGX string on a hypervisor is still a VM
    ],
)
def test_hypervisor_dmi_is_virtual_and_never_dgx_hardware(monkeypatch, sys_vendor: str, product_name: str) -> None:
    _patch_env_cloud(monkeypatch, {
        "board_vendor": "NVIDIA",
        "sys_vendor": sys_vendor,
        "product_name": product_name,
        "board_name": "NVIDIA DGX Spark",
    })
    assert env.is_virtual_machine() is True
    assert env.is_dgx_hardware() is False
    assert env._detect_cloud_provider()[:2] == (True, "cloud_desktop")


def test_microsoft_vendor_alone_is_not_a_hypervisor(monkeypatch) -> None:
    """Surface hardware reports 'Microsoft Corporation' too — only with 'Virtual Machine' is it Hyper-V."""
    _patch_env_cloud(monkeypatch, {"sys_vendor": "Microsoft Corporation", "product_name": "Surface Laptop 7"})
    assert env.is_virtual_machine() is False


def test_real_dgx_spark_is_dgx_hardware_not_cloud(monkeypatch) -> None:
    _patch_env_cloud(monkeypatch, {
        "board_vendor": "NVIDIA",
        "sys_vendor": "NVIDIA",
        "product_name": "NVIDIA DGX Spark",
        "board_name": "NVIDIA DGX Spark",
        "product_family": "DGX",
    })
    assert env.is_virtual_machine() is False
    assert env.is_dgx_hardware() is True
    assert env._detect_cloud_provider() == (False, "unknown", "unknown", "")


@pytest.mark.parametrize(
    "product_name",
    [
        "NVIDIA DGX Station",
        "DGXA100 920-23687-2530-000",
        "NVIDIA DGX H100",
        "DGX-2",
        "NVIDIA DGX GB200",
        "NVIDIA GB10",
    ],
)
def test_dgx_models_and_gb10_are_hardware(monkeypatch, product_name: str) -> None:
    _patch_env_cloud(monkeypatch, {"board_vendor": "NVIDIA", "sys_vendor": "NVIDIA", "product_name": product_name})
    assert env.is_dgx_hardware() is True
    assert env._detect_cloud_provider()[0] is False


@pytest.mark.parametrize(
    "product_name",
    [
        "NVIDIA DGX Cloud Desktop",
        "DGX OS 7 golden image",
        "NVIDIA Cloud Linux Desktop",
        "DGX",
        "",
    ],
)
def test_bare_dgx_strings_are_not_hardware(monkeypatch, product_name: str) -> None:
    _patch_env_cloud(monkeypatch, {"board_vendor": "NVIDIA", "sys_vendor": "NVIDIA", "product_name": product_name})
    assert env.is_dgx_hardware() is False
    assert env._detect_cloud_provider()[:2] == (True, "cloud_desktop")


def test_non_nvidia_board_vendor_is_not_cloud_desktop(monkeypatch) -> None:
    """Regression guard: a no-GPU laptop / x86 workstation never trips the NVIDIA heuristic."""
    _patch_env_cloud(monkeypatch, {"board_vendor": "LENOVO", "sys_vendor": "LENOVO", "product_name": "21FV"})
    assert env.is_dgx_hardware() is False
    assert env._detect_cloud_provider() == (False, "unknown", "unknown", "")


# ---------------------------------------------------------------------------
# S8: underscore-joined DMI strings — ``_`` is a word character, so ``\b`` missed them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "product_name",
    [
        "NVIDIA_DGX_Spark",
        "NVIDIA_DGX_H100",
        "NVIDIA_DGX_H100_SXM",
        "DGX_Station",
        "NVIDIA_DGXA100_920-23687",
        "NVIDIA_GB10",
        "NVIDIA_GB10_SUPERCHIP",
    ],
)
def test_underscore_joined_dmi_strings_are_dgx_hardware(monkeypatch, product_name: str) -> None:
    _patch_env_cloud(monkeypatch, {"board_vendor": "NVIDIA", "sys_vendor": "NVIDIA", "product_name": product_name})
    assert env.is_dgx_hardware() is True
    assert env._detect_cloud_provider()[0] is False  # a Spark with odd firmware is still not a cloud desktop


@pytest.mark.parametrize(
    "product_name",
    ["NVIDIA_GB100", "NVIDIA_GB200_NVL72", "GB10X", "XGB10", "DGX_Cloud", "DGXCloud", "NVIDIA_DGX_Cloud_Desktop"],
)
def test_gb100_gb200_and_bare_dgx_are_still_not_hardware(monkeypatch, product_name: str) -> None:
    _patch_env_cloud(monkeypatch, {"board_vendor": "NVIDIA", "sys_vendor": "NVIDIA", "product_name": product_name})
    assert env.is_dgx_hardware() is False
    assert env._detect_cloud_provider()[:2] == (True, "cloud_desktop")


def test_dgx_model_regex_uses_explicit_boundaries() -> None:
    assert r"\b" not in env._DGX_MODEL_RE.pattern
    assert env._DGX_MODEL_RE.search("x_DGX_H100_y") is not None
    assert env._DGX_MODEL_RE.search("DGX H100") is not None
    assert env._DGX_MODEL_RE.search("ADGX H100") is None  # glued to a preceding letter is not the token


@pytest.mark.parametrize(
    "text",
    ["NVIDIA DGX Spark", "NVIDIA_DGX_Spark", "DGX-Spark", "dgx spark", "x_DGX_SPARK_y", "NVIDIA DGX  Spark"],
)
def test_dgx_spark_regex_is_separator_safe(text: str) -> None:
    """R3: one shared Spark-by-DMI predicate (platform_facts rule 1 imports it), with explicit
    boundaries so ``_``-joined firmware strings match and glued tokens do not."""
    assert env.DGX_SPARK_RE.search(text) is not None


@pytest.mark.parametrize(
    "text", ["DGX Station", "NVIDIA DGX Station GB300", "ADGX Spark", "DGX Sparkle", "DGX_SPARK2", "GB10"],
)
def test_dgx_spark_regex_rejects_non_spark_strings(text: str) -> None:
    assert env.DGX_SPARK_RE.search(text) is None


def test_dgx_spark_regex_is_exported_and_shares_the_dgx_token() -> None:
    assert "DGX_SPARK_RE" in env.__all__
    assert r"\b" not in env.DGX_SPARK_RE.pattern
    assert env.DGX_SPARK_RE.pattern.startswith(env._DGX_TOKEN)
    assert env._DGX_MODEL_RE.pattern.startswith(env._DGX_TOKEN)
    # Every Spark spelling the Spark regex accepts is also DGX hardware.
    for text in ("NVIDIA DGX Spark", "NVIDIA_DGX_Spark", "DGX-Spark"):
        assert env._DGX_MODEL_RE.search(text) is not None


# ---------------------------------------------------------------------------
# S11: one implementation of the NVIDIA-board-vendor cloud-desktop heuristic
# ---------------------------------------------------------------------------


def test_is_nvidia_cloud_desktop_dmi_probes_dmi_when_not_told(monkeypatch) -> None:
    _patch_env_cloud(monkeypatch, {"board_vendor": "NVIDIA", "product_name": "NVIDIA Cloud Linux Desktop"})
    assert env.is_nvidia_cloud_desktop_dmi() is True
    _patch_env_cloud(monkeypatch, {"board_vendor": "NVIDIA", "product_name": "NVIDIA DGX Spark"})
    assert env.is_nvidia_cloud_desktop_dmi() is False
    _patch_env_cloud(monkeypatch, {"board_vendor": "LENOVO"})
    assert env.is_nvidia_cloud_desktop_dmi() is False


def test_is_nvidia_cloud_desktop_dmi_trusts_explicit_arguments(monkeypatch) -> None:
    """platform_facts already read DMI and ran is_dgx_hardware — neither may be re-probed."""
    monkeypatch.setattr(env, "_read_dmi", lambda key: pytest.fail("DMI must not be re-read"))
    monkeypatch.setattr(env, "is_dgx_hardware", lambda: pytest.fail("DGX must not be re-probed"))
    assert env.is_nvidia_cloud_desktop_dmi("NVIDIA", dgx_hardware=False) is True
    assert env.is_nvidia_cloud_desktop_dmi("nvidia corporation", dgx_hardware=False) is True
    assert env.is_nvidia_cloud_desktop_dmi("NVIDIA", dgx_hardware=True) is False
    assert env.is_nvidia_cloud_desktop_dmi("Supermicro", dgx_hardware=False) is False
    assert env.is_nvidia_cloud_desktop_dmi("", dgx_hardware=False) is False


def test_network_cloud_path_uses_the_shared_heuristic(monkeypatch) -> None:
    _patch_env_cloud(monkeypatch, {})
    calls: list[tuple] = []

    def spy(board_vendor=None, *, dgx_hardware=None):
        calls.append((board_vendor, dgx_hardware))
        return True

    monkeypatch.setattr(env, "is_nvidia_cloud_desktop_dmi", spy)
    assert env._detect_cloud_provider()[:2] == (True, "cloud_desktop")
    assert calls == [(None, None)]  # the network path lets the helper read DMI itself
    assert "is_nvidia_cloud_desktop_dmi" in env.__all__
