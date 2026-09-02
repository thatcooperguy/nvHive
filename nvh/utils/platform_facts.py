"""Platform facts — *what machine is nvHive running on?*

The GPU probes in :mod:`nvh.utils.gpu` answer "which GPU"; the environment
probes in :mod:`nvh.utils.environment` answer "cloud or not". Neither answers
the question the AI Wizard needs first: *is this a DGX Spark, an RTX Spark, a
rented cloud desktop, or someone's laptop?* — because that decides whether
"VRAM" is really a unified LPDDR5x pool shared with the OS, whether ``sudo``
is even on the table, and which model families to recommend.

:func:`detect_platform_facts` folds those signals into one frozen
:class:`PlatformFacts`. It never raises: every probe is wrapped and falls back
to an empty/False value.

Caching — three tiers, by how often the answer can change
---------------------------------------------------------

* **Process lifetime** — :func:`probe_host`: DMI, os-release, kernel, arch,
  distro, DGX markers, battery presence, uid, group membership, ``sudo -n``
  and the cloud provider. None of these change while the server runs, and
  several are expensive or side-effectful (NSS group lookups can hit LDAP,
  ``sudo -n`` writes to the auth log, cloud metadata is a network round
  trip), so they are asked exactly once — and never under the module lock:
  the probe runs outside it and is published under it (double-checked), so a
  request that lands while the warm-up thread is still inside an NSS lookup
  or ``sudo -n`` is not held up — and a request that finds the cache cold
  with no probe running starts one on a daemon thread rather than paying for
  those lookups inline. Such a request gets a *provisional*
  :class:`PlatformFacts` from :func:`_probe_host_cheap` — file reads only:
  DMI, os-release, arch, the GPU names — with the privilege fields False and
  ``host_probe_pending=True``; it is never cached, so the next call after the
  probe lands sees the full answer. :func:`clear_platform_facts_cache` drops
  them (tests, or after a deliberate privilege change).
* **:data:`FACTS_CACHE_TTL_S`** — the folded :class:`PlatformFacts`, so
  ``memory_available_gb`` (and the GPU list when the caller did not pass one)
  stay fresh.
* **Network, opt-in** — the cloud metadata endpoints (``curl`` to AWS/GCP/
  Azure IMDS, 1.5–2 s each on a box that is not that cloud) and
  ``hostname -f`` run only with ``probe_network=True``. The default is False,
  so request handlers get an answer built from local signals alone: DMI vendor
  strings (EC2 / GCE / the Azure asset tag), provider environment variables
  (RunPod, Vast, Paperspace), NVIDIA as board vendor without DGX hardware, or
  the cached result of an earlier network probe. :func:`warm_platform_facts`
  runs the full probe once; the API server runs it on a daemon thread at
  startup (``NVH_PLATFORM_WARMUP=0`` skips it) so neither readiness, shutdown
  nor the Wizard ever blocks on a metadata timeout.
* **Seeded** — :func:`seed_platform_facts` fills every tier with a caller-
  supplied snapshot and marks the network probe done, so embedders and tests
  that must not spawn ``sudo -n`` / ``curl`` / group lookups get answers
  without any probe until :func:`clear_platform_facts_cache`.

Classification rules — evaluated in order, first match wins
-----------------------------------------------------------

1. ``dgx-spark``   — GPU name is the GB10 token (:func:`nvh.utils.hw_ids.is_gb10_name`,
   underscore-joined spellings included) **or** the DMI board / product /
   family name contains ``DGX Spark`` (:data:`nvh.utils.environment.DGX_SPARK_RE`
   — ``DGX_Spark`` / ``DGX-Spark`` spellings included, ``DGX Station`` excluded)
   or ``GB10``. ``unified_memory=True``.
2. ``rtx-spark``   — Windows **and** arm64 **and** an NVIDIA GPU is present.
   *Provisional* until RTX Spark hardware ships and its GPU name is known
   (#136); the rule will be tightened to a GPU/board-name match then.
   ``unified_memory`` follows the primary :class:`~nvh.utils.gpu.GPUInfo`
   row rather than being asserted: the model recommender and the OOM check
   read the rows, and the platform block must not say "unified" while they
   budget for a discrete card.
3. ``dgx``         — :func:`nvh.utils.environment.is_dgx_hardware` (DMI names
   a DGX): the non-Spark systems — DGX Station, DGX H100, GB200/GB300 nodes.
4. ``dgx-spark``   — DGX OS detected **and** arm64 **and** no GPU name is
   known (or it is GB10, which rule 1 already claimed). This is the path when
   the GPU is not enumerable (NVML blocked in a container, the driver not
   loaded yet) and it must not depend on DMI being empty: OEM GB10 boxes
   carry their own product strings, and containers see the host's. Rule 3
   has already claimed the arm64 DGX systems that are not Sparks, and a
   *visible* non-GB10 GPU — a Grace Hopper node, a Jetson Thor dev kit on a
   DGX-OS-derived image — never yields a Spark. ``unified_memory=True``.
5. ``cloud-desktop`` — ``is_cloud`` (see *Caching* for which signals). Only
   probed on Linux. A DGX / DGX Spark never reaches this rule, and the
   NVIDIA-board-vendor heuristic (:func:`nvh.utils.environment.is_nvidia_cloud_desktop_dmi`)
   is DGX-aware on both the local and the network path.
6. ``laptop``      — DMI ``chassis_type`` in {8, 9, 10, 14} or a *system*
   battery under ``/sys/class/power_supply`` (peripheral batteries with
   ``scope == Device`` — wireless mice, gamepads — do not count).
7. ``workstation`` — everything else with a known OS.
8. ``unknown``     — the OS itself could not be determined.

``unified_memory`` is True for rules 1/4 and otherwise whenever the primary
:class:`~nvh.utils.gpu.GPUInfo` reports ``unified_memory`` (the NVML
system-RAM fallback path) — rule 2 included.

Privileges: ``has_root`` is uid 0. ``can_sudo`` is *non-interactive*
escalation: uid 0, an elevated Windows process, or ``sudo -n -k true``
succeeding (``-k`` makes sudo ignore a still-valid credential timestamp, so
a recent interactive ``sudo`` in another shell cannot fake a passwordless
yes). The ``sudo -n -k`` probe is gated and memoised — see
:func:`_can_sudo_once` for why. On a stock DGX OS box the OOBE user is in the
``sudo`` group but a password is still required, so ``can_sudo`` is usually
False there; ``in_sudo_group`` is the honest "you have sudo, I'll hand you the
command" signal. On Windows ``can_sudo`` means the process is elevated and
``in_sudo_group`` means the user is in Administrators.

Architecture comes from :mod:`nvh.utils.hw_ids` — the one place that knows an
x64 Python under Windows-on-Arm emulation reports ``AMD64`` while the real
ISA leaks through ``PROCESSOR_ARCHITEW6432``.
"""

