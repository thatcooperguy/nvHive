"""AI Wizard tool registry — the Wizard's instance of the one tool model.

The tool model itself — :class:`~nvh.core.tools.Tool`,
:class:`~nvh.core.tools.ToolRegistry`, the safety classes, the approval
tokens, the kill switch, the vault audit, the tool-result window and the
prompt/UI parameter shape — lives in :mod:`nvh.core.tools` since 0.44 (issue
#132 D2). This module keeps the Wizard-facing names importable
(:class:`WizardTool`, :class:`WizardToolRegistry`, every helper and constant
below) and owns what is Wizard-specific: the stock handlers, plugin discovery
and :func:`default_registry`, the curated catalogue the chat and the API build.

Safety classes
==============

  - ``auto``     — idempotent, read-only or trivially reversible. The Wizard
                   may run these without asking. Examples: refresh model list,
                   re-detect GPU, validate config, run safe-repair pass.
  - ``confirm``  — meaningful side effect; the UI must surface a "Do this?"
                   button and the caller must pass ``confirmed=True``.
                   Examples: install a pack, save a provider key, restart a
                   service.
  - ``privileged`` — changes the *machine* nvHive runs on, usually through
                   ``sudo`` (system settings, apt/snap installs, enabling a
                   service). Everything ``confirm`` requires, plus: the
                   unconfirmed answer carries the exact ``plan`` (the commands
                   a dry run says it would execute) so the UI can render a red
                   approval card; the ``NVH_ALLOW_PRIVILEGED`` kill switch
                   (:func:`privileged_enabled`) is re-checked on *both* the
                   card and the confirmed path; the card carries an
                   ``approval_token`` the confirmed call must bring back
                   (below); an apply that touched the host — complete,
                   partial or failed — is written to the vault under
                   ``Decisions/`` (:func:`record_privileged_change`); and the
                   result is cut to the tool-result window
                   (:func:`fit_tool_window`). No auto-approve path exists —
                   ``chat.py`` buckets anything that is not exactly ``auto``
                   as needing a click, and so does the WebUI.
  - ``never``    — disabled at the registry level. Not exposed. Examples:
                   uninstall user data, delete the vault, change RBAC.

The registry only exposes ``auto`` + ``confirm`` + ``privileged`` tools.
``never``-class operations never appear in the registry at all — they're
admin-only paths on the server side. Privileged tools stay *registered* when
the kill switch is off (so the catalogue can explain what they would do) but
``execute()`` refuses them, naming the variable.

Sudo reality (docs/proposals/SPARK_CONCIERGE_2026-09.md §3.4, §5): nvHive
never prompts for, sees or stores a password. Privileged handlers use
``sudo -n`` only where :mod:`nvh.utils.platform_facts` found passwordless
sudo; a sudo-group member without it gets the exact command to run in a
terminal. There is no password parameter anywhere in this module or in
:mod:`nvh.integrations.wizard.system_settings`.

Approval tokens — what the red card's click proves
==================================================

``confirmed=True`` is a JSON field any client can send, and the default
install runs the API in open mode (no ``HIVE_API_KEY``), so on its own it
proves nothing about a human having read the card. A privileged card
therefore carries an ``approval_token``: an HMAC-SHA256 over the exact tool
name and canonical arguments plus the issue time, keyed with a secret drawn
once per process (:func:`issue_approval`). The confirmed path
(:meth:`WizardToolRegistry.execute`) *requires* a valid token for that exact
call (:func:`verify_approval`: constant-time compare, 15-minute TTL, single
use) and refuses with ``approval_required`` otherwise — nothing runs. The
model never sees the token: it rides on the surfaced call to the WebUI and
comes back with the click, so a ``TOOL_CALL`` cannot mint one, a blind CSRF
POST cannot forge one, and a captured one cannot be replayed or re-aimed at
different arguments.

Out of scope, deliberately: another process running as the same local user.
It already holds the user's sudo and needs nothing from nvHive. The token
binds the click to the card that was shown; the HTTP layer's Host/Origin
check (``nvh.api.server.wizard_tools_execute``) defeats DNS rebinding, and
open mode over a non-loopback bind is refused outright there.

Wire-up
=======

The HTTP layer (``/v1/wizard/tools/*``) handles auth + envelope; this module
owns the tool definitions and their handlers. Tools are async by convention
so they can chain into other engine async paths cleanly. Desktop-hands tools
(``mouse_*``, ``keyboard_*``, ``scroll``) are never registered here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from functools import partial
from typing import Any, ClassVar

from nvh.core.atohi import AtohiAdmission
from nvh.core.tools import (  # noqa: F401 — re-exported for every existing importer
    _CONSUMED_APPROVALS,
    APPROVAL_REQUIRED_ERROR,
    APPROVAL_TTL_S,
    AUDIT_OUTPUT_CHARS,
    PRIVILEGED_DISABLED_ERROR,
    PRIVILEGED_ENV,
    SAFETY_CLASSES,
    TOOL_RESULT_CHARS,
    SafetyClass,
    Tool,
    ToolHandler,
    ToolRegistry,
    ToolResult,
    _dry_run,
    _pinned_arguments,
    _privileged_applied,
    audit_privileged_change,
    fit_tool_window,
    format_summary,
    issue_approval,
    json_schema_from_parameters,
    parameters_from_json_schema,
    privileged_enabled,
    record_privileged_change,
    reset_approvals,
    translate_parameters,
    verify_approval,
)

logger = logging.getLogger(__name__)

__all__ = [
    "APPROVAL_REQUIRED_ERROR",
    "APPROVAL_TTL_S",
    "AUDIT_OUTPUT_CHARS",
    "ENTRY_POINT_GROUP",
    "PRIVILEGED_DISABLED_ERROR",
    "PRIVILEGED_ENV",
    "SAFETY_CLASSES",
    "TOOL_RESULT_CHARS",
    "WORKSPACE_PLUGIN_DIR_ENV",
    "SafetyClass",
    "Tool",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "WizardTool",
    "WizardToolRegistry",
    "audit_privileged_change",
    "default_registry",
    "fit_tool_window",
    "format_summary",
    "issue_approval",
    "json_schema_from_parameters",
    "parameters_from_json_schema",
    "privileged_enabled",
    "record_privileged_change",
    "reset_approvals",
    "translate_parameters",
    "verify_approval",
]


class WizardTool(Tool):
    """A :class:`~nvh.core.tools.Tool` declared the Wizard way.

    Three differences from the base. The handler takes one ``dict``
    (``handler(arguments)``); ``parameters`` may be given — and reads back —
    in the prompt/UI shape ``{name: {type, description, required}}`` (the
    canonical JSON Schema is still ``input_schema``, a schema given as
    ``parameters`` is accepted too, and the Wizard shape is derived from it by
    :func:`~nvh.core.tools.translate_parameters`, never stored twice); and
    the safety class is **required**: a Wizard tool built without
    ``safety_class`` (or the older ``safe=``) raises ``TypeError`` at
    construction, so a plugin that forgets it fails to load instead of being
    registered as ``auto`` and run without a click.
    """

    _default_handler_style: ClassVar[str] = "mapping"

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Mapping[str, Any] | None = None,
        handler: ToolHandler | None = None,
        safe: bool | None = None,
        *,
        safety_class: SafetyClass | None = None,
        **fields: Any,
    ) -> None:
        if safety_class is None and safe is None:
            raise TypeError(
                f"WizardTool {name!r} needs an explicit safety_class "
                f"({', '.join(repr(c) for c in SAFETY_CLASSES)}); a Wizard tool without one "
                "is refused at load, never registered as auto.",
            )
        super().__init__(name, description, parameters, handler, safe, safety_class=safety_class, **fields)

    @property
    def parameters(self) -> dict[str, Any]:  # type: ignore[override]
        """The Wizard shape ``{name: {type, description, required}}`` (derived from ``input_schema``)."""
        return self.wizard_parameters


class WizardToolRegistry(ToolRegistry):
    """The Wizard's instance of the one registry: empty at construction, click-enforcing.

    No built-ins, no system tools; ``execute()`` requires ``confirmed=True``
    for every ``confirm`` tool and the card's token for every ``privileged``
    one — the single enforcement point the HTTP layer and the chat loop rely
    on (they add nothing).
    """

    _logger: ClassVar[logging.Logger] = logger

    def __init__(self) -> None:
        super().__init__(None, False, builtins=False, include_mcp=False, enforce_confirmation=True)


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
    # wizard_context spawns nvidia-smi / reads Ollama over HTTP; keep that
    # off the server's event loop (the chat turn already does the same).
    snapshot = await asyncio.to_thread(wizard_context, home_dir=home_dir)
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

    summary = await asyncio.to_thread(_refresh_ollama_models)
    return {"summary": summary}


async def _tool_repair_workspace(args: dict[str, Any]) -> dict[str, Any]:
    """Run the idempotent rootless safe-repair pass."""
    from nvh.integrations.wizard.auto_repair import run_safe_repairs

    home_dir = args.get("home_dir")
    return await asyncio.to_thread(run_safe_repairs, home_dir=home_dir)


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


async def _tool_rag_ingest(args: dict[str, Any], *, admission: AtohiAdmission | None = None) -> dict[str, Any]:
    """Ingest a folder of text/source files into the local RAG index."""
    from nvh.integrations.rag import ingest_folder

    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        return {"ok": False, "error": "path required (string)"}
    collection = args.get("collection") if isinstance(args.get("collection"), str) else None
    home_dir = args.get("home_dir") if isinstance(args.get("home_dir"), str) else None
    return await ingest_folder(path, collection=collection, home_dir=home_dir, admission=admission)


async def _tool_rag_ask(args: dict[str, Any], *, admission: AtohiAdmission | None = None) -> dict[str, Any]:
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
    return await ask(question, collection=collection, top_k=top_k, home_dir=home_dir, admission=admission)


async def _tool_rag_ask_vault(args: dict[str, Any], *, admission: AtohiAdmission | None = None) -> dict[str, Any]:
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
    return await ask_vault(question, top_k=top_k, home_dir=home_dir, admission=admission)


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
# 0.46 adds: ask_user (dialogue), remember (vault), generate_image (images)
# ────────────────────────────────────────────────────────────────────────────

#: ``ask_user``: the one question is cut here; the card shows it verbatim.
ASK_USER_QUESTION_CHARS = 500
ASK_USER_MAX_CHOICES = 6
ASK_USER_CHOICE_CHARS = 80
ASK_USER_NOTE = "ask_user is answered in the chat; nothing runs on the server."

#: ``remember``: the tool's own caps (the API keeps its larger body cap), the
#: one category a model-composed note may land in, and its ``Source:``. A body
#: is refused for two exact literals — ``approval_token`` (the red card's
#: secret) and :data:`~nvh.integrations.wizard.system_settings.NEEDS_TERMINAL_HINT`
#: (the sudo note a privileged outcome carries): the privileged audit writer
#: (``Decisions/``) is the only writer allowed to describe a privileged
#: action. The word "sudo" on its own is a legitimate memory.
REMEMBER_TITLE_CHARS = 120
REMEMBER_BODY_CHARS = 2000
REMEMBER_MAX_TAGS = 8
REMEMBER_CATEGORY = "Wizard Memory"
REMEMBER_SOURCE = "wizard"
REMEMBER_REFUSED_ERROR = "refused: a memory must not carry an approval token or privileged tool output"

#: ``generate_image``: the sizes and backends the model may ask for, the
#: output scope and the refusal for a call whose ``profile`` the chat did not
#: pin (``profile`` is not in the model-visible schema — ``_surfaced_call``
#: pins it onto the card and the web echoes it back).
GENERATE_IMAGE_SIZES: tuple[int, ...] = (512, 768, 1024)
GENERATE_IMAGE_BACKENDS: tuple[str, ...] = ("auto", "local", "nvidia")
GENERATE_IMAGE_DEFAULT_SIZE = 1024
GENERATE_IMAGE_PROMPT_CHARS = 2000
GENERATE_IMAGE_SCOPE = "wizard"
IMAGE_PROFILE_REFUSAL = "this image request did not come from a Wizard card; re-run it from the chat"
IMAGE_UNAVAILABLE_ERROR = "image generation is not available in this build (nvh.integrations.imagegen is missing)"


async def _tool_ask_user(args: dict[str, Any]) -> dict[str, Any]:
    """``ask_user`` is answered in the chat; this runs only if a stale client POSTs it.

    The turn that proposes the question ends with a question card; the answer
    arrives as the user's next message (and as a ``TOOL_RESULT ask_user``
    history line). Nothing happens on the server, so a client that still
    renders the card as Run/Skip gets a harmless result.
    """
    question = args.get("question")
    text = " ".join(question.split())[:ASK_USER_QUESTION_CHARS] if isinstance(question, str) else ""
    return {"answered_in_chat": True, "question": text, "note": ASK_USER_NOTE}


def _remember_forbidden_literals() -> tuple[str, ...]:
    from nvh.integrations.wizard.system_settings import NEEDS_TERMINAL_HINT

    return ("approval_token", NEEDS_TERMINAL_HINT)


def _remember_tags(raw: Any) -> list[str]:
    """At most :data:`REMEMBER_MAX_TAGS` non-empty string tags (the ``wizard`` tag is always added)."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    tags: list[str] = []
    for tag in raw:
        if not isinstance(tag, str) or not tag.strip():
            continue
        clean = tag.strip()[:40]
        if clean.lower() == REMEMBER_SOURCE or clean in tags:
            continue
        tags.append(clean)
        if len(tags) >= REMEMBER_MAX_TAGS:
            break
    return tags


