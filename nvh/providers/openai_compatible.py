"""LiteLLM adapter shared by every OpenAI-compatible cloud provider.

Per-provider facts live in :mod:`nvh.providers.specs`; this module holds the
behaviour once. The ``nvh.providers.<name>_provider`` modules are one-release
compat shims over :class:`OpenAICompatibleProvider`.

A spec's ``api_surface`` picks the LiteLLM call: ``"chat"`` goes through
``litellm.acompletion``; ``"responses"`` (Perplexity's Agent API) through
``litellm.aresponses`` and is folded into the same ``CompletionResponse`` /
``StreamChunk`` shapes here.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from collections.abc import AsyncIterator, Collection, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import litellm

from nvh.core.atohi import AdmittedProvider, ResourcePaused
from nvh.core.tools import normalize_tool_calls, wire_tool_calls
from nvh.providers.base import (
    AuthenticationError,
    CompletionResponse,
    ContentFilterError,
    FinishReason,
    HealthStatus,
    InvalidRequestError,
    Message,
    ModelInfo,
    ModelNotFoundError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    StreamChunk,
    TokenLimitError,
    Usage,
)
from nvh.providers.lazy_provider import LazyProvider
from nvh.providers.registry import ProviderRegistry, resolve_provider_key
from nvh.providers.specs import PROVIDER_SPECS, ProviderSpec

__all__ = [
    "LIVE_MODELS_NEGATIVE_TTL",
    "LIVE_MODELS_TTL",
    "MODELS_PROBE_MAX_BYTES",
    "MODELS_PROBE_MAX_IDS",
    "MODELS_PROBE_TIMEOUT",
    "PROVIDER_SPECS",
    "OpenAICompatibleProvider",
    "ProviderSpec",
    "concrete_provider",
    "live_model_ids_for",
    "live_model_keys",
    "model_id_tail",
    "model_ids_from_payload",
    "model_served",
    "models_probe_request",
]

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT = 15
#: Seconds the free ``GET /models`` probe may take. The ``/v1/models``
#: intersect and ``nvh status --deep`` wait no longer than this per provider.
MODELS_PROBE_TIMEOUT = 5.0
#: How long one live model list stays good -- per adapter instance here and
#: per provider in the server's ``/v1/models`` cache -- so a health check and
#: a model list inside one interval share a single request.
LIVE_MODELS_TTL = 300.0
#: How long a failed probe is remembered before the next attempt: short, so a
#: provider that comes back is seen again quickly.
LIVE_MODELS_NEGATIVE_TTL = 30.0
#: Caps on one ``GET /models`` answer. The body is read in chunks and dropped
#: once it passes this many bytes (a real listing is a few hundred KB at most),
#: and at most this many ids are kept, so a huge or hostile answer from a
#: configured proxy can neither hold the event loop nor fill memory.
MODELS_PROBE_MAX_BYTES = 8 * 1024 * 1024
MODELS_PROBE_MAX_IDS = 10_000

_FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
}

# Responses API ``incomplete_details.reason`` -> finish reason.
_INCOMPLETE_REASONS = {
    "max_output_tokens": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
}


def _map_error(e: Exception, provider: str) -> ProviderError:
    """Map LiteLLM/OpenAI exceptions to our error taxonomy with actionable messages."""
    msg = str(e)
    if "AuthenticationError" in type(e).__name__ or "401" in msg:
        from nvh.providers.quota_info import get_quota_info

        info = get_quota_info(provider)
        parts = [
            f"API key invalid or missing permissions for '{provider}'.",
            "  Fix: nvh setup  (reconfigure keys)",
        ]
        if info.upgrade_url:
            parts.append(f"  Get key: {info.upgrade_url}")
        return AuthenticationError(
            "\n".join(parts),
            provider=provider,
            original_error=e,
        )
    if "RateLimitError" in type(e).__name__ or "429" in msg:
        from nvh.providers.quota_info import format_rate_limit_message, parse_retry_after

        retry_after = getattr(e, "retry_after", None) or parse_retry_after(msg)
        friendly = format_rate_limit_message(provider, msg)
        return RateLimitError(
            friendly,
            provider=provider,
            retry_after=retry_after,
            original_error=e,
        )
    if "InvalidRequestError" in type(e).__name__ or "400" in msg:
        if "context_length" in msg.lower() or "max_tokens" in msg.lower():
            return TokenLimitError(
                f"Input too long for '{provider}' model.\n"
                f"Try: nvh config set defaults.max_tokens 2048\n"
                f"Or use a model with a larger context window.\n"
                f"Original error: {msg}",
                provider=provider,
                original_error=e,
            )
        if "content_filter" in msg.lower() or "content_policy" in msg.lower():
            return ContentFilterError(msg, provider=provider, original_error=e)
        return InvalidRequestError(msg, provider=provider, original_error=e)
    if "NotFoundError" in type(e).__name__ or "404" in msg:
        return ModelNotFoundError(
            f"Model not found on '{provider}'.\n"
            f"Run: nvh model list  (to see available models)\n"
            f"Or update your model: nvh config set providers.{provider}.default_model <model>\n"
            f"Original error: {msg}",
            provider=provider,
            original_error=e,
        )
    if "ServiceUnavailableError" in type(e).__name__ or "503" in msg or "500" in msg:
        return ProviderUnavailableError(
            f"'{provider}' is temporarily unavailable (server error).\n"
            f"NVHive will try the next advisor automatically.\n"
            f"Check status: https://status.openai.com  (or provider status page)\n"
            f"Original error: {msg}",
            provider=provider,
            original_error=e,
        )
    return ProviderError(msg, provider=provider, original_error=e)


def _build_messages(
    messages: list[Message],
    system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Convert our Message models to LiteLLM format.

    An assistant turn's ``tool_calls`` and a ``tool`` turn's ``tool_call_id``
    travel with the message so a native tool round trip can be replayed to
    the provider in its own protocol.
    """
    result: list[dict[str, Any]] = []
    if system_prompt:
        result.append({"role": "system", "content": system_prompt})
    for msg in messages:
        d: dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.name:
            d["name"] = msg.name
        if msg.tool_calls:
            d["tool_calls"] = msg.tool_calls
        if msg.tool_call_id:
            d["tool_call_id"] = msg.tool_call_id
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Native function calling (0.44 D3): tools= only where the model can take it,
# never to the Responses surface; LiteLLM's tool_calls mapped back to the
# OpenAI wire shape ``Message.tool_calls`` / ``CompletionResponse.tool_calls``
# carry.
# ---------------------------------------------------------------------------


