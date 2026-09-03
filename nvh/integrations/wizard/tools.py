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
so they can chain into other engine async paths cleanly.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
SafetyClass = str  # "auto" | "confirm" | "privileged" — "never" is never registered

#: The safety classes ``register()`` accepts, in the order ``list_tools()`` uses.
SAFETY_CLASSES: tuple[str, ...] = ("auto", "confirm", "privileged")
_SAFETY_ORDER = {name: index for index, name in enumerate(SAFETY_CLASSES)}

#: Kill switch for the ``privileged`` class. Unset means on; the falsy
#: vocabulary matches ``_platform_warmup_enabled`` in nvh/api/server.py.
PRIVILEGED_ENV = "NVH_ALLOW_PRIVILEGED"
_FALSY = frozenset({"0", "false", "no", "off"})
PRIVILEGED_DISABLED_ERROR = f"privileged tools are disabled ({PRIVILEGED_ENV}=0)"

#: Characters a tool result may occupy in the model's ``TOOL_RESULT`` message
#: (chat.py imports this for its cut); privileged results are fitted to it
#: *before* they leave ``execute()`` so the cut never lands mid-JSON.
TOOL_RESULT_CHARS = 1500
#: Per-command output kept in the vault audit note (redacted first).
AUDIT_OUTPUT_CHARS = 4000

#: How long a red card's approval token stays valid (seconds). A card left
#: open across lunch has to be re-issued; a leaked token dies with it.
APPROVAL_TTL_S = 15 * 60
#: Refusal for a confirmed privileged call that did not bring its card's token.
APPROVAL_REQUIRED_ERROR = "privileged call needs the approval token from its card"
#: Process-lifetime HMAC key for approval tokens. Never persisted, never
#: exposed; a restart invalidates every outstanding card, which is the point.
_APPROVAL_SECRET = secrets.token_bytes(32)
#: Tokens already spent, token → expiry. Bounded so a flood cannot grow it.
_CONSUMED_APPROVALS: dict[str, float] = {}
_CONSUMED_MAX = 1024


def privileged_enabled() -> bool:
    """``NVH_ALLOW_PRIVILEGED=0`` (or false/no/off) disables every ``privileged`` tool.

    Default on. Registration is unaffected — the catalogue still lists the
    tools with ``enabled: false`` so the Wizard can explain what they would
    do — but ``execute()`` refuses them on the card path and the confirmed
    path alike, naming the variable.
    """
    return os.environ.get(PRIVILEGED_ENV, "1").strip().lower() not in _FALSY


def _canonical_call(name: str, arguments: Mapping[str, Any] | None) -> str:
    """``name`` + newline + the arguments as sorted, compact JSON — the bytes a token signs."""
    return name + "\n" + json.dumps(
        dict(arguments or {}), sort_keys=True, separators=(",", ":"), default=str,
    )


def _approval_mac(name: str, arguments: Mapping[str, Any] | None, issued: int, nonce: str) -> str:
    message = f"{_canonical_call(name, arguments)}\n{issued}\n{nonce}".encode()
    digest = hmac.new(_APPROVAL_SECRET, message, "sha256").digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def issue_approval(
    name: str, arguments: Mapping[str, Any] | None, *, now: float | None = None,
) -> dict[str, Any]:
    """Mint the token a privileged card carries: ``{approval_token, approval_expires_at}``.

    ``approval_token`` is ``base64url(HMAC-SHA256(secret, name \\n canonical
    arguments \\n issued \\n nonce)) . issued . nonce`` with ``issued`` in
    whole seconds since the epoch and a random ``nonce`` so two cards for
    the same call in the same second are still two tokens (each spent
    separately); ``approval_expires_at`` is ``issued + APPROVAL_TTL_S``.
    Bound to the exact name and arguments shown on the card, so a token
    cannot be re-aimed. ``now`` exists for tests.
    """
    issued = int(now if now is not None else time.time())
    nonce = secrets.token_urlsafe(8)
    return {
        "approval_token": f"{_approval_mac(name, arguments, issued, nonce)}.{issued}.{nonce}",
        "approval_expires_at": issued + APPROVAL_TTL_S,
    }


def _prune_consumed(current: float) -> None:
    for token, expires in list(_CONSUMED_APPROVALS.items()):
        if expires <= current:
            del _CONSUMED_APPROVALS[token]
    while len(_CONSUMED_APPROVALS) > _CONSUMED_MAX:
        _CONSUMED_APPROVALS.pop(next(iter(_CONSUMED_APPROVALS)))