async def _tool_remember(args: dict[str, Any], *, admission: AtohiAdmission | None = None) -> dict[str, Any]:
    """Save a model-composed note under the vault's ``Wizard Memory`` — process home only.

    Exactly ``{title, body, tags}`` are read; every other key — a
    ``home_dir``, a ``category`` — is dropped, so the model never chooses
    where a file lands and never masquerades as the privileged audit writer.
    The body is refused when it carries the red card's ``approval_token`` or
    the sudo note a privileged outcome carries. Redaction happens inside
    :func:`~nvh.integrations.workspace.vault.append_vault_memory` (best-effort
    patterns; the confirm card showing the exact note is the human check),
    then the one note is indexed into the RAG ``vault`` collection,
    best-effort (``indexed``).
    """
    title = args.get("title")
    body = args.get("body")
    if not isinstance(title, str) or not title.strip():
        return {"saved": False, "error": "title required (string)"}
    if not isinstance(body, str) or not body.strip():
        return {"saved": False, "error": "body required (string)"}
    clean_title = " ".join(title.split())[:REMEMBER_TITLE_CHARS]
    clean_body = body.strip()[:REMEMBER_BODY_CHARS]
    for literal in _remember_forbidden_literals():
        if literal in clean_body or literal in clean_title:
            return {"saved": False, "error": REMEMBER_REFUSED_ERROR}
    tags = _remember_tags(args.get("tags"))

    from nvh.integrations.workspace.vault import append_vault_memory

    try:
        note = await asyncio.to_thread(
            append_vault_memory, clean_title, clean_body,
            category=REMEMBER_CATEGORY, tags=[REMEMBER_SOURCE, *tags], source=REMEMBER_SOURCE,
        )
    except ValueError as exc:
        return {"saved": False, "error": str(exc)}

    indexed = False
    try:
        from nvh.integrations.rag.vault_bridge import index_vault_note

        indexed = bool(await index_vault_note(note["path"], admission=admission))
    except Exception as exc:  # the index is derived; the note is saved either way
        logger.debug("remember: index_vault_note failed: %s", exc)
    return {
        "saved": True,
        "id": note["id"],
        "path": note["path"],
        "title": note["title"],
        "category": note["category"],
        "tags": note["tags"],
        "indexed": indexed,
        "redacted": note["redacted"],
    }


