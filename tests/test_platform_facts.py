"""Tests for nvh.utils.platform_facts — DGX Spark / RTX Spark / cloud / laptop classification.

Every hardware probe is monkeypatched so these run identically on the dev
box, CI, and an actual Spark. Two properties get the most attention because
they bit real users:

* the request path (``probe_network=False``, the default) must never spawn
  ``curl``/``hostname``/``sudo`` — cloud classification comes from local
  signals, the network probe runs once in :func:`warm_platform_facts`;
* the privilege probes must not generate auth-log noise: ``sudo -n`` runs at
  most once per process and only for a sudo-group member.

The environment-module DMI heuristic is tested at its own layer too, because
a DGX Spark must never be reported as a rented cloud desktop.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from nvh.utils import environment as env
from nvh.utils import hw_ids
from nvh.utils import platform_facts as pf
from nvh.utils.gpu import GPUInfo

_DMI_PREFIX = f"{pf._DMI_DIR}/"
_PS_DIR = pf._POWER_SUPPLY_DIR
_ALL_DEVICE_CLASSES = {"dgx-spark", "rtx-spark", "dgx", "cloud-desktop", "workstation", "laptop", "unknown"}


def _gpu(name: str, vram_mb: int = 131072, *, unified: bool = False) -> GPUInfo:
    return GPUInfo(
        name=name,
        vram_mb=vram_mb,
        vram_gb=round(vram_mb / 1024, 1),
        driver_version="580.65",
        cuda_version="13.0",
        utilization_pct=0,
        memory_used_mb=0,
        memory_free_mb=vram_mb,
        index=0,
        unified_memory=unified,
    )


def _join_host_probe_threads(timeout: float = 5.0) -> None:
    """Wait for the daemon thread a non-blocking ``probe_host()`` started on a cold cache (if any)."""
    for thread in threading.enumerate():
        if thread.name == "nvh-host-probe":
            thread.join(timeout)
            assert not thread.is_alive(), "background host probe did not finish"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    # Depending on ``monkeypatch`` orders it outside this fixture, so a background probe
    # still running at teardown is joined while the test's fakes are still installed.
    pf.clear_platform_facts_cache()
    yield
    _join_host_probe_threads()
    pf.clear_platform_facts_cache()


def _simulate(
    monkeypatch,
    *,
    os_name: str,
    machine: str,
    files: dict[str, str] | None = None,
    exists: set[str] | None = None,
    globs: set[tuple[str, str]] | None = None,
    power_supply: dict[str, dict[str, str]] | None = None,
    battery: bool = False,
    cloud: tuple[bool, str] | None = None,
    can_sudo: bool = False,
    in_sudo_group: bool = False,
    patch_privileges: bool = True,
    root: bool = False,
    memory: tuple[float, float] = (128.0, 100.0),
) -> None:
    """Fake a host end to end: the file/DMI readers on both modules, never the classifier."""
    files = dict(files or {})
    exists = exists or set()
    globs = globs or set()
    power_supply = dict(power_supply or {})
    if battery:
        power_supply.setdefault("BAT0", {"type": "Battery", "scope": "System"})
    for name, attrs in power_supply.items():
        for attr, value in attrs.items():
            files[f"{_PS_DIR}/{name}/{attr}"] = value
    # environment.is_dgx_hardware() reads DMI through its own shim — same fake data.
    dmi = {path[len(_DMI_PREFIX):]: value for path, value in files.items() if path.startswith(_DMI_PREFIX)}

    monkeypatch.setattr(pf, "_detect_os", lambda: os_name)
    monkeypatch.setattr(pf, "detect_machine", lambda: machine)
    monkeypatch.setattr(pf, "_read_text", lambda path: files.get(path, ""))
    monkeypatch.setattr(pf, "_exists", lambda path: path in exists or path in files)
    monkeypatch.setattr(pf, "_glob_any", lambda d, p: (d, p) in globs)
    monkeypatch.setattr(pf, "_list_dir", lambda d: sorted(power_supply) if d == _PS_DIR else [])
    monkeypatch.setattr(env, "_read_dmi", lambda key: dmi.get(key, ""))
    monkeypatch.setattr(pf, "_has_root", lambda: root)
    monkeypatch.setattr(pf, "_memory_gb", lambda: memory)
    monkeypatch.setattr(pf, "_kernel", lambda: "6.11.0-1004-nvidia")
    if patch_privileges:
        monkeypatch.setattr(pf, "_can_sudo", lambda _os: can_sudo)
        monkeypatch.setattr(pf, "_in_sudo_group", lambda _os: in_sudo_group)
    if cloud is not None:
        monkeypatch.setattr(pf, "_cloud_local", lambda dmi, is_dgx: cloud)
    for var, _provider in pf._ENV_CLOUD_MARKERS:
        monkeypatch.delenv(var, raising=False)


def _forbid_subprocess(monkeypatch) -> list[list[str]]:
    """Make any subprocess spawn record itself and fail; returns the recorder."""
    spawned: list[list[str]] = []

    def spy(argv, *args, **kwargs):
        spawned.append([argv] if isinstance(argv, str) else list(argv))
        raise AssertionError(f"subprocess spawned on a no-network path: {argv}")

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setattr(subprocess, "Popen", spy)
    monkeypatch.setattr(subprocess, "check_output", spy)
    return spawned


def _fake_group_db(
    monkeypatch,
    *,
    groups: dict[str, tuple[int, list[str]]],
    process_gids: list[int],
    user: str = "ccooper",
    primary_gid: int = 1000,
) -> None:
    """Install fake ``grp``/``pwd`` modules: ``groups`` = {name: (gid, members)}."""
    by_name = {n: SimpleNamespace(gr_name=n, gr_gid=g, gr_mem=list(m)) for n, (g, m) in groups.items()}
    by_gid = {entry.gr_gid: entry for entry in by_name.values()}

    def getgrnam(name):
        if name not in by_name:
            raise KeyError(name)
        return by_name[name]

    def getgrgid(gid):
        if gid not in by_gid:
            raise KeyError(gid)
        return by_gid[gid]

    monkeypatch.setitem(sys.modules, "grp", SimpleNamespace(getgrnam=getgrnam, getgrgid=getgrgid))
    monkeypatch.setitem(
        sys.modules, "pwd", SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_name=user, pw_gid=primary_gid)),
    )
    monkeypatch.setattr(pf.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(pf.os, "getgroups", lambda: list(process_gids), raising=False)


_DGX_OS_RELEASE = 'ID=ubuntu\nNAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.2 LTS (DGX OS 7)"\n'
_UBUNTU_OS_RELEASE = 'ID=ubuntu\nNAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.2 LTS"\n'
_H100 = "NVIDIA H100 80GB HBM3"


# ---------------------------------------------------------------------------
# (a) DGX Spark
# ---------------------------------------------------------------------------


def test_dgx_spark_gb10_aarch64_dgx_release(monkeypatch) -> None:
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={
            "/etc/os-release": _DGX_OS_RELEASE,
            f"{_DMI_PREFIX}board_vendor": "NVIDIA",
            f"{_DMI_PREFIX}product_name": "NVIDIA DGX Spark",
            f"{_DMI_PREFIX}chassis_type": "3",
        },
        exists={"/etc/dgx-release"},
        in_sudo_group=True,
    )
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GB10", unified=True)], use_cache=False)

    assert facts.device_class == "dgx-spark"
    assert facts.unified_memory is True
    assert facts.is_cloud is False
    assert facts.is_dgx_os is True
    assert facts.os == "linux"
    assert facts.arch == "arm64"
    assert facts.machine == "aarch64"
    assert facts.distro == "Ubuntu 24.04.2 LTS (DGX OS 7)"
    assert facts.gpu_name == "NVIDIA GB10"
    assert facts.memory_total_gb == 128.0
    assert facts.memory_available_gb == 100.0
    assert facts.device_label == "NVIDIA DGX Spark (GB10, 128 GB unified)"
    assert facts.windows_on_arm is False
    # Stock DGX OS: sudo group member, but a password is required → can_sudo False.
    assert facts.can_sudo is False
    assert facts.in_sudo_group is True


def test_dgx_spark_from_gpu_name_alone(monkeypatch) -> None:
    """Inside a container the DMI/os-release are the container's — GB10 still wins."""
    _simulate(monkeypatch, os_name="linux", machine="aarch64", files={"/etc/os-release": _UBUNTU_OS_RELEASE})
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GB10")], use_cache=False)
    assert facts.device_class == "dgx-spark"
    assert facts.unified_memory is True
    assert facts.is_dgx_os is False