def verify_approval(
    name: str, arguments: Mapping[str, Any] | None, token: Any, *, now: float | None = None,
) -> bool:
    """Is ``token`` a live, unspent approval for exactly this call? Spends it when so.

    Constant-time MAC comparison; refuses anything older than
    :data:`APPROVAL_TTL_S`, issued in the future, malformed, minted for a
    different name or different arguments, or already used. Never raises.
    """
    if not isinstance(token, str) or token.count(".") != 2:
        return False
    mac, issued_raw, nonce = token.split(".")
    if not mac or not nonce or not issued_raw.isdigit():
        return False
    issued = int(issued_raw)
    current = now if now is not None else time.time()
    if issued > current + 5 or current - issued > APPROVAL_TTL_S:
        return False
    expected = _approval_mac(name, arguments, issued, nonce)
    if not hmac.compare_digest(mac.encode("ascii", "replace"), expected.encode("ascii")):
        return False
    _prune_consumed(current)
    if token in _CONSUMED_APPROVALS:
        return False
    _CONSUMED_APPROVALS[token] = issued + APPROVAL_TTL_S
    return True


class _MissingArgs(dict):
    """``format_map`` mapping that renders unknown placeholders as ``?``."""

    def __missing__(self, key: str) -> str:
        return "?"


def format_summary(template: str, arguments: Mapping[str, Any] | None) -> str:
    """Render a ``summary_template`` against model-supplied arguments; never raises.

    The model decides which arguments it sends, so a required name may be
    missing (``KeyError``), a placeholder may index into a string
    (``{a[0]}``), or the template may use positional fields. Missing names
    render as ``?``; anything else falls back to the raw template so the
    confirmation card still shows *something* instead of the HTTP layer
    turning a formatting slip into a 500.
    """
    if not template:
        return ""
    try:
        return template.format_map(_MissingArgs(arguments or {}))
    except Exception:
        return template


@dataclass(frozen=True)
class WizardTool:
    """One executable capability the Wizard can request.

    Attributes:
        name: Stable identifier the LLM emits when it wants to call this tool.
        description: One-line user-facing description; shown in confirm cards.
        safety_class: "auto" (run without asking), "confirm" (user clicks) or
            "privileged" (user clicks a red card; sudo-class host change).
        parameters: JSON-schema-ish dict of {param_name: {type, description, required?}}.
        handler: Async callable that takes the param dict and returns a result dict.
        summary_template: User-facing one-liner that gets formatted with the
            executed args. The UI shows this on the confirmation card.
        planner: Optional dry run with the handler's signature. For
            ``privileged`` tools ``execute()`` calls it on the unconfirmed
            path and puts its answer on the card as ``plan`` — the exact
            commands, whether sudo is needed, what changes, how to undo —
            without running anything.
    """

    name: str
    description: str
    safety_class: SafetyClass
    parameters: dict[str, Any]
    handler: ToolHandler
    summary_template: str = ""
    planner: ToolHandler | None = None

    @property
    def enabled(self) -> bool:
        """False only for a ``privileged`` tool while the kill switch is off."""
        return self.safety_class != "privileged" or privileged_enabled()

    def as_public_dict(self) -> dict[str, Any]:
        """Return the schema fields the LLM and UI can see (no handler)."""
        return {
            "name": self.name,
            "description": self.description,
            "safety_class": self.safety_class,
            "parameters": self.parameters,
            "summary_template": self.summary_template,
            "enabled": self.enabled,
        }