def _image_profile_ceiling(profile: Any, *, home_dir: Any = None) -> tuple[bool, float | None]:
    """``(accepted, ceiling_usd)`` for the ``profile`` the chat pinned onto the card.

    Two shapes are accepted: the general sentinel
    (:data:`~nvh.integrations.wizard.chat.GENERAL_WIZARD_PROFILE`, no ceiling)
    and a name :func:`~nvh.integrations.wizard.profiles.get_profile` resolves
    — in ``home_dir`` when the chat pinned one (``arguments.profile_home``,
    the request's ``NVH_HOME`` override it read the catalog from), else in the
    process home — with its ``max_cost_usd_per_turn`` as the ceiling (``None``
    or ``<= 0`` means none, the profile field's own convention: ``0`` is "no
    limit", never a $0 cap). Anything else — missing, empty, unknown — is a
    refusal, never "no ceiling": the pin is how a profile's ceiling reaches
    the handler, and a call without one did not come through a Wizard card.
    """
    if not isinstance(profile, str) or not profile.strip():
        return False, None
    name = profile.strip()
    from nvh.integrations.wizard.chat import GENERAL_WIZARD_PROFILE

    if name == GENERAL_WIZARD_PROFILE:
        return True, None
    from nvh.integrations.wizard.profiles import get_profile

    home = home_dir if isinstance(home_dir, str) and home_dir.strip() else None
    resolved = get_profile(name, home_dir=home)
    if resolved is None:
        return False, None
    ceiling = resolved.max_cost_usd_per_turn
    if ceiling is not None and ceiling <= 0:
        ceiling = None
    return True, None if ceiling is None else float(ceiling)