def model_supports_function_calling(model: str) -> bool:
    """Does the shipped catalog (else LiteLLM's model table) say ``model`` takes ``tools=``?

    ``ModelInfo.supports_function_calling`` when a row has it (the 0.44 wave-2
    LiteLLM-derived flag), else the row's ``supports_tools``; a model without
    a catalog row falls back to ``litellm.supports_function_calling`` — an
    in-package table, no network. Unknown means ``False``: the text protocol.
    """
    if not model:
        return False
    catalog = _catalog()
    bare = model.split("/", 1)[1] if "/" in model else model
    for candidate in dict.fromkeys((model, bare)):
        info = catalog.get_model_info(candidate)
        if info is None:
            continue
        native = getattr(info, "supports_function_calling", None)
        if native is not None:
            return native is True
        return info.supports_tools is True
    try:
        return litellm.supports_function_calling(model) is True
    except Exception:
        return False


def _tool_calls_from_message(message: Any) -> list[dict[str, Any]] | None:
    """``message.tool_calls`` mapped to wire dicts; ``None`` when the model made no call.

    One mapping for every adapter: :func:`nvh.core.tools.normalize_tool_calls`
    reads LiteLLM's objects (``.function.name`` / ``.arguments``) and dicts
    alike, :func:`nvh.core.tools.wire_tool_calls` writes the OpenAI wire shape
    and fills a missing ``id`` with ``call_<n>`` — the same fallback the
    streaming path and the Ollama adapter use, so a backend that omits ids
    (a vLLM-style local server) yields one contract however it was called.
    """
    raw = _get(message, "tool_calls") if message is not None else None
    if not raw:
        return None
    calls = wire_tool_calls(normalize_tool_calls(raw))
    return calls or None


def _merge_tool_call_delta(partial: dict[int, dict[str, Any]], deltas: Any) -> None:
    """Fold one streaming chunk's ``delta.tool_calls`` into ``partial`` (keyed by index)."""
    for delta in deltas or []:
        index = _get(delta, "index")
        index = int(index) if isinstance(index, int) else len(partial)
        entry = partial.setdefault(index, {"id": "", "name": "", "arguments": ""})
        call_id = _get(delta, "id")
        if call_id:
            entry["id"] = str(call_id)
        function = _get(delta, "function")
        if function is not None:
            name = _get(function, "name")
            if name:
                entry["name"] = str(name)
            piece = _get(function, "arguments")
            if piece:
                entry["arguments"] += piece if isinstance(piece, str) else json.dumps(piece, default=str)


