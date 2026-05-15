"""AI Wizard tool registry — natural-language action authority with safety.

The Wizard chat (Wizard-2) can read live state and answer questions. This
module is the action layer: the Wizard can *do* things on behalf of the user,
with an explicit safety class on every tool so the system never silently does
something destructive.

Safety classes
==============

  - ``auto``     — idempotent, read-only or trivially reversible. The Wizard
                   may run these without asking. Examples: refresh model list,
                   re-detect GPU, validate config, run safe-repair pass.
  - ``confirm``  — meaningful side effect; the UI must surface a "Do this?"
                   button and the caller must pass ``confirmed=True``.
                   Examples: install a pack, save a provider key, restart a
                   service.
  - ``never``    — disabled at the registry level. Not exposed. Examples:
                   uninstall user data, delete the vault, change RBAC.

The registry only exposes ``auto`` + ``confirm`` tools. ``never``-class
operations never appear in the registry at all — they're admin-only paths
on the server side.

Wire-up
=======

The HTTP layer (``/v1/wizard/tools/*``) handles auth + envelope; this module
owns the tool definitions and their handlers. Tools are async by convention
so they can chain into other engine async paths cleanly.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
SafetyClass = str  # "auto" | "confirm" — "never" is never registered


@dataclass(frozen=True)
class WizardTool:
    """One executable capability the Wizard can request.

    Attributes:
        name: Stable identifier the LLM emits when it wants to call this tool.
        description: One-line user-facing description; shown in confirm cards.
        safety_class: "auto" (run without asking) or "confirm" (user clicks).
        parameters: JSON-schema-ish dict of {param_name: {type, description, required?}}.
        handler: Async callable that takes the param dict and returns a result dict.
        summary_template: User-facing one-liner that gets formatted with the
            executed args. The UI shows this on the confirmation card.
    """

    name: str
    description: str
    safety_class: SafetyClass
    parameters: dict[str, Any]
    handler: ToolHandler
    summary_template: str = ""

    def as_public_dict(self) -> dict[str, Any]:
        """Return the schema fields the LLM and UI can see (no handler)."""
        return {
            "name": self.name,
            "description": self.description,
            "safety_class": self.safety_class,
            "parameters": self.parameters,
            "summary_template": self.summary_template,
        }


class WizardToolRegistry:
    """Lookup table for the Wizard's executable tools.

    Safety enforcement lives here, not in the handlers: registering a tool
    with ``safety_class="never"`` raises immediately so the constant can't
    drift past code review. ``execute()`` rejects ``confirm`` calls that
    arrive without ``confirmed=True``.
    """

    def __init__(self) -> None:
        self._tools: dict[str, WizardTool] = {}

    def register(self, tool: WizardTool) -> None:
        if tool.safety_class == "never":
            raise ValueError(
                f"Tool '{tool.name}' has safety_class=never — never-class operations "
                "are admin-only paths, not registry tools.",
            )
        if tool.safety_class not in ("auto", "confirm"):
            raise ValueError(
                f"Tool '{tool.name}' has unknown safety_class '{tool.safety_class}'. "
                "Allowed: 'auto', 'confirm'.",
            )
        if tool.name in self._tools:
            logger.warning("Overwriting wizard tool '%s'", tool.name)
        self._tools[tool.name] = tool

    def get(self, name: str) -> WizardTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[WizardTool]:
        return sorted(self._tools.values(), key=lambda t: (t.safety_class, t.name))

    async def execute(
        self,
        name: str,
        *,
        arguments: dict[str, Any] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Run a tool by name. Returns ``{ok, result?, error?, needs_confirmation?}``.

        - ``auto`` tools run regardless of ``confirmed``.
        - ``confirm`` tools require ``confirmed=True``; otherwise return a
          structured "I need a confirmation" response so the UI can render
          the button card.
        - Unknown tools return ``ok=False`` with an error.
        """
        tool = self.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}

        if tool.safety_class == "confirm" and not confirmed:
            return {
                "ok": False,
                "needs_confirmation": True,
                "tool": tool.as_public_dict(),
                "arguments": arguments or {},
                "summary": (tool.summary_template or tool.description).format(
                    **(arguments or {})
                ) if tool.summary_template else tool.description,
            }

        try:
            result = await tool.handler(arguments or {})
            return {"ok": True, "result": result, "tool": name, "safety_class": tool.safety_class}
        except Exception as exc:
            logger.warning("Wizard tool '%s' raised: %s", name, exc)
            return {"ok": False, "error": str(exc)[:300], "tool": name}


# ────────────────────────────────────────────────────────────────────────────
# Default tool handlers — bound to existing nvHive subsystems.
# Imports stay inside the handlers so the registry is cheap to import.
# ────────────────────────────────────────────────────────────────────────────


async def _tool_refresh_models(args: dict[str, Any]) -> dict[str, Any]:
    """Re-query the local Ollama daemon for installed models."""
    from nvh.integrations.wizard.auto_repair import _refresh_ollama_models

    summary = _refresh_ollama_models()
    return {"summary": summary}