def _image_dimension(value: Any, name: str) -> tuple[int, str | None]:
    """``(pixels, error)`` for a width/height argument: one of the allowed sizes, default 1024."""
    if value is None:
        return GENERATE_IMAGE_DEFAULT_SIZE, None
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = -1
    if size not in GENERATE_IMAGE_SIZES:
        return 0, f"{name} must be one of {', '.join(str(s) for s in GENERATE_IMAGE_SIZES)}"
    return size, None


def _image_backend(value: Any) -> str:
    return value if isinstance(value, str) and value in GENERATE_IMAGE_BACKENDS else "auto"


async def _plan_generate_image(args: dict[str, Any]) -> dict[str, Any]:
    """The card's plan: which backend would render and its cost verdict, before Run.

    Computed from the ``profile`` the chat pinned — its ceiling decides
    whether the paid path may run (a finite ceiling and an unknown price skip
    it, design §3); a missing pin plans the refusal. Runs nothing: the
    reachability probe of the recorded ComfyUI runtime is the only I/O, off
    the event loop. ``summary`` is the plan line the card shows.
    """
    accepted, ceiling = _image_profile_ceiling(args.get("profile"), home_dir=args.get("profile_home"))
    if not accepted:
        return {"ok": False, "error": IMAGE_PROFILE_REFUSAL, "commands": [], "summary": IMAGE_PROFILE_REFUSAL}
    try:
        from nvh.integrations.imagegen import plan_image_backend
    except ImportError:
        return {"ok": False, "error": IMAGE_UNAVAILABLE_ERROR, "commands": [], "summary": IMAGE_UNAVAILABLE_ERROR}
    raw = await asyncio.to_thread(
        plan_image_backend, cost_ceiling_usd=ceiling, requested=_image_backend(args.get("backend")),
    )
    plan: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    plan.setdefault("commands", [])
    plan["ok"] = plan.get("backend") is not None
    if not plan["ok"]:
        plan.setdefault("error", plan.get("reason") or "no image backend available")
    plan.setdefault("summary", plan.get("reason") or "")
    return plan


