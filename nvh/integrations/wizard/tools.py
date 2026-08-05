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


async def _tool_diagnose(args: dict[str, Any]) -> dict[str, Any]:
    """Return current diagnostic findings + the live workspace snapshot.

    The Wizard already gets these in the system prompt at turn start, but
    state can change mid-conversation (e.g. user installs a model in another
    tab). Calling ``diagnose`` mid-turn refreshes the agent's view without
    waiting for the next reconnect.
    """
    from nvh.integrations.wizard.context import wizard_context
    from nvh.integrations.wizard.findings import derive_findings

    home_dir = args.get("home_dir")
    snapshot = wizard_context(home_dir=home_dir)
    findings = derive_findings(snapshot)
    return {
        "findings": [f.to_dict() for f in findings],
        "context": snapshot,
        "summary": (
            f"{len(findings)} active finding(s)"
            + (f": {', '.join(f.id for f in findings[:5])}" if findings else "")
        ),
    }


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


async def _tool_rag_ingest(args: dict[str, Any]) -> dict[str, Any]:
    """Ingest a folder of text/source files into the local RAG index."""
    from nvh.integrations.rag import ingest_folder

    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "path required (string)"}
    collection = args.get("collection") if isinstance(args.get("collection"), str) else None
    home_dir = args.get("home_dir") if isinstance(args.get("home_dir"), str) else None
    return await ingest_folder(path, collection=collection, home_dir=home_dir)


async def _tool_rag_ask(args: dict[str, Any]) -> dict[str, Any]:
    """Ask a question grounded in the local RAG index — returns retrieved chunks."""
    from nvh.integrations.rag import ask

    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return {"ok": False, "error": "question required (string)"}
    collection = args.get("collection") if isinstance(args.get("collection"), str) else None
    home_dir = args.get("home_dir") if isinstance(args.get("home_dir"), str) else None
    top_k_raw = args.get("top_k", 5)
    try:
        top_k = max(1, min(20, int(top_k_raw)))
    except (TypeError, ValueError):
        top_k = 5
    return await ask(question, collection=collection, top_k=top_k, home_dir=home_dir)


async def _tool_rag_ask_vault(args: dict[str, Any]) -> dict[str, Any]:
    """Search the nvHive Vault (user's own notes) — auto-indexes on first use."""
    from nvh.integrations.rag import ask_vault

    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return {"ok": False, "error": "question required (string)"}
    home_dir = args.get("home_dir") if isinstance(args.get("home_dir"), str) else None
    top_k_raw = args.get("top_k", 5)
    try:
        top_k = max(1, min(20, int(top_k_raw)))
    except (TypeError, ValueError):
        top_k = 5
    return await ask_vault(question, top_k=top_k, home_dir=home_dir)


async def _tool_web_search(args: dict[str, Any]) -> dict[str, Any]:
    """Run a web search and return top-k hits with title/url/snippet."""
    from nvh.integrations.web_search import web_search

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "query required (string)"}
    top_k_raw = args.get("top_k", 5)
    try:
        top_k = max(1, min(20, int(top_k_raw)))
    except (TypeError, ValueError):
        top_k = 5
    return await web_search(query, top_k=top_k)


# ────────────────────────────────────────────────────────────────────────────
# Tool discovery — entry-points + workspace plugin directory
# ────────────────────────────────────────────────────────────────────────────

# Distributions that ship Wizard tools (incl. third-party plugins down the
# road) advertise them under this entry-point group. Each entry point should
# resolve to a callable ``register(reg: WizardToolRegistry) -> None`` so
# multi-tool packages don't have to publish one entry per tool.
ENTRY_POINT_GROUP = "nvh.wizard_tools"

# Workspace-local plugin directory. Drop a Python file with a top-level
# ``register(reg)`` callable here and it gets loaded on registry build. This
# is the simplest possible "extend the Wizard" path that doesn't need a wheel
# rebuild. Sandbox is the user's filesystem; same trust boundary as their
# own scripts. The directory is ignored if it doesn't exist.
WORKSPACE_PLUGIN_DIR_ENV = "NVH_WIZARD_PLUGIN_DIR"


