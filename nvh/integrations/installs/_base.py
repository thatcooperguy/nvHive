"""Base class for studio pack installers.

The existing free-function installers in ``studio_packs.py`` follow a copy-pasted
shape: take a ``StudioPack`` + ``force_update`` bool, yield progress events as
``dict[str, Any]``, and emit a final terminal event. The functions also share
several private helpers (probing, downloading, extracting, writing launchers).

This base class formalizes the shape so future installers — and the eventual
refactor of the existing 7 installers — have an obvious pattern to follow.
It is intentionally minimal: no behavior change, no breaking import path,
and the existing free-function installers continue to work as-is.

Migration plan (not in this PR):
- Refactor each ``_install_*`` function into a ``PackInstaller`` subclass.
- Move shared helpers (``_run_capture``, ``_download``, ``_extract_archive``,
  ``_write_launcher``) into protected methods on the base.
- Replace the big ``async for event in _install_X(pack, ...)`` dispatch in
  ``install_studio_packs`` with a lookup table keyed by ``install_kind``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nvh.integrations.installs.studio_packs import StudioPack

logger = logging.getLogger(__name__)

# Conventional event payload shape used throughout the installer protocol.
# Producers emit dicts with these keys; consumers (the API streaming layer,
# the CLI progress renderer, the install-job event store) read them.
InstallEvent = dict[str, Any]


def make_event(
    event: str,
    *,
    status: str = "info",
    message: str = "",
    **extra: Any,
) -> InstallEvent:
    """Build a progress event dict with the conventional shape.

    Args:
        event: Short event kind, e.g. ``"download_start"``, ``"complete"``.
        status: One of ``"info" | "ok" | "warn" | "error"``. Drives UI color.
        message: Human-readable line shown to the operator.
        **extra: Extra fields specific to this event kind (e.g. percent, url).
    """
    return {"event": event, "status": status, "message": message, **extra}


class PackInstaller(ABC):
    """Strategy for installing a single studio pack.

    Subclasses implement :meth:`install` as an async generator that yields
    progress events. The orchestrator (``install_studio_packs`` in
    ``studio_packs.py``) is responsible for: looking up the right installer
    for a given ``StudioPack.install_kind``, threading the user's
    ``force_update`` flag, calling :meth:`install`, and forwarding events.

    Subclasses may override :meth:`detect` if "is this already installed?"
    is more nuanced than file existence. The default is conservative — always
    re-run the installer and let it short-circuit internally.
    """

    #: Matches ``StudioPack.install_kind``. Subclasses set this.
    install_kind: str = ""

    @abstractmethod
    def install(
        self,
        pack: StudioPack,
        force_update: bool,
    ) -> AsyncIterator[InstallEvent]:
        """Install ``pack``, yielding progress events.

        Implementations should:
        - Yield an initial ``make_event("start", message=...)`` so the UI can
          render a row even if the actual work takes a while.
        - Yield ``make_event("complete", status="ok", ...)`` on success or
          ``make_event("error", status="error", ...)`` on failure.
        - Never raise — wrap exceptions and emit an error event so the
          orchestrator can keep going with the next pack.
        """

    async def detect(self, pack: StudioPack) -> bool:
        """Return True if the pack appears already installed.

        Default: always False (re-run installer; let it short-circuit). Subclass
        when there's a fast, cheap probe — e.g. ``which ollama`` for the
        Ollama installer.
        """
        return False


class PackInstallerRegistry:
    """Lookup table mapping ``install_kind`` to a :class:`PackInstaller`.

    Designed so the studio_packs orchestrator can dispatch by kind without a
    long ``if/elif`` chain. Empty until installers register themselves.
    """

    def __init__(self) -> None:
        self._by_kind: dict[str, PackInstaller] = {}

    def register(self, installer: PackInstaller) -> None:
        if not installer.install_kind:
            raise ValueError(
                f"{type(installer).__name__} has empty install_kind",
            )
        if installer.install_kind in self._by_kind:
            logger.warning(
                "Overwriting installer for kind %s (%s -> %s)",
                installer.install_kind,
                type(self._by_kind[installer.install_kind]).__name__,
                type(installer).__name__,
            )
        self._by_kind[installer.install_kind] = installer

    def get(self, kind: str) -> PackInstaller | None:
        return self._by_kind.get(kind)

    def known_kinds(self) -> list[str]:
        return sorted(self._by_kind)


# Shared singleton — populated as installers migrate to the base class.
default_registry = PackInstallerRegistry()