async def _tool_generate_image(args: dict[str, Any], *, admission: AtohiAdmission | None = None) -> dict[str, Any]:
    """Render one image through :func:`nvh.integrations.imagegen.render_image`.

    ``profile`` is not in the model-visible schema: the chat pins it onto the
    surfaced call and the web echoes it back, so the ceiling enforced here is
    the turn's profile's, not one the model chose — and a call without a
    valid pin is refused rather than run without a ceiling. The file lands
    under ``$NVH_HOME/outputs/images/<scope>/``; nothing prunes that folder in
    0.46 and the result says so.
    """
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "prompt required (string)"}
    accepted, ceiling = _image_profile_ceiling(args.get("profile"), home_dir=args.get("profile_home"))
    if not accepted:
        return {"ok": False, "error": IMAGE_PROFILE_REFUSAL}
    width, error = _image_dimension(args.get("width"), "width")
    if error:
        return {"ok": False, "error": error}
    height, error = _image_dimension(args.get("height"), "height")
    if error:
        return {"ok": False, "error": error}
    try:
        from nvh.integrations.imagegen import ImageGenerationUnavailable, render_image
    except ImportError:
        return {"ok": False, "error": IMAGE_UNAVAILABLE_ERROR}
    try:
        image = await render_image(
            prompt.strip()[:GENERATE_IMAGE_PROMPT_CHARS],
            width=width, height=height, backend=_image_backend(args.get("backend")),
            cost_ceiling_usd=ceiling, scope=GENERATE_IMAGE_SCOPE,
        )
    except ImageGenerationUnavailable as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "url": image.url,
        "path": image.path,
        "backend": image.backend,
        "model": image.model,
        "width": image.width,
        "height": image.height,
        "cost_usd": image.cost_usd,
        "cost_known": image.cost_known,
        "latency_ms": image.latency_ms,
        "note": f"saved to {image.path}; nvHive does not prune this folder",
    }


