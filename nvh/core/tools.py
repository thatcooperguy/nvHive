"""NVHive Tool System — one ``Tool``, one ``ToolRegistry``, two instances.

Tools are registered async functions an LLM can call during a query. This
module is the single home of the tool model (issue #132, 0.44 D2/D3):

  - :class:`Tool` — name, description, JSON-Schema ``input_schema``, handler,
    ``safety_class`` (``auto`` | ``confirm`` | ``privileged``), an optional
    ``summary_template`` for confirmation cards and an optional ``planner``
    (dry run) for privileged tools. ``safe`` (``== auto``) is kept as a
    derived property for older callers. The Wizard prompt/UI shape
    ``{name: {type, description, required}}`` is *derived* from the schema by
    :func:`translate_parameters` — it is never hand-typed twice.
  - :class:`ToolRegistry` — registration with safety validation, lookup, the
    tool catalogue, and one ``execute()`` carrying every enforcement rule:
    the ``NVH_ALLOW_PRIVILEGED`` kill switch, confirmation cards, HMAC
    approval tokens (15 min, single use, argument-bound), the vault audit of
    privileged applies, the tool-result window, and the core guardrails
    (``check_command`` on the right argument per tool, ``check_path``,
    redaction, truncation).
  - :class:`ToolResult` — the one execute() envelope: a JSON-able dict
    (``ok``, ``result``/``error``, ``tool``, ``safety_class``, card fields)
    that also answers the agent loop's ``success`` / ``output`` / ``error`` /
    ``tool_name`` attributes.

There are still TWO registry *instances*:

  - the agent registry (``ToolRegistry()``: built-ins + system, browser and
    vision tools) used by ``nvh do``, the REPL and the coding agent. Its
    callers own the confirmation click (the CLI's prompt, the agent loop's
    ``confirm_unsafe`` callback), so ``enforce_confirmation`` is off there —
    privileged tools still need an approval token whatever the instance.
  - the Wizard registry (``nvh.integrations.wizard.tools.default_registry()``):
    the curated set plus the sandbox/vision bridges, MCP servers and plugins,
    with ``enforce_confirmation`` on: nothing that is not exactly ``auto``
    runs without a click. Desktop-hands tools (``mouse_*``, ``keyboard_*``,
    ``scroll``) are never registered into it.

Safety classes
==============

  - ``auto``       — idempotent, read-only or trivially reversible.
  - ``confirm``    — meaningful side effect; the UI must ask.
  - ``privileged`` — changes the machine (usually via ``sudo``): a red card
                     with the dry-run ``plan``, the kill switch re-checked on
                     the card and the confirmed path, an ``approval_token``
                     the click must bring back, a vault ``Decisions/`` note
                     for every apply that touched the host, and a result cut
                     to the tool-result window (``command`` never cut).
  - ``never``      — not a registry class: ``register()`` refuses it.

Text protocol (the one taught and parsed everywhere)
====================================================

Models without native function calling emit one line per call::

    TOOL_CALL: {"name": "<tool_name>", "arguments": {...}}

:func:`parse_tool_calls` reads it back. The pre-0.44 fenced ```` ```tool_call ````
block and the bare ``{"tool": ..., "args": {...}}`` object are still parsed
for one release on the agent-loop path only (``legacy=True``; never emitted,
never taught, never read by the Wizard) and go away in 0.45.

Native function calling (D3) degrades: ``Provider.complete/stream`` accept
``tools=`` / ``tool_choice=`` and answer ``tool_calls`` in the OpenAI wire
shape; :func:`normalize_tool_calls` turns either that or a parsed text call
into ``{id, name, arguments}``; :func:`provider_supports_native_tools` asks
the provider whether the picked model can take ``tools=`` at all.
"""

from __future__ import annotations

import base64
import glob as globmod
import hmac
import inspect
import json
import logging
import os
import pathlib
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from nvh.core import agent_guardrails as _guardrails

logger = logging.getLogger(__name__)

ToolHandler = Callable[..., Awaitable[Any]]
SafetyClass = str  # "auto" | "confirm" | "privileged" — "never" is never registered

#: The safety classes ``register()`` accepts, in the order ``list_tools()`` uses.
SAFETY_CLASSES: tuple[str, ...] = ("auto", "confirm", "privileged")
_SAFETY_ORDER = {name: index for index, name in enumerate(SAFETY_CLASSES)}

#: How a handler takes its arguments: ``kwargs`` (``handler(**arguments)``,
#: the core tools) or ``mapping`` (``handler(arguments)``, the Wizard tools).
HANDLER_STYLES: tuple[str, ...] = ("kwargs", "mapping")

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

#: Opt-in: register the cached MCP tools into the *agent* registry too
#: (``nvh do`` / REPL). Off by default so ``nvh do`` is unchanged and the
#: registry build never depends on the machine's MCP cache.
AGENT_MCP_TOOLS_ENV = "NVH_AGENT_MCP_TOOLS"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: The one text protocol marker.
TOOL_CALL_MARKER = "TOOL_CALL:"


# ────────────────────────────────────────────────────────────────────────────
# Kill switch and approval tokens
# ────────────────────────────────────────────────────────────────────────────


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


def reset_approvals() -> None:
    """Forget every spent approval token (test reset hook; the secret stays)."""
    _CONSUMED_APPROVALS.clear()


# ────────────────────────────────────────────────────────────────────────────
# Summaries and parameter shapes
# ────────────────────────────────────────────────────────────────────────────


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