def test_dgx_spark_fallback_dgx_os_arm64_without_dmi_or_gpu(monkeypatch) -> None:
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={"/etc/os-release": _DGX_OS_RELEASE},
        exists={"/etc/dgx-release"},
    )
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.device_class == "dgx-spark"
    assert facts.unified_memory is True


def test_dgx_spark_fallback_with_oem_dmi_and_no_gpu(monkeypatch) -> None:
    """P6: an OEM GB10 box carries its own DMI strings; DGX OS + arm64 is still a Spark.

    Before, rule 4 demanded an *empty* DMI blob, so an ASUS/Dell Spark whose
    GPU was not enumerable (NVML blocked, driver not loaded) fell through to
    ``workstation`` with ``unified_memory=False`` — the worst possible answer
    on a 128 GB unified box.
    """
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={
            "/etc/os-release": _DGX_OS_RELEASE,
            f"{_DMI_PREFIX}board_vendor": "ASUSTeK COMPUTER INC.",
            f"{_DMI_PREFIX}board_name": "GX10",
            f"{_DMI_PREFIX}product_name": "Ascent GX10",
            f"{_DMI_PREFIX}chassis_type": "3",
        },
        exists={"/etc/dgx-release"},
    )
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.device_class == "dgx-spark"
    assert facts.unified_memory is True
    assert facts.board_vendor == "ASUSTeK COMPUTER INC."


def test_dgx_spark_fallback_inside_container_sees_host_dmi(monkeypatch) -> None:
    """A container on a Spark sees the host's DMI but its own os-release; with
    /etc/dgx-release bind-mounted and no NVML it must still be a Spark."""
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={
            "/etc/os-release": _UBUNTU_OS_RELEASE,
            f"{_DMI_PREFIX}board_vendor": "NVIDIA",
            f"{_DMI_PREFIX}product_name": "NVIDIA DGX Spark",
        },
        exists={"/etc/dgx-release"},
    )
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.device_class == "dgx-spark"
    assert facts.is_cloud is False  # NVIDIA board vendor + DGX hardware is not a cloud desktop


def test_dgx_os_arm64_non_spark_dgx_is_dgx_not_spark(monkeypatch) -> None:
    """Rule 3 precedes rule 4: an arm64 DGX Station on DGX OS is ``dgx``, not a Spark."""
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={
            "/etc/os-release": _DGX_OS_RELEASE,
            f"{_DMI_PREFIX}board_vendor": "NVIDIA",
            f"{_DMI_PREFIX}product_name": "NVIDIA DGX Station GB300",
        },
        exists={"/etc/dgx-release"},
    )
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.device_class == "dgx"
    assert facts.unified_memory is False
    assert facts.device_label == "NVIDIA DGX Station GB300"


@pytest.mark.parametrize("gpu_name", ["NVIDIA GH200 480GB", _H100, "NVIDIA Thor"])
def test_dgx_os_arm64_with_a_visible_non_gb10_gpu_is_never_a_spark(monkeypatch, gpu_name: str) -> None:
    """Rule 4 is gated on the GPU: a Grace Hopper node or a Jetson Thor dev kit on a
    DGX-OS-derived image whose GPU *is* enumerable must not become dgx-spark/unified."""
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={
            "/etc/os-release": _DGX_OS_RELEASE,
            f"{_DMI_PREFIX}board_vendor": "Supermicro",
            f"{_DMI_PREFIX}product_name": "ARS-111GL-NHR",
            f"{_DMI_PREFIX}chassis_type": "23",
        },
        exists={"/etc/dgx-release"},
        memory=(480.0, 400.0),
    )
    facts = pf.detect_platform_facts(gpus=[_gpu(gpu_name, 98304)], use_cache=False)
    assert facts.device_class == "workstation"
    assert facts.unified_memory is False
    assert facts.is_dgx_os is True
    assert facts.gpu_name == gpu_name
    assert "Spark" not in facts.device_label


def test_rule4_fires_only_when_no_gpu_name_is_known() -> None:
    host = pf.HostProbe(os="linux", arch="arm64", machine="aarch64", is_dgx_os=True)
    assert pf.classify(host, "", False) == ("dgx-spark", True)             # GPU hidden: still a Spark
    assert pf.classify(host, "NVIDIA GB10", False) == ("dgx-spark", True)  # rule 1 already, rule 4 agrees
    assert pf.classify(host, "NVIDIA GH200 480GB", False) == ("workstation", False)
    assert pf.classify(host, "NVIDIA GH200 480GB", True) == ("workstation", True)  # row-reported unified survives


def _fake_pynvml_module(name: str, *, memory_error: Exception, cc: tuple[int, int] = (9, 0)):
    """A ``pynvml`` stand-in for one GPU whose memory query fails; every other extended
    query raises ``Not Supported`` (the real probe wraps each one in try/except)."""
    import types

    mod = types.ModuleType("pynvml")
    mod.nvmlInit = lambda: None
    mod.nvmlShutdown = lambda: None
    mod.nvmlSystemGetDriverVersion = lambda: "580.65"
    mod.nvmlSystemGetCudaDriverVersion_v2 = lambda: 13000
    mod.nvmlDeviceGetCount = lambda: 1
    mod.nvmlDeviceGetHandleByIndex = lambda i: i
    mod.nvmlDeviceGetName = lambda h: name
    mod.nvmlDeviceGetCudaComputeCapability = lambda h: cc

    def memory_info(h):
        raise memory_error

    def unsupported(attr):  # module __getattr__ (PEP 562): constants and extended queries
        raise RuntimeError(f"Not Supported: {attr}")

    mod.nvmlDeviceGetMemoryInfo = memory_info
    mod.__getattr__ = unsupported
    return mod


def test_dgx_os_arm64_visible_gpu_with_unreadable_memory_is_not_a_spark(monkeypatch) -> None:
    """R4, at the mechanism: the rule-4 gate only sees GPU rows that survive
    ``gpu._resolve_memory_pool``. That helper used to *drop* a non-GB10 row whose memory
    could not be read, so a Grace Hopper node on DGX OS whose NVML memory query failed
    reached ``classify()`` with no GPU name and became dgx-spark / unified. The row is now
    kept (0 GB, ``memory-unavailable``, status ``blocked``) and the classifier sees the name."""
    from nvh.utils import gpu

    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={
            "/etc/os-release": _DGX_OS_RELEASE,
            f"{_DMI_PREFIX}board_vendor": "Supermicro",
            f"{_DMI_PREFIX}product_name": "ARS-111GL-NHR",
            f"{_DMI_PREFIX}chassis_type": "23",
        },
        exists={"/etc/dgx-release"},
        memory=(480.0, 400.0),
    )
    monkeypatch.setitem(
        sys.modules, "pynvml",
        _fake_pynvml_module("NVIDIA GH200 480GB", memory_error=RuntimeError("NVML_ERROR_GPU_IS_LOST")),
    )
    monkeypatch.setattr(gpu.shutil, "which", lambda command: None)   # no nvidia-smi fallback to rescue it
    monkeypatch.setattr(gpu, "_nvidia_device_files_present", lambda: True)

    status = gpu.detect_gpu_status()
    assert status["status"] == "blocked"
    assert [(g.name, g.index, g.vram_mb, g.unified_memory, g.compute_capability) for g in status["gpus"]] == [
        ("NVIDIA GH200 480GB", 0, 0, False, (9, 0)),
    ]
    assert any(i["code"] == "memory-unavailable" and "GH200" in i["message"] for i in status["issues"])
    assert "NVIDIA GH200 480GB" in status["summary"] and "could not be read" in status["summary"]

    facts = pf.detect_platform_facts(use_cache=False)  # gpus=None: goes through gpu.detect_gpus()
    assert facts.gpu_name == "NVIDIA GH200 480GB"
    assert facts.device_class == "workstation"
    assert facts.unified_memory is False
    assert "Spark" not in facts.device_label