class WizardToolRegistry:
    """Lookup table for the Wizard's executable tools.

    Safety enforcement lives here, not in the handlers: registering a tool
    with ``safety_class="never"`` raises immediately so the constant can't
    drift past code review. ``execute()`` rejects ``confirm`` and
    ``privileged`` calls that arrive without ``confirmed=True``, and refuses
    ``privileged`` calls outright while :func:`privileged_enabled` is False.
    ``execute()`` is the only enforcement point — the HTTP layer and the chat
    loop both call it and add nothing — so the kill switch is checked here on
    every call, not at registration.
    """

    def __init__(self) -> None:
        self._tools: dict[str, WizardTool] = {}

    def register(self, tool: WizardTool) -> None:
        if tool.safety_class == "never":
            raise ValueError(
                f"Tool '{tool.name}' has safety_class=never — never-class operations "
                "are admin-only paths, not registry tools.",
            )
        if tool.safety_class not in SAFETY_CLASSES:
            raise ValueError(
                f"Tool '{tool.name}' has unknown safety_class '{tool.safety_class}'. "
                "Allowed: 'auto', 'confirm', 'privileged'.",
            )
        if tool.name in self._tools:
            logger.warning("Overwriting wizard tool '%s'", tool.name)
        self._tools[tool.name] = tool

    def get(self, name: str) -> WizardTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[WizardTool]:
        """Tools ordered auto, confirm, privileged (then by name) — an explicit key,
        so the classes' spelling never decides the catalogue order."""
        return sorted(
            self._tools.values(),
            key=lambda t: (_SAFETY_ORDER.get(t.safety_class, len(SAFETY_CLASSES)), t.name),
        )

    async def plan(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """The dry run for ``name`` — what a privileged tool *would* execute.

        Runs nothing. ``None`` for an unknown tool or one without a planner;
        a planner that raises becomes ``{ok: False, error, commands: []}``.
        This is what the unconfirmed card carries as ``plan`` and what
        ``chat.py`` puts on a surfaced privileged call.
        """
        tool = self.get(name)
        if tool is None:
            return None
        return await _dry_run(tool, arguments or {})

    async def execute(
        self,
        name: str,
        *,
        arguments: dict[str, Any] | None = None,
        confirmed: bool = False,
        approval_token: str | None = None,
    ) -> dict[str, Any]:
        """Run a tool by name. Returns ``{ok, result?, error?, needs_confirmation?}``.

        - ``auto`` tools run regardless of ``confirmed``.
        - ``confirm`` tools require ``confirmed=True``; otherwise return a
          structured "I need a confirmation" response so the UI can render
          the button card.
        - ``privileged`` tools: refused (``disabled=True``) whenever the kill
          switch is off, confirmed or not. Unconfirmed, the confirmation shape
          above plus ``privileged=True``, ``plan`` (the tool's dry run, or
          ``None`` when it has no planner) and the card's ``approval_token``
          / ``approval_expires_at`` (:func:`issue_approval`). Confirmed, the
          call must bring a token valid for exactly this name and these
          arguments (:func:`verify_approval`) or it is refused with
          ``approval_required=True`` and nothing runs; then the handler runs,
          an apply that changed the host (complete, partial or failed) is
          recorded in the vault (``audit``) and the result is fitted to the
          tool-result window.
        - Unknown tools return ``ok=False`` with an error.

        Handlers never raise out of here: an exception becomes ``ok=False``.
        """
        tool = self.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}

        privileged = tool.safety_class == "privileged"
        if privileged and not privileged_enabled():
            return {
                "ok": False,
                "error": PRIVILEGED_DISABLED_ERROR,
                "disabled": True,
                "tool": name,
                "safety_class": tool.safety_class,
            }

        if tool.safety_class != "auto" and not confirmed:
            card: dict[str, Any] = {
                "ok": False,
                "needs_confirmation": True,
                "tool": tool.as_public_dict(),
                "arguments": arguments or {},
                "summary": format_summary(tool.summary_template, arguments) or tool.description,
            }
            if privileged:
                card["privileged"] = True
                card["plan"] = await self.plan(name, arguments or {})
                card.update(issue_approval(name, arguments or {}))
            return card

        if privileged and not verify_approval(name, arguments or {}, approval_token):
            return {
                "ok": False,
                "error": APPROVAL_REQUIRED_ERROR,
                "approval_required": True,
                "tool": name,
                "safety_class": tool.safety_class,
            }

        try:
            result = await tool.handler(arguments or {})
        except Exception as exc:
            logger.warning("Wizard tool '%s' raised: %s", name, exc)
            return {"ok": False, "error": str(exc)[:300], "tool": name}

        envelope: dict[str, Any] = {"ok": True, "result": result, "tool": name, "safety_class": tool.safety_class}
        if privileged:
            if _privileged_applied(result):
                envelope["audit"] = record_privileged_change(tool, arguments or {}, result)
            if isinstance(result, dict):
                envelope["result"] = fit_tool_window(result)
        return envelope


