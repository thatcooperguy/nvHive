"""Provider registry: discovery, registration, and lookup.

The model catalog (:meth:`ProviderRegistry.load_capabilities`) merges three
sources, in this precedence: an explicit, ``source:``-annotated override in
``nvh/config/capabilities.yaml``; LiteLLM's model table (``litellm.model_cost``,
with ``nvh/config/model_overrides.json`` registered into it for the spec
defaults LiteLLM does not carry); the ``ModelInfo`` defaults. The YAML owns the
hand-maintained fields (:data:`CATALOG_OWN_FIELDS`); ``scripts/gen_capabilities.py``
normalises it and its ``--check`` compares exactly those fields.

Process-level state a test must reset when it touches the catalog:
``reset_registry()`` (the module singleton), :func:`reset_catalog_state` (the
once-per-model "unpriced" log set and the applied-overrides set),
``nvh.providers.openai_compatible._catalog.cache_clear()`` (the adapters'
cached catalog) and ``nvh.api.server._LIVE_MODELS_CACHE.clear()``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from nvh.config.settings import CouncilConfig
from nvh.core.atohi import AdmissionBroker, AdmittedProvider, AtohiAdmission
from nvh.providers.base import ModelInfo, Provider
from nvh.providers.lazy_provider import LazyProvider
from nvh.providers.specs import PROVIDER_FACTS, PROVIDER_SPECS

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CATALOG_PATH = CONFIG_DIR / "capabilities.yaml"
MODEL_OVERRIDES_PATH = CONFIG_DIR / "model_overrides.json"

# The catalog fields capabilities.yaml owns (the generator writes exactly
# these plus explicit overrides, and ``--check`` compares exactly these).
# Every other ``ModelInfo`` field in a row is an explicit override of what
# LiteLLM says and the row must then carry a ``source:`` string.
CATALOG_OWN_FIELDS: tuple[str, ...] = (
    "provider", "display_name", "status", "successor", "source", "typical_latency_ms", "capability_scores",
)
# ``ModelInfo`` fields a YAML row may override (in the order the generator writes them).
CATALOG_OVERRIDE_FIELDS: tuple[str, ...] = (
    "context_window", "max_output_tokens", "supports_streaming", "supports_tools",
    "supports_function_calling", "supports_vision", "supports_json_mode", "supports_system_prompt",
    "input_cost_per_1m_tokens", "output_cost_per_1m_tokens", "deprecation_date",
)

# Models logged once as unpriced, and override ids already registered with
# LiteLLM in this process (see :func:`reset_catalog_state`).
_UNPRICED_LOGGED: set[str] = set()
_OVERRIDES_APPLIED: set[str] = set()
_SPEC_IDS: frozenset[str] = frozenset(
    m for spec in PROVIDER_SPECS.values() for m in (spec.default_model, spec.fallback_model)
)


def reset_catalog_state() -> None:
    """Forget which models were logged as unpriced and which overrides were registered (tests)."""
    _UNPRICED_LOGGED.clear()
    _OVERRIDES_APPLIED.clear()


def load_model_overrides(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The ``litellm.register_model`` payloads in model_overrides.json, keyed by model id.

    Each payload keeps its ``source`` string (stripped before registration).
    """
    data = json.loads((path or MODEL_OVERRIDES_PATH).read_text(encoding="utf-8"))
    models = data.get("models", {})
    return {model_id: dict(payload) for model_id, payload in models.items()}


def _litellm_table() -> dict[str, Any] | None:
    """``litellm.model_cost`` (the table the bill reads), or ``None`` without LiteLLM."""
    try:
        import litellm
    except Exception:  # pragma: no cover - litellm is a hard dependency
        return None
    table = getattr(litellm, "model_cost", None)
    return table if isinstance(table, dict) else None