def test_dgx_os_detected_via_etc_nvidia_glob(monkeypatch) -> None:
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={"/etc/os-release": _UBUNTU_OS_RELEASE},
        globs={("/etc/nvidia", "dgx-*")},
    )
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GB10")], use_cache=False)
    assert facts.is_dgx_os is True


def test_gb10_in_product_family_is_dgx_spark(monkeypatch) -> None:
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={f"{_DMI_PREFIX}board_vendor": "NVIDIA", f"{_DMI_PREFIX}product_family": "GB10"},
    )
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.device_class == "dgx-spark"


@pytest.mark.parametrize("product_name", ["NVIDIA DGX Spark", "NVIDIA_DGX_Spark", "DGX-Spark"])
def test_rule1_dmi_spark_match_is_separator_safe(monkeypatch, product_name: str) -> None:
    """R3: some firmware joins the tokens with ``_`` or ``-``. A literal "DGX SPARK" substring
    test let ``NVIDIA_DGX_Spark`` miss rule 1 and land in rule 3 as a plain ``dgx`` with
    ``unified_memory=False`` — on a 128 GB unified box, with no GPU row to rescue it."""
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={
            "/etc/os-release": _DGX_OS_RELEASE,
            f"{_DMI_PREFIX}board_vendor": "NVIDIA",
            f"{_DMI_PREFIX}product_name": product_name,
        },
        exists={"/etc/dgx-release"},
    )
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert (facts.device_class, facts.unified_memory) == ("dgx-spark", True)
    assert facts.device_label == "NVIDIA DGX Spark (GB10, 128 GB unified)"


@pytest.mark.parametrize("field", ["board_name", "product_name", "product_family"])
@pytest.mark.parametrize("spelling", ["NVIDIA DGX Spark", "NVIDIA_DGX_Spark", "DGX-Spark", "dgx_spark"])
def test_rule1_uses_the_shared_spark_regex_on_every_dmi_field(field: str, spelling: str) -> None:
    host = pf.HostProbe(os="linux", arch="x86_64", machine="x86_64", is_dgx_hardware=True, **{field: spelling})
    assert pf.classify(host, "", False) == ("dgx-spark", True)
    assert env.DGX_SPARK_RE.search(spelling) is not None  # the same predicate, not a second copy


def test_rule1_does_not_take_dgx_station() -> None:
    """The negative: ``DGX Station`` is DGX hardware (rule 3), never a Spark."""
    host = pf.HostProbe(
        os="linux", arch="x86_64", machine="x86_64", product_name="NVIDIA DGX Station", is_dgx_hardware=True,
    )
    assert pf.classify(host, "", False) == ("dgx", False)
    assert pf.classify(host, _H100, False) == ("dgx", False)
    assert env.DGX_SPARK_RE.search("NVIDIA DGX Station") is None


# ---------------------------------------------------------------------------
# (b) x86 workstation / laptop
# ---------------------------------------------------------------------------


def test_x86_workstation_rtx_4090(monkeypatch) -> None:
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="x86_64",
        files={
            "/etc/os-release": _UBUNTU_OS_RELEASE,
            f"{_DMI_PREFIX}board_vendor": "ASUSTeK COMPUTER INC.",
            f"{_DMI_PREFIX}product_name": "System Product Name",
            f"{_DMI_PREFIX}chassis_type": "3",
        },
        memory=(64.0, 40.0),
    )
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GeForce RTX 4090", 24576)], use_cache=False)

    assert facts.device_class == "workstation"
    assert facts.unified_memory is False
    assert facts.is_dgx_os is False
    assert facts.is_cloud is False
    assert facts.arch == "x86_64"
    assert facts.device_label == "Workstation (linux/x86_64; NVIDIA GeForce RTX 4090)"


@pytest.mark.parametrize("chassis", ["8", "9", "10", "14"])
def test_laptop_by_chassis_type(monkeypatch, chassis: str) -> None:
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="x86_64",
        files={f"{_DMI_PREFIX}chassis_type": chassis},
    )
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GeForce RTX 4070 Laptop GPU", 8192)], use_cache=False)
    assert facts.device_class == "laptop"


def test_laptop_by_battery(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="linux", machine="x86_64", battery=True)
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.device_class == "laptop"


def test_desktop_with_wireless_mouse_battery_is_not_a_laptop(monkeypatch) -> None:
    """P5, end to end: a Logitech receiver registers ``hidpp_battery_0`` with type Battery."""
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="x86_64",
        files={f"{_DMI_PREFIX}chassis_type": "3"},
        power_supply={
            "AC": {"type": "Mains"},
            "hidpp_battery_0": {"type": "Battery", "scope": "Device"},
        },
    )
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GeForce RTX 4090", 24576)], use_cache=False)
    assert facts.device_class == "workstation"


def test_has_battery_ignores_peripheral_batteries(monkeypatch) -> None:
    supplies = {
        "AC": {"type": "Mains"},
        "hidpp_battery_0": {"type": "Battery", "scope": "Device"},          # wireless mouse
        "sony_controller_battery_a1": {"type": "Battery", "scope": "Device"},  # gamepad
    }
    files = {f"{_PS_DIR}/{n}/{a}": v for n, attrs in supplies.items() for a, v in attrs.items()}
    monkeypatch.setattr(pf, "_list_dir", lambda d: sorted(supplies) if d == _PS_DIR else [])
    monkeypatch.setattr(pf, "_read_text", lambda p: files.get(p, ""))
    assert pf._has_battery() is False


@pytest.mark.parametrize("scope", ["System", ""], ids=["scope=System", "no-scope-attribute"])
def test_has_battery_accepts_system_battery(monkeypatch, scope: str) -> None:
    supplies = {"AC": {"type": "Mains"}, "BAT0": {"type": "Battery"}}
    if scope:
        supplies["BAT0"]["scope"] = scope
    files = {f"{_PS_DIR}/{n}/{a}": v for n, attrs in supplies.items() for a, v in attrs.items()}
    monkeypatch.setattr(pf, "_list_dir", lambda d: sorted(supplies) if d == _PS_DIR else [])
    monkeypatch.setattr(pf, "_read_text", lambda p: files.get(p, ""))
    assert pf._has_battery() is True


def test_has_battery_false_when_sysfs_missing(monkeypatch) -> None:
    monkeypatch.setattr(pf, "_list_dir", lambda d: [])
    assert pf._has_battery() is False


# ---------------------------------------------------------------------------
# (c) RTX Spark (Windows on Arm) — provisional
# ---------------------------------------------------------------------------


def test_windows_arm64_with_nvidia_gpu_is_rtx_spark(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="windows", machine="ARM64", can_sudo=True, in_sudo_group=True)
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GB10")], use_cache=False)

    # GB10 name wins first — still dgx-spark by rule 1. Use an unknown name for rule 2.
    assert facts.device_class == "dgx-spark"

    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA RTX Spark GPU")], use_cache=False)
    assert facts.device_class == "rtx-spark"
    # The GPUInfo row said discrete, so the platform block does too (rule 2, #136).
    assert facts.unified_memory is False
    assert facts.windows_on_arm is True
    assert facts.os == "windows"
    assert facts.arch == "arm64"
    assert facts.distro == ""  # no os-release on Windows
    assert facts.can_sudo is True  # elevated process
    assert "provisional" in facts.device_label
    assert "Windows on Arm" in facts.device_label
    assert "discrete VRAM reported" in facts.device_label


def test_rtx_spark_unified_memory_follows_the_gpu_row(monkeypatch) -> None:
    """#136: until RTX Spark hardware ships and its GPU name is known, the platform
    block must agree with recommend_models / check_oom_risk, which read
    ``GPUInfo.unified_memory`` — never "unified" in the prompt and "discrete" in the budget."""
    from nvh.utils.gpu import check_oom_risk

    _simulate(monkeypatch, os_name="windows", machine="ARM64", memory=(64.0, 48.0))
    discrete = [_gpu("NVIDIA RTX Spark GPU", 32768, unified=False)]
    unified = [_gpu("NVIDIA RTX Spark GPU", 65536, unified=True)]

    facts_d = pf.detect_platform_facts(gpus=discrete, use_cache=False)
    facts_u = pf.detect_platform_facts(gpus=unified, use_cache=False)
    assert facts_d.device_class == facts_u.device_class == "rtx-spark"  # class stays provisional either way
    assert facts_d.unified_memory is False
    assert facts_u.unified_memory is True
    assert "64 GB unified" in facts_u.device_label
    assert "unified" not in facts_d.device_label
    assert facts_d.unified_memory == check_oom_risk(8.0, discrete)["unified_memory"]
    assert facts_u.unified_memory == check_oom_risk(8.0, unified)["unified_memory"]