async def _dry_run(tool: WizardTool, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """The plan a privileged tool would execute; ``None`` without a planner, never raises."""
    if tool.planner is None:
        return None
    try:
        plan = await tool.planner(arguments)
    except Exception as exc:
        logger.warning("Wizard tool '%s' planner raised: %s", tool.name, exc)
        return {"ok": False, "error": f"dry run failed: {str(exc)[:200]}", "commands": []}
    return plan if isinstance(plan, dict) else {"ok": True, "commands": [], "detail": str(plan)[:300]}


def _privileged_applied(result: Any) -> bool:
    """Did a privileged handler actually change the host?

    ``applied: True`` is authoritative whatever ``ok`` says — a plan that
    failed at step 3 changed the host in steps 1–2, and a single command
    that exited non-zero may have changed it before failing (``systemctl
    enable --now`` with a bad ExecStart enables the unit, ``apt-get`` exiting
    100 after unpacking); both get a vault note. ``applied: False`` is
    authoritative too: the handler says it touched nothing yet — a job it
    started (``playbook_install``) audits itself when it finishes, having
    seen what actually ran. Otherwise refusals (``ok: False``), terminal
    hand-offs (``needs_terminal``) and non-dict answers are not applies and
    get none.
    """
    if not isinstance(result, dict):
        return False
    if result.get("applied") is True:
        return True
    if result.get("applied") is False:
        return False
    return result.get("ok", True) is not False and not result.get("needs_terminal")


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _shrink_text(value: Any, budget: int) -> Any:
    if not isinstance(value, str) or len(value) <= budget:
        return value
    return value[:budget] + "…"


def _shrink_list(value: Any, keep: int) -> Any:
    if not isinstance(value, list) or len(value) <= keep:
        return value
    return value[:keep] + [f"… {len(value) - keep} more"]


#: Free text that is shrunk first, top level and inside ``steps``.
_WINDOW_TEXT_KEYS = ("stdout", "stderr", "output", "changes")
#: Lists that are shortened before anything is dropped.
_WINDOW_LIST_KEYS = ("undo", "notes", "commands")
#: What the last resort keeps: the verdict and every field the hand-off and
#: refusal contracts depend on. ``command`` is never cut — a truncated command
#: pasted into a terminal is worse than a long tool window.
_WINDOW_KEEP_KEYS = (
    "ok", "error", "summary", "setting", "needs_terminal", "command", "commands", "hint",
    "denied", "disabled", "applied", "partial", "truncated", "note",
)


def fit_tool_window(result: dict[str, Any], limit: int = TOOL_RESULT_CHARS) -> dict[str, Any]:
    """Cut a tool result so its JSON fits the model's tool-result window.

    Shrinks the free-text fields first (top-level ``stdout`` / ``stderr`` /
    ``output`` / ``changes`` and the same keys inside ``steps``) and shortens
    the list fields (``undo``, ``notes``, ``commands``) in ever smaller
    budgets, marking the result ``truncated`` with a note pointing at the
    vault note. The last resort keeps the verdict plus the hand-off and
    refusal fields (``needs_terminal``, ``command``, ``hint``, ``denied``,
    ``applied``, ``partial``, …) and drops the rest, listing them in
    ``dropped_keys``; if even that is over the limit, ``commands`` (a copy of
    ``command`` for a one-step plan) goes too, but ``command`` itself is
    never shortened. Returns the input untouched when it already fits.
    Never raises.
    """
    if len(_dumps(result)) <= limit:
        return result
    out: dict[str, Any] = dict(result)
    out["truncated"] = True
    out["note"] = f"output cut to fit the {limit}-char tool window; the vault Decisions note keeps more"
    for budget, keep_items in ((600, 12), (300, 6), (120, 3), (40, 1), (0, 1)):
        for key in _WINDOW_TEXT_KEYS:
            if key in out:
                out[key] = _shrink_text(out[key], budget)
        steps = out.get("steps")
        if isinstance(steps, list):
            out["steps"] = [
                {k: (_shrink_text(v, budget) if k in _WINDOW_TEXT_KEYS else v) for k, v in step.items()}
                if isinstance(step, dict) else step
                for step in steps
            ]
        for key in _WINDOW_LIST_KEYS:
            if key in out:
                out[key] = _shrink_list(out[key], keep_items)
        if len(_dumps(out)) <= limit:
            return out
    # Still too big (a handler stuffed something else in): keep the verdict
    # and the fields the hand-off / refusal contracts need.
    keep = {k: out[k] for k in _WINDOW_KEEP_KEYS if k in out}
    keep["dropped_keys"] = sorted(k for k in out if k not in keep)
    if len(_dumps(keep)) > limit and "commands" in keep:
        del keep["commands"]
        keep["dropped_keys"] = sorted([*keep["dropped_keys"], "commands"])
    return keep


def _audit_body(name: str, arguments: Mapping[str, Any], result: dict[str, Any]) -> str:
    """Markdown body of the vault note for one privileged apply (``name`` is the tool's)."""
    from nvh.core.agent_guardrails import redact_secrets

    try:
        from nvh.utils.platform_facts import detect_platform_facts

        device = detect_platform_facts().device_label or "unknown device"
    except Exception:
        device = "unknown device"

    lines = [
        f"Tool: `{name}`",
        f"Device: {device}",
        f"Arguments: `{redact_secrets(_dumps(arguments))[:500]}`",
        f"Outcome: {_audit_outcome(result)}",
    ]
    summary = result.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(f"Summary: {redact_secrets(summary.strip())[:500]}")
    steps = result.get("steps")
    if isinstance(steps, list) and steps:
        lines += ["", "## Commands", ""]
        for index, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                lines.append(f"{index}. `{redact_secrets(str(step))[:500]}`")
                continue
            command = redact_secrets(str(step.get("command", "")))[:500]
            exit_code = step.get("exit_code", "n/a")
            lines.append(f"{index}. `{command}` — exit {exit_code}")
            for stream in ("stdout", "stderr"):
                text = step.get(stream)
                if isinstance(text, str) and text.strip():
                    body = redact_secrets(text.strip())
                    if len(body) > AUDIT_OUTPUT_CHARS:
                        body = body[:AUDIT_OUTPUT_CHARS] + f"\n[cut at {AUDIT_OUTPUT_CHARS} chars]"
                    lines += ["", f"{stream}:", "", "```text", body, "```"]
        lines.append("")
    else:
        lines += ["", "## Result", "", "```json", redact_secrets(_dumps(result))[:AUDIT_OUTPUT_CHARS], "```"]
    return "\n".join(lines)


def _audit_verdict(result: dict[str, Any]) -> str:
    """``""`` for a clean apply, ``" (partial)"`` or ``" (failed)"`` otherwise — the title suffix."""
    if result.get("ok") is False:
        return " (partial)" if result.get("partial") else " (failed)"
    return ""


def _audit_outcome(result: dict[str, Any]) -> str:
    verdict = _audit_verdict(result).strip(" ()") or "applied"
    error = result.get("error")
    if verdict != "applied" and isinstance(error, str) and error.strip():
        from nvh.core.agent_guardrails import redact_secrets

        return f"{verdict} — {redact_secrets(error.strip())[:300]}"
    return verdict


def audit_privileged_change(
    name: str,
    arguments: Mapping[str, Any] | None,
    result: dict[str, Any],
    *,
    summary: str = "",
    home_dir: Any = None,
) -> dict[str, Any]:
    """Write the vault audit note for a privileged change made under tool ``name``. Never raises.

    The shared sink: :func:`record_privileged_change` calls it from
    ``execute()`` for a tool's own apply, and the playbook job runner calls it
    when a ``playbook-run`` finishes, having seen what ran — no
    :class:`WizardTool` needed, only the name, the arguments and a result in
    the apply shape (``ok``, ``applied``, ``partial``, ``error``, ``summary``,
    ``steps`` with ``command`` / ``exit_code`` / output).

    ``Decisions/`` in the vault (``append_vault_memory``), titled
    ``Privileged change: <summary>`` — ``Privileged change (partial): …`` when
    later steps never ran, ``Privileged change (failed): …`` when the command
    that ran exited non-zero — body with the outcome, the commands, exit
    codes, truncated redacted output and the platform's device label; tags
    ``privileged`` and ``name``. ``summary`` falls back to ``result.summary``
    then to ``name``. The vault is the one under ``NVH_HOME`` unless the
    *caller's code* passes ``home_dir`` (the CLI's ``--home``); nothing in
    ``arguments`` — the model wrote those — can point the note anywhere else.
    Returns the writer's status (``saved``/``path``/``category``) or
    ``{saved: False, error}``.
    """
    try:
        from nvh.integrations.workspace.vault import append_vault_memory

        result_summary = result.get("summary") if isinstance(result.get("summary"), str) else ""
        title = (result_summary or summary or name).strip()
        note = append_vault_memory(
            f"Privileged change{_audit_verdict(result)}: {title[:80]}",
            _audit_body(name, dict(arguments or {}), result),
            category="Decisions",
            tags=["privileged", name],
            home_dir=home_dir,
        )
        return {"saved": bool(note.get("saved")), "path": note.get("path"), "category": note.get("category")}
    except Exception as exc:
        logger.warning("privileged audit note for '%s' not written: %s", name, exc)
        return {"saved": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def record_privileged_change(
    tool: WizardTool, arguments: dict[str, Any], result: dict[str, Any],
) -> dict[str, Any]:
    """Write the audit note for a privileged apply that touched the host. Never raises.

    ``execute()``'s sink for a tool's own apply: :func:`audit_privileged_change`
    under the tool's name, with the card's summary
    (``summary_template`` rendered against the arguments) when the result
    carries none. The vault is the one under ``NVH_HOME``.
    """
    return audit_privileged_change(
        tool.name, arguments, result,
        summary=format_summary(tool.summary_template, arguments) or tool.name,
    )


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