def _apply_model_overrides(table: dict[str, Any], overrides: Mapping[str, Mapping[str, Any]]) -> None:
    """Register the override payloads LiteLLM's table lacks; LiteLLM wins where it has a row.

    Idempotent per process. LiteLLM warns once per registered model that the
    id is "not in built-in cost map"; that is the point, so the warning is
    silenced for the call.
    """
    pending = {
        model_id: {k: v for k, v in payload.items() if k != "source"}
        for model_id, payload in overrides.items()
        if model_id not in table and model_id not in _OVERRIDES_APPLIED
    }
    if not pending:
        return
    import litellm

    litellm_logger = logging.getLogger("LiteLLM")
    previous = litellm_logger.level
    litellm_logger.setLevel(logging.ERROR)
    try:
        litellm.register_model(pending)
    finally:
        litellm_logger.setLevel(previous)
    _OVERRIDES_APPLIED.update(pending)


def _per_million(cost_per_token: Any) -> Decimal:
    """A LiteLLM per-token price as a per-1M-token Decimal (``2.5e-06`` -> ``2.5``)."""
    value = (Decimal(str(cost_per_token)) * 1_000_000).quantize(Decimal("0.000001")).normalize()
    return value.quantize(Decimal(1)) if value == value.to_integral() else value


def _litellm_facts(table: dict[str, Any] | None, model_id: str, provider: str) -> dict[str, Any] | None:
    """LiteLLM's row for a catalog id: the id itself, else the provider-routed id.

    Plain table lookups only -- ``litellm.get_model_info`` runs provider code
    (Ollama's probes the daemon over HTTP), and the table is what
    ``litellm.cost_per_token`` bills from, so catalog and bill agree.
    """
    if not table:
        return None
    row = table.get(model_id)
    if row is None:
        spec = PROVIDER_FACTS.get(provider)
        if spec is not None and spec.litellm_prefix:
            row = table.get(spec.route(model_id))
    return row if isinstance(row, dict) else None


def _row_to_model_info(model_id: str, row: Mapping[str, Any], table: dict[str, Any] | None) -> ModelInfo:
    """Compose one ``ModelInfo`` from a YAML row and LiteLLM's facts (see the module docstring)."""
    provider = str(row.get("provider", ""))
    spec = PROVIDER_FACTS.get(provider)
    facts = _litellm_facts(table, model_id, provider)
    fields: dict[str, Any] = {
        key: value for key, value in row.items() if key in CATALOG_OWN_FIELDS and key != "source"
    }
    explicit = {key: row[key] for key in CATALOG_OVERRIDE_FIELDS if key in row}

    if facts is not None:
        max_input = facts.get("max_input_tokens") or facts.get("max_tokens")
        if max_input:
            fields["context_window"] = int(max_input)
        max_output = facts.get("max_output_tokens") or facts.get("max_tokens")
        if max_output:
            fields["max_output_tokens"] = int(max_output)
        native = facts.get("supports_function_calling")
        if native is not None:
            fields["supports_function_calling"] = bool(native)
            fields["supports_tools"] = bool(native)
        if facts.get("supports_vision") is not None:
            fields["supports_vision"] = bool(facts["supports_vision"])
        if facts.get("supports_response_schema") is not None:
            fields["supports_json_mode"] = bool(facts["supports_response_schema"])
        if facts.get("supports_system_messages") is not None:
            fields["supports_system_prompt"] = bool(facts["supports_system_messages"])
        if facts.get("deprecation_date"):
            fields["deprecation_date"] = str(facts["deprecation_date"])

    # Price: a zero-cost provider is free by definition; else the row's explicit
    # rates; else LiteLLM's; else unknown (never a silent $0).
    if spec is not None and spec.zero_cost:
        fields["input_cost_per_1m_tokens"] = Decimal(0)
        fields["output_cost_per_1m_tokens"] = Decimal(0)
    elif "input_cost_per_1m_tokens" in explicit or "output_cost_per_1m_tokens" in explicit:
        pass  # applied with the other explicit overrides below
    elif facts is not None and (
        facts.get("input_cost_per_token") is not None or facts.get("output_cost_per_token") is not None
    ):
        fields["input_cost_per_1m_tokens"] = _per_million(facts.get("input_cost_per_token") or 0)
        fields["output_cost_per_1m_tokens"] = _per_million(facts.get("output_cost_per_token") or 0)
    else:
        fields["cost_known"] = False
        if model_id not in _UNPRICED_LOGGED:
            _UNPRICED_LOGGED.add(model_id)
            if spec is not None and spec.metered_upstream:
                # Expected: the provider meters usage on its own account at a
                # rate nvHive cannot see, so 'cost unknown' is the honest row.
                logger.debug(
                    "model %s (%s): metered on the provider's own account; LiteLLM bills $0 and the "
                    "catalog shows 'cost unknown'", model_id, provider,
                )
            else:
                log = logger.warning if model_id in _SPEC_IDS else logger.info
                log(
                    "model %s (%s): cost unknown -- not in LiteLLM's table, model_overrides.json or a "
                    "capabilities.yaml override; it is recorded as $0 but shown as 'cost unknown'",
                    model_id, provider,
                )

    if explicit.get("deprecation_date") is not None:
        # YAML reads a bare ISO date (``deprecation_date: 2026-06-01``) as a
        # ``datetime.date``; ModelInfo keeps the string, as the LiteLLM branch does.
        explicit["deprecation_date"] = str(explicit["deprecation_date"])
    fields.update(explicit)
    if "supports_function_calling" in explicit and "supports_tools" not in explicit:
        fields["supports_tools"] = bool(explicit["supports_function_calling"])
    return ModelInfo(model_id=model_id, **fields)