def test_windows_arm64_without_gpu_is_not_rtx_spark(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="windows", machine="ARM64")
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.device_class == "workstation"
    assert facts.windows_on_arm is True
    assert facts.unified_memory is False


def test_windows_x64_with_gpu_is_workstation(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="windows", machine="AMD64")
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GeForce RTX 5090", 32768)], use_cache=False)
    assert facts.device_class == "workstation"
    assert facts.arch == "x86_64"
    assert facts.windows_on_arm is False


def test_machine_and_arch_come_from_hw_ids() -> None:
    """P7: one implementation of the Windows-on-Arm / GB10 / arch logic, in hw_ids."""
    assert pf.detect_machine is hw_ids.detect_machine
    assert pf.normalize_arch is hw_ids.normalize_arch
    assert pf.is_gb10_name is hw_ids.is_gb10_name
    assert not hasattr(pf, "_detect_machine")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
        ("ARM64", "arm64"),
        ("AMD64", "x86_64"),
        ("x86_64", "x86_64"),
        ("riscv64", "riscv64"),
        ("", "unknown"),
    ],
)
def test_arch_is_normalized(monkeypatch, raw: str, expected: str) -> None:
    _simulate(monkeypatch, os_name="linux", machine=raw)
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.arch == expected
    assert facts.machine == raw


# ---------------------------------------------------------------------------
# (d) cloud desktop — local signals only on the request path
# ---------------------------------------------------------------------------


def test_cloud_desktop_classification_end_to_end_without_network(monkeypatch) -> None:
    """P8(a): NVIDIA board vendor, no DGX → cloud desktop, from DMI alone, with no subprocess.

    Nothing about the cloud answer is monkeypatched here: ``_cloud_local`` and
    ``environment.is_dgx_hardware`` both read the same fake DMI.
    """
    spawned = _forbid_subprocess(monkeypatch)
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="x86_64",
        files={
            "/etc/os-release": _UBUNTU_OS_RELEASE,
            f"{_DMI_PREFIX}board_vendor": "NVIDIA",
        },
        memory=(64.0, 40.0),
    )
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA L40S", 46068)], use_cache=False)
    assert facts.device_class == "cloud-desktop"
    assert facts.is_cloud is True
    assert facts.cloud_provider == "cloud_desktop"
    assert facts.unified_memory is False
    assert "NVIDIA cloud Linux desktop" in facts.device_label
    assert spawned == []


@pytest.mark.parametrize(
    "dmi, provider",
    [
        ({"sys_vendor": "Amazon EC2", "product_name": "g5.xlarge"}, "aws"),
        ({"bios_version": "4.2.amazon", "sys_vendor": "Xen"}, "aws"),
        ({"sys_vendor": "Google", "product_name": "Google Compute Engine"}, "gcp"),
        (
            {
                "sys_vendor": "Microsoft Corporation",
                "product_name": "Virtual Machine",
                "chassis_asset_tag": "7783-7084-3265-9085-8269-3286-77",
            },
            "azure",
        ),
    ],
    ids=["aws-nitro", "aws-xen", "gcp", "azure"],
)
def test_cloud_from_dmi_markers_without_network(monkeypatch, dmi: dict[str, str], provider: str) -> None:
    spawned = _forbid_subprocess(monkeypatch)
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="x86_64",
        files={f"{_DMI_PREFIX}{k}": v for k, v in dmi.items()},
    )
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA A10G", 23028)], use_cache=False)
    assert facts.device_class == "cloud-desktop"
    assert facts.cloud_provider == provider
    assert spawned == []


def test_cloud_from_provider_env_var_without_network(monkeypatch) -> None:
    spawned = _forbid_subprocess(monkeypatch)
    _simulate(monkeypatch, os_name="linux", machine="x86_64")
    monkeypatch.setenv("RUNPOD_POD_ID", "abc123")
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA A100 80GB PCIe", 81920)], use_cache=False)
    assert facts.device_class == "cloud-desktop"
    assert facts.cloud_provider == "runpod"
    assert "RunPod" in facts.device_label
    assert spawned == []


def test_local_hyperv_vm_is_not_cloud(monkeypatch) -> None:
    """Hyper-V's vendor strings alone are not Azure — only the asset tag is."""
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="x86_64",
        files={f"{_DMI_PREFIX}sys_vendor": "Microsoft Corporation", f"{_DMI_PREFIX}product_name": "Virtual Machine"},
    )
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.is_cloud is False
    assert facts.device_class == "workstation"


def test_gb10_beats_cloud_signal(monkeypatch) -> None:
    """Even if some cloud heuristic fired, a GB10 is a DGX Spark, not a rented desktop."""
    _simulate(monkeypatch, os_name="linux", machine="aarch64", cloud=(True, "cloud_desktop"))
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GB10")], use_cache=False)
    assert facts.device_class == "dgx-spark"


def test_dgx_non_spark(monkeypatch) -> None:
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="x86_64",
        files={
            f"{_DMI_PREFIX}board_vendor": "NVIDIA",
            f"{_DMI_PREFIX}product_name": "NVIDIA DGX H100",
        },
        memory=(2048.0, 1800.0),
    )
    facts = pf.detect_platform_facts(gpus=[_gpu(_H100, 81920)], use_cache=False)
    assert facts.device_class == "dgx"
    assert facts.unified_memory is False
    assert facts.is_cloud is False
    assert facts.device_label == "NVIDIA DGX H100"


def test_rule3_uses_environment_is_dgx_hardware(monkeypatch) -> None:
    """P7: the DGX/GB10 DMI match lives in environment.is_dgx_hardware — not re-implemented here."""
    _simulate(monkeypatch, os_name="linux", machine="x86_64")
    monkeypatch.setattr(env, "is_dgx_hardware", lambda: True)
    facts = pf.detect_platform_facts(gpus=[_gpu(_H100, 81920)], use_cache=False)
    assert facts.device_class == "dgx"


# ---------------------------------------------------------------------------
# P1: no network on the request path; warm_platform_facts() probes once
# ---------------------------------------------------------------------------


def test_request_path_spawns_no_subprocess(monkeypatch) -> None:
    """P8(c): with probe_network=False nothing forks — not curl, not hostname, not sudo, not id."""
    spawned = _forbid_subprocess(monkeypatch)
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="x86_64",
        files={"/etc/os-release": _UBUNTU_OS_RELEASE, f"{_DMI_PREFIX}board_vendor": "NVIDIA"},
        patch_privileges=False,  # the real group/sudo logic, backed by a fake group DB
    )
    _fake_group_db(
        monkeypatch,
        groups={"ccooper": (1000, []), "users": (100, []), "sudo": (27, ["someone-else"])},
        process_gids=[1000, 100],
    )

    provisional = pf.detect_platform_facts(gpus=[], use_cache=True)  # cold cache: the probe runs on a thread
    _join_host_probe_threads()
    cached = pf.detect_platform_facts(gpus=[], use_cache=True)
    fresh = pf.detect_platform_facts(gpus=[], use_cache=False)
    pf.probe_host(use_cache=False)

    assert spawned == []
    assert provisional.host_probe_pending is True and cached.host_probe_pending is False
    assert provisional.device_class == cached.device_class == fresh.device_class == "cloud-desktop"
    assert cached.in_sudo_group is False
    assert cached.can_sudo is False