# ────────────────────────────────────────────────────────────────────────────
# Tool discovery — entry-points + the plugins directory
# ────────────────────────────────────────────────────────────────────────────

# Distributions that ship Wizard tools (incl. third-party plugins down the
# road) advertise them under this entry-point group. Each entry point should
# resolve to a callable ``register(reg: WizardToolRegistry) -> None`` so
# multi-tool packages don't have to publish one entry per tool. (Provider /
# agent / cabinet plugins use the ``nvhive.plugins`` group — see
# nvh/plugins/manager.py; the two contracts differ.)
ENTRY_POINT_GROUP = "nvh.wizard_tools"

# Workspace-local plugin directory. Drop a Python file with a top-level
# ``register(reg)`` callable in the one plugins directory (``NVH_HOME/plugins``,
# :func:`nvh.plugins.manager.plugins_dir`) and it gets loaded on registry
# build. This is the simplest possible "extend the Wizard" path that doesn't
# need a wheel rebuild. Sandbox is the user's filesystem; same trust boundary
# as their own scripts — but only files that declare ``register`` are ever
# executed here (provider plugins in the same directory are not). Missing
# directories are ignored. The variable overrides the directory outright
# (tests, one-off experiments).
WORKSPACE_PLUGIN_DIR_ENV = "NVH_WIZARD_PLUGIN_DIR"
#: Pre-0.44 location, read (never written) for one more release.
LEGACY_WIZARD_PLUGIN_SUBDIR = "wizard-tools"


def _load_entry_point_tools(reg: ToolRegistry) -> None:
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


def _wizard_plugin_dirs() -> list[Any]:
    """The directories ``.py`` Wizard plugins are read from, in load order."""
    import os as _os
    from pathlib import Path as _Path

    override = _os.environ.get(WORKSPACE_PLUGIN_DIR_ENV)
    if override:
        return [_Path(override).expanduser()]
    try:
        from nvh.plugins.manager import plugins_dir

        primary = plugins_dir()
    except Exception:
        return []
    return [primary, primary.parent / LEGACY_WIZARD_PLUGIN_SUBDIR]