from __future__ import annotations

import locale
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from nvh.utils import environment
from nvh.utils.hw_ids import detect_machine, is_gb10_name, normalize_arch

logger = logging.getLogger(__name__)

DeviceClass = Literal[
    "dgx-spark", "rtx-spark", "dgx", "cloud-desktop", "workstation", "laptop", "unknown",
]

FACTS_CACHE_TTL_S = 15.0
_LAPTOP_CHASSIS_TYPES = frozenset({"8", "9", "10", "14"})  # portable, laptop, notebook, sub-notebook
_SUDO_GROUPS = frozenset({"sudo", "admin", "wheel"})
_WINDOWS_ADMINISTRATORS_SID = "S-1-5-32-544"
_PROBE_TIMEOUT_S = 2.0
_DMI_DIR = "/sys/class/dmi/id"
_POWER_SUPPLY_DIR = "/sys/class/power_supply"
_DMI_KEYS = (
    "board_vendor", "board_name", "product_name", "product_family", "chassis_type",
    "sys_vendor", "bios_vendor", "bios_version", "chassis_asset_tag",
)

# Local (no-network) cloud markers: (DMI key, lower-cased substring, provider).
# These are the same strings cloud-init and systemd-detect-virt key on.
_DMI_CLOUD_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("sys_vendor", "amazon", "aws"),                      # Nitro: "Amazon EC2"
    ("bios_vendor", "amazon", "aws"),
    ("bios_version", "amazon", "aws"),                    # Xen-era EC2: "4.2.amazon"
    ("chassis_asset_tag", "amazon", "aws"),
    ("product_name", "google compute engine", "gcp"),
    ("sys_vendor", "google", "gcp"),
    ("chassis_asset_tag", "7783-7084-3265-9085-8269-3286-77", "azure"),
)
# Provider-native environment variables set inside their containers.
_ENV_CLOUD_MARKERS: tuple[tuple[str, str], ...] = (
    ("RUNPOD_POD_ID", "runpod"),
    ("VAST_CONTAINERLABEL", "vast_ai"),
    ("PAPERSPACE_CLUSTER_ID", "paperspace"),
    ("PAPERSPACE_NOTEBOOK_REPO_ID", "paperspace"),
)


@dataclass(frozen=True)
class PlatformFacts:
    """Immutable snapshot of the host nvHive is running on."""

    os: str = "unknown"                 # "linux" | "windows" | "macos" | "unknown"
    arch: str = "unknown"               # normalized: "x86_64" | "arm64" | other
    machine: str = ""                   # raw machine string (Windows-on-Arm corrected)
    distro: str = ""                    # PRETTY_NAME from /etc/os-release ("" elsewhere)
    kernel: str = ""                    # platform.release()
    is_dgx_os: bool = False             # DGX OS markers present
    board_vendor: str = ""              # /sys/class/dmi/id/board_vendor
    board_name: str = ""                # /sys/class/dmi/id/board_name
    product_name: str = ""              # /sys/class/dmi/id/product_name
    gpu_name: str = ""                  # primary NVIDIA GPU name ("" when none)
    unified_memory: bool = False        # GPU shares the system RAM pool
    memory_total_gb: float = 0.0        # system RAM (== the GPU pool on unified parts)
    memory_available_gb: float = 0.0    # MemAvailable — the honest "how much can I load"
    device_class: DeviceClass = "unknown"
    device_label: str = ""              # e.g. "NVIDIA DGX Spark (GB10, 128 GB unified)"
    has_root: bool = False              # uid 0
    can_sudo: bool = False              # non-interactive escalation works (see module doc)
    in_sudo_group: bool = False         # member of sudo/admin/wheel (or Windows Administrators)
    windows_on_arm: bool = False
    is_cloud: bool = False
    cloud_provider: str = ""
    # True when the host probe was still running on another thread: the
    # privilege fields (can_sudo / in_sudo_group) are placeholders, not answers.
    host_probe_pending: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Tiny I/O shims — monkeypatch these in tests to simulate hardware
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str:
    try:
        p = Path(path)
        if not p.is_file():
            return ""
        return p.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""