def test_warm_platform_facts_probes_network_once_then_serves_cache(monkeypatch) -> None:
    """The metadata probe runs exactly once, in warm_platform_facts(); afterwards every call is cached."""
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="x86_64",
        files={"/etc/os-release": _UBUNTU_OS_RELEASE, f"{_DMI_PREFIX}board_vendor": "Supermicro"},
    )
    monkeypatch.setattr(pf, "_primary_gpu", lambda gpus: ("NVIDIA A10G", False))
    # environment.detect_cloud_provider's *inputs*: pretend the metadata endpoint answered.
    network_calls: list[str] = []
    monkeypatch.setattr(env, "_detect_platform", lambda: "linux")
    monkeypatch.setattr(env, "_detect_aws", lambda: network_calls.append("aws") or (True, "g5.xlarge", "203.0.113.7"))
    monkeypatch.setattr(env, "_detect_gcp", lambda: (False, "unknown", ""))
    monkeypatch.setattr(env, "_detect_azure", lambda: (False, "unknown", ""))

    before = pf.detect_platform_facts()
    assert before.is_cloud is False  # request path: local signals only
    assert network_calls == []

    warmed = pf.warm_platform_facts()
    assert warmed.is_cloud is True
    assert warmed.cloud_provider == "aws"
    assert warmed.device_class == "cloud-desktop"
    assert network_calls == ["aws"]

    # Facts built before the warm-up are invalidated; later calls see the upgrade without re-probing.
    after = pf.detect_platform_facts()
    assert after.is_cloud is True
    pf.detect_platform_facts(probe_network=True)
    pf.detect_platform_facts(gpus=[], use_cache=True)
    pf.warm_platform_facts()
    assert network_calls == ["aws"]


def test_network_probe_skipped_when_local_signals_already_say_cloud(monkeypatch) -> None:
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="x86_64",
        files={f"{_DMI_PREFIX}sys_vendor": "Amazon EC2"},
    )
    monkeypatch.setattr(pf, "_primary_gpu", lambda gpus: ("", False))
    monkeypatch.setattr(pf, "_cloud_network", lambda: pytest.fail("network probe must not run"))
    warmed = pf.warm_platform_facts()
    assert warmed.cloud_provider == "aws"
    assert pf.probe_host().network_probed is True


def test_network_probe_not_run_off_linux(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="windows", machine="AMD64")
    monkeypatch.setattr(pf, "_primary_gpu", lambda gpus: ("", False))
    monkeypatch.setattr(pf, "_cloud_network", lambda: pytest.fail("network probe must not run on Windows"))
    assert pf.warm_platform_facts().is_cloud is False


def test_probe_host_without_cache_does_not_populate_cache(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="linux", machine="x86_64")
    monkeypatch.setattr(pf, "_cloud_network", lambda: (False, ""))
    probe = pf.probe_host(use_cache=False, probe_network=True)
    assert probe.network_probed is True
    assert pf._host_cache is None


# ---------------------------------------------------------------------------
# G4: the request path never waits on a host probe another thread is running
# ---------------------------------------------------------------------------


def _slow_privilege_probe(monkeypatch, *, answer: bool = True) -> tuple[threading.Event, threading.Event, list[int]]:
    """Make ``_in_sudo_group`` block until released — the NSS / ``id -Gn`` stall, under test control."""
    started, release, calls = threading.Event(), threading.Event(), []

    def slow(_os_name: str) -> bool:
        calls.append(1)
        started.set()
        assert release.wait(5), "test never released the probe"
        return answer

    monkeypatch.setattr(pf, "_in_sudo_group", slow)
    return started, release, calls


def _spark_host(monkeypatch) -> None:
    _simulate(
        monkeypatch,
        os_name="linux",
        machine="aarch64",
        files={"/etc/os-release": _DGX_OS_RELEASE, f"{_DMI_PREFIX}product_name": "NVIDIA DGX Spark"},
        exists={"/etc/dgx-release"},
        in_sudo_group=True,
        can_sudo=True,
    )
    monkeypatch.setattr(pf, "_primary_gpu", lambda gpus: ("NVIDIA GB10", True))


def test_request_path_answers_at_once_while_the_host_probe_is_in_flight(monkeypatch) -> None:
    """The warm-up thread is inside a 2 s group lookup; a request must not queue behind it on the
    module lock. It gets a provisional answer — the right OS/arch/device_class from DMI and the
    GPU name, privilege fields False, ``host_probe_pending`` — that is never cached."""
    _spark_host(monkeypatch)
    started, release, calls = _slow_privilege_probe(monkeypatch)
    warmup = threading.Thread(target=pf.probe_host, name="fake-warmup", daemon=True)
    warmup.start()
    assert started.wait(2)
    try:
        t0 = time.perf_counter()
        facts = pf.detect_platform_facts()
        elapsed = time.perf_counter() - t0
    finally:
        release.set()
        warmup.join(5)
    assert not warmup.is_alive()

    assert elapsed < 0.1, f"request path waited {elapsed:.3f}s on the probe"
    assert (facts.os, facts.arch, facts.device_class, facts.unified_memory) == ("linux", "arm64", "dgx-spark", True)
    assert facts.gpu_name == "NVIDIA GB10"
    assert facts.host_probe_pending is True
    assert facts.can_sudo is False and facts.in_sudo_group is False
    assert pf._facts_cache is None                      # provisional facts are not cached
    assert calls == [1]                                 # the request did not start a second probe

    settled = pf.detect_platform_facts()
    assert settled.host_probe_pending is False
    assert settled.in_sudo_group is True and settled.can_sudo is True
    assert pf.detect_platform_facts() is settled        # the full answer is what gets cached


def test_blocking_callers_share_the_one_probe_in_flight(monkeypatch) -> None:
    """probe_host() callers that may wait queue on the in-flight probe instead of each spawning
    their own NSS / sudo probes."""
    _spark_host(monkeypatch)
    started, release, calls = _slow_privilege_probe(monkeypatch)
    results: list[pf.HostProbe | None] = []
    threads = [threading.Thread(target=lambda: results.append(pf.probe_host()), daemon=True) for _ in range(3)]
    for t in threads:
        t.start()
    assert started.wait(2)
    release.set()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads)

    assert calls == [1]
    assert len(results) == 3 and all(r is results[0] for r in results)
    assert results[0] is not None and results[0].in_sudo_group is True


def test_probe_publish_is_double_checked_against_a_seed(monkeypatch) -> None:
    """A probe that started before seed_platform_facts() must not overwrite the seed when it lands."""
    _spark_host(monkeypatch)
    started, release, _calls = _slow_privilege_probe(monkeypatch)
    warmup = threading.Thread(target=pf.probe_host, daemon=True)
    warmup.start()
    assert started.wait(2)
    pf.seed_platform_facts(_SEED)
    release.set()
    warmup.join(5)
    assert not warmup.is_alive()

    host = pf.probe_host()
    assert host is not None
    assert host.network_probed is True                                          # the seed...
    assert (host.os, host.arch, host.in_sudo_group) == ("linux", "x86_64", False)  # ...not the slower local probe
    assert pf.detect_platform_facts() is _SEED


def test_probe_finishing_after_a_clear_does_not_resurrect_the_cache(monkeypatch) -> None:
    _spark_host(monkeypatch)
    started, release, calls = _slow_privilege_probe(monkeypatch)
    warmup = threading.Thread(target=pf.probe_host, daemon=True)
    warmup.start()
    assert started.wait(2)
    pf.clear_platform_facts_cache()
    release.set()
    warmup.join(5)
    assert not warmup.is_alive()

    assert pf._host_cache is None
    # The next caller runs a fresh probe rather than being served the stale one.
    assert pf.probe_host().in_sudo_group is True
    assert calls == [1, 1]


def test_cheap_probe_spawns_nothing_and_skips_the_privilege_probes(monkeypatch) -> None:
    """What the request path answers from while a probe is in flight: file reads only."""
    spawned = _forbid_subprocess(monkeypatch)
    _spark_host(monkeypatch)
    monkeypatch.setattr(pf, "_in_sudo_group", lambda _os: pytest.fail("group lookup ran in the cheap probe"))
    monkeypatch.setattr(pf, "_can_sudo", lambda _os: pytest.fail("sudo -n ran in the cheap probe"))

    probe = pf._probe_host_cheap()

    assert spawned == []
    assert (probe.os, probe.arch, probe.is_dgx_os, probe.is_dgx_hardware) == ("linux", "arm64", True, True)
    assert probe.product_name == "NVIDIA DGX Spark"
    assert (probe.in_sudo_group, probe.can_sudo, probe.network_probed) == (False, False, False)
    assert pf.classify(probe, "NVIDIA GB10", True) == ("dgx-spark", True)