def _load_workspace_plugin_tools(reg: ToolRegistry) -> None:
    """Load ``.py`` Wizard tool plugins from the plugins directory.

    Walks ``$NVH_WIZARD_PLUGIN_DIR`` when set, otherwise the one plugins
    directory (``$NVH_HOME/plugins``) and, for one release, the pre-0.44
    ``$NVH_HOME/wizard-tools`` — through the same
    :func:`nvh.plugins.manager.plugin_files` walk ``nvh plugins`` uses. A file
    is executed only when its source declares a top-level ``register``
    (:func:`nvh.plugins.manager.declares_top_level`, an ``ast`` probe that
    runs nothing): provider / agent plugins (an ``NVHIVE_PLUGIN`` manifest,
    no ``register``) are never imported into the API server process by a
    chat turn. A file that does not parse, raises on import or registers a
    tool without a safety class logs a warning and is skipped — never fatal
    to the rest of the registry build.
    """
    from nvh.plugins.manager import (
        WIZARD_REGISTER_NAME,
        declares_top_level,
        load_plugin_module,
        plugin_files,
    )

    for plugin_dir in _wizard_plugin_dirs():
        for path in plugin_files(plugin_dir):
            try:
                if not declares_top_level(path, WIZARD_REGISTER_NAME):
                    continue
                mod = load_plugin_module(path, f"nvh_wizard_plugin_{path.stem}")
                reg_fn = getattr(mod, WIZARD_REGISTER_NAME, None)
                if callable(reg_fn):
                    reg_fn(reg)
                    logger.info("loaded wizard plugin %s", path.name)
            except Exception as exc:
                logger.warning("wizard plugin %s failed: %s", path.name, exc)