def _exists(path: str) -> bool:
    try:
        return Path(path).exists()
    except Exception:
        return False


def _glob_any(directory: str, pattern: str) -> bool:
    try:
        return any(Path(directory).glob(pattern))
    except Exception:
        return False


def _list_dir(directory: str) -> list[str]:
    """Entry names under ``directory`` (sorted); [] when unreadable or absent."""
    try:
        return sorted(p.name for p in Path(directory).iterdir())
    except Exception:
        return []


def _dmi(key: str) -> str:
    return _read_text(f"{_DMI_DIR}/{key}")


def _output_encodings() -> list[str]:
    """Decode order for subprocess output: UTF-8, then the console/OEM code page, then the locale.

    Windows console tools (``whoami``) write in the OEM code page — cp850,
    cp437, cp932 — not UTF-8 and not even the ANSI page ``locale`` reports.
    """
    candidates = ["utf-8"]
    if sys.platform == "win32":
        try:
            import ctypes

            candidates.append(f"cp{ctypes.windll.kernel32.GetOEMCP()}")  # type: ignore[attr-defined]
        except Exception:
            pass
    try:
        candidates.append(locale.getpreferredencoding(False) or "")
    except Exception:
        pass
    return [c for c in candidates if c]


def _decode_output(raw: bytes | str | None) -> str:
    """Bytes → str without ever raising; ASCII (SIDs, group names) survives any code page."""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for encoding in _output_encodings():
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _run(argv: list[str]) -> str:
    """Run a short read-only command; stdout or "" (never raises, never prompts)."""
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except Exception:
        return ""
    return _decode_output(result.stdout)


# ---------------------------------------------------------------------------
# Individual probes — each returns a safe default on any failure
# ---------------------------------------------------------------------------


def _detect_os() -> str:
    plat = sys.platform
    if plat.startswith("linux"):
        return "linux"
    if plat == "darwin":
        return "macos"
    if plat.startswith("win") or plat == "cygwin":
        return "windows"
    return "unknown"


def _read_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    for line in _read_text("/etc/os-release").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def _is_dgx_os(os_release: dict[str, str]) -> bool:
    if _exists("/etc/dgx-release"):
        return True
    if _glob_any("/etc/nvidia", "dgx-*"):
        return True
    blob = " ".join(os_release.get(k, "") for k in ("ID", "NAME", "PRETTY_NAME")).upper()
    return "DGX" in blob


def _kernel() -> str:
    try:
        return platform.release() or ""
    except Exception:
        return ""


def _has_battery() -> bool:
    """A *system* battery under ``/sys/class/power_supply`` — laptops, not peripherals.

    Wireless mice, keyboards and gamepads (``hidpp_battery_N`` and friends)
    register with ``type == Battery`` too; the kernel marks those
    ``scope == Device``. A laptop battery reports ``scope == System`` or has
    no ``scope`` attribute at all.
    """
    for name in _list_dir(_POWER_SUPPLY_DIR):
        base = f"{_POWER_SUPPLY_DIR}/{name}"
        if _read_text(f"{base}/type").lower() != "battery":
            continue
        if _read_text(f"{base}/scope").lower() == "device":
            continue
        return True
    return False


def _has_root() -> bool:
    try:
        return hasattr(os, "getuid") and os.getuid() == 0  # type: ignore[attr-defined]
    except Exception:
        return False