async def _tool_repair_workspace(args: dict[str, Any]) -> dict[str, Any]:
    """Run the idempotent rootless safe-repair pass."""
    from nvh.integrations.wizard.auto_repair import run_safe_repairs

    home_dir = args.get("home_dir")
    return run_safe_repairs(home_dir=home_dir)


async def _tool_validate_provider_key(args: dict[str, Any]) -> dict[str, Any]:
    """Validate a provider API key by health-checking with it. Does NOT save."""
    import os

    provider = args.get("provider")
    api_key = args.get("api_key")
    if not isinstance(provider, str) or not isinstance(api_key, str):
        return {"ok": False, "error": "provider + api_key required (both strings)"}

    from nvh.api.server import _provider_env_var, get_engine  # type: ignore

    env_key = _provider_env_var(provider)
    previous = os.environ.get(env_key)
    os.environ[env_key] = api_key
    try:
        engine = get_engine()
        if engine is None:
            return {"valid": False, "error": "engine not initialized"}
        engine._initialized = False
        await engine.initialize()
        provider_obj = engine.registry.get(provider)
        if provider_obj is None:
            return {"valid": False, "error": f"provider '{provider}' not registered after key swap"}
        health = await provider_obj.health_check()
        if health.healthy:
            return {
                "valid": True,
                "latency_ms": health.latency_ms,
                "model_count": health.models_available,
            }
        return {"valid": False, "error": health.error or "provider rejected the key"}
    finally:
        if previous is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = previous
        try:
            engine = get_engine()
            if engine is not None:
                engine._initialized = False
                await engine.initialize()
        except Exception as exc:
            logger.debug("validate-key engine restore failed: %s", exc)


async def _tool_save_provider_key(args: dict[str, Any]) -> dict[str, Any]:
    """Persist a provider API key under the rootless workspace config."""
    provider = args.get("provider")
    api_key = args.get("api_key")
    if not isinstance(provider, str) or not isinstance(api_key, str):
        return {"ok": False, "error": "provider + api_key required (both strings)"}

    import os

    from nvh.api.server import (  # type: ignore
        _enable_provider_in_config,
        _provider_env_var,
        _write_provider_env_key,
        get_engine,
    )

    env_key = _provider_env_var(provider)
    os.environ[env_key] = api_key
    env_file = _write_provider_env_key(env_key, api_key)
    config_file = _enable_provider_in_config(provider, env_key)
    try:
        import keyring

        keyring.set_password("nvhive", f"{provider}_api_key", api_key)
        keyring_status = "stored"
    except Exception as exc:
        logger.debug("keyring save skipped: %s", exc)
        keyring_status = f"skipped ({exc})"
    try:
        engine = get_engine()
        if engine is not None:
            engine._initialized = False
            await engine.initialize()
    except Exception as exc:
        logger.debug("engine reinit after save: %s", exc)

    return {
        "ok": True,
        "provider": provider,
        "env_file": str(env_file),
        "config_file": str(config_file),
        "keyring": keyring_status,
    }


def default_registry() -> WizardToolRegistry:
    """Build the registry with nvHive's stock tools.

    Kept as a builder rather than a module-level singleton so the API layer
    can rebuild it for tests without import-time side effects.
    """
    reg = WizardToolRegistry()

    reg.register(WizardTool(
        name="refresh_models",
        description="Re-query the local Ollama daemon for installed models so the picker stays fresh.",
        safety_class="auto",
        parameters={},
        handler=_tool_refresh_models,
        summary_template="Refresh the local model list.",
    ))

    reg.register(WizardTool(
        name="repair_workspace",
        description="Run the idempotent rootless safe-repair pass: env file, catalog cache, ComfyUI examples, model list, config validation.",
        safety_class="auto",
        parameters={
            "home_dir": {"type": "string", "description": "Optional NVH_HOME override.", "required": False},
        },
        handler=_tool_repair_workspace,
        summary_template="Run safe rootless repairs across the workspace.",
    ))

    reg.register(WizardTool(
        name="validate_provider_key",
        description="Validate an API key against its provider's health endpoint. Does NOT save.",
        safety_class="auto",
        parameters={
            "provider": {"type": "string", "required": True, "description": "Provider id (openai, anthropic, ...)."},
            "api_key": {"type": "string", "required": True, "description": "The key to validate."},
        },
        handler=_tool_validate_provider_key,
        summary_template="Validate the {provider} key (does not save).",
    ))

    reg.register(WizardTool(
        name="save_provider_key",
        description="Save a validated API key to the rootless workspace config so the engine can use it.",
        safety_class="confirm",
        parameters={
            "provider": {"type": "string", "required": True, "description": "Provider id."},
            "api_key": {"type": "string", "required": True, "description": "The key to persist."},
        },
        handler=_tool_save_provider_key,
        summary_template="Save the {provider} API key under the rootless workspace config.",
    ))

    return reg