# Adapters with their own transport or discovery code. Every other provider is
# a PROVIDER_SPECS row served by OpenAICompatibleProvider.
BESPOKE_ADAPTERS: dict[str, tuple[str, str]] = {
    "ollama": ("nvh.providers.ollama_provider", "OllamaProvider"),
    "triton": ("nvh.providers.triton_provider", "TritonProvider"),
    "mock": ("nvh.providers.mock_provider", "MockProvider"),
}

# provider name -> date its service shut down. Stanzas left in a user's
# config are skipped here and removed by `nvh config migrate`.
RETIRED_PROVIDERS: dict[str, str] = {
    "github": "2026-07-30",
}


def _keyring_enabled() -> bool:
    # Headless/rootless cloud desktops often have a slow or unavailable
    # keyring service, so startup never blocks on it unless asked to.
    return os.environ.get("NVH_USE_KEYRING", "0").lower() in {"1", "true", "yes"}


def configured_default_model(name: str, pconfig: Any = None) -> str | None:
    """The default model a config stanza names for a provider, else its spec row's; ``None`` when neither does.

    The one answer ``/v1/models`` (its ``warnings``) and the DEEP
    ``default_models_served`` check compare against a live model list.
    """
    configured = str(getattr(pconfig, "default_model", "") or "")
    if configured:
        return configured
    spec = PROVIDER_SPECS.get(str(getattr(pconfig, "type", "") or "") or name)
    return (spec.default_model if spec is not None else None) or None


def resolve_provider_key(
    name: str, pconfig: Any = None, *, ptype: str | None = None,
) -> tuple[str | None, str]:
    """``(key, source)`` for a provider, or ``(None, "none")``.

    One resolution order for the registry, the adapters and the diagnostics:
    the config value (unless it is an unexpanded ``${VAR}`` placeholder),
    ``COUNCIL_<NAME>_API_KEY``, ``<NAME>_API_KEY``, the spec's ``env_keys``,
    the keyring when ``NVH_USE_KEYRING`` is set, then the spec's anonymous
    key. ``source`` is ``config``, ``env:<VAR>``, ``keyring`` or ``anonymous``.
    """
    configured = str(getattr(pconfig, "api_key", "") or "")
    if configured and not configured.startswith("${"):
        return configured, "config"
    spec = PROVIDER_SPECS.get(ptype or name)
    upper = name.upper()
    env_names = [f"COUNCIL_{upper}_API_KEY", f"{upper}_API_KEY"]
    if spec is not None:
        env_names.extend(v for v in spec.env_keys if v not in env_names)
    for env_name in env_names:
        value = os.environ.get(env_name, "")
        if value:
            return value, f"env:{env_name}"
    if _keyring_enabled():
        try:
            import keyring

            value = keyring.get_password("nvhive", f"{name}_api_key") or ""
        except Exception:
            value = ""
        if value:
            return value, "keyring"
    if spec is not None and spec.anonymous_key:
        return spec.anonymous_key, "anonymous"
    return None, "none"