# ---------------------------------------------------------------------------
# D1: a cold cache never makes the request path run the slow probe inline
# ---------------------------------------------------------------------------


def _probe_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "nvh-host-probe"]


def test_cold_cache_request_starts_the_probe_on_a_thread_and_answers_at_once(monkeypatch) -> None:
    """With no warm-up (NVH_PLATFORM_WARMUP=0, or a request that beats the warm-up thread to
    the lock) the first request found the cache cold, *claimed* the probe and ran the NSS /
    ``id -Gn`` / ``sudo -n -k`` lookups inline — up to 2 s each — instead of answering
    provisionally like a request that finds a probe already in flight. The claim is now handed
    to a daemon thread: the request gets the cheap facts at once, a second request while the
    thread runs starts nothing, and the full answer lands behind them."""
    _spark_host(monkeypatch)
    started, release, calls = _slow_privilege_probe(monkeypatch)

    t0 = time.perf_counter()
    facts = pf.detect_platform_facts()
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.1, f"cold-cache request ran the slow probe inline ({elapsed:.3f}s)"
    assert facts.host_probe_pending is True
    assert (facts.os, facts.arch, facts.device_class, facts.unified_memory) == ("linux", "arm64", "dgx-spark", True)
    assert facts.gpu_name == "NVIDIA GB10"
    assert facts.can_sudo is False and facts.in_sudo_group is False
    assert pf._facts_cache is None                      # provisional facts are not cached
    assert started.wait(2), "the probe did not start on a background thread"
    (worker,) = _probe_threads()
    assert worker.daemon is True

    # A second non-blocking caller while the thread runs: no second probe, no wait.
    t0 = time.perf_counter()
    assert pf.probe_host(block=False) is None
    again = pf.detect_platform_facts()
    assert time.perf_counter() - t0 < 0.1
    assert again.host_probe_pending is True
    assert calls == [1]
    assert _probe_threads() == [worker]

    release.set()
    _join_host_probe_threads()
    settled = pf.detect_platform_facts()
    assert settled.host_probe_pending is False
    assert settled.in_sudo_group is True and settled.can_sudo is True
    assert pf.detect_platform_facts() is settled        # the full answer is what gets cached
    assert calls == [1]                                 # one probe for the whole sequence


def test_blocking_caller_still_runs_the_cold_probe_inline(monkeypatch) -> None:
    """block=True (the warm-up, use_cache=False callers) keeps the synchronous path: the caller
    runs the probe itself, returns the full answer and leaves no thread behind."""
    _spark_host(monkeypatch)
    host = pf.probe_host()
    assert host is not None and host.in_sudo_group is True and host.can_sudo is True
    assert _probe_threads() == []
    assert pf.probe_host(block=False) is host           # warm cache: the non-blocking path answers too


def test_blocking_caller_waits_for_the_thread_a_request_started(monkeypatch) -> None:
    """The warm-up arriving after a cold request queues on that request's probe thread rather
    than running a second probe."""
    _spark_host(monkeypatch)
    started, release, calls = _slow_privilege_probe(monkeypatch)
    assert pf.detect_platform_facts().host_probe_pending is True
    assert started.wait(2)

    results: list[pf.HostProbe | None] = []
    waiter = threading.Thread(target=lambda: results.append(pf.probe_host()), daemon=True)
    waiter.start()
    release.set()
    waiter.join(5)
    assert not waiter.is_alive()
    _join_host_probe_threads()

    assert calls == [1]
    assert results and results[0] is not None and results[0].in_sudo_group is True
    assert pf.probe_host() is results[0]


def test_background_probe_finishing_after_a_clear_does_not_resurrect_the_cache(monkeypatch) -> None:
    """Same double-checked publish as the inline path: a clear while the thread runs wins."""
    _spark_host(monkeypatch)
    started, release, calls = _slow_privilege_probe(monkeypatch)
    assert pf.detect_platform_facts().host_probe_pending is True
    assert started.wait(2)
    pf.clear_platform_facts_cache()
    release.set()
    _join_host_probe_threads()

    assert pf._host_cache is None
    assert pf.probe_host().in_sudo_group is True        # a fresh probe, not the stale one
    assert calls == [1, 1]


# ---------------------------------------------------------------------------
# P2: `sudo -n true` — gated and at most once per process
# ---------------------------------------------------------------------------


def test_can_sudo_not_probed_when_not_in_sudo_group(monkeypatch) -> None:
    """P8(d): an account outside sudo/admin/wheel would only generate an auth-log failure."""
    _simulate(monkeypatch, os_name="linux", machine="x86_64", in_sudo_group=False)
    sudo_calls: list[str] = []
    monkeypatch.setattr(pf, "_can_sudo", lambda _os: sudo_calls.append(_os) or True)

    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    pf.detect_platform_facts(gpus=[], use_cache=True)
    assert facts.can_sudo is False
    assert facts.in_sudo_group is False
    assert sudo_calls == []


def test_sudo_probe_runs_at_most_once_per_process(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="linux", machine="x86_64", in_sudo_group=True)
    sudo_calls: list[str] = []
    monkeypatch.setattr(pf, "_can_sudo", lambda _os: sudo_calls.append(_os) or True)

    for _ in range(3):
        assert pf.detect_platform_facts(gpus=[], use_cache=False).can_sudo is True
    pf.detect_platform_facts(gpus=[], use_cache=True)
    pf.probe_host(use_cache=False)
    assert sudo_calls == ["linux"]  # use_cache=False recomputes everything else, not this

    pf.clear_platform_facts_cache()
    pf.detect_platform_facts(gpus=[], use_cache=False)
    assert sudo_calls == ["linux", "linux"]


def test_root_can_sudo_without_spawning_sudo(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="linux", machine="x86_64", root=True, in_sudo_group=False)
    monkeypatch.setattr(pf, "_can_sudo", lambda _os: pytest.fail("uid 0 needs no sudo probe"))
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.has_root is True
    assert facts.can_sudo is True
    assert facts.in_sudo_group is True


def test_can_sudo_is_non_interactive(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(pf.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)
    monkeypatch.setattr(pf.subprocess, "run", fake_run)

    assert pf._can_sudo("linux") is False
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/sudo", "-n", "-k", "true"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] <= 2.0

    monkeypatch.setattr(
        pf.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    )
    assert pf._can_sudo("linux") is True


def test_can_sudo_false_when_sudo_missing_or_erroring(monkeypatch) -> None:
    monkeypatch.setattr(pf.shutil, "which", lambda name: None)
    assert pf._can_sudo("linux") is False

    monkeypatch.setattr(pf.shutil, "which", lambda name: "/usr/bin/sudo")

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="sudo", timeout=2)

    monkeypatch.setattr(pf.subprocess, "run", boom)
    assert pf._can_sudo("linux") is False
    assert pf._can_sudo("plan9") is False