def default_registry(*, admission: AtohiAdmission | None = None) -> WizardToolRegistry:
    """Build the Wizard registry with nvHive's stock tools + any discovered plugins.

    Kept as a builder rather than a module-level singleton so the API layer
    can rebuild it for tests without import-time side effects.

    After the stock tools land, we run two discovery passes:
      1. ``importlib.metadata`` entry points under the ``nvh.wizard_tools``
         group — for packaged plugins installed via pip.
      2. ``.py`` files under the plugins directory — for one-off user tools
         dropped into the rootless home without a wheel rebuild.

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
        handler=partial(_tool_rag_ask, admission=admission),
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
        handler=partial(_tool_rag_ingest, admission=admission),
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
        handler=partial(_tool_rag_ask_vault, admission=admission),
        summary_template="Search your nvHive Vault for: {question}",
    ))

    # 0.46: the dialogue tool. Confirm-class by mechanism — the UI renders a
    # question card and the turn ends; the handler executes nothing and is
    # reached only by a stale client. chat.py offers it to every profile
    # (DIALOGUE_TOOLS, O10) and never on an unattended turn.
    reg.register(WizardTool(
        name="ask_user",
        description=(
            "Ask the user one clarifying question; the answer arrives on the next turn. "
            "Use it only when the context and tools cannot answer it and the choice changes "
            "disk, model size or cost. Your turn ends when you ask."
        ),
        safety_class="confirm",
        parameters={
            "question": {
                "type": "string", "required": True,
                "description": "The one question, in plain words (at most 500 characters).",
                "minLength": 1, "maxLength": ASK_USER_QUESTION_CHARS,
            },
            "choices": {
                "type": "array", "required": False,
                "description": "Up to six short answer options shown as chips (each at most 80 characters).",
                "items": {"type": "string", "maxLength": ASK_USER_CHOICE_CHARS}, "maxItems": ASK_USER_MAX_CHOICES,
            },
            "allow_free_text": {
                "type": "boolean", "required": False,
                "description": "Whether the user may also type a free-text answer (default true).",
                "default": True,
            },
        },
        handler=_tool_ask_user,
        summary_template="Question for you: {question}",
    ))

    # 0.46: the vault memory tool. Confirm-class — the card shows the exact
    # note before it is written; the handler writes only under the process
    # home's ``Wizard Memory``. Not in WIZARD_CORE_AUTO_TOOLS (it writes).
    reg.register(WizardTool(
        name="remember",
        description=(
            "Save a fact the user wants kept across chats as a note in their vault (Wizard Memory). "
            "Compose a short title and body from the conversation; never include keys, passwords, "
            "approval tokens or privileged tool output."
        ),
        safety_class="confirm",
        parameters={
            "title": {
                "type": "string", "required": True,
                "description": "One-line title (at most 120 characters).", "maxLength": REMEMBER_TITLE_CHARS,
            },
            "body": {
                "type": "string", "required": True,
                "description": "The note as the user would want to read it back (at most 2000 characters).",
                "maxLength": REMEMBER_BODY_CHARS,
            },
            "tags": {
                "type": "array", "required": False,
                "description": "Up to eight short tags.", "items": {"type": "string"}, "maxItems": REMEMBER_MAX_TAGS,
            },
        },
        handler=partial(_tool_remember, admission=admission),
        summary_template="Save to your vault: {title}",
    ))

    # 0.46: image generation from chat. Confirm-class with a planner: the
    # card's plan line names the backend (NVIDIA-hosted when NVAPI_KEY is
    # set, else the recorded local ComfyUI) and the cost verdict before Run.
    # ``profile`` is deliberately absent from the schema — the chat pins it.
    reg.register(WizardTool(
        name="generate_image",
        description=(
            "Generate an image from a text prompt and save it under the workspace's outputs/images folder. "
            "NVIDIA-hosted when NVAPI_KEY is set, otherwise the local ComfyUI; the card shows which "
            "backend will render and what it costs before anything runs."
        ),
        safety_class="confirm",
        parameters={
            "prompt": {
                "type": "string", "required": True,
                "description": "What to draw (at most 2000 characters).",
                "minLength": 1, "maxLength": GENERATE_IMAGE_PROMPT_CHARS,
            },
            "width": {
                "type": "integer", "required": False,
                "description": "Image width in pixels: 512, 768 or 1024 (default 1024).",
                "enum": list(GENERATE_IMAGE_SIZES),
            },
            "height": {
                "type": "integer", "required": False,
                "description": "Image height in pixels: 512, 768 or 1024 (default 1024).",
                "enum": list(GENERATE_IMAGE_SIZES),
            },
            "backend": {
                "type": "string", "required": False,
                "description": (
                    "auto (NVIDIA-hosted when NVAPI_KEY is set, else local ComfyUI), "
                    "local (ComfyUI on this machine only) or nvidia."
                ),
                "enum": list(GENERATE_IMAGE_BACKENDS),
            },
        },
        handler=partial(_tool_generate_image, admission=admission),
        planner=_plan_generate_image,
        summary_template="Generate an image: {prompt}",
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

    # Home Assistant (2026-09-02): four smart-home reads run auto; the one
    # write (home_assistant_call) is confirm-class so the WebUI shows the
    # exact service call before anything switches. Registered even when
    # HASS_URL/HASS_TOKEN are unset — the handlers then return the setup
    # hint without network I/O, so the Wizard can explain how to connect.
    from nvh.integrations.home_assistant import register_wizard_tools as _register_home_assistant

    _register_home_assistant(reg)

    # System settings (2026-09-03, the Spark concierge's privileged tier):
    # two read-only auto tools (facts, dry-run plan) and four ``privileged``
    # ones (apply, apt/snap install, enable a service). Registered even with
    # NVH_ALLOW_PRIVILEGED=0 — execute() refuses them, the catalogue can
    # still explain them.
    from nvh.integrations.wizard.system_settings import (
        register_wizard_tools as _register_system_settings,
    )

    _register_system_settings(reg)

    # Spark playbooks (2026-09-03, design brief phase 2b): the upstream DGX
    # Spark install guides as approved runs. ``playbook_list`` / ``playbook_plan``
    # are auto (catalogue + receipt status, dry run); ``playbook_install`` is
    # privileged — the red card carries the compiled plan, the confirmed call
    # starts a ``playbook-run`` job that audits itself when it finishes.
    from nvh.integrations.installs.playbooks import register_wizard_tools as _register_playbooks

    _register_playbooks(reg)

    # The sandbox bridge (2026-09-03, design brief phase 3): the core agent's
    # ``shell`` (privileged — the red card renders the command and how it
    # will run: Docker sandbox or directly on this machine) and ``run_code``
    # (confirm; Docker required, refused in-band without it). Both deny lists
    # run before anything spawns; every shell run is audited under Decisions.
    from nvh.integrations.wizard.sandbox_tools import register_wizard_tools as _register_sandbox

    _register_sandbox(reg)

    # The vision bridge (2026-09-03, design brief phase 3): ``analyze_image``
    # and ``read_text_from_image`` (auto) behind a path allowlist, an
    # image-only rule and a cloud rule — the chat's attached images reach the
    # model only through these two.
    from nvh.integrations.wizard.vision_bridge import register_wizard_tools as _register_vision

    _register_vision(reg, admission=admission)

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