def lazy_adapter(name: str, ptype: str = "", **kwargs: Any) -> LazyProvider:
    """Wrap the adapter for ``name`` so it is imported on first use.

    Blank kwargs are dropped so the adapter's own defaults apply. Unknown
    types (including ``openai_compatible``) take the generic OpenAI route with
    the caller's ``base_url``.
    """
    kwargs = {k: v for k, v in kwargs.items() if v not in ("", None)}
    ptype = ptype or name
    if ptype in BESPOKE_ADAPTERS:
        module_path, class_name = BESPOKE_ADAPTERS[ptype]
        return LazyProvider(name, module_path, class_name, provider_name=name, **kwargs)
    spec = PROVIDER_SPECS.get(ptype, PROVIDER_SPECS["openai"])
    return LazyProvider(
        name,
        "nvh.providers.openai_compatible",
        "OpenAICompatibleProvider",
        spec=spec,
        provider_name=name,
        **kwargs,
    )


class ProviderRegistry:
    """Central registry for all LLM provider adapters."""

    def __init__(self, *, atohi_broker: AdmissionBroker | None = None) -> None:
        self._providers: dict[str, Provider] = {}
        self._model_catalog: dict[str, ModelInfo] = {}
        self._atohi_broker = atohi_broker
        self._admission: AtohiAdmission | None = None
        self._provider_types: dict[str, str] = {}
        self._default_models: dict[str, str] = {}
        self._bound_admission = False
        self._admitted: dict[str, AdmittedProvider] = {}

    def _matches_admission(self, config: CouncilConfig) -> bool:
        return self._admission is not None and (
            self._admission.enabled == config.atohi.enabled
            and self._admission.managed_providers == frozenset(config.atohi.managed_providers)
            and self._provider_types == {
                name: value.type or name for name, value in config.providers.items()
            }
            and self._default_models == {name: value.default_model for name, value in config.providers.items()}
        )

    def scoped(self, config: CouncilConfig) -> ProviderRegistry:
        """Bind one caller's policy while preserving shared adapter registration.

        Engine and its Council reuse a matching bound view. Other callers share
        raw adapters/catalog, never policy or wrappers; later registrations from
        either the original registry or the engine remain immediately visible.
        """
        if self._bound_admission and self._matches_admission(config):
            return self
        view = ProviderRegistry(atohi_broker=self._atohi_broker)
        view._providers = self._providers
        view._model_catalog = self._model_catalog
        view.configure_admission(config)
        view._bound_admission = True
        return view

    def configure_admission(self, config: CouncilConfig) -> None:
        if self._bound_admission:
            if not self._matches_admission(config):
                raise ValueError("Bound admission policy cannot change; create a new scoped registry")
            return
        self._admission = AtohiAdmission(config.atohi, broker=self._atohi_broker)
        self._provider_types = {name: value.type or name for name, value in config.providers.items()}
        self._default_models = {name: value.default_model for name, value in config.providers.items()}
        self._admitted.clear()

    def check_admission_available(self) -> None:
        """Fail before fan-out or initialization can contact any provider."""
        if self._admission is not None:
            self._admission.require_broker()

    @property
    def admission(self) -> AtohiAdmission:
        """The configured policy for trusted auxiliary calls owned by this view."""
        if self._admission is None:
            raise ValueError("Registry admission policy is not configured")
        return self._admission

    def register(self, name: str, provider: Provider) -> None:
        if isinstance(provider, AdmittedProvider):
            provider = provider.provider
        self._providers[name] = provider
        self._admitted.pop(name, None)

    def get(self, name: str) -> Provider:
        if name not in self._providers:
            raise KeyError(f"Provider '{name}' is not registered. Available: {list(self._providers.keys())}")
        provider = self._providers[name]
        if self._admission is not None and self._admission.manages(
            name, self._provider_types.get(name, name),
        ):
            wrapped = self._admitted.get(name)
            if wrapped is None or wrapped.provider is not provider:
                wrapped = AdmittedProvider(provider, self._admission, name,
                                           default_model=self._default_models.get(name, ""))
                self._admitted[name] = wrapped
            return wrapped
        return provider

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())

    def list_enabled(self) -> list[str]:
        return list(self._providers.keys())

    def has(self, name: str) -> bool:
        return name in self._providers

    # -----------------------------------------------------------------------
    # Model Catalog
    # -----------------------------------------------------------------------

    def load_capabilities(self, path: Path | None = None, *, overrides_path: Path | None = None) -> None:
        """Load the model catalog: capabilities.yaml rows merged with LiteLLM's model table.

        Order per row: the row's explicit overrides (``source:``-annotated
        fields) beat LiteLLM's table, which beats the ``ModelInfo`` defaults;
        the ``model_overrides.json`` payloads are registered into LiteLLM's
        table first for the ids it lacks (so the bill and the catalog agree)
        and skipped once LiteLLM carries the id. A zero-cost provider's rows
        are $0; a row no source prices gets ``cost_known=False`` and is logged
        once per process (warning for a spec default or fallback, info
        otherwise). No network: the table is whatever ``import litellm``
        loaded, and only plain lookups touch it.
        """
        if path is None:
            path = CATALOG_PATH
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        models = data.get("models") or {}
        table = _litellm_table()
        if table is not None:
            overrides_file = overrides_path or MODEL_OVERRIDES_PATH
            if overrides_file.exists():
                try:
                    _apply_model_overrides(table, load_model_overrides(overrides_file))
                except Exception as exc:  # a bad override file must not take the catalog down
                    logger.warning("model_overrides.json not applied: %s", exc)
        for model_id, info in models.items():
            self._model_catalog[model_id] = _row_to_model_info(model_id, info or {}, table)

    def get_model_info(self, model_id: str) -> ModelInfo | None:
        return self._model_catalog.get(model_id)

    def list_models(self, provider: str | None = None) -> list[ModelInfo]:
        models = list(self._model_catalog.values())
        if provider:
            models = [m for m in models if m.provider == provider]
        return models

    def get_models_for_provider(self, provider_name: str) -> list[ModelInfo]:
        return [m for m in self._model_catalog.values() if m.provider == provider_name]

    # -----------------------------------------------------------------------
    # Auto-setup from config
    # -----------------------------------------------------------------------

    def setup_from_config(self, config: CouncilConfig) -> list[str]:
        """Initialize provider adapters from config. Returns list of enabled provider names."""
        self.configure_admission(config)
        self.check_admission_available()
        enabled = []

        for name, pconfig in config.providers.items():
            if not pconfig.enabled:
                continue
            if name in RETIRED_PROVIDERS:
                logger.warning(
                    "provider %s was retired on %s — remove it or run `nvh config migrate`",
                    name, RETIRED_PROVIDERS[name],
                )
                continue

            ptype = pconfig.type or name
            api_key = resolve_provider_key(name, pconfig, ptype=ptype)[0] or ""
            if ptype == "mock":
                # Mock provider: construct directly without API key forwarding
                from nvh.providers.mock_provider import MockProvider

                provider: Provider = MockProvider(
                    default_model=pconfig.default_model or "mock/default",
                    fallback_model=pconfig.fallback_model or "mock/fast",
                    provider_name=name,
                )
            else:
                provider = lazy_adapter(
                    name,
                    ptype,
                    api_key=api_key,
                    default_model=pconfig.default_model,
                    fallback_model=pconfig.fallback_model,
                    base_url=pconfig.base_url,
                )
            self.register(name, provider)
            enabled.append(name)

        self.load_capabilities()
        return enabled


# Module-level singleton
_registry: ProviderRegistry | None = None


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None