def test_can_sudo_probe_ignores_cached_credentials(monkeypatch) -> None:
    """R2: ``sudo -n true`` also succeeds while the user's credential timestamp is still
    valid — they typed a password into another shell minutes ago — and _can_sudo_once
    freezes that transient yes for the whole process. ``-k`` *with a command* makes sudo
    ignore (and not touch) the cached credentials, so success means passwordless sudo."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        # A still-valid timestamp: plain ``-n`` would say yes, ``-n -k`` says no.
        return SimpleNamespace(returncode=1 if "-k" in argv else 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(pf.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)
    monkeypatch.setattr(pf.subprocess, "run", fake_run)

    assert pf._can_sudo("linux") is False
    (argv,) = calls
    assert argv[0] == "/usr/bin/sudo" and argv[-1] == "true"
    assert "-n" in argv and "-k" in argv             # non-interactive AND credential-cache-blind
    assert argv.index("-k") < argv.index("true")     # -k applies to the command, not a bare ``sudo -k``


# ---------------------------------------------------------------------------
# P3: group membership from the group database, not the frozen process token
# ---------------------------------------------------------------------------


def test_in_sudo_group_sees_membership_added_after_login(monkeypatch) -> None:
    """``usermod -aG sudo`` after login: /etc/group says yes, the process token still says no."""
    _fake_group_db(
        monkeypatch,
        groups={"ccooper": (1000, []), "users": (100, []), "sudo": (27, ["ccooper"])},
        process_gids=[1000, 100],
    )
    spawned = _forbid_subprocess(monkeypatch)
    assert pf._in_sudo_group("linux") is True
    assert spawned == []


def test_in_sudo_group_by_primary_gid(monkeypatch) -> None:
    _fake_group_db(monkeypatch, groups={"wheel": (10, [])}, process_gids=[], primary_gid=10)
    assert pf._in_sudo_group("macos") is True


def test_in_sudo_group_false_without_membership(monkeypatch) -> None:
    _fake_group_db(
        monkeypatch,
        groups={"ccooper": (1000, []), "users": (100, []), "sudo": (27, ["someone-else"])},
        process_gids=[1000, 100],
    )
    spawned = _forbid_subprocess(monkeypatch)
    assert pf._in_sudo_group("linux") is False
    assert spawned == []


def test_in_sudo_group_falls_back_to_process_token(monkeypatch) -> None:
    """NSS setups that hide gr_mem (sssd ignore_group_members): the token still counts."""
    _fake_group_db(
        monkeypatch,
        groups={"ccooper": (1000, []), "sudo": (27, [])},
        process_gids=[1000, 27],
    )
    assert pf._in_sudo_group("linux") is True


def test_in_sudo_group_falls_back_to_id_command(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "grp", None)  # import grp → ImportError
    monkeypatch.setitem(sys.modules, "pwd", None)
    monkeypatch.setattr(pf, "_run", lambda argv: "ccooper adm cdrom sudo plugdev\n" if argv == ["id", "-Gn"] else "")
    assert pf._in_sudo_group("linux") is True
    monkeypatch.setattr(pf, "_run", lambda argv: "ccooper users\n")
    assert pf._in_sudo_group("linux") is False
    assert pf._in_sudo_group("plan9") is False


def test_in_sudo_group_windows_uses_administrators_sid(monkeypatch) -> None:
    monkeypatch.setattr(pf, "_run", lambda argv: "BUILTIN\\Administrators Alias S-1-5-32-544 Group used for deny only")
    assert pf._in_sudo_group("windows") is True
    monkeypatch.setattr(pf, "_run", lambda argv: "BUILTIN\\Users Alias S-1-5-32-545 Mandatory group")
    assert pf._in_sudo_group("windows") is False


def test_root_implies_in_sudo_group(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="linux", machine="x86_64", root=True)
    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.has_root is True
    assert facts.in_sudo_group is True


# ---------------------------------------------------------------------------
# P4: subprocess output in a non-UTF-8 code page must not become "unknown"
# ---------------------------------------------------------------------------


def test_run_decodes_oem_code_page_output(monkeypatch) -> None:
    """German Windows: ``whoami /groups`` writes cp850; ``ü`` is 0x81, illegal in UTF-8."""
    raw = "BUILTIN\\Administratoren Alias S-1-5-32-544 Gruppe nur für Verweigerungen\n".encode("cp850")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    monkeypatch.setattr(pf.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout=raw, stderr=b""))

    out = pf._run(["whoami", "/groups"])
    assert "S-1-5-32-544" in out
    assert pf._in_sudo_group("windows") is True


def test_run_never_raises_on_undecodable_bytes(monkeypatch) -> None:
    monkeypatch.setattr(pf, "_output_encodings", lambda: ["utf-8", "ascii"])
    raw = b"\xff\xfe\x81 S-1-5-32-544 \x81"
    monkeypatch.setattr(pf.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=0, stdout=raw, stderr=b""))
    out = pf._run(["whoami", "/groups"])
    assert "S-1-5-32-544" in out
    assert pf._decode_output(None) == ""
    assert pf._decode_output("already text") == "already text"


def test_run_returns_empty_when_command_fails(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise FileNotFoundError("whoami")

    monkeypatch.setattr(pf.subprocess, "run", boom)
    assert pf._run(["whoami", "/groups"]) == ""
    assert pf._in_sudo_group("windows") is False


# ---------------------------------------------------------------------------
# environment.py layer: the board_vendor heuristic must be DGX-aware
# ---------------------------------------------------------------------------


def _patch_env_cloud(monkeypatch, dmi: dict[str, str]) -> None:
    monkeypatch.setattr(env, "_read_dmi", lambda key: dmi.get(key, ""))
    monkeypatch.setattr(env, "_detect_aws", lambda: (False, "unknown", ""))
    monkeypatch.setattr(env, "_detect_gcp", lambda: (False, "unknown", ""))
    monkeypatch.setattr(env, "_detect_azure", lambda: (False, "unknown", ""))
    monkeypatch.setattr(env, "_curl_metadata", lambda *a, **k: "")

    def _no_hostname(*args, **kwargs):
        raise OSError("hostname unavailable in test")

    monkeypatch.setattr(env.subprocess, "run", _no_hostname)


def test_environment_board_vendor_nvidia_without_dgx_is_cloud_desktop(monkeypatch) -> None:
    _patch_env_cloud(monkeypatch, {"board_vendor": "NVIDIA"})
    is_cloud, provider, _itype, _ip = env._detect_cloud_provider()
    assert is_cloud is True
    assert provider == "cloud_desktop"


def test_environment_dgx_spark_is_not_cloud(monkeypatch) -> None:
    _patch_env_cloud(monkeypatch, {"board_vendor": "NVIDIA", "product_name": "NVIDIA DGX Spark"})
    assert env.is_dgx_hardware() is True
    is_cloud, provider, _itype, _ip = env._detect_cloud_provider()
    assert is_cloud is False
    assert provider == "unknown"


def test_environment_gb10_board_name_is_dgx_hardware(monkeypatch) -> None:
    _patch_env_cloud(monkeypatch, {"board_vendor": "NVIDIA", "board_name": "GB10"})
    assert env.is_dgx_hardware() is True
    assert env._detect_cloud_provider()[0] is False


def test_environment_detect_cloud_provider_public_wrapper_skips_non_linux(monkeypatch) -> None:
    monkeypatch.setattr(env, "_detect_platform", lambda: "windows")
    called = []
    monkeypatch.setattr(env, "_detect_cloud_provider", lambda: called.append(1) or (True, "aws", "x", ""))
    assert env.detect_cloud_provider() == (False, "unknown", "unknown", "")
    assert called == []


# ---------------------------------------------------------------------------
# Robustness + caching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "os_probe, expected_class",
    [(None, "unknown"), ("linux", "workstation")],
    ids=["os-probe-explodes", "os-known"],
)
def test_detect_platform_facts_survives_every_probe_exploding(monkeypatch, os_probe, expected_class) -> None:
    """P8(b): each probe is isolated — the others' answers survive, including the GPU one."""

    def boom(*args, **kwargs):
        raise RuntimeError("probe exploded")

    for name in (
        "_read_text", "_exists", "_glob_any", "_list_dir", "_cloud_local", "_cloud_network",
        "_can_sudo", "_in_sudo_group", "_has_root", "_has_battery", "_memory_gb", "_kernel",
        "_primary_gpu",
    ):
        monkeypatch.setattr(pf, name, boom)
    monkeypatch.setattr(env, "is_dgx_hardware", boom)
    monkeypatch.setattr(pf, "_detect_os", boom if os_probe is None else (lambda: os_probe))
    # detect_machine is left real: arch must still be detected when everything else fails.

    facts = pf.detect_platform_facts(use_cache=False)
    assert isinstance(facts, pf.PlatformFacts)
    assert facts.device_class == expected_class
    assert facts.arch == hw_ids.normalize_arch(hw_ids.detect_machine())
    assert facts.arch != "unknown"
    assert facts.gpu_name == ""
    assert facts.unified_memory is False
    assert facts.memory_total_gb == 0.0
    assert facts.can_sudo is False
    assert facts.is_cloud is False