def _can_sudo(os_name: str) -> bool:
    """Raw non-interactive privilege probe. NEVER prompts; False on any doubt.

    Linux/macOS run ``sudo -n -k true``. ``-n`` refuses to prompt; ``-k``
    *with a command* makes sudo ignore the user's cached credential timestamp
    (and leave it untouched), so the probe cannot succeed merely because the
    user typed a sudo password into another shell a few minutes ago — the
    answer :func:`_can_sudo_once` memoises for the process must mean
    "passwordless sudo", not "recently authenticated". Callers go through
    :func:`_can_sudo_once`; this is the thing it gates.
    """
    try:
        if os_name == "windows":
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        if os_name in ("linux", "macos"):
            sudo = shutil.which("sudo")
            if not sudo:
                return False
            result = subprocess.run(
                [sudo, "-n", "-k", "true"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_PROBE_TIMEOUT_S,
            )
            return result.returncode == 0
    except Exception:
        pass
    return False


_sudo_probe: bool | None = None
_sudo_lock = threading.Lock()


def _can_sudo_once(os_name: str, has_root: bool, in_sudo_group: bool) -> bool:
    """``can_sudo`` with ``sudo -n -k true`` spawned at most once per process.

    Why the gate: ``sudo -n`` run by an account that is *not* in sudoers is an
    authentication failure — it is written to the auth log and, on many
    distros, mailed to root — and an earlier version ran it on every
    host-cache refresh under the API server's own account. So:

    * uid 0 needs no escalation → True, nothing spawned;
    * Windows asks the token (``IsUserAnAdmin``) → no subprocess;
    * an account outside ``sudo``/``admin``/``wheel`` cannot succeed and would
      only generate the failure → False, nothing spawned;
    * a group member is probed **once**, non-interactively (``stdin`` is
      ``DEVNULL``, 2 s timeout, ``-k`` so a still-valid credential timestamp
      cannot fake a passwordless yes), and the answer is memoised for the
      process — ``use_cache=False`` recomputes everything else but not this.
    """
    global _sudo_probe
    if os_name == "windows":
        return _can_sudo(os_name)
    if has_root:
        return True
    if not in_sudo_group:
        return False
    with _sudo_lock:
        if _sudo_probe is None:
            _sudo_probe = _can_sudo(os_name)
        return _sudo_probe


def _group_db_membership(groups: frozenset[str]) -> bool:
    """Is the login user a member of any of ``groups`` per the group database (``grp``)?"""
    import grp
    import pwd

    entry = pwd.getpwuid(os.getuid())  # type: ignore[attr-defined]
    user, primary_gid = entry.pw_name, entry.pw_gid
    for name in groups:
        try:
            group = grp.getgrnam(name)
        except KeyError:
            continue
        if group.gr_gid == primary_gid or user in group.gr_mem:
            return True
    return False


def _process_group_names() -> set[str]:
    """Names of the process's supplementary groups (frozen at login)."""
    import grp

    names: set[str] = set()
    for gid in os.getgroups():  # type: ignore[attr-defined]
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return names


def _in_sudo_group(os_name: str) -> bool:
    """Group membership only — says nothing about whether a password is needed.

    Linux/macOS consult the group database for the login user *first*, not the
    process's supplementary-group list: that list is frozen when the session
    starts, so a user added to ``sudo`` afterwards would otherwise be reported
    False until they log out and back in. The process list and ``id -Gn``
    remain as fallbacks for NSS setups that hide group members.
    """
    if os_name == "windows":
        return _WINDOWS_ADMINISTRATORS_SID in _run(["whoami", "/groups"])
    if os_name not in ("linux", "macos"):
        return False
    try:
        if _group_db_membership(_SUDO_GROUPS):
            return True
        names = _process_group_names()
    except Exception:
        names = set()
    if not names:
        names = set(_run(["id", "-Gn"]).split())
    return bool(names & _SUDO_GROUPS)


def _memory_gb() -> tuple[float, float]:
    """(total_gb, available_gb) of system RAM; MemAvailable-based on Linux."""
    try:
        from nvh.utils.gpu import detect_system_memory

        mem = detect_system_memory()
        return float(mem.total_ram_gb), float(mem.available_ram_gb)
    except Exception:
        return 0.0, 0.0


def _cloud_local(dmi: dict[str, str], is_dgx_hardware: bool) -> tuple[bool, str]:
    """Cloud classification from local signals only — never touches the network.

    DMI vendor strings, provider environment variables, and NVIDIA as the board
    vendor on something that is not DGX hardware (NVIDIA's cloud Linux desktop
    vGPU images). Anything this misses is picked up by the one-time network
    probe in :func:`warm_platform_facts`.
    """
    for key, needle, provider in _DMI_CLOUD_MARKERS:
        if needle in dmi.get(key, "").lower():
            return True, provider
    for var, provider in _ENV_CLOUD_MARKERS:
        if os.environ.get(var, "").strip():
            return True, provider
    # One implementation, shared with environment._detect_cloud_provider.
    if environment.is_nvidia_cloud_desktop_dmi(dmi.get("board_vendor", ""), dgx_hardware=is_dgx_hardware):
        return True, "cloud_desktop"
    return False, ""


def _cloud_network() -> tuple[bool, str]:
    """The slow path: cloud metadata endpoints + hostname heuristics (Linux only)."""
    is_cloud, provider, _itype, _ip = environment.detect_cloud_provider()
    return bool(is_cloud), (provider if is_cloud else "")


def _safe(label: str, fn: Any, default: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("platform_facts: %s probe failed: %s", label, exc)
        return default


# ---------------------------------------------------------------------------
# Host probe (process-lifetime cache) + GPU fold-in + classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostProbe:
    """Everything about the host that does not depend on GPU detection."""

    os: str = "unknown"
    arch: str = "unknown"
    machine: str = ""
    distro: str = ""
    kernel: str = ""
    is_dgx_os: bool = False
    is_dgx_hardware: bool = False       # environment.is_dgx_hardware(): DMI names a DGX / GB10
    board_vendor: str = ""
    board_name: str = ""
    product_name: str = ""
    product_family: str = ""
    chassis_type: str = ""
    has_battery: bool = False
    is_cloud: bool = False
    cloud_provider: str = ""
    has_root: bool = False
    can_sudo: bool = False
    in_sudo_group: bool = False
    network_probed: bool = False        # the metadata/hostname probe has run for this result


_lock = threading.Lock()
_probe_done = threading.Condition(_lock)      # notified when an in-flight host probe finishes
_host_cache: HostProbe | None = None
_host_probe_running = False                    # a thread is inside _probe_host_local(); guarded by _lock
_generation = 0                                # bumped by clear/seed: an older probe never overwrites them
_facts_cache: tuple[float, PlatformFacts, bool] | None = None  # (monotonic, facts, network_probed)


def clear_platform_facts_cache() -> None:
    """Drop every cached probe, including the once-per-process ``sudo -n`` answer."""
    global _host_cache, _facts_cache, _sudo_probe, _generation
    with _lock:
        _host_cache = None
        _facts_cache = None
        _generation += 1
    with _sudo_lock:
        _sudo_probe = None


def seed_platform_facts(facts: PlatformFacts) -> None:
    """Pre-fill every cache tier with ``facts`` as if the full probe had already run.

    For test suites and embedders that must not let nvHive touch ``sudo -n``,
    DMI, NSS group lookups, ``hostname`` or the cloud metadata endpoints.
    After seeding, :func:`probe_host`, :func:`detect_platform_facts` (with or
    without caller-supplied GPU rows) and :func:`warm_platform_facts` are all
    answered from the cache with no subprocess, and the seeded snapshot never
    goes stale — it lasts until :func:`clear_platform_facts_cache`.

    The host tier is derived from ``facts`` so a caller passing its own GPU
    rows is re-classified against the seeded OS/arch/DMI facts; the memoised
    ``sudo -n`` answer is seeded from ``facts.can_sudo`` so even
    ``use_cache=False`` never spawns ``sudo``.
    """
    global _host_cache, _facts_cache, _sudo_probe
    host = HostProbe(
        os=facts.os,
        arch=facts.arch,
        machine=facts.machine,
        distro=facts.distro,
        kernel=facts.kernel,
        is_dgx_os=facts.is_dgx_os,
        is_dgx_hardware=facts.device_class in ("dgx", "dgx-spark"),
        board_vendor=facts.board_vendor,
        board_name=facts.board_name,
        product_name=facts.product_name,
        is_cloud=facts.is_cloud,
        cloud_provider=facts.cloud_provider,
        has_root=facts.has_root,
        can_sudo=facts.can_sudo,
        in_sudo_group=facts.in_sudo_group,
        network_probed=True,
    )
    global _generation
    with _lock:
        _host_cache = host
        # A timestamp in the future: ``now - inf < TTL`` holds forever, so the
        # seeded facts are never rebuilt (which would re-run GPU detection).
        _facts_cache = (float("inf"), facts, True)
        _generation += 1  # a host probe already in flight must not overwrite the seed
    with _sudo_lock:
        _sudo_probe = facts.can_sudo


def _probe_host_cheap() -> HostProbe:
    """The GPU-independent host facts that cost only file reads — no subprocess, no NSS.

    DMI, os-release, kernel, arch, DGX markers, battery, uid and the local
    cloud markers: everything :func:`classify` needs for ``device_class``.
    The privilege fields stay at their defaults (``in_sudo_group`` /
    ``can_sudo`` False) — those are the probes that can take up to
    :data:`_PROBE_TIMEOUT_S` *each* (group-database lookups that may hit
    LDAP, ``id -Gn`` / ``whoami /groups``, ``sudo -n -k true``), and
    :func:`detect_platform_facts` serves this answer while
    :func:`_probe_host_local` is still running them on another thread.
    """
    os_name = _safe("os", _detect_os, "unknown")
    machine = _safe("machine", detect_machine, "")
    linux = os_name == "linux"
    os_release = _safe("os-release", _read_os_release, {}) if linux else {}
    dmi = {key: _safe("dmi", _dmi, "", key) for key in _DMI_KEYS} if linux else {}
    is_dgx_hardware = _safe("dgx-hardware", environment.is_dgx_hardware, False) if linux else False
    is_cloud, provider = (
        _safe("cloud", _cloud_local, (False, ""), dmi, is_dgx_hardware) if linux else (False, "")
    )
    return HostProbe(
        os=os_name,
        arch=normalize_arch(machine) or "unknown",
        machine=machine,
        distro=os_release.get("PRETTY_NAME", ""),
        kernel=_safe("kernel", _kernel, ""),
        is_dgx_os=_safe("dgx-os", _is_dgx_os, False, os_release) if linux else False,
        is_dgx_hardware=is_dgx_hardware,
        board_vendor=dmi.get("board_vendor", ""),
        board_name=dmi.get("board_name", ""),
        product_name=dmi.get("product_name", ""),
        product_family=dmi.get("product_family", ""),
        chassis_type=dmi.get("chassis_type", ""),
        has_battery=_safe("battery", _has_battery, False) if linux else False,
        is_cloud=is_cloud,
        cloud_provider=provider,
        has_root=_safe("root", _has_root, False),
        network_probed=False,
    )


def _probe_host_local() -> HostProbe:
    """Collect the GPU-independent host facts from local signals only (no network).

    :func:`_probe_host_cheap` plus the privilege probes — the slow, possibly
    side-effectful part (see :func:`_can_sudo_once`).
    """
    probe = _probe_host_cheap()
    in_sudo_group = _safe("sudo-group", _in_sudo_group, False, probe.os)
    return replace(
        probe,
        in_sudo_group=in_sudo_group,
        can_sudo=_safe("sudo", _can_sudo_once, False, probe.os, probe.has_root, in_sudo_group),
    )


def _richer(existing: HostProbe | None, fresh: HostProbe) -> HostProbe:
    """Double-checked publish: a network-probed result that landed meanwhile beats a plain local one."""
    if existing is not None and existing.network_probed and not fresh.network_probed:
        return existing
    return fresh


def _with_network_cloud(probe: HostProbe) -> HostProbe:
    """``probe`` with the network-bound cloud probe folded in (skipped when local signals already said cloud)."""
    if probe.os != "linux" or probe.is_cloud:
        return replace(probe, network_probed=True)
    is_cloud, provider = _safe("cloud-network", _cloud_network, (False, ""))
    return replace(probe, is_cloud=is_cloud, cloud_provider=provider, network_probed=True)


def _run_host_probe(generation: int) -> HostProbe | None:
    """Run the claimed host probe outside the lock and publish it under the lock.

    The caller has already set ``_host_probe_running`` under ``_probe_done``
    and read ``generation``. Publication is double-checked: a result
    :func:`seed_platform_facts` or a network upgrade published meanwhile is
    kept if richer, and a probe that started before
    :func:`clear_platform_facts_cache` (``generation`` moved on) never
    resurrects the cleared cache. Waiters are woken either way, and the flag
    is dropped even if the probe raised, so the next caller can run its own.
    Returns what this caller should answer with.
    """
    global _host_cache, _host_probe_running
    fresh: HostProbe | None = None
    try:
        fresh = _probe_host_local()
    finally:
        with _probe_done:
            _host_probe_running = False
            if fresh is not None and generation == _generation:
                _host_cache = _richer(_host_cache, fresh)
            # Cleared or seeded meanwhile: answer this caller, leave the cache alone.
            probe = _host_cache if _host_cache is not None else fresh
            _probe_done.notify_all()
    return probe


def _run_host_probe_in_background(generation: int) -> None:
    """Thread target for the probe a non-blocking caller claimed: nothing awaits it."""
    try:
        _run_host_probe(generation)
    except Exception as exc:  # pragma: no cover - every probe is already _safe()-wrapped
        logger.debug("platform_facts: background host probe failed: %s", exc)


def probe_host(
    *, use_cache: bool = True, probe_network: bool = False, block: bool = True,
) -> HostProbe | None:
    """Collect the GPU-independent host facts; cached for the life of the process.

    ``probe_network=False`` (the default) never spawns ``curl`` or resolves a
    hostname — cloud classification comes from local signals or from a
    previous network probe still in the cache. ``probe_network=True`` upgrades
    the cached result exactly once.

    Nothing slow ever runs under the module lock, and nothing slow ever runs
    on a non-blocking caller's thread. On a cold cache the first caller marks
    the probe in flight and releases the lock; a blocking caller then runs
    :func:`_probe_host_local` itself, while a ``block=False`` caller (the
    request path, via :func:`detect_platform_facts`) starts it on a daemon
    thread and returns ``None`` at once — it answers from
    :func:`_probe_host_cheap` instead of paying for the NSS / ``id -Gn`` /
    ``sudo -n`` probes inline. Either way the result is published under the
    lock again, double-checked (see :func:`_run_host_probe`). Further callers
    wait for that one probe rather than starting their own; non-blocking ones
    get ``None`` while it runs.
    """
    global _host_cache, _host_probe_running
    if not use_cache:
        probe = _probe_host_local()
        return _with_network_cloud(probe) if probe_network else probe

    run_probe = False
    with _probe_done:
        while True:
            probe = _host_cache
            generation = _generation
            if probe is not None:
                break
            if not _host_probe_running:
                _host_probe_running = run_probe = True
                break
            if not block:
                return None
            _probe_done.wait()

    if run_probe:
        if not block:
            # Cold cache and nobody probing: the non-blocking caller has claimed
            # the probe, but it must not run the privilege probes inline — that
            # is up to three 2 s stalls (NSS group lookup, ``id -Gn`` /
            # ``whoami /groups``, ``sudo -n -k``) on the first request after
            # start-up with NVH_PLATFORM_WARMUP=0, or on the first request that
            # beats the warm-up thread to the lock. Hand the claim to a daemon
            # thread that publishes exactly as an inline probe would, and
            # answer provisionally now.
            try:
                threading.Thread(
                    target=_run_host_probe_in_background,
                    args=(generation,),
                    name="nvh-host-probe",
                    daemon=True,
                ).start()
            except Exception as exc:  # thread limit / interpreter shutdown
                # Never leave the module believing a probe is in flight.
                logger.debug("platform_facts: host probe thread failed to start (%s)", exc)
                with _probe_done:
                    _host_probe_running = False
                    _probe_done.notify_all()
            return None
        probe = _run_host_probe(generation)

    if probe_network and probe is not None and not probe.network_probed:
        upgraded = _with_network_cloud(probe)
        with _lock:
            if generation == _generation and (_host_cache is None or not _host_cache.network_probed):
                _host_cache = upgraded
            probe = _host_cache if generation == _generation and _host_cache is not None else upgraded
    return probe


def _primary_gpu(gpus: list[Any] | None) -> tuple[str, bool]:
    """(name, unified_memory) of the primary GPU; accepts GPUInfo or dict rows."""
    if gpus is None:
        try:
            from nvh.utils.gpu import detect_gpus

            gpus = detect_gpus()
        except Exception:
            gpus = []
    if not gpus:
        return "", False
    primary = gpus[0]
    if isinstance(primary, dict):
        name = str(primary.get("name") or "")
        unified = bool(primary.get("unified_memory", False))
    else:
        name = str(getattr(primary, "name", "") or "")
        unified = bool(getattr(primary, "unified_memory", False))
    return name, unified or is_gb10_name(name)


def classify(host: HostProbe, gpu_name: str, gpu_unified: bool) -> tuple[DeviceClass, bool]:
    """Apply the module-docstring rules. Returns ``(device_class, unified_memory)``."""
    spark_dmi = f"{host.board_name} {host.product_name} {host.product_family}".upper()

    # 1. DGX Spark by GPU or by DMI name. The DMI match is the shared
    #    separator-safe regex: some firmware joins the tokens with "_" or "-"
    #    ("NVIDIA_DGX_Spark"), and a literal "DGX SPARK" substring test let
    #    those fall through to rule 3 as a plain, non-unified ``dgx``.
    if (
        is_gb10_name(gpu_name)
        or environment.DGX_SPARK_RE.search(spark_dmi)
        or is_gb10_name(spark_dmi)
    ):
        return "dgx-spark", True
    # 2. RTX Spark (provisional, #136): Windows on Arm with an NVIDIA GPU. Until
    #    the hardware ships and its GPU name is known, the memory model is the
    #    GPUInfo row's — recommend_models / check_oom_risk read the rows, and the
    #    platform block must agree with them rather than assert "unified".
    if host.os == "windows" and host.arch == "arm64" and gpu_name:
        return "rtx-spark", gpu_unified
    # 3. Any other DGX named by DMI (environment.is_dgx_hardware is the one implementation).
    if host.is_dgx_hardware:
        return "dgx", gpu_unified
    # 4. DGX OS on arm64 is a Spark whatever DMI says — but only while the GPU is
    #    not enumerable (or is a GB10, which rule 1 already took). A visible
    #    non-GB10 GPU on a DGX-OS arm64 box (Grace Hopper node, Jetson Thor dev
    #    kit on a DGX-OS-derived image) is not a Spark and not unified.
    if host.is_dgx_os and host.arch == "arm64" and (not gpu_name or is_gb10_name(gpu_name)):
        return "dgx-spark", True
    # 5–8.
    if host.is_cloud:
        return "cloud-desktop", gpu_unified
    if host.chassis_type.strip() in _LAPTOP_CHASSIS_TYPES or host.has_battery:
        return "laptop", gpu_unified
    if host.os == "unknown":
        return "unknown", gpu_unified
    return "workstation", gpu_unified


_CLOUD_LABELS = {
    "cloud_desktop": "NVIDIA cloud Linux desktop",
    "aws": "AWS",
    "gcp": "Google Cloud",
    "azure": "Azure",
    "lambda": "Lambda",
    "coreweave": "CoreWeave",
    "vast_ai": "Vast.ai",
    "runpod": "RunPod",
    "paperspace": "Paperspace",
    "tensordock": "TensorDock",
}


def _device_label(
    device_class: DeviceClass, host: HostProbe, gpu_name: str, memory_total_gb: float,
    unified: bool = False,
) -> str:
    mem = f"{memory_total_gb:.0f} GB" if memory_total_gb else ""
    gpu = gpu_name or "no NVIDIA GPU"
    if device_class == "dgx-spark":
        return f"NVIDIA DGX Spark (GB10, {mem} unified)" if mem else "NVIDIA DGX Spark (GB10, unified memory)"
    if device_class == "rtx-spark":
        # The label states the memory model the GPU rows reported (see rule 2).
        if unified:
            pool = f"{mem} unified" if mem else "unified memory"
        else:
            pool = "discrete VRAM reported"
        return f"NVIDIA RTX Spark, provisional ({gpu}, {pool}, Windows on Arm)"
    if device_class == "dgx":
        product = host.product_name.strip() or "DGX"
        return product if "NVIDIA" in product.upper() else f"NVIDIA {product}"
    if device_class == "cloud-desktop":
        provider = _CLOUD_LABELS.get(host.cloud_provider, host.cloud_provider or "cloud")
        return f"Cloud GPU desktop ({provider}; {gpu})"
    if device_class in ("laptop", "workstation"):
        kind = "Laptop" if device_class == "laptop" else "Workstation"
        return f"{kind} ({host.os}/{host.arch}; {gpu})"
    return "Unknown platform"


def detect_platform_facts(
    *,
    gpus: list[Any] | None = None,
    use_cache: bool = True,
    probe_network: bool = False,
) -> PlatformFacts:
    """Return the :class:`PlatformFacts` for this host. Never raises.

    ``gpus`` lets a caller that already ran GPU detection pass the rows in
    (``GPUInfo`` objects or their dict form) so the GPU is not probed twice.
    When ``gpus`` is omitted the full result is cached for
    :data:`FACTS_CACHE_TTL_S`; host probes are cached for the process either
    way. ``probe_network`` is False by default — safe to call from a request
    handler; see :func:`warm_platform_facts` for the one place it is True.

    The request path never waits on a host probe, and never runs the slow
    part of one: while the warm-up is still inside the privilege probes — or
    when the cache is cold and this call is what starts the probe, on a
    daemon thread — the answer is built from :func:`_probe_host_cheap` (OS,
    arch, DMI, GPU names → the same ``device_class``), carries
    ``host_probe_pending=True`` with the privilege fields False, and is not
    cached — the next call after the probe lands gets the full result.
    ``use_cache=False`` runs the whole probe inline, as before.
    """
    global _facts_cache
    now = time.monotonic()
    with _lock:
        cached = _facts_cache
        generation = _generation
    if gpus is None and use_cache:
        if cached is not None and now - cached[0] < FACTS_CACHE_TTL_S and (cached[2] or not probe_network):
            return cached[1]

    network_probed = False
    provisional = False
    try:
        # Only the warm-up path (probe_network=True) may wait for a probe in flight.
        host = probe_host(use_cache=use_cache, probe_network=probe_network, block=probe_network)
        if host is None:
            host = _probe_host_cheap()
            provisional = True
        network_probed = host.network_probed
        gpu_name, gpu_unified = _safe("gpu", _primary_gpu, ("", False), gpus)
        device_class, unified = classify(host, gpu_name, gpu_unified)
        memory_total_gb, memory_available_gb = _safe("memory", _memory_gb, (0.0, 0.0))
        facts = PlatformFacts(
            os=host.os,
            arch=host.arch,
            machine=host.machine,
            distro=host.distro,
            kernel=host.kernel,
            is_dgx_os=host.is_dgx_os,
            board_vendor=host.board_vendor,
            board_name=host.board_name,
            product_name=host.product_name,
            gpu_name=gpu_name,
            unified_memory=unified,
            memory_total_gb=round(memory_total_gb, 1),
            memory_available_gb=round(memory_available_gb, 1),
            device_class=device_class,
            device_label=_device_label(device_class, host, gpu_name, memory_total_gb, unified),
            has_root=host.has_root,
            can_sudo=host.can_sudo,
            in_sudo_group=host.in_sudo_group or host.has_root or host.can_sudo,
            windows_on_arm=(host.os == "windows" and host.arch == "arm64"),
            is_cloud=host.is_cloud,
            cloud_provider=host.cloud_provider,
            host_probe_pending=provisional,
        )
    except Exception as exc:  # pragma: no cover - belt and braces
        logger.debug("platform_facts: detection failed: %s", exc)
        facts = PlatformFacts()

    # Provisional facts are never cached (they would pin can_sudo=False for a
    # TTL after the probe landed), and a cache cleared or seeded meanwhile is
    # not overwritten by facts built against the old host probe.
    if gpus is None and use_cache and not provisional:
        with _lock:
            if generation == _generation:
                _facts_cache = (now, facts, network_probed)
    return facts


def warm_platform_facts() -> PlatformFacts:
    """Run the full probe once — including the network-bound cloud metadata check.

    Meant for the API server's startup, on a dedicated daemon thread that
    nothing awaits or joins (not ``asyncio.to_thread``: the loop's default
    executor is joined by ``asyncio.run()`` at shutdown, which made stopping
    the server wait on this): it may take several seconds on a machine that
    is not in any cloud — a DGX Spark on a desk — because each metadata
    endpoint has to time out, so nothing may wait on it on the readiness or
    the shutdown path. After it returns, every ``detect_platform_facts()``
    call on the request path is served from the process-lifetime host cache
    and never spawns ``curl``. A cache filled by :func:`seed_platform_facts`
    counts as already probed.
    """
    global _facts_cache
    with _lock:
        already = _host_cache is not None and _host_cache.network_probed
    if not already:
        probe_host(use_cache=True, probe_network=True)
        with _lock:
            _facts_cache = None  # facts built before the network probe are stale
    return detect_platform_facts(use_cache=True, probe_network=True)