def _wire_partial_calls(partial: dict[int, dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The assembled stream deltas (by index) as wire dicts, through the one mapping."""
    calls = wire_tool_calls(normalize_tool_calls([entry for _index, entry in sorted(partial.items())]))
    return calls or None


def _responses_content(role: str, content: Any) -> Any:
    """Chat-style content parts converted to Responses API input parts.

    A plain string passes through. A list of parts -- what the API server
    builds for image attachments (``{"type": "text"}`` / ``{"type":
    "image_url", "image_url": {"url": ...}}``) -- becomes ``input_text`` /
    ``input_image`` items (``output_text`` for an assistant turn, which is
    what the Responses API expects there). Parts already in Responses shape
    pass through untouched.
    """
    if not isinstance(content, list):
        return content
    text_type = "output_text" if role == "assistant" else "input_text"
    parts: list[Any] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": text_type, "text": part})
            continue
        if not isinstance(part, dict):
            parts.append({"type": text_type, "text": str(part)})
            continue
        kind = str(part.get("type", "")).lower()
        if kind == "text":
            parts.append({"type": text_type, "text": str(part.get("text", ""))})
        elif kind == "image_url":
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                url, detail = image_url.get("url", ""), image_url.get("detail")
            else:
                url, detail = image_url, None
            parts.append({"type": "input_image", "image_url": str(url or ""), "detail": detail or "auto"})
        else:
            parts.append(part)
    return parts


def _build_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert our Message models to Responses API input items.

    The system prompt travels separately as ``instructions``; ``name`` has no
    Responses counterpart and is dropped. Multimodal content parts go through
    :func:`_responses_content`.
    """
    return [
        {"role": msg.role, "content": _responses_content(msg.role, msg.content)}
        for msg in messages
    ]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Field access that works on LiteLLM's pydantic objects and on plain dicts."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _responses_text(response: Any) -> str:
    """Concatenate the ``output_text`` parts of a Responses API result."""
    parts: list[str] = []
    for item in _get(response, "output") or []:
        if _get(item, "type") != "message":
            continue
        for part in _get(item, "content") or []:
            if _get(part, "type") == "output_text":
                parts.append(_get(part, "text", "") or "")
    return "".join(parts)


def _responses_usage(response: Any) -> tuple[Usage, Decimal | None]:
    """Usage plus, when the provider bills the request itself, its exact cost.

    Perplexity returns ``usage.cost`` (a ``{"total_cost": ...}`` dict, which
    LiteLLM may already have flattened to a float); that beats a per-token
    estimate because presets bill per request as well as per token.
    """
    data = _get(response, "usage")
    if data is None:
        return Usage(), None
    usage = Usage(
        input_tokens=_get(data, "input_tokens", 0) or 0,
        output_tokens=_get(data, "output_tokens", 0) or 0,
        total_tokens=_get(data, "total_tokens", 0) or 0,
    )
    cost = _get(data, "cost")
    if isinstance(cost, dict):
        cost = cost.get("total_cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return usage, Decimal(str(round(cost, 6)))
    return usage, None


def _responses_finish(response: Any) -> FinishReason:
    if _get(response, "status") != "incomplete":
        return FinishReason.STOP
    reason = _get(_get(response, "incomplete_details"), "reason")
    return _INCOMPLETE_REASONS.get(reason or "", FinishReason.LENGTH)


def _responses_error(obj: Any) -> str:
    """Message of a failed response or an ``error`` stream event."""
    err = _get(obj, "error")
    return str(_get(err, "message", err) or "response failed")


@functools.lru_cache(maxsize=1)
def _catalog() -> ProviderRegistry:
    """A registry holding only the shipped capabilities.yaml catalog (loaded once)."""
    registry = ProviderRegistry()
    registry.load_capabilities()
    return registry


def _catalog_cost(model: str, usage: Usage) -> Decimal:
    """``usage`` priced at the capabilities.yaml per-1M-token rates for ``model``; $0 without a row."""
    info = _catalog().get_model_info(model)
    if info is None:
        return Decimal("0")
    cost = (
        Decimal(usage.input_tokens) * info.input_cost_per_1m_tokens
        + Decimal(usage.output_tokens) * info.output_cost_per_1m_tokens
    ) / Decimal(1_000_000)
    return cost.quantize(Decimal("0.000001"))


# Models whose bill did not come from LiteLLM's price (the catalog rate, or
# $0) and were logged once; tests clear it alongside ``_catalog``.
_COST_FALLBACK_LOGGED: set[str] = set()


def _log_cost_fallback_once(model: str, level: int, message: str, *args: Any) -> None:
    """Log ``message`` the first time ``model`` is billed off LiteLLM's price in this process."""
    if model in _COST_FALLBACK_LOGGED:
        return
    _COST_FALLBACK_LOGGED.add(model)
    logger.log(level, message, *args)


def _calc_cost(model: str, usage: Usage) -> Decimal:
    """Cost of ``usage``: LiteLLM's price table, else the capabilities.yaml rates, else $0.

    LiteLLM raises for a model it has not mapped (Perplexity's Agent API
    presets, for one); the catalog row nvHive ships for that model then
    prices the request instead of it being recorded as free. Each model is
    logged once per process when its bill is not LiteLLM's price: INFO when
    the catalog rate takes over, WARNING when nothing prices it and $0 is
    recorded (``ModelInfo.cost_known`` is false and the model list says
    "cost unknown"), INFO when LiteLLM itself bills $0 for tokens used.
    """
    try:
        prompt_cost, completion_cost = litellm.cost_per_token(
            model=model,
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
        )
    except Exception:
        cost = _catalog_cost(model, usage)
        info = _catalog().get_model_info(model)
        if info is not None and info.cost_known:
            _log_cost_fallback_once(
                model, logging.INFO,
                "model %s: LiteLLM has no price; billed at the capabilities.yaml rate", model,
            )
        else:
            _log_cost_fallback_once(
                model, logging.WARNING,
                "model %s: no price in LiteLLM's table or the catalog; recorded as $0 (cost unknown)",
                model,
            )
        return cost
    cost = Decimal(str(round(prompt_cost + completion_cost, 6)))
    if cost == 0 and (usage.input_tokens or usage.output_tokens):
        _log_cost_fallback_once(
            model, logging.INFO, "model %s: LiteLLM prices this request at $0; recorded as $0", model,
        )
    return cost


def _resolve_api_key(spec: ProviderSpec, api_key: str, provider_name: str) -> str:
    # An unexpanded "${VAR}" placeholder means the registry found no env value.
    if api_key and not api_key.startswith("${"):
        return api_key
    return resolve_provider_key(provider_name, ptype=spec.name)[0] or ""


def _litellm_api_base(model: str) -> str | None:
    """The endpoint LiteLLM routes ``model`` to, when it publishes one."""
    try:
        return litellm.get_llm_provider(model)[3]
    except Exception:
        return None


def model_ids_from_payload(payload: Any) -> list[str]:
    """Model ids in a ``GET /models`` body, in the provider's own spelling.

    OpenAI's shape (``{"data": [{"id": ...}]}``: also Anthropic, Mistral,
    Groq and every OpenAI-compatible host), the ``{"models": [{"name":
    ...}]}`` shape (Google, Cohere) and a bare list of ids or objects all
    parse; anything else yields ``[]``. Order kept, blanks and repeats
    dropped, and at most ``MODELS_PROBE_MAX_IDS`` kept.
    """
    items: Any = payload
    if isinstance(payload, dict):
        items = next((payload[k] for k in ("data", "models") if isinstance(payload.get(k), list)), [])
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = (item.get("id") or item.get("name")) if isinstance(item, dict) else item
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            ids.append(value)
            if len(ids) >= MODELS_PROBE_MAX_IDS:
                break
    return ids


def model_id_tail(model_id: str) -> str:
    """The segment after the last ``/``: what a provider's spelling and nvHive's share."""
    return model_id.rsplit("/", 1)[-1]


def live_model_keys(model_ids: Iterable[str]) -> frozenset[str]:
    """``model_ids`` plus their tails: the set :func:`model_served` matches against."""
    ids = {m for m in model_ids if m}
    return frozenset(ids | {model_id_tail(m) for m in ids})


def model_served(model_id: str, live_keys: Collection[str]) -> bool:
    """Does a live list (keyed by :func:`live_model_keys`) carry ``model_id``?

    An exact match first, else the id's tail, so a provider that lists
    ``models/gemini-x`` or ``meta/llama-x`` still vouches for the catalog's
    ``gemini/gemini-x`` or ``nvidia_nim/meta/llama-x``. Looser than exact on
    purpose: an intersect may keep a model, never hide a served one.
    """
    return model_id in live_keys or model_id_tail(model_id) in live_keys


def models_probe_request(probe: tuple[str, str], key: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """``(url, headers, params)`` for a spec's ``models_probe`` sent with ``key``.

    ``bearer`` puts the key in ``Authorization``; ``x-api-key`` in that header
    beside the API-version header Anthropic requires; ``query-key`` in a
    ``key`` query parameter, returned as ``params`` so the URL nvHive keeps
    (``_ModelsProbe.url``, the ``HTTP <status> from <url>`` health error)
    never carries it. httpx still merges ``params`` into the request URL it
    logs at INFO, which is why nvHive's entry points keep the ``httpx``
    logger at WARNING and :func:`_redact` scrubs every error text.
    """
    url, auth = probe
    if auth == "x-api-key":
        return url, {"x-api-key": key, "anthropic-version": "2023-06-01"}, {}
    if auth == "query-key":
        return url, {}, {"key": key}
    return url, {"Authorization": f"Bearer {key}"}, {}


def concrete_provider(provider: Any) -> Any:
    """The adapter behind the registry's wrappers.

    ``ProviderRegistry.get`` hands out a :class:`LazyProvider` (constructed on
    first use) and, under Atohi admission, an :class:`AdmittedProvider` around
    it; neither forwards :meth:`OpenAICompatibleProvider.live_model_ids`. A
    model list is metadata, not an admitted operation, so reading it on the
    adapter is the same call ``AdmittedProvider.list_models`` passes through.
    """
    for _ in range(4):
        if isinstance(provider, AdmittedProvider):
            provider = provider.provider
        elif isinstance(provider, LazyProvider):
            provider = provider._load()
        else:
            break
    return provider


async def live_model_ids_for(
    provider: Any, *, timeout: float, list_models_fallback: bool = True,
) -> frozenset[str] | None:
    """The model ids an adapter serves right now (nvHive's spelling), or ``None`` when unknown.

    The one unwrap-and-probe sequence behind ``/v1/models`` and the DEEP
    ``default_models_served`` check; callers match against
    :func:`live_model_keys` of the result. ``provider`` is whatever the
    registry handed out (:func:`concrete_provider` unwraps it); an
    :class:`OpenAICompatibleProvider` answers through :meth:`live_model_ids`
    and any other adapter through ``list_models`` when ``list_models_fallback``
    is set (the DEEP check passes ``False``: Ollama has its own check). A
    missing probe, a timeout, an error, ``ResourcePaused`` (a
    ``BaseException``: admission pausing the call) or an empty list is
    ``None``, so a caller intersecting a catalog against it filters nothing.
    """
    try:
        provider = concrete_provider(provider)
        live_ids = getattr(provider, "live_model_ids", None)
        if live_ids is not None:
            ids = await asyncio.wait_for(live_ids(), timeout=timeout)
        elif list_models_fallback:
            live = await asyncio.wait_for(provider.list_models(), timeout=timeout)
            ids = {m.model_id for m in live if getattr(m, "model_id", None)}
        else:
            return None
    except ResourcePaused:
        return None
    except Exception:
        return None
    return frozenset(ids) if ids else None


def _redact(text: str, secret: str) -> str:
    """``text`` with ``secret`` blanked; applied before any truncation."""
    if secret and len(secret) >= 4 and secret in text:
        return text.replace(secret, "***")
    return text


async def _read_capped(resp: Any, limit: int) -> bytes | None:
    """The streamed body of ``resp``, or ``None`` once it is known to exceed ``limit`` bytes.

    A ``Content-Length`` over the limit ends it before any read; otherwise
    the (decoded) chunks are summed as they arrive and the read stops the
    moment the total passes the limit, so a gzip bomb is bounded too.
    """
    declared = str(resp.headers.get("content-length") or "")
    if declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass
class _ModelsProbe:
    """One ``GET /models`` outcome, cached per adapter instance."""

    url: str
    status_code: int | None = None  # None: the request itself failed
    ids: list[str] | None = None  # the body's ids on a 2xx/3xx; None when the body was over the cap
    error: str = ""  # redacted request error

    @property
    def ok(self) -> bool:
        return self.status_code is not None and self.status_code < 400


class OpenAICompatibleProvider:
    """LiteLLM adapter parameterised by a :class:`ProviderSpec`.

    Blank ``default_model``/``fallback_model``/``base_url``/``provider_name``/
    ``timeout`` fall back to the spec, so a config stanza that omits them gets
    the shipped defaults.
    """

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str = "",
        default_model: str = "",
        fallback_model: str = "",
        base_url: str | None = None,
        provider_name: str = "",
        timeout: int | None = None,
    ):
        self._spec = spec
        self._provider_name = provider_name or spec.name
        self._api_key = _resolve_api_key(spec, api_key, self._provider_name)
        self._default_model = default_model or spec.default_model
        self._fallback_model = fallback_model or spec.fallback_model
        self._health_model = spec.health_model or self._default_model
        self._base_url = base_url or spec.base_url
        self._timeout = timeout or spec.timeout
        self._models_probe_cache: tuple[float, _ModelsProbe] | None = None

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def spec(self) -> ProviderSpec:
        return self._spec

    def _get_model(self, model: str | None) -> str:
        return self._spec.route(model or self._default_model)

    def _kwargs(self, model: str) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self._spec.route(model)}
        if self._api_key:
            kw["api_key"] = self._api_key
        if self._base_url:
            kw["api_base"] = self._base_url
        return kw

    def _cost(self, model: str, usage: Usage) -> Decimal:
        return Decimal("0") if self._spec.zero_cost else _calc_cost(model, usage)

    @property
    def _responses(self) -> bool:
        return self._spec.api_surface == "responses"

    async def supports_tools(self, model: str | None = None) -> bool:
        """Can ``model`` (default: the configured one) take ``tools=`` here?

        Never on the Responses surface (I11); otherwise what
        :func:`model_supports_function_calling` says of the routed model.
        """
        if self._responses:
            return False
        return model_supports_function_calling(self._get_model(model))

    def _tool_kwargs(
        self, model_name: str, tools: list[dict[str, Any]] | None, tool_choice: Any,
    ) -> dict[str, Any]:
        """``tools`` / ``tool_choice`` for LiteLLM, or ``{}`` when the model cannot take them."""
        if not tools or self._responses or not model_supports_function_calling(model_name):
            return {}
        kw: dict[str, Any] = {"tools": list(tools)}
        if tool_choice is not None:
            kw["tool_choice"] = tool_choice
        return kw

    def _responses_cost(self, model: str, usage: Usage, billed: Decimal | None) -> Decimal:
        if billed is None or self._spec.zero_cost:
            return self._cost(model, usage)
        return billed

    def _responses_kwargs(
        self,
        model: str,
        temperature: float,
        max_tokens: int,
        system_prompt: str | None,
    ) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "timeout": self._timeout,
            **self._kwargs(model),
        }
        if system_prompt:
            kw["instructions"] = system_prompt
        return kw

    async def _complete_responses(
        self,
        messages: list[Message],
        model_name: str,
        temperature: float,
        max_tokens: int,
        system_prompt: str | None,
        **kwargs: Any,
    ) -> CompletionResponse:
        start = time.monotonic()
        try:
            response = await litellm.aresponses(
                input=_build_input(messages),
                **self._responses_kwargs(model_name, temperature, max_tokens, system_prompt),
                **kwargs,
            )
        except Exception as e:
            raise _map_error(e, self._provider_name) from e
        if _get(response, "status") == "failed":
            raise ProviderError(_responses_error(response), provider=self._provider_name)

        usage, billed = _responses_usage(response)
        return CompletionResponse(
            content=_responses_text(response),
            model=_get(response, "model") or model_name,
            provider=self._provider_name,
            usage=usage,
            cost_usd=self._responses_cost(model_name, usage, billed),
            latency_ms=int((time.monotonic() - start) * 1000),
            finish_reason=_responses_finish(response),
        )

    def _final_responses_chunk(
        self, response: Any, model_name: str, accumulated: str
    ) -> StreamChunk:
        if response is not None:
            usage, billed = _responses_usage(response)
            finish = _responses_finish(response)
        else:
            # Stream ended without a terminal event: estimate, as the chat path does.
            est_out = self.estimate_tokens(accumulated)
            usage = Usage(input_tokens=0, output_tokens=est_out, total_tokens=est_out)
            billed, finish = None, FinishReason.STOP
        return StreamChunk(
            delta="",
            is_final=True,
            accumulated_content=accumulated,
            model=model_name,
            provider=self._provider_name,
            usage=usage,
            cost_usd=self._responses_cost(model_name, usage, billed),
            finish_reason=finish,
        )

    async def _stream_responses(
        self,
        messages: list[Message],
        model_name: str,
        temperature: float,
        max_tokens: int,
        system_prompt: str | None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        accumulated = ""
        try:
            events = await litellm.aresponses(
                input=_build_input(messages),
                stream=True,
                **self._responses_kwargs(model_name, temperature, max_tokens, system_prompt),
                **kwargs,
            )
        except Exception as e:
            raise _map_error(e, self._provider_name) from e

        final: StreamChunk | None = None
        try:
            async for event in events:
                etype = _get(event, "type", "")
                etype = getattr(etype, "value", etype)  # str enum -> its value
                if etype == "response.output_text.delta":
                    delta = _get(event, "delta", "") or ""
                    accumulated += delta
                    yield StreamChunk(
                        delta=delta,
                        accumulated_content=accumulated,
                        model=model_name,
                        provider=self._provider_name,
                    )
                elif etype in ("response.completed", "response.incomplete"):
                    final = self._final_responses_chunk(
                        _get(event, "response"), model_name, accumulated
                    )
                elif etype == "response.failed":
                    raise ProviderError(
                        _responses_error(_get(event, "response")), provider=self._provider_name
                    )
                elif etype == "error":
                    raise ProviderError(_responses_error(event), provider=self._provider_name)
        except ProviderError:
            raise
        except Exception as e:
            raise _map_error(e, self._provider_name) from e
        yield final or self._final_responses_chunk(None, model_name, accumulated)

    async def complete(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        model_name = self._get_model(model)
        if self._responses:
            # ``tools`` never reach the Responses surface (I11): the caller's
            # text protocol applies there.
            return await self._complete_responses(
                messages, model_name, temperature, max_tokens, system_prompt, **kwargs
            )
        msgs = _build_messages(messages, system_prompt)
        start = time.monotonic()

        try:
            response = await litellm.acompletion(
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=self._timeout,
                **self._kwargs(model_name),
                **self._tool_kwargs(model_name, tools, tool_choice),
                **kwargs,
            )
        except Exception as e:
            raise _map_error(e, self._provider_name) from e

        elapsed = int((time.monotonic() - start) * 1000)
        usage_data = response.usage
        usage = Usage(
            input_tokens=usage_data.prompt_tokens or 0,
            output_tokens=usage_data.completion_tokens or 0,
            total_tokens=usage_data.total_tokens or 0,
        )
        message = response.choices[0].message
        content = message.content or ""
        finish = response.choices[0].finish_reason or "stop"
        tool_calls = _tool_calls_from_message(message)

        return CompletionResponse(
            content=content,
            model=response.model or model_name,
            provider=self._provider_name,
            usage=usage,
            cost_usd=self._cost(model_name, usage),
            latency_ms=elapsed,
            finish_reason=(
                FinishReason.TOOL_CALLS if tool_calls and finish == "stop"
                else _FINISH_REASONS.get(finish, FinishReason.STOP)
            ),
            tool_calls=tool_calls,
        )

    async def stream(
        self,
        messages: list[Message],
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        model_name = self._get_model(model)
        if self._responses:
            # ``tools`` never reach the Responses surface (I11).
            async for chunk in self._stream_responses(
                messages, model_name, temperature, max_tokens, system_prompt, **kwargs
            ):
                yield chunk
            return
        msgs = _build_messages(messages, system_prompt)
        accumulated = ""
        # Native tool calls arrive as deltas keyed by index; the final chunk
        # carries them assembled.
        partial_calls: dict[int, dict[str, Any]] = {}
        emitted_final = False

        try:
            response = await litellm.acompletion(
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                timeout=self._timeout,
                **self._kwargs(model_name),
                **self._tool_kwargs(model_name, tools, tool_choice),
                **kwargs,
            )
        except Exception as e:
            raise _map_error(e, self._provider_name) from e

        try:
            async for chunk in response:
                delta = ""
                finish_reason = None

                if chunk.choices and chunk.choices[0].delta:
                    delta = chunk.choices[0].delta.content or ""
                    _merge_tool_call_delta(partial_calls, getattr(chunk.choices[0].delta, "tool_calls", None))
                if chunk.choices and chunk.choices[0].finish_reason:
                    fr = chunk.choices[0].finish_reason
                    finish_reason = _FINISH_REASONS.get(fr, FinishReason.STOP)

                accumulated += delta
                is_final = finish_reason is not None

                usage = None
                cost = None
                if is_final:
                    emitted_final = True
                    # Estimate usage for streaming (not all providers send it)
                    usage_data = getattr(chunk, "usage", None)
                    if usage_data:
                        usage = Usage(
                            input_tokens=getattr(usage_data, "prompt_tokens", 0) or 0,
                            output_tokens=getattr(usage_data, "completion_tokens", 0) or 0,
                            total_tokens=getattr(usage_data, "total_tokens", 0) or 0,
                        )
                    else:
                        est_out = self.estimate_tokens(accumulated)
                        usage = Usage(
                            input_tokens=0,
                            output_tokens=est_out,
                            total_tokens=est_out,
                        )
                    cost = self._cost(model_name, usage)

                yield StreamChunk(
                    delta=delta,
                    is_final=is_final,
                    accumulated_content=accumulated,
                    model=model_name,
                    provider=self._provider_name,
                    usage=usage,
                    cost_usd=cost,
                    finish_reason=finish_reason,
                    tool_calls=_wire_partial_calls(partial_calls) if is_final else None,
                )
        except Exception as e:
            raise _map_error(e, self._provider_name) from e

        if partial_calls and not emitted_final:
            # The provider streamed tool calls but never a finish_reason:
            # close the stream ourselves so the calls are not lost.
            est_out = self.estimate_tokens(accumulated)
            usage = Usage(input_tokens=0, output_tokens=est_out, total_tokens=est_out)
            yield StreamChunk(
                delta="",
                is_final=True,
                accumulated_content=accumulated,
                model=model_name,
                provider=self._provider_name,
                usage=usage,
                cost_usd=self._cost(model_name, usage),
                finish_reason=FinishReason.TOOL_CALLS,
                tool_calls=_wire_partial_calls(partial_calls),
            )

    def _models_probe(self) -> tuple[str, str] | None:
        """``(url, auth)`` of the free ``GET /models``, or ``None`` when none is known.

        A configured ``base_url`` that overrides the spec's (a proxy, a
        self-hosted server) first, else the spec row's ``models_probe``, else
        the base LiteLLM publishes for the default model.
        """
        if self._base_url and self._base_url != self._spec.base_url:
            return f"{self._base_url.rstrip('/')}/models", "bearer"
        if self._spec.models_probe is not None:
            return self._spec.models_probe
        base = _litellm_api_base(self._get_model(None))
        if base:
            return f"{base.rstrip('/')}/models", "bearer"
        return None

    def _models_url(self) -> str | None:
        """The free ``GET /models`` URL, when one is known and we hold a key."""
        probe = self._models_probe()
        if probe is None or not self._api_key:
            return None
        return probe[0]

    async def _fetch_models(self) -> _ModelsProbe | None:
        """The one ``GET /models`` both :meth:`health_check` and :meth:`list_models` read.

        ``None`` when there is no URL or no key (callers keep the static list,
        or ping). Otherwise the outcome, cached on this instance for
        ``LIVE_MODELS_TTL`` after a success and ``LIVE_MODELS_NEGATIVE_TTL``
        after a failure, so a health check followed by a model list is one
        request. ``MODELS_PROBE_TIMEOUT`` bounds the call, the body is
        streamed and dropped past ``MODELS_PROBE_MAX_BYTES`` (``ids`` then
        stays ``None``: unknown, never a guess), a body that is not JSON or
        nests too deep yields no ids, and the key is redacted from any error
        before it is kept.
        """
        now = time.monotonic()
        if self._models_probe_cache is not None:
            fetched_at, cached = self._models_probe_cache
            ttl = LIVE_MODELS_TTL if cached.ok else LIVE_MODELS_NEGATIVE_TTL
            if now - fetched_at < ttl:
                return cached
        probe = self._models_probe()
        if probe is None or not self._api_key:
            return None
        url, headers, params = models_probe_request(probe, self._api_key)
        result = _ModelsProbe(url=url)
        body: bytes | None = b""
        try:
            async with httpx.AsyncClient(timeout=MODELS_PROBE_TIMEOUT) as client:
                async with client.stream(
                    "GET", url, headers=headers, **({"params": params} if params else {}),
                ) as resp:
                    result.status_code = int(resp.status_code)
                    if result.ok:
                        body = await _read_capped(resp, MODELS_PROBE_MAX_BYTES)
        except Exception as e:
            result.status_code = None
            result.error = _redact(str(e) or type(e).__name__, self._api_key)
        else:
            if result.ok:
                if body is None:
                    result.error = f"models body over {MODELS_PROBE_MAX_BYTES} bytes; list unknown"
                else:
                    try:
                        payload = json.loads(body) if body else None
                    except (ValueError, RecursionError):
                        payload = None
                    result.ids = model_ids_from_payload(payload)
        self._models_probe_cache = (now, result)
        return result

    async def live_model_ids(self) -> set[str] | None:
        """Model ids the provider serves right now, in nvHive's spelling; ``None`` when unknown.

        Unknown (no probe URL, no key, a failed or rejected request, an
        empty body) is ``None`` rather than an empty set, so a caller
        intersecting a catalog against it filters nothing: fail open.
        """
        probe = await self._fetch_models()
        if probe is None or not probe.ok or not probe.ids:
            return None
        return {self._nvh_model_id(model_id) for model_id in probe.ids}

    def _nvh_model_id(self, live_id: str) -> str:
        """A provider-spelled id as nvHive writes it for this spec.

        With the LiteLLM route when the spec's own ids carry it
        (``groq/openai/...``); bare when they do not (llm7, SiliconFlow: the
        route is applied at call time).
        """
        prefix = self._spec.litellm_prefix
        if prefix and self._spec.default_model.startswith(prefix):
            return self._spec.route(live_id)
        return live_id

    def _model_info(self, model_id: str, by_tail: dict[str, ModelInfo] | None = None) -> ModelInfo:
        """The catalog row for ``model_id`` (exact; by tail when ``by_tail`` is given) under this instance's name, else a bare row."""
        info = _catalog().get_model_info(model_id)
        if info is None and by_tail is not None:
            info = by_tail.get(model_id_tail(model_id))
        if info is None:
            return ModelInfo(model_id=model_id, provider=self._provider_name)
        return info.model_copy(update={"provider": self._provider_name})

    async def list_models(self) -> list[ModelInfo]:
        """The models the provider serves: the live ``GET /models`` list when one can
        be fetched, else the static ``[default, fallback]`` pair.

        Live ids are written in nvHive's spelling and matched to their catalog
        rows (exactly, else by tail: Google lists ``models/gemini-x``); a model
        the catalog does not know is a bare row.
        """
        live = await self.live_model_ids()
        if live is None:
            static = [m for m in dict.fromkeys((self._default_model, self._fallback_model)) if m]
            return [self._model_info(model_id) for model_id in static]
        by_tail = {model_id_tail(m.model_id): m for m in _catalog().list_models(provider=self._spec.name)}
        rows: dict[str, ModelInfo] = {}
        for model_id in sorted(live):
            info = self._model_info(model_id, by_tail)
            rows.setdefault(info.model_id, info)
        return list(rows.values())

    def _health(
        self, start: float, healthy: bool, error: str = "", models: int = 0
    ) -> HealthStatus:
        return HealthStatus(
            provider=self._provider_name,
            healthy=healthy,
            latency_ms=int((time.monotonic() - start) * 1000),
            # Redaction first, then the 200-character cut.
            error=_redact(error, self._api_key)[:200] or None,
            models_available=models,
        )

    async def health_check(self) -> HealthStatus:
        """Free ``GET /models`` when the endpoint is known; otherwise a one-token ping.

        The probe is :meth:`_fetch_models`, shared with :meth:`list_models`; a
        host without ``/models`` (404/405) falls through to the billed ping.
        """
        start = time.monotonic()
        probe = await self._fetch_models()
        if probe is not None:
            if probe.status_code is None:
                return self._health(start, False, probe.error or "models probe failed")
            if probe.ok:
                return self._health(start, True, models=len(probe.ids or []))
            if probe.status_code not in (404, 405):
                return self._health(start, False, f"HTTP {probe.status_code} from {probe.url}")
            # No /models on this host: fall through to the billed ping.
        try:
            if self._responses:
                # The Responses API floors max_output_tokens at 16.
                await litellm.aresponses(
                    input="ping",
                    max_output_tokens=16,
                    timeout=_HEALTH_TIMEOUT,
                    **self._kwargs(self._health_model),
                )
            else:
                await litellm.acompletion(
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    timeout=_HEALTH_TIMEOUT,
                    **self._kwargs(self._health_model),
                )
        except Exception as e:
            return self._health(start, False, str(e))
        return self._health(start, True)

    def estimate_tokens(self, text: str) -> int:
        try:
            import tiktoken

            enc = tiktoken.encoding_for_model(self._default_model)
            return len(enc.encode(text))
        except Exception:
            return len(text) // 4