def test_host_probe_is_cached_for_the_process_while_memory_refreshes(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="linux", machine="x86_64", files={"/etc/os-release": _UBUNTU_OS_RELEASE})
    os_release_reads: list[str] = []
    real_read = pf._read_text
    monkeypatch.setattr(pf, "_read_text", lambda p: os_release_reads.append(p) or real_read(p) if p == "/etc/os-release" else real_read(p))
    memory_reads: list[int] = []
    monkeypatch.setattr(pf, "_memory_gb", lambda: memory_reads.append(1) or (64.0, 64.0 - len(memory_reads)))
    clock = [1000.0]
    monkeypatch.setattr(pf, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    pf.probe_host()  # the process-lifetime host tier, filled once (blocking, as the warm-up does)
    first = pf.detect_platform_facts(gpus=[], use_cache=True)
    clock[0] += 3600.0  # far beyond any TTL
    second = pf.detect_platform_facts(gpus=[], use_cache=True)
    assert os_release_reads == ["/etc/os-release"]  # host probe: once per process
    assert (first.memory_available_gb, second.memory_available_gb) == (63.0, 62.0)  # volatile: refreshed

    # Without a caller-supplied GPU list, the folded facts are cached for FACTS_CACHE_TTL_S.
    monkeypatch.setattr(pf, "_primary_gpu", lambda gpus: ("", False))
    third = pf.detect_platform_facts()
    assert pf.detect_platform_facts() is third
    clock[0] += pf.FACTS_CACHE_TTL_S + 1
    assert pf.detect_platform_facts() is not third
    assert os_release_reads == ["/etc/os-release"]


def test_detect_platform_facts_caches_host_probe(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="linux", machine="x86_64", in_sudo_group=True)
    sudo_calls: list[int] = []
    monkeypatch.setattr(pf, "_can_sudo", lambda _os: sudo_calls.append(1) or False)

    pf.detect_platform_facts(gpus=[], use_cache=True)  # cold cache: the probe (incl. sudo) runs on a thread
    _join_host_probe_threads()
    pf.detect_platform_facts(gpus=[], use_cache=True)
    assert len(sudo_calls) == 1  # host probe (incl. sudo) cached across calls

    pf.clear_platform_facts_cache()
    pf.detect_platform_facts(gpus=[], use_cache=True)
    _join_host_probe_threads()
    assert len(sudo_calls) == 2


# ---------------------------------------------------------------------------
# The NVIDIA-board-vendor cloud heuristic has one implementation (environment)
# ---------------------------------------------------------------------------


def test_cloud_local_delegates_to_environment_shared_heuristic(monkeypatch) -> None:
    calls: list[tuple[str | None, bool | None]] = []

    def spy(board_vendor=None, *, dgx_hardware=None):
        calls.append((board_vendor, dgx_hardware))
        return dgx_hardware is False

    monkeypatch.setattr(env, "is_nvidia_cloud_desktop_dmi", spy)
    for var, _provider in pf._ENV_CLOUD_MARKERS:
        monkeypatch.delenv(var, raising=False)

    assert pf._cloud_local({"board_vendor": "NVIDIA"}, False) == (True, "cloud_desktop")
    assert pf._cloud_local({"board_vendor": "NVIDIA"}, True) == (False, "")
    assert calls == [("NVIDIA", False), ("NVIDIA", True)]
    # DMI cloud markers and provider env vars still win before the heuristic is consulted.
    assert pf._cloud_local({"sys_vendor": "Amazon EC2", "board_vendor": "NVIDIA"}, False) == (True, "aws")
    assert calls == [("NVIDIA", False), ("NVIDIA", True)]


# ---------------------------------------------------------------------------
# seed_platform_facts — the hermetic path for tests and embedders
# ---------------------------------------------------------------------------

_SEED = pf.PlatformFacts(
    os="linux", arch="x86_64", machine="x86_64", device_class="workstation",
    device_label="Workstation (linux/x86_64; no NVIDIA GPU)",
)
_HOST_PROBES = (
    "_read_text", "_exists", "_glob_any", "_list_dir", "_has_root", "_in_sudo_group", "_can_sudo",
    "_has_battery", "_cloud_local", "_cloud_network", "_detect_os", "detect_machine", "_memory_gb",
)


def _forbid_host_probes(monkeypatch) -> None:
    for name in _HOST_PROBES:
        monkeypatch.setattr(pf, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} ran on a seeded cache"))
    monkeypatch.setattr(env, "is_dgx_hardware", lambda: pytest.fail("is_dgx_hardware ran on a seeded cache"))
    monkeypatch.setattr(env, "detect_cloud_provider", lambda: pytest.fail("network probe ran on a seeded cache"))


def test_seeded_facts_answer_every_entry_point_without_a_probe(monkeypatch) -> None:
    spawned = _forbid_subprocess(monkeypatch)
    _forbid_host_probes(monkeypatch)
    monkeypatch.setattr(pf, "_primary_gpu", lambda gpus: pytest.fail("GPU detection ran on a seeded cache"))

    pf.seed_platform_facts(_SEED)

    assert pf.detect_platform_facts() is _SEED
    assert pf.detect_platform_facts(probe_network=True) is _SEED
    assert pf.warm_platform_facts() is _SEED           # counts as already network-probed
    assert pf.probe_host().network_probed is True
    assert pf.probe_host(probe_network=True).os == "linux"
    assert spawned == []


def test_seeded_host_reclassifies_caller_supplied_gpu_rows(monkeypatch) -> None:
    """wizard_context() passes its own GPU rows: they are classified against the seeded host, no probe."""
    spawned = _forbid_subprocess(monkeypatch)
    _forbid_host_probes(monkeypatch)
    monkeypatch.setattr(pf, "_memory_gb", lambda: (64.0, 40.0))
    pf.seed_platform_facts(_SEED)

    spark = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GB10", unified=True)])
    assert spark.device_class == "dgx-spark"  # rule 1 from the rows...
    assert (spark.os, spark.arch) == ("linux", "x86_64")  # ...host facts from the seed
    box = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GeForce RTX 4090", 24576)])
    assert box.device_class == "workstation"
    assert box.unified_memory is False
    assert box.memory_total_gb == 64.0
    assert spawned == []


def test_seeded_facts_never_go_stale_until_cleared(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(pf, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(pf, "_primary_gpu", lambda gpus: pytest.fail("seeded facts must not be rebuilt"))

    pf.seed_platform_facts(_SEED)
    clock[0] += 24 * 3600.0  # far beyond FACTS_CACHE_TTL_S
    assert pf.detect_platform_facts() is _SEED

    pf.clear_platform_facts_cache()
    assert pf._host_cache is None
    assert pf._facts_cache is None
    assert pf._sudo_probe is None


def test_seeded_can_sudo_short_circuits_the_sudo_probe(monkeypatch) -> None:
    """Even use_cache=False (a fresh host probe) must not spawn ``sudo -n`` on a seeded process."""
    _simulate(monkeypatch, os_name="linux", machine="x86_64", in_sudo_group=True)
    monkeypatch.setattr(pf, "_can_sudo", lambda _os: pytest.fail("sudo -n spawned on a seeded process"))
    pf.seed_platform_facts(_SEED)

    facts = pf.detect_platform_facts(gpus=[], use_cache=False)
    assert facts.in_sudo_group is True
    assert facts.can_sudo is False  # the seeded answer, not a probe


def test_detect_platform_facts_accepts_dict_gpu_rows(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="linux", machine="aarch64")
    facts = pf.detect_platform_facts(gpus=[{"name": "NVIDIA GB10", "vram_gb": 128.0}], use_cache=False)
    assert facts.device_class == "dgx-spark"
    assert facts.gpu_name == "NVIDIA GB10"


def test_to_dict_round_trips_all_fields(monkeypatch) -> None:
    _simulate(monkeypatch, os_name="linux", machine="aarch64")
    facts = pf.detect_platform_facts(gpus=[_gpu("NVIDIA GB10")], use_cache=False)
    data = facts.to_dict()
    for key in (
        "os", "arch", "distro", "kernel", "is_dgx_os", "board_vendor", "board_name",
        "gpu_name", "unified_memory", "memory_total_gb", "memory_available_gb",
        "device_class", "device_label", "has_root", "can_sudo", "in_sudo_group", "windows_on_arm",
    ):
        assert key in data
    assert data["device_class"] in _ALL_DEVICE_CLASSES