def _json_schema_type(spec: Any) -> str:
    """The single type name a JSON-Schema property means for the Wizard shape.

    ``"string"`` when unspecified; a nullable list (``["string", "null"]``)
    is its first non-null member; a type-less ``anyOf`` / ``oneOf`` is the
    first member that names a type.
    """
    if not isinstance(spec, dict):
        return "string"
    declared = spec.get("type")
    if isinstance(declared, str) and declared and declared != "null":
        return declared
    if isinstance(declared, list):
        for item in declared:
            if isinstance(item, str) and item and item != "null":
                return item
    for key in ("anyOf", "oneOf"):
        for member in spec.get(key) or []:
            found = _json_schema_type(member)
            if found != "string" or (isinstance(member, dict) and member.get("type") == "string"):
                return found
    return "string"


def parameters_from_json_schema(
    schema: Mapping[str, Any] | None, descriptions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A JSON-Schema ``{"type": "object", "properties": …, "required": […]}`` as the Wizard shape.

    ``{name: {type, description, required}}`` — the one translation the
    prompt, the UI and the MCP adapter use, so nullable types and missing
    descriptions are handled in one place. ``descriptions`` fills in what the
    schema left blank.
    """
    schema = schema or {}
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    overlay = descriptions or {}
    return {
        key: {
            "type": _json_schema_type(val),
            "description": overlay.get(key) or (val or {}).get("description", ""),
            "required": key in required,
        }
        for key, val in properties.items()
    }


#: D2's name for the one translation; ``parameters_from_json_schema`` is the
#: name the Phase 3 bridge shipped under.
translate_parameters = parameters_from_json_schema


def json_schema_from_parameters(parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    """The Wizard shape ``{name: {type, description, required, …}}`` as a JSON-Schema object.

    The inverse of :func:`translate_parameters` for tools still declared in
    the Wizard shape: ``required`` becomes the schema's ``required`` list,
    everything else on a parameter (``type``, ``description``, ``enum``,
    ``items``, …) is carried into its property.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for key, spec in (parameters or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        prop = {k: v for k, v in spec.items() if k != "required"}
        prop.setdefault("type", "string")
        if not prop.get("description"):
            prop.pop("description", None)
        properties[str(key)] = prop
        if spec.get("required"):
            required.append(str(key))
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _normalize_schema(parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    """``parameters`` as a JSON-Schema object, whichever shape it was declared in.

    A dict with ``properties`` (or ``type: object``, ``$schema``, ``anyOf`` /
    ``oneOf`` / ``allOf``) is already a schema; a dict whose values are all
    dicts is the Wizard shape and is converted; empty means "no parameters".
    """
    if not parameters:
        return {"type": "object", "properties": {}}
    params = dict(parameters)
    if (
        "properties" in params
        or params.get("type") == "object"
        or "$schema" in params
        or any(key in params for key in ("anyOf", "oneOf", "allOf"))
    ):
        params.setdefault("type", "object")
        params.setdefault("properties", {})
        return params
    if all(isinstance(value, dict) for value in params.values()):
        return json_schema_from_parameters(params)
    return {"type": "object", "properties": {}, **params}


def _pinned_arguments(plan: Any) -> dict[str, Any]:
    """The ``pinned_arguments`` a plan declares (string keys), else ``{}``."""
    if not isinstance(plan, dict):
        return {}
    pinned = plan.get("pinned_arguments")
    if not isinstance(pinned, dict):
        return {}
    return {str(key): value for key, value in pinned.items()}


# ────────────────────────────────────────────────────────────────────────────
# The text protocol and native tool calls
# ────────────────────────────────────────────────────────────────────────────

_MARKER_RE = re.compile(r"TOOL_CALL\s*:\s*", re.IGNORECASE)
# Deprecated (parsed through 0.44 by the agent loop only — ``legacy=True`` —
# dropped in 0.45): the pre-0.44 agent-loop fenced block and its bare inline
# object. Never emitted, never taught, never read on the Wizard path.
_FENCED_RE = re.compile(r"```tool_call\s*\n(.*?)\n\s*```", re.DOTALL | re.IGNORECASE)
_INLINE_LEGACY_RE = re.compile(r'\{\s*"tool"\s*:\s*"[^"]+"\s*,\s*"args"\s*:\s*\{')


def format_tool_call(name: str, arguments: Mapping[str, Any] | None = None) -> str:
    """The one text form of a call: ``TOOL_CALL: {"name": …, "arguments": {…}}``."""
    return f"{TOOL_CALL_MARKER} " + json.dumps(
        {"name": name, "arguments": dict(arguments or {})}, default=str,
    )


def _json_object_span(text: str, start: int) -> int | None:
    """Index just past the JSON object opening at ``text[start] == "{"``; ``None`` when unbalanced."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _call_from_object(obj: Any) -> dict[str, Any] | None:
    """``{name, arguments}`` from one parsed call object in any accepted spelling, or ``None``."""
    if not isinstance(obj, dict):
        return None
    function = obj.get("function")
    if isinstance(function, dict):
        obj = {**function, "id": obj.get("id")}
    name = obj.get("name", obj.get("tool"))
    if not isinstance(name, str) or not name.strip():
        return None
    arguments = obj.get("arguments", obj.get("args", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            arguments = {}
    call: dict[str, Any] = {"name": name.strip(), "arguments": arguments if isinstance(arguments, dict) else {}}
    if isinstance(obj.get("id"), str) and obj["id"]:
        call["id"] = obj["id"]
    return call


def _parse_call_json(body: str) -> dict[str, Any] | None:
    try:
        return _call_from_object(json.loads(body))
    except ValueError:
        return None


def _line_end(text: str, index: int) -> int:
    newline = text.find("\n", index)
    return len(text) if newline < 0 else newline


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(start < span[1] and span[0] < end for start, end in spans)


def parse_tool_calls(text: str, *, legacy: bool = False) -> tuple[str, list[dict[str, Any]]]:
    """Read every tool call out of a model's text; ``(text without the calls, [{name, arguments}])``.

    The protocol is ``TOOL_CALL: {json}`` — the JSON object may span lines and
    nest freely; ``name``/``arguments`` (the taught spelling) and
    ``tool``/``args`` are both read. A marker whose JSON is malformed or
    unbalanced is stripped from the text and dropped (the user never sees a
    raw marker, the loop never crashes); a marker that is *not* followed by a
    JSON object is prose about the protocol ("write TOOL_CALL: followed by
    JSON") and is left exactly as written. Repeated identical calls collapse
    to one.

    ``legacy=True`` — the agent loop only, for one release — also reads the
    pre-0.44 fenced ```` ```tool_call ```` block and the bare
    ``{"tool": …, "args": {…}}`` object. The Wizard never emitted or taught
    those, so its path keeps the default: a quoted log line or pasted config
    that happens to contain such an object is never a call.
    """
    if not text:
        return "", []
    calls: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []

    def _add(call: dict[str, Any] | None, span: tuple[int, int]) -> None:
        spans.append(span)
        if call is not None and call not in calls:
            calls.append(call)

    for match in _MARKER_RE.finditer(text):
        brace = match.end()
        if brace < len(text) and text[brace] == "{":
            end = _json_object_span(text, brace)
            if end is None:
                _add(None, (match.start(), _line_end(text, brace)))
            else:
                _add(_parse_call_json(text[brace:end]), (match.start(), end))

    if legacy:
        for match in _FENCED_RE.finditer(text):  # deprecated form
            if _overlaps(match.span(), spans):
                continue
            body = _MARKER_RE.sub("", match.group(1).strip(), count=1)
            _add(_parse_call_json(body), match.span())

        for match in _INLINE_LEGACY_RE.finditer(text):  # deprecated form
            if _overlaps((match.start(), match.start() + 1), spans):
                continue
            end = _json_object_span(text, match.start())
            if end is not None:
                _add(_parse_call_json(text[match.start():end]), (match.start(), end))

    stripped = text
    for start, end in sorted(spans, reverse=True):
        stripped = stripped[:start] + stripped[end:]
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped, calls


def normalize_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Native tool calls in any shape as ``[{id, name, arguments}]`` (``arguments`` a dict).

    Accepts the OpenAI wire shape ``{id, type, function: {name, arguments}}``
    (``arguments`` a JSON string or a dict), LiteLLM's / Ollama's objects with
    the same attributes, and the flat ``{name, arguments}`` / ``{tool, args}``
    dicts the text protocol yields. Anything unreadable is skipped.
    """
    if not raw or isinstance(raw, (str, bytes)):
        return []
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, Iterable) else [raw]:
        if not isinstance(item, dict):
            function = getattr(item, "function", None)
            item = {
                "id": getattr(item, "id", None),
                "name": getattr(function, "name", None) if function is not None else getattr(item, "name", None),
                "arguments": (
                    getattr(function, "arguments", None) if function is not None else getattr(item, "arguments", None)
                ),
            }
        call = _call_from_object(item)
        if call is not None:
            call.setdefault("id", "")
            out.append(call)
    return out


def wire_tool_calls(calls: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``[{id, name, arguments}]`` (or wire dicts) as the OpenAI wire shape ``Message.tool_calls`` carries."""
    out: list[dict[str, Any]] = []
    for index, call in enumerate(normalize_tool_calls(list(calls)), 1):
        out.append({
            "id": call.get("id") or f"call_{index}",
            "type": "function",
            "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], default=str)},
        })
    return out


async def provider_supports_native_tools(provider: Any, model: str | None) -> bool:
    """Can ``provider`` take ``tools=`` for ``model``? ``False`` unless it says so itself.

    Duck-typed: a provider that implements ``supports_tools(model)`` (sync or
    async — :class:`~nvh.providers.openai_compatible.OpenAICompatibleProvider`
    reads the catalog, :class:`~nvh.providers.ollama_provider.OllamaProvider`
    asks ``/api/show``) is asked; anything else, a probe that raises, or an
    answer that is not exactly ``True`` means the text protocol.
    """
    probe = getattr(provider, "supports_tools", None)
    if not callable(probe):
        return False
    try:
        verdict = probe(model)
        if inspect.isawaitable(verdict):
            verdict = await verdict
    except Exception as exc:
        logger.debug("native tool probe failed for %r: %s", model, exc)
        return False
    return verdict is True


# ────────────────────────────────────────────────────────────────────────────
# Tool and ToolResult
# ────────────────────────────────────────────────────────────────────────────


def _empty_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


@dataclass(frozen=True)
class Tool:
    """One executable capability an LLM can request.

    Attributes:
        name: Stable identifier the model emits when it wants to call this tool.
        description: One-line user-facing description; shown in the prompt and
            on confirmation cards.
        input_schema: JSON Schema (``type: object``) of the arguments — the
            canonical shape; ``parameters`` reads it back.
        handler: Async callable that runs the tool. ``handler_style`` says how
            it takes its arguments: ``kwargs`` (``handler(**arguments)``) or
            ``mapping`` (``handler(arguments)``).
        safety_class: ``auto`` (runs without asking), ``confirm`` (user
            clicks) or ``privileged`` (user clicks a red card; sudo-class host
            change). ``safe=False`` on construction means ``confirm``.
        summary_template: User-facing one-liner formatted with the arguments
            for the confirmation card.
        planner: Optional dry run with the handler's signature. For
            ``privileged`` tools ``execute()`` calls it on the unconfirmed
            path and puts its answer on the card as ``plan``. A plan may carry
            ``pinned_arguments`` (a dict): ``execute()`` folds them into the
            card's ``arguments`` before minting the approval token, so what
            the card showed is what the confirmed call must bring back.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=_empty_schema)
    handler: ToolHandler | None = None
    safety_class: SafetyClass = "auto"
    summary_template: str = ""
    planner: ToolHandler | None = None
    handler_style: str = "kwargs"

    _default_handler_style: ClassVar[str] = "kwargs"

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Mapping[str, Any] | None = None,
        handler: ToolHandler | None = None,
        safe: bool | None = None,
        *,
        safety_class: SafetyClass | None = None,
        summary_template: str = "",
        planner: ToolHandler | None = None,
        handler_style: str | None = None,
        input_schema: Mapping[str, Any] | None = None,
    ) -> None:
        if safety_class is None:
            safety_class = "auto" if safe is None or safe else "confirm"
        style = handler_style or self._default_handler_style
        if style not in HANDLER_STYLES:
            raise ValueError(f"Tool '{name}' has unknown handler_style '{style}'. Allowed: {', '.join(HANDLER_STYLES)}.")
        schema = _normalize_schema(input_schema if input_schema is not None else parameters)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "input_schema", schema)
        object.__setattr__(self, "handler", handler)
        object.__setattr__(self, "safety_class", safety_class)
        object.__setattr__(self, "summary_template", summary_template or "")
        object.__setattr__(self, "planner", planner)
        object.__setattr__(self, "handler_style", style)

    @property
    def parameters(self) -> dict[str, Any]:
        """The arguments' JSON Schema (``WizardTool`` presents the derived Wizard shape instead)."""
        return self.input_schema

    @property
    def wizard_parameters(self) -> dict[str, Any]:
        """The prompt/UI shape ``{name: {type, description, required}}``, derived from the schema."""
        return translate_parameters(self.input_schema)

    @property
    def safe(self) -> bool:
        """``True`` only for ``auto`` tools — the pre-0.44 flag, derived."""
        return self.safety_class == "auto"

    @property
    def enabled(self) -> bool:
        """False only for a ``privileged`` tool while the kill switch is off."""
        return self.safety_class != "privileged" or privileged_enabled()

    def as_public_dict(self) -> dict[str, Any]:
        """The fields the LLM and UI can see (no handler, no planner); parameters in the Wizard shape."""
        return {
            "name": self.name,
            "description": self.description,
            "safety_class": self.safety_class,
            "parameters": self.wizard_parameters,
            "summary_template": self.summary_template,
            "enabled": self.enabled,
        }

    def as_openai_tool(self) -> dict[str, Any]:
        """The native function-calling shape ``{"type": "function", "function": {...}}``."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description[:1024],
                "parameters": self.input_schema,
            },
        }


class ToolResult(dict):
    """The one ``execute()`` envelope.

    A plain JSON-able dict — ``ok``, ``result`` or ``error``, ``tool``,
    ``safety_class`` and, on a card, ``needs_confirmation`` / ``arguments`` /
    ``summary`` / ``plan`` / ``approval_token`` — that also answers the
    attribute API the agent loop, the CLI action path and the REPL read:
    ``success`` (``ok``), ``output`` (``result`` as text), ``error`` and
    ``tool_name``. ``ToolResult(tool_name=…, success=…, output=…, error=…)``
    still builds one.
    """

    def __init__(
        self,
        envelope: Mapping[str, Any] | None = None,
        /,
        *,
        tool_name: str | None = None,
        success: bool | None = None,
        output: Any = None,
        error: str | None = None,
        **fields: Any,
    ) -> None:
        super().__init__(envelope or {})
        if tool_name is not None:
            self["tool"] = tool_name
        if success is not None:
            self["ok"] = bool(success)
        if output is not None:
            self["result"] = output
        if error:
            self["error"] = error
        self.update(fields)

    @property
    def success(self) -> bool:
        return self.get("ok") is True

    @property
    def output(self) -> str:
        result = self.get("result")
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, default=str)
        except Exception:
            return str(result)

    @property
    def error(self) -> str:
        error = self.get("error")
        return "" if error is None else str(error)

    @property
    def tool_name(self) -> str:
        tool = self.get("tool")
        if isinstance(tool, dict):
            return str(tool.get("name", ""))
        return "" if tool is None else str(tool)

    def __repr__(self) -> str:
        return f"ToolResult({dict.__repr__(self)})"


# ────────────────────────────────────────────────────────────────────────────
# The registry
# ────────────────────────────────────────────────────────────────────────────

#: Each tool's blocklisted text lives under its own argument: ``shell`` sends
#: ``command``, ``run_code`` sends ``code``. Reading ``command`` for both left
#: run_code unchecked before 0.42.
_COMMAND_ARGUMENTS: dict[str, str] = {"shell": "command", "run_code": "code"}


def _agent_mcp_tools_enabled() -> bool:
    return os.environ.get(AGENT_MCP_TOOLS_ENV, "").strip().lower() in _TRUTHY


class ToolRegistry:
    """Lookup table and the single enforcement point for a set of tools.

    ``register()`` refuses ``safety_class="never"`` and unknown classes so the
    constants can't drift past code review. ``execute()`` is where every rule
    lives — the HTTP layer, the chat loop, the CLI and the agent loop call it
    and add nothing: the kill switch is checked on every privileged call,
    confirm-class calls need ``confirmed=True`` when ``enforce_confirmation``
    is on (the Wizard instance; the agent instance's callers own the click),
    privileged calls always need the card's approval token, the core
    guardrails run before the handler, and outputs are redacted / truncated /
    fitted after it.
    """

    #: Where registration warnings go; the Wizard subclass points this at its own module logger.
    _logger: ClassVar[logging.Logger] = logger

    def __init__(
        self,
        workspace: str | None = None,
        include_system: bool = True,
        *,
        builtins: bool = True,
        include_mcp: bool | None = None,
        enforce_confirmation: bool = False,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self.workspace = workspace or os.getcwd()
        self.enforce_confirmation = enforce_confirmation
        if builtins:
            register_builtin_tools(self)
        if include_system:
            for module_name, register_name in (
                ("nvh.core.system_tools", "register_system_tools"),
                ("nvh.core.browser_tools", "register_browser_tools"),
                ("nvh.core.vision_tools", "register_vision_tools"),
            ):
                try:
                    module = __import__(module_name, fromlist=[register_name])
                    getattr(module, register_name)(self)
                except Exception as exc:  # optional tool packs
                    self._logger.debug("%s skipped: %s", module_name, exc)
        if include_mcp is None:
            include_mcp = _agent_mcp_tools_enabled()
        if include_mcp:
            try:
                from nvh.integrations.mcp_client import register_mcp_tools

                register_mcp_tools(self)
            except Exception as exc:
                self._logger.warning("mcp tool registration skipped: %s", exc)

    # -- registration and lookup ------------------------------------------

    def register(self, tool: Tool) -> None:
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
            self._logger.warning("Overwriting wizard tool '%s'", tool.name)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        """Tools ordered auto, confirm, privileged (then by name) — an explicit key,
        so the classes' spelling never decides the catalogue order."""
        return sorted(
            self._tools.values(),
            key=lambda t: (_SAFETY_ORDER.get(t.safety_class, len(SAFETY_CLASSES)), t.name),
        )

    def get_tool_descriptions(self) -> str:
        """The tool list for the one agent prompt: ``name(param: type, …) [class]: description``."""
        lines = ["Available tools:"]
        for tool in self.list_tools():
            params = ", ".join(
                f"{key}: {_json_schema_type(spec)}"
                for key, spec in (tool.input_schema.get("properties") or {}).items()
            )
            tag = "" if tool.safety_class == "auto" else f" [{tool.safety_class}]"
            lines.append(f"  - {tool.name}({params}){tag}: {tool.description}")
        return "\n".join(lines)

    def openai_tools(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """The native function-calling specs, optionally only for ``names``."""
        wanted = None if names is None else set(names)
        return [t.as_openai_tool() for t in self.list_tools() if wanted is None or t.name in wanted]

    # -- planning and execution -------------------------------------------

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
        arguments: dict[str, Any] | None = None,
        *,
        confirmed: bool = False,
        approval_token: str | None = None,
    ) -> ToolResult:
        """Run a tool by name. Returns the :class:`ToolResult` envelope.

        - ``auto`` tools run regardless of ``confirmed``.
        - ``confirm`` tools: with ``enforce_confirmation`` on they require
          ``confirmed=True`` and otherwise return the "I need a confirmation"
          card so the UI can render the button; with it off (the agent
          registry) the caller has already asked and they run.
        - ``privileged`` tools: refused (``disabled=True``) whenever the kill
          switch is off, confirmed or not. Unconfirmed, the card plus
          ``privileged=True``, ``plan`` (the tool's dry run, or ``None``
          when it has no planner) and the card's ``approval_token`` /
          ``approval_expires_at`` (:func:`issue_approval`). When the plan
          declares ``pinned_arguments`` they are folded into the card's
          ``arguments`` first, so the token signs what the card showed.
          Confirmed, the call must bring a token valid for exactly this name
          and these arguments (:func:`verify_approval`) or it is refused with
          ``approval_required=True`` and nothing runs — on every instance.
        - Guardrails: ``check_command`` on ``shell.command`` / ``run_code.code``,
          ``check_file_read`` + ``check_path`` for ``read_file``, ``check_path``
          + ``check_write_size`` for ``write_file``; a hit is
          ``error="GUARDRAIL: …"`` and the handler never runs.
        - Results are redacted and capped whatever their shape
          (:func:`sanitize_tool_result`: a text result is redacted and
          truncated; every string inside a dict / list result is redacted and
          the whole is cut to the output limit); a privileged apply that
          changed the host is recorded in the vault (``audit``) and its dict
          result fitted to the tool-result window.
        - Unknown tools return ``ok=False`` with an error.

        Handlers never raise out of here: an exception becomes ``ok=False``.
        """
        tool = self.get(name)
        if tool is None:
            return ToolResult({"ok": False, "error": f"Unknown tool: {name}", "tool": name})
        arguments = dict(arguments or {})

        privileged = tool.safety_class == "privileged"
        if privileged and not privileged_enabled():
            return ToolResult({
                "ok": False,
                "error": PRIVILEGED_DISABLED_ERROR,
                "disabled": True,
                "tool": name,
                "safety_class": tool.safety_class,
            })

        if tool.safety_class != "auto" and not confirmed and (privileged or self.enforce_confirmation):
            card = ToolResult({
                "ok": False,
                "needs_confirmation": True,
                "tool": tool.as_public_dict(),
                "arguments": arguments,
                "summary": format_summary(tool.summary_template, arguments) or tool.description,
            })
            if privileged:
                card["privileged"] = True
                plan = await self.plan(name, arguments)
                card["plan"] = plan
                pinned = _pinned_arguments(plan)
                if pinned:
                    # The planner's decisions (e.g. the isolation a shell run
                    # will get) become part of the approved call: the UI sends
                    # these arguments back, the token binds them, the handler
                    # enforces them.
                    arguments = {**arguments, **pinned}
                    card["arguments"] = arguments
                card.update(issue_approval(name, arguments))
            return card

        if privileged and not verify_approval(name, arguments, approval_token):
            return ToolResult({
                "ok": False,
                "error": APPROVAL_REQUIRED_ERROR,
                "approval_required": True,
                "tool": name,
                "safety_class": tool.safety_class,
            })

        try:
            self._guard(tool, arguments)
        except _guardrails.GuardrailError as exc:
            return ToolResult({"ok": False, "error": f"GUARDRAIL: {exc}", "tool": name, "safety_class": tool.safety_class})
        except PermissionError as exc:
            return ToolResult({"ok": False, "error": f"GUARDRAIL: {exc}", "tool": name, "safety_class": tool.safety_class})

        try:
            result = await self._invoke(tool, arguments)
        except Exception as exc:
            # The pre-0.44 Wizard envelope, byte for byte: ``{ok, error, tool}``
            # (tests/test_wizard_privileged_tools.py pins it).
            self._logger.warning("tool '%s' raised: %s", name, exc)
            return ToolResult({"ok": False, "error": str(exc)[:300], "tool": name})

        # Nothing that looks like a key goes back into the model's context,
        # and a runaway output is cut before it costs a context window —
        # whatever shape the handler answered in (a text result, or every
        # string inside a dict / list result: MCP ``content``, ``stdout``…).
        result = sanitize_tool_result(result)

        envelope = ToolResult({"ok": True, "result": result, "tool": name, "safety_class": tool.safety_class})
        if privileged:
            if _privileged_applied(result):
                envelope["audit"] = record_privileged_change(tool, arguments, result)
            if isinstance(result, dict):
                envelope["result"] = fit_tool_window(result)
        return envelope

    async def _invoke(self, tool: Tool, arguments: dict[str, Any]) -> Any:
        if tool.handler is None:
            raise RuntimeError(f"tool '{tool.name}' has no handler")
        if tool.handler_style == "mapping":
            return await tool.handler(arguments)
        return await tool.handler(**arguments)

    def _guard(self, tool: Tool, arguments: dict[str, Any]) -> None:
        """The core guardrails, keyed by the built-in tool names they protect.

        They apply to the core (``kwargs``-style) tools. A ``mapping``-style
        tool of the same name — the Wizard's sandbox bridge ``shell`` /
        ``run_code`` — runs both deny lists itself and answers in its own
        in-band refusal shape (``denied``, ``applied: False``, ``command``),
        which the card and the audit contracts depend on.
        """
        if tool.handler_style != "kwargs":
            return
        command_argument = _COMMAND_ARGUMENTS.get(tool.name)
        if command_argument is not None:
            _guardrails.check_command(str(arguments.get(command_argument, "") or ""))
        if tool.name in ("read_file", "write_file"):
            workspace = pathlib.Path(self.workspace)
            path = str(arguments.get("path", "") or "")
            if tool.name == "read_file":
                _guardrails.check_file_read(path)
                _guardrails.check_path(self._resolve_path(path), workspace)
            else:
                _guardrails.check_path(self._resolve_path(path), workspace)
                _guardrails.check_write_size(str(arguments.get("content", "") or ""), path)

    def _resolve_path(self, path: str) -> str:
        """Resolve a path relative to workspace, preventing traversal."""
        resolved = os.path.normpath(os.path.join(self.workspace, path))
        # Prevent path traversal outside workspace
        if not resolved.startswith(os.path.normpath(self.workspace)):
            raise PermissionError(f"Path traversal blocked: {path}")
        return resolved


# ────────────────────────────────────────────────────────────────────────────
# Privileged plumbing: dry runs, the tool window, the vault audit
# ────────────────────────────────────────────────────────────────────────────


async def _dry_run(tool: Tool, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """The plan a privileged tool would execute; ``None`` without a planner, never raises."""
    if tool.planner is None:
        return None
    try:
        plan = await tool.planner(arguments)
    except Exception as exc:
        logger.warning("tool '%s' planner raised: %s", tool.name, exc)
        return {"ok": False, "error": f"dry run failed: {str(exc)[:200]}", "commands": []}
    return plan if isinstance(plan, dict) else {"ok": True, "commands": [], "detail": str(plan)[:300]}


def _redact_nested(value: Any) -> Any:
    """``value`` with :func:`redact_secrets` applied to every string in it (keys untouched)."""
    if isinstance(value, str):
        return _guardrails.redact_secrets(value)
    if isinstance(value, dict):
        return {key: _redact_nested(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_nested(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_nested(item) for item in value)
    return value


def sanitize_tool_result(result: Any) -> Any:
    """A handler's result with secrets redacted and its size capped, in any shape.

    A ``str`` is redacted and truncated as before 0.44. A ``dict`` / ``list``
    (the Wizard tools, MCP tools, any mapping-style handler) has every string
    inside it redacted, keeping its structure so the UI and the privileged
    audit still read their fields; when its JSON form exceeds the output
    limit (``agent_guardrails.MAX_COMMAND_OUTPUT``) the truncated JSON text
    is returned instead, so no result can cost the model a context window.
    Anything else (numbers, ``None``) passes through. Never raises.
    """
    try:
        if isinstance(result, str):
            return _guardrails.truncate_output(_guardrails.redact_secrets(result))
        if isinstance(result, dict | list | tuple):
            redacted = _redact_nested(result)
            serialized = json.dumps(redacted, default=str)
            if len(serialized) > _guardrails.MAX_COMMAND_OUTPUT:
                return _guardrails.truncate_output(serialized)
            return redacted
    except Exception:
        return result
    return result


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
    redact_secrets = _guardrails.redact_secrets

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
        return f"{verdict} — {_guardrails.redact_secrets(error.strip())[:300]}"
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
    :class:`Tool` needed, only the name, the arguments and a result in the
    apply shape (``ok``, ``applied``, ``partial``, ``error``, ``summary``,
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
    tool: Tool, arguments: dict[str, Any], result: dict[str, Any],
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
# The built-in agent tools
# ────────────────────────────────────────────────────────────────────────────


def _render_execution(result: Any) -> str:
    # A refusal (fail-closed, no Docker) executed nothing; raise so the
    # agent sees a failed tool call rather than empty "output".
    if result.error and not result.isolation:
        raise RuntimeError(result.error)
    output = result.stdout
    if result.stderr:
        output += f"\nSTDERR:\n{result.stderr}"
    if result.timed_out:
        output += "\n(execution timed out)"
    if result.exit_code and not output.strip():
        output = f"[EXIT {result.exit_code}] Command failed with no output."
    if result.isolation == "subprocess":
        output += (
            "\n[isolation: subprocess — Docker unavailable, "
            "no network/memory isolation]"
        )
    return output


def _render_search_hits(query: str, envelope: Mapping[str, Any]) -> str:
    """The numbered ``title / url / snippet`` text the agent reads, from the web_search client's envelope."""
    if not envelope.get("ok"):
        error = envelope.get("error") or "search failed"
        hint = envelope.get("hint")
        return f"Web search failed ({envelope.get('backend', 'unknown')} backend): {error}" + (f"\n{hint}" if hint else "")
    hits = envelope.get("results") or []
    if not hits:
        return f"No results for: {query}"
    return "\n".join(
        f"{index}. {hit.get('title', '')}\n   {hit.get('url', '')}\n   {hit.get('snippet', '')}\n"
        for index, hit in enumerate(hits, 1)
    )


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register the core agent tools on ``registry`` (files, sandbox, web, screenshot, image)."""

    async def read_file(path: str) -> str:
        """Read a file's contents."""
        full_path = registry._resolve_path(path)
        if not os.path.isfile(full_path):
            raise FileNotFoundError(f"File not found: {path}")
        with open(full_path) as f:
            content = f.read()
        if len(content) > 100_000:
            content = content[:100_000] + f"\n... (truncated, {len(content)} chars total)"
        return content

    async def write_file(path: str, content: str) -> str:
        """Write content to a file."""
        full_path = registry._resolve_path(path)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)
        return f"Written {len(content)} chars to {path}"

    async def list_files(pattern: str = "*", directory: str = ".") -> str:
        """List files matching a glob pattern."""
        full_dir = registry._resolve_path(directory)
        matches = globmod.glob(os.path.join(full_dir, pattern), recursive=True)
        # Limit results
        if len(matches) > 100:
            matches = matches[:100]
            matches.append(f"... ({len(matches)} total, showing first 100)")
        return "\n".join(os.path.relpath(m, registry.workspace) for m in matches)

    async def search_files(query: str, pattern: str = "*.py", directory: str = ".") -> str:
        """Search file contents for a string."""
        full_dir = registry._resolve_path(directory)
        results = []
        files = globmod.glob(os.path.join(full_dir, "**", pattern), recursive=True)
        for fpath in files[:50]:  # limit files searched
            try:
                with open(fpath) as f:
                    for i, line in enumerate(f, 1):
                        if query.lower() in line.lower():
                            rel = os.path.relpath(fpath, registry.workspace)
                            results.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(results) >= 30:
                                break
            except (UnicodeDecodeError, PermissionError):
                continue
            if len(results) >= 30:
                break
        return "\n".join(results) if results else f"No matches for '{query}'"

    async def run_code(code: str, language: str = "python") -> str:
        """Execute code in a sandboxed environment."""
        from nvh.sandbox.executor import SandboxExecutor
        result = await SandboxExecutor().execute(code=code, language=language)
        return _render_execution(result)

    async def shell(command: str) -> str:
        """Run a shell command with the workspace mounted.

        Docker when available (workspace read-write at /workspace, no
        network, non-root, memory/pids caps); otherwise a subprocess in
        the workspace with stdin closed and key/token-shaped variables
        removed from its environment. NVH_SANDBOX_REQUIRE_DOCKER=1 /
        `nvh do --sandbox` refuses the unisolated fallback.
        """
        from nvh.sandbox.executor import SandboxConfig, SandboxExecutor
        executor = SandboxExecutor(
            SandboxConfig(mount_dir=registry.workspace, timeout_seconds=60)
        )
        return _render_execution(await executor.run_shell(command))

    async def web_search(query: str, num_results: int = 5) -> str:
        """Search the web and return top results with snippets.

        The one implementation is :mod:`nvh.integrations.web_search` — the
        backend is chosen by ``NVH_SEARXNG_URL`` (self-hosted SearXNG), then
        ``BRAVE_API_KEY``, else the DuckDuckGo HTML fallback. No public
        SearXNG instance is ever defaulted to.
        """
        from nvh.integrations.web_search import web_search as _search

        return _render_search_hits(query, await _search(query, top_k=num_results))

    async def web_fetch(url: str, max_chars: int = 10000) -> str:
        """Fetch a web page and extract readable text content."""
        import html as html_mod
        import ipaddress
        from urllib.parse import urlparse

        import httpx

        # SSRF protection: block private/internal URLs
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if not hostname:
            return "Error: Invalid URL"

        # Block private IPs, loopback, link-local, and cloud metadata
        blocked_hosts = {"169.254.169.254", "metadata.google.internal", "localhost", "127.0.0.1", "0.0.0.0"}
        if hostname in blocked_hosts:
            return "Error: Access to internal/metadata URLs is blocked for security"

        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return f"Error: Access to private IP {hostname} is blocked for security"
        except ValueError:
            pass  # Not an IP — hostname is fine

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, timeout=15,
                                    headers={"User-Agent": "NVHive/0.1"})
            resp.raise_for_status()
            html = resp.text
            html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
            html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'\s+', ' ', text).strip()
            text = html_mod.unescape(text)
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
            return text

    async def screenshot(region: str = "full") -> str:
        """Take a screenshot and describe it using a multimodal model."""
        import base64 as _b64
        import subprocess
        import sys
        import tempfile

        path = tempfile.mktemp(suffix=".png")

        if sys.platform == "darwin":
            # macOS: screencapture is always available
            try:
                subprocess.run(
                    ["screencapture", "-x", path],
                    timeout=5, capture_output=True, check=True,
                )
            except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                return "Screenshot failed on macOS — screencapture unavailable."
        else:
            # Linux: try various screenshot tools
            for cmd in [
                ["gnome-screenshot", "-f", path],
                ["scrot", path],
                ["import", "-window", "root", path],   # ImageMagick
                ["xfce4-screenshooter", "-f", "-s", path],
            ]:
                try:
                    subprocess.run(cmd, timeout=5, capture_output=True)
                    if os.path.exists(path):
                        break
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    continue

        if not os.path.exists(path):
            return (
                "Screenshot failed - no screenshot tool found. "
                "Expose scrot, gnome-screenshot, or spectacle in PATH; nvHive will not call apt."
            )

        # Read and base64 encode
        with open(path, "rb") as f:
            img_data = _b64.b64encode(f.read()).decode()

        return (
            f"Screenshot saved to {path}. "
            f"Base64 data length: {len(img_data)} chars. "
            "Use a multimodal model to analyze it."
        )

    async def imagine(prompt: str, provider: str = "auto", size: str = "1024x1024") -> str:
        """Generate an image from a text prompt using AI image generation."""
        from nvh.core.image_gen import generate_image
        output_path = await generate_image(prompt=prompt, provider=provider, size=size)
        return f"Image generated and saved to: {output_path}"

    registry.register(Tool(
        name="read_file",
        description="Read a file's contents",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path to read"}},
            "required": ["path"],
        },
        handler=read_file,
    ))
    registry.register(Tool(
        name="write_file",
        description="Write content to a file",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        handler=write_file,
        safety_class="confirm",
        summary_template="Write {path}",
    ))
    registry.register(Tool(
        name="list_files",
        description="List files matching a glob pattern",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "default": "*"},
                "directory": {"type": "string", "default": "."},
            },
            "required": [],
        },
        handler=list_files,
    ))
    registry.register(Tool(
        name="search_files",
        description="Search file contents for a string",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "pattern": {"type": "string", "default": "*.py"},
                "directory": {"type": "string", "default": "."},
            },
            "required": ["query"],
        },
        handler=search_files,
    ))
    registry.register(Tool(
        name="run_code",
        description="Execute code in a sandboxed environment",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "language": {"type": "string", "default": "python"},
            },
            "required": ["code"],
        },
        handler=run_code,
        safety_class="confirm",
        summary_template="Run a {language} snippet in the sandbox",
    ))
    registry.register(Tool(
        name="shell",
        description="Run a shell command",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
        handler=shell,
        safety_class="confirm",
        summary_template="Run shell command: {command}",
    ))
    registry.register(Tool(
        name="web_search",
        description="Search the web and return top results with snippets",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "num_results": {"type": "integer", "default": 5, "description": "Number of results to return"},
            },
            "required": ["query"],
        },
        handler=web_search,
    ))
    registry.register(Tool(
        name="web_fetch",
        description="Fetch a web page and extract readable text content",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "max_chars": {"type": "integer", "default": 10000, "description": "Maximum characters to return"},
            },
            "required": ["url"],
        },
        handler=web_fetch,
    ))
    registry.register(Tool(
        name="screenshot",
        description=(
            "Take a screenshot of the current screen and return its path and base64 data "
            "for analysis by a multimodal model"
        ),
        parameters={
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "default": "full",
                    "description": "Screen region to capture: full (default)",
                },
            },
            "required": [],
        },
        handler=screenshot,
    ))
    registry.register(Tool(
        name="imagine",
        description="Generate an image from a text description using AI image generation",
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Text description of the image to generate",
                },
                "provider": {
                    "type": "string",
                    "default": "auto",
                    "description": "Image provider: auto, openai, stability, pollinations",
                },
                "size": {
                    "type": "string",
                    "default": "1024x1024",
                    "description": "Image dimensions, e.g. 1024x1024",
                },
            },
            "required": ["prompt"],
        },
        handler=imagine,
    ))
