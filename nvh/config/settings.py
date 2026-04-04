"""Configuration system with YAML loading, env var interpolation, and Pydantic validation."""

from __future__ import annotations

import logging
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env var interpolation
# ---------------------------------------------------------------------------

_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-(.*?))?\}")


def _interpolate_env(value: Any) -> Any:
    """Recursively resolve ${VAR} and ${VAR:-default} in strings."""
    if isinstance(value, str):
        def _replacer(m: re.Match[str]) -> str:
            var_name = m.group(1)
            default = m.group(2)
            env_val = os.environ.get(var_name)
            if env_val is not None:
                return env_val
            if default is not None:
                # Resolve nested ${INNER} in default values
                return _interpolate_env(default)
            _log.warning(
                "Config: env var $%s is not set and has no default"
                " — value will be empty string",
                var_name,
            )
            return ""  # empty string, not raw ${VAR}

        return _ENV_PATTERN.sub(_replacer, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Config Models
# ---------------------------------------------------------------------------

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
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    council: CouncilModeConfig = Field(default_factory=CouncilModeConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    webhooks: list[WebhookConfigModel] = Field(default_factory=list)
    hooks: list[dict] = Field(default_factory=list)

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

DEFAULT_CONFIG_DIR = Path.home() / ".hive"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"
PROJECT_CONFIG_NAMES = [".hive.yaml", ".hive/config.yaml"]


def _find_project_config() -> Path | None:
    """Search upward from cwd for a project-level config file."""
    current = Path.cwd()
    for _ in range(20):  # limit depth
        for name in PROJECT_CONFIG_NAMES:
            candidate = current / name
            if candidate.is_file():
                return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


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

    # User-level config
    user_path = config_path or DEFAULT_CONFIG_PATH
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
    """Return the config directory, creating it if needed."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_CONFIG_DIR


def save_config(config: CouncilConfig, path: Path | None = None) -> Path:
    """Write config to YAML file."""
    target = path or DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude_defaults=False)
    with open(target, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return target


def generate_default_config() -> str:
    """Generate a default config YAML string for `hive config init`."""
    return """\
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
  openai:
    api_key: ${OPENAI_API_KEY}
    default_model: gpt-4o
    fallback_model: gpt-4o-mini
    enabled: false

  anthropic:
    api_key: ${ANTHROPIC_API_KEY}
    default_model: claude-sonnet-4-6
    fallback_model: claude-haiku-4-5-20251001
    enabled: false

  google:
    api_key: ${GOOGLE_API_KEY}
    default_model: gemini/gemini-2.0-flash
    fallback_model: gemini/gemini-2.0-flash
    enabled: false

  ollama:
    base_url: http://localhost:11434
    default_model: ollama/nemotron-small
    type: ollama
    enabled: false

  groq:
    api_key: ${GROQ_API_KEY}
    default_model: groq/llama-3.3-70b-versatile
    fallback_model: groq/llama-3.1-8b-instant
    enabled: false

  grok:
    api_key: ${XAI_API_KEY}
    default_model: xai/grok-2
    fallback_model: xai/grok-2
    base_url: https://api.x.ai/v1
    enabled: false

  mistral:
    api_key: ${MISTRAL_API_KEY}
    default_model: mistral/mistral-large-latest
    fallback_model: mistral/mistral-small-latest
    enabled: false

  cohere:
    api_key: ${COHERE_API_KEY}
    default_model: command-r-plus
    fallback_model: command-r
    enabled: false

  deepseek:
    api_key: ${DEEPSEEK_API_KEY}
    default_model: deepseek/deepseek-chat
    fallback_model: deepseek/deepseek-chat
    base_url: https://api.deepseek.com
    enabled: false

  mock:
    default_model: mock/default
    fallback_model: mock/fast
    type: mock
    enabled: false

  perplexity:
    api_key: ${PERPLEXITY_API_KEY}
    default_model: perplexity/llama-3.1-sonar-large-128k-online
    fallback_model: perplexity/llama-3.1-sonar-small-128k-online
    enabled: false

  together:
    api_key: ${TOGETHER_API_KEY:-${TOGETHERAI_API_KEY}}
    default_model: together_ai/meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo
    fallback_model: together_ai/meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo
    enabled: false

  fireworks:
    api_key: ${FIREWORKS_API_KEY}
    default_model: fireworks_ai/accounts/fireworks/models/llama-v3p1-70b-instruct
    fallback_model: fireworks_ai/accounts/fireworks/models/llama-v3p1-8b-instruct
    enabled: false

  openrouter:
    api_key: ${OPENROUTER_API_KEY}
    default_model: openrouter/meta-llama/llama-3.1-70b-instruct
    fallback_model: openrouter/meta-llama/llama-3.1-8b-instruct
    enabled: false

  cerebras:
    api_key: ${CEREBRAS_API_KEY}
    default_model: cerebras/llama3.1-70b
    fallback_model: cerebras/llama3.1-8b
    enabled: false

  sambanova:
    api_key: ${SAMBANOVA_API_KEY}
    default_model: sambanova/Meta-Llama-3.1-70B-Instruct
    fallback_model: sambanova/Meta-Llama-3.1-8B-Instruct
    enabled: false

  huggingface:
    api_key: ${HUGGINGFACE_API_KEY:-${HF_TOKEN}}
    default_model: huggingface/meta-llama/Meta-Llama-3-8B-Instruct
    fallback_model: huggingface/mistralai/Mistral-7B-Instruct-v0.3
    enabled: false

  ai21:
    api_key: ${AI21_API_KEY}
    default_model: jamba-1.5-large
    fallback_model: jamba-1.5-mini
    enabled: false

  github:
    api_key: ${GITHUB_TOKEN}
    default_model: gpt-4o-mini
    fallback_model: meta-llama-3.1-8b-instruct
    base_url: https://models.inference.ai.azure.com
    enabled: false

  nvidia:
    api_key: ${NVIDIA_API_KEY:-${NIM_API_KEY}}
    default_model: meta/llama-3.1-70b-instruct
    fallback_model: meta/llama-3.1-8b-instruct
    base_url: https://integrate.api.nvidia.com/v1
    enabled: false

  triton:
    base_url: ${TRITON_URL:-${TRITON_ENDPOINT:-http://localhost:8001}}
    default_model: ""
    type: triton
    enabled: false

  siliconflow:
    api_key: ${SILICONFLOW_API_KEY}
    default_model: Qwen/Qwen2.5-7B-Instruct
    base_url: https://api.siliconflow.cn/v1
    enabled: false

  llm7:
    api_key: ${LLM7_API_KEY:-anonymous}
    default_model: deepseek-r1-0528
    base_url: https://api.llm7.io/v1
    enabled: true

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
