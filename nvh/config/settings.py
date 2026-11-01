"""Configuration system with YAML loading, env var interpolation, and Pydantic validation."""

from __future__ import annotations

import logging
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from nvh.integrations.workspace.storage import storage_layout

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env var interpolation
# ---------------------------------------------------------------------------

def _matching_brace(text: str, open_at: int) -> int:
    """Index of the ``}`` that closes the ``{`` at ``open_at``, or ``-1`` when it never closes."""
    depth = 0
    for j in range(open_at, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _expand_env_refs(text: str) -> str:
    """``text`` with every ``${VAR}`` / ``${VAR:-default}`` resolved, nested defaults included.

    Braces are matched, so ``${A:-${B:-x}}`` reads ``A``, else ``B``, else
    ``x`` (the generated config's key references nest a provider's alternate
    variables this way, and Triton's ``base_url`` chains its two endpoint
    variables). A default is expanded only when it is needed, so an unset
    alternate warns only when the primary is unset too. An unset variable
    with no default becomes the empty string, with a warning; text that is
    not a variable reference (``${}``, an unterminated ``${``) is kept.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        start = text.find("${", i)
        if start < 0:
            out.append(text[i:])
            break
        out.append(text[i:start])
        end = _matching_brace(text, start + 1)
        if end < 0:
            out.append(text[start:])
            break
        name, sep, default = text[start + 2:end].partition(":-")
        if not name or any(ch in name for ch in "${}:"):
            out.append(text[start:end + 1])
        else:
            env_val = os.environ.get(name)
            if env_val is not None:
                out.append(env_val)
            elif sep:
                out.append(_expand_env_refs(default))
            else:
                _log.warning(
                    "Config: env var $%s is not set and has no default"
                    " — value will be empty string",
                    name,
                )
                out.append("")  # empty string, not raw ${VAR}
        i = end + 1
    return "".join(out)


def _interpolate_env(value: Any) -> Any:
    """Recursively resolve ``${VAR}`` and ``${VAR:-default}`` in strings (see :func:`_expand_env_refs`)."""
    if isinstance(value, str):
        return _expand_env_refs(value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Config Models
# ---------------------------------------------------------------------------

class AtohiConfig(BaseModel):
    """Opt-in admission boundary; no production broker is installed yet."""

    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = False
    managed_providers: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("managed_providers")
    @classmethod
    def provider_names(cls, names: list[str]) -> list[str]:
        if len(set(names)) != len(names) or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) for name in names
        ):
            raise ValueError("Atohi managed providers must be unique bounded provider names")
        return names


class ProviderConfig(BaseModel):
    api_key: str = ""
    default_model: str = ""
    fallback_model: str = ""
    base_url: str = ""
    type: str = ""  # e.g. "openai_compatible"
    enabled: bool = True
    timeout: int = Field(default=60, ge=1, le=600)
    retry_attempts: int = Field(default=3, ge=0, le=10)
    retry_initial_delay: float = Field(default=1.0, ge=0.0)
    retry_multiplier: float = Field(default=2.0, ge=1.0)
    retry_max_delay: float = Field(default=30.0, ge=0.0)


class CouncilWeights(BaseModel):
    """Weights per advisor for hive mode. Values are normalized to sum to 1.0."""
    weights: dict[str, float] = Field(default_factory=dict)

    @field_validator("weights")
    @classmethod
    def normalize_weights(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            return v
        total = sum(v.values())
        if total <= 0:
            return v
        if abs(total - 1.0) > 0.01:
            return {k: val / total for k, val in v.items()}
        return v


class CouncilModeConfig(BaseModel):
    default_weights: dict[str, float] = Field(default_factory=dict)
    synthesis_provider: str = ""
    strategy: str = "weighted_consensus"  # weighted_consensus | majority_vote | best_of
    fallback_order: list[str] = Field(default_factory=list)
    quorum: int = Field(default=2, ge=1, le=10)
    timeout: int = Field(default=60, ge=5, le=600)


class RoutingRule(BaseModel):
    match: dict[str, str] = Field(default_factory=dict)
    provider: str = ""
    model: str = ""


class RoutingConfig(BaseModel):
    weights: dict[str, float] = Field(default_factory=lambda: {
        "capability": 0.4,
        "cost": 0.3,
        "latency": 0.2,
        "health": 0.1,
    })
    rules: list[RoutingRule] = Field(default_factory=list)


class BudgetConfig(BaseModel):
    daily_limit_usd: Decimal = Field(
        default=Decimal("5"), ge=0,
    )
    monthly_limit_usd: Decimal = Field(
        default=Decimal("20"), ge=0,
    )
    alert_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    hard_stop: bool = True
    degrade_on_limit: bool = True


class DefaultsConfig(BaseModel):
    mode: str = "ask"  # "ask" (single LLM), "convene" (council), "poll" (compare), "throwdown"
    provider: str = ""
    model: str = ""
    output: str = "text"
    stream: bool = True
    timeout: int = Field(default=30, ge=1, le=600)
    max_tokens: int = Field(default=4096, ge=1, le=200_000)
    temperature: float = Field(default=1.0, ge=0.0, le=2.0)
    system_prompt: str = "Always respond in English unless the user explicitly requests another language."
    show_metadata: bool = True
    orchestration_mode: str = "auto"  # off, light, full, auto
    prefer_nvidia: bool = False


class CacheConfig(BaseModel):
    enabled: bool = True
    ttl_seconds: int = Field(default=86400, ge=1)
    max_size: int = Field(default=1000, ge=1)
    cache_nonzero_temp: bool = False


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = ""


class ProfileConfig(BaseModel):
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    council: CouncilModeConfig = Field(default_factory=CouncilModeConfig)

    @model_validator(mode="before")
    @classmethod
    def _accept_advisors_key(cls, data: Any) -> Any:
        """Accept 'advisors' as an alias for 'providers' in YAML configs."""
        if isinstance(data, dict) and "atohi" in data:
            raise ValueError("Atohi admission is top-level configuration, not a profile override")
        if isinstance(data, dict) and "advisors" in data and "providers" not in data:
            data = dict(data)
            data["providers"] = data.pop("advisors")
        return data


class WebhookConfigModel(BaseModel):
    url: str = ""
    events: list[str] = Field(default_factory=list)
    secret: str = ""
    enabled: bool = True
    retry_count: int = 3
    timeout_seconds: int = 10


class CouncilConfig(BaseModel):
    """Root configuration model."""
    version: str = "1"
    atohi: AtohiConfig = Field(default_factory=AtohiConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    council: CouncilModeConfig = Field(default_factory=CouncilModeConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    webhooks: list[WebhookConfigModel] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_advisors_key(cls, data: Any) -> Any:
        """Accept 'advisors' as an alias for 'providers' in YAML configs."""
        if isinstance(data, dict) and "advisors" in data and "providers" not in data:
            data = dict(data)
            data["providers"] = data.pop("advisors")
        return data


# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------

# ``DEFAULT_CONFIG_DIR`` / ``DEFAULT_CONFIG_PATH`` are the one path oracle's
# answer, ``storage_layout().config_dir`` ($NVH_CONFIG, else $HIVE_CONFIG_HOME,
# else $NVH_HOME/config, else ~/.nvh/config), taken at import like every
# other module constant (design D7). They are plain module attributes, so
# ``monkeypatch.setattr`` / ``mock.patch`` on either name and
# ``activate_storage()`` rebind them as before; a test that changes
# ``NVH_HOME`` after the import calls ``reset_default_paths()`` to re-derive.
DEFAULT_CONFIG_DIR = storage_layout().config_dir
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"

# Project overlay files, searched upward from cwd (``.nvh`` since 0.44; the
# pre-0.44 ``.hive`` names are read as a fallback at every level).
PROJECT_CONFIG_NAMES = [".nvh.yaml", ".nvh/config.yaml"]


def reset_default_paths() -> None:
    """Re-derive ``DEFAULT_CONFIG_DIR`` / ``DEFAULT_CONFIG_PATH`` from ``storage_layout()``.

    The reset hook for tests that change ``NVH_HOME`` / ``NVH_CONFIG`` after
    the module was imported; ``activate_storage()`` does the same for a
    layout chosen at runtime.
    """
    global DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_PATH
    DEFAULT_CONFIG_DIR = storage_layout().config_dir
    DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"


def _project_config_names() -> list[str]:
    from nvh.integrations.workspace.migrate_legacy import LEGACY_PROJECT_CONFIG_NAMES

    return [*PROJECT_CONFIG_NAMES, *LEGACY_PROJECT_CONFIG_NAMES]


def _find_project_config() -> Path | None:
    """Search upward from cwd for a project-level config file.

    The user's own config locations — the layout's ``config_dir``, the
    ``NVH_HOME`` root and the pre-0.44 ``~/.hive`` — are never a project
    overlay: merging one on top of the real user config once left an
    ``advisors:``-style config with zero providers whenever the CLI ran from
    anywhere under ``$HOME``. The walk also stops at the home directory —
    nothing above it is a project.
    """
    from nvh.integrations.workspace.migrate_legacy import legacy_hive_home

    home = Path.home().resolve()
    layout = storage_layout()
    user_config_dirs = {
        legacy_hive_home().resolve(),
        layout.home.resolve(),
        layout.config_dir.resolve(),
        Path(DEFAULT_CONFIG_DIR).resolve(),
    }
    names = _project_config_names()
    current = Path.cwd()
    for _ in range(20):  # limit depth
        for name in names:
            candidate = current / name
            if candidate.is_file() and candidate.parent.resolve() not in user_config_dirs:
                return candidate
        parent = current.parent
        if parent == current or current.resolve() == home:
            break
        current = parent
    return None


def _migrate_legacy_homes_once() -> None:
    """Run the one-shot pre-0.44 import; never let it break config loading."""
    try:
        from nvh.integrations.workspace.migrate_legacy import migrate_legacy_homes

        migrate_legacy_homes()
    except Exception as exc:  # noqa: BLE001 — a migration must never block startup
        _log.warning("Legacy home migration skipped: %s", exc)


def _legacy_user_config_exists() -> bool:
    try:
        from nvh.integrations.workspace.migrate_legacy import legacy_hive_home

        return (legacy_hive_home() / "config.yaml").is_file()
    except Exception:  # noqa: BLE001
        return False


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and apply env var interpolation."""
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        _log.error("Invalid YAML in %s: %s", path, e)
        raise ValueError(
            f"Config file has invalid YAML syntax: {path}\n"
            f"Error: {e}\n"
            f"Fix the file or regenerate with: nvh config init --force"
        ) from e
    except PermissionError:
        _log.error("Cannot read config file (permission denied): %s", path)
        raise
    if not isinstance(raw, dict):
        _log.error("Config file must be a YAML mapping, got %s", type(raw).__name__)
        raise ValueError(
            f"Config file must be a YAML mapping (dict), not {type(raw).__name__}: {path}"
        )
    return _interpolate_env(raw)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge override into base, returning a new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(
    config_path: Path | None = None,
    profile: str | None = None,
) -> CouncilConfig:
    """Load configuration from YAML files with env var interpolation and profile merging.

    Precedence: project config > user config > defaults.
    CLI flags and env vars are applied separately at the call site.
    """
    merged: dict[str, Any] = {}

    # User-level config. An upgraded install whose config still sits in the
    # pre-0.44 ``~/.hive`` gets it copied into the layout once, right here,
    # so the first command after the upgrade does not run unconfigured.
    if config_path is None:
        user_path = Path(DEFAULT_CONFIG_PATH)
        if not user_path.is_file() and _legacy_user_config_exists():
            _migrate_legacy_homes_once()
    else:
        user_path = config_path
    if user_path.is_file():
        merged = _load_yaml(user_path)

    # Project-level config (overrides user)
    project_path = _find_project_config()
    if project_path and project_path != user_path:
        project_data = _load_yaml(project_path)
        merged = _deep_merge(merged, project_data)

    # Apply profile overrides
    if profile:
        profiles = merged.get("profiles", {})
        if profile in profiles:
            profile_data = profiles[profile]
            merged = _deep_merge(merged, profile_data)

    # Also check env var for profile
    if not profile:
        env_profile = os.environ.get("HIVE_PROFILE")
        if env_profile:
            profiles = merged.get("profiles", {})
            if env_profile in profiles:
                merged = _deep_merge(merged, profiles[env_profile])

    try:
        return CouncilConfig(**merged)
    except Exception as e:
        _log.error("Config validation failed: %s", e)
        raise ValueError(
            f"Config validation error: {e}\n"
            f"Check your config file for invalid values.\n"
            f"Reset to defaults: nvh config init --force"
        ) from e


def get_config_dir() -> Path:
    """Return the config directory, creating it if needed.

    This is the init path (``nvh config init``, ``nvh setup``), so the
    one-shot pre-0.44 home import runs here before the directory is used.
    """
    _migrate_legacy_homes_once()
    config_dir = Path(DEFAULT_CONFIG_DIR)
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def save_config(config: CouncilConfig, path: Path | None = None) -> Path:
    """Write config to YAML file."""
    target = path or Path(DEFAULT_CONFIG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude_defaults=False)
    with open(target, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return target


# Budget the generated config's local default is sized for: the smallest GPU
# tier of the table (4-8 GB), whose chat pick also runs from system RAM on a
# CPU-only box. ``hive config init`` runs before any hardware is known.
_DEFAULT_LOCAL_BUDGET_GB = 4.0


def _default_local_model() -> str:
    """``ollama/<tag>`` a freshly generated config names as the local default.

    The tag is :func:`nvh.core.local_models.pick` for
    :data:`_DEFAULT_LOCAL_BUDGET_GB` and the ``chat`` column, so the template
    never carries a hand-typed tag that the registry may since have retired.
    Imported lazily: loading settings must not pull the tier table in.
    """
    from nvh.core.local_models import pick

    chosen = pick(_DEFAULT_LOCAL_BUDGET_GB, "chat")
    assert chosen is not None  # every tier has a chat pick
    return f"ollama/{chosen.tag}"


def _key_reference(spec: Any, default: str = "") -> str:
    """``${PRIMARY:-${ALT:-default}}``: a spec row's key variables as nested shell-style defaults.

    The primary variable first, then the provider's own alternates
    (:attr:`~nvh.providers.specs.ProviderSpec.alt_key_envs`), then
    ``default`` — the anonymous key for a provider that has one.
    """
    reference = default
    for name in reversed([spec.key_env, *spec.alt_key_envs]):
        reference = f"${{{name}:-{reference}}}" if reference else f"${{{name}}}"
    return reference


def _advisor_stanzas() -> str:
    """The ``advisors:`` body of the default config: one stanza per spec row.

    Cloud rows carry their key reference, default and fallback model and base
    URL; Ollama's ``default_model`` is the ``@OLLAMA_DEFAULT_MODEL@``
    placeholder :func:`generate_default_config` fills from the tier table;
    Triton's ``base_url`` reads its endpoint variables; the bespoke adapters
    name their ``type``. Only the anonymous tier (LLM7) starts enabled.
    Imported lazily so loading settings never pulls the spec table in.
    """
    from nvh.providers.specs import BESPOKE_SPECS, PROVIDER_FACTS
    from nvh.providers.triton_provider import DEFAULT_TRITON_URL
    from nvh.utils.ollama import DEFAULT_OLLAMA_URL

    stanzas = []
    for name, spec in PROVIDER_FACTS.items():
        lines = [f"  {name}:"]
        if name == "ollama":
            lines.append(f"    base_url: {DEFAULT_OLLAMA_URL}")
            lines.append("    default_model: @OLLAMA_DEFAULT_MODEL@")
        elif name == "triton":
            lines.append(f"    base_url: {_key_reference(spec, default=DEFAULT_TRITON_URL)}")
            lines.append('    default_model: ""')
        else:
            if spec.takes_api_key:
                lines.append(f"    api_key: {_key_reference(spec, default=spec.anonymous_key)}")
            lines.append(f"    default_model: {spec.default_model}")
            if spec.fallback_model:
                lines.append(f"    fallback_model: {spec.fallback_model}")
            if spec.base_url:
                lines.append(f"    base_url: {spec.base_url}")
        if name in BESPOKE_SPECS:
            lines.append(f"    type: {name}")
        lines.append(f"    enabled: {'true' if spec.anonymous_key else 'false'}")
        stanzas.append("\n".join(lines))
    return "\n\n".join(stanzas)


def generate_default_config() -> str:
    """Generate a default config YAML string for `hive config init`.

    The ``advisors:`` stanzas are rendered from the provider spec table
    (:func:`_advisor_stanzas`) and the Ollama ``default_model`` is filled
    from the tier table at call time (:func:`_default_local_model`);
    everything else is the literal template.
    """
    template = """\
version: "1"

defaults:
  provider: ""
  output: text
  stream: true
  timeout: 30
  max_tokens: 4096
  temperature: 1.0
  show_metadata: true

advisors:
@ADVISOR_STANZAS@

council:
  default_weights:
    openai: 0.40
    anthropic: 0.35
    google: 0.25
  synthesis_provider: anthropic
  strategy: weighted_consensus
  fallback_order: [openai, anthropic, google, ollama]
  quorum: 2

routing:
  weights:
    capability: 0.4
    cost: 0.3
    latency: 0.2
    health: 0.1
  rules: []

budget:
  daily_limit_usd: 5
  monthly_limit_usd: 20
  alert_threshold: 0.80
  hard_stop: true
  degrade_on_limit: true

cache:
  enabled: true
  ttl_seconds: 86400
  max_size: 1000

logging:
  level: INFO
"""
    return (
        template.replace("@ADVISOR_STANZAS@", _advisor_stanzas())
        .replace("@OLLAMA_DEFAULT_MODEL@", _default_local_model())
    )
