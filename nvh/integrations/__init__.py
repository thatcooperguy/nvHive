"""Integrations: installers, diagnostics, services, workspace, wizard.

The flat layout has been split into subpackages by concern. Existing callers
that did ``from nvh.integrations import studio_packs`` keep working via the
re-exports below; new code should import from the subpackage directly:

  - nvh.integrations.installs     (studio packs, ComfyUI, OpenClaw, etc.)
  - nvh.integrations.diagnostics  (preflight, compatibility, readiness)
  - nvh.integrations.services     (registry, jobs, receipts, runtime)
  - nvh.integrations.workspace    (storage, vault, mount autopilot)
  - nvh.integrations.wizard       (setup agent, mission builder, troubleshooter)
"""

from __future__ import annotations

from . import cloud_session, local_chat, setup_catalog
from .diagnostics import (
    boot_preflight,
    compatibility,
    detector,
    model_fit,
    production_readiness,
    smoke_tests,
    workspace_state,
)
from .diagnostics import report as diagnostics
from .installs import (
    comfyui,
    node_runtime,
    openclaw,
    studio_packs,
    workstation,
)
from .services import (
    jobs,
    mission_control,
    receipts,
    runtime,
    service,
    service_registry,
)
from .wizard import (
    auto_repair,
    mission_builder,
    setup_agent,
    troubleshooter,
)
from .workspace import (
    hostname,
    mount_autopilot,
    storage,
    vault,
)
from .workspace import passport as workspace_passport

__all__ = [
    "auto_repair",
    "boot_preflight",
    "cloud_session",
    "comfyui",
    "compatibility",
    "detector",
    "diagnostics",
    "hostname",
    "jobs",
    "local_chat",
    "mission_builder",
    "mission_control",
    "model_fit",
    "mount_autopilot",
    "node_runtime",
    "openclaw",
    "production_readiness",
    "receipts",
    "runtime",
    "service",
    "service_registry",
    "setup_agent",
    "setup_catalog",
    "smoke_tests",
    "storage",
    "studio_packs",
    "troubleshooter",
    "vault",
    "workspace_passport",
    "workspace_state",
    "workstation",
]