def _load_entry_point_tools(reg: WizardToolRegistry) -> None:
    """Discover Wizard-tool registrations advertised via importlib.metadata.

    Best-effort: a broken entry point logs a warning and is skipped — never
    fatal to the rest of the registry build.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:
        return
    try:
        eps = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:
        logger.debug("entry-point discovery failed: %s", exc)
        return
    for ep in eps:
        try:
            fn = ep.load()
            if callable(fn):
                fn(reg)
                logger.info("loaded wizard tools from entry point %s", ep.name)
        except Exception as exc:
            logger.warning("entry point %s failed: %s", ep.name, exc)


def _load_workspace_plugin_tools(reg: WizardToolRegistry) -> None:
    """Load .py plugins from the workspace plugin directory.

    Walks ``$NVH_WIZARD_PLUGIN_DIR`` (or ``$NVH_HOME/wizard-tools/`` by
    default) and imports each ``.py`` file via spec_from_file_location. If
    the file exposes a top-level ``register(reg)`` callable, it gets called.
    """
    import os as _os
    from importlib import util as _util

    plugin_dir_str = _os.environ.get(WORKSPACE_PLUGIN_DIR_ENV)
    if plugin_dir_str:
        from pathlib import Path as _Path

        plugin_dir = _Path(plugin_dir_str).expanduser()
    else:
        try:
            from nvh.integrations.workspace.storage import nvh_home

            home, _src = nvh_home(None)
            from pathlib import Path as _Path

            plugin_dir = home / "wizard-tools"
        except Exception:
            return
    if not plugin_dir.is_dir():
        return
    for path in plugin_dir.glob("*.py"):
        if path.name.startswith("_"):
            continue
        try:
            spec = _util.spec_from_file_location(f"nvh_wizard_plugin_{path.stem}", path)
            if spec is None or spec.loader is None:
                continue
            mod = _util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            reg_fn = getattr(mod, "register", None)
            if callable(reg_fn):
                reg_fn(reg)
                logger.info("loaded wizard plugin %s", path.name)
        except Exception as exc:
            logger.warning("wizard plugin %s failed: %s", path.name, exc)


def default_registry() -> WizardToolRegistry:
    """Build the registry with nvHive's stock tools + any discovered plugins.

    Kept as a builder rather than a module-level singleton so the API layer
    can rebuild it for tests without import-time side effects.

    After the stock tools land, we run two discovery passes:
      1. ``importlib.metadata`` entry points under the ``nvh.wizard_tools``
         group — for packaged plugins installed via pip.
      2. ``.py`` files under the workspace plugin directory — for one-off
         user tools dropped into the rootless home without a wheel rebuild.

    Both passes are best-effort: a broken plugin logs and is skipped.
    """
    reg = WizardToolRegistry()

    reg.register(WizardTool(
        name="diagnose",
        description=(
            "Refresh and return the current diagnostic findings (GPU, storage, "
            "providers, models, runtime). Use this when the user asks 'what's "
            "wrong' or after running a repair to check whether the issue "
            "cleared."
        ),
        safety_class="auto",
        parameters={
            "home_dir": {
                "type": "string",
                "description": "Optional NVH_HOME override.",
                "required": False,
            },
        },
        handler=_tool_diagnose,
        summary_template="Refresh diagnostic findings.",
    ))

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

    reg.register(WizardTool(
        name="rag_ask",
        description="Search the local RAG index for chunks relevant to a question and return them with source citations.",
        safety_class="auto",
        parameters={
            "question": {"type": "string", "required": True, "description": "The natural-language question."},
            "collection": {"type": "string", "required": False, "description": "Named collection; defaults to 'default'."},
            "top_k": {"type": "integer", "required": False, "description": "Max chunks to return (1-20, default 5)."},
        },
        handler=_tool_rag_ask,
        summary_template="Search the RAG index for: {question}",
    ))

    reg.register(WizardTool(
        name="rag_ingest",
        description="Walk a folder, chunk + embed every text/source file, and store under a RAG collection.",
        safety_class="confirm",
        parameters={
            "path": {"type": "string", "required": True, "description": "Folder to index."},
            "collection": {"type": "string", "required": False, "description": "Named collection; defaults to 'default'."},
        },
        handler=_tool_rag_ingest,
        summary_template="Ingest {path} into the RAG index.",
    ))

    reg.register(WizardTool(
        name="rag_ask_vault",
        description="Search the nvHive Vault (user's own Markdown notes) for chunks relevant to a question. Auto-indexes the vault on first use.",
        safety_class="auto",
        parameters={
            "question": {"type": "string", "required": True, "description": "The natural-language question."},
            "top_k": {"type": "integer", "required": False, "description": "Max chunks to return (1-20, default 5)."},
        },
        handler=_tool_rag_ask_vault,
        summary_template="Search your nvHive Vault for: {question}",
    ))

    reg.register(WizardTool(
        name="web_search",
        description="Run a web search via the active backend (SearXNG, Brave, or DuckDuckGo) and return top hits with title, URL, and snippet.",
        safety_class="auto",
        parameters={
            "query": {"type": "string", "required": True, "description": "Natural-language search query."},
            "top_k": {"type": "integer", "required": False, "description": "Max hits to return (1-20, default 5)."},
        },
        handler=_tool_web_search,
        summary_template="Search the web for: {query}",
    ))

    # Pull in any third-party / workspace-local tools after the stock set so
    # plugins can override (with a logged warning) or extend without forking.
    _load_entry_point_tools(reg)
    _load_workspace_plugin_tools(reg)

    # External MCP tool servers (2026-08-05, roadmap critical #1): tools
    # cached by `nvh mcp refresh` register as mcp_<server>_<tool>, confirm-
    # class by default (arbitrary third-party subprocesses), auto only via
    # the server's auto_approve allowlist. Cache-read only — never spawns
    # servers on the chat-turn path. Best-effort like the other passes.
    try:
        from nvh.integrations.mcp_client import register_mcp_tools

        register_mcp_tools(reg)
    except Exception as exc:
        logger.warning("mcp tool registration skipped: %s", exc)

    return reg
