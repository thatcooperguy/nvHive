"""Agent profiles — named personas with their own system prompt + LLM mapping.

A *profile* is a saved configuration of:

  - ``name``         — stable id ("coder", "researcher", "writer", ...)
  - ``title``        — display label ("Code Reviewer")
  - ``description``  — one-line user-facing summary
  - ``system_prompt``— persona / instructions appended to the Wizard's
                       global persona for this profile
  - ``provider``     — preferred provider id (ollama, openai, groq, ...)
  - ``model``        — preferred model id ("ollama/qwen2.5-coder:7b", ...)
  - ``temperature``  — sampling override (None inherits engine default)
  - ``max_tokens``   — generation cap override
  - ``tools_allowed``— optional whitelist of wizard tool names (None = all)
  - ``built_in``     — True for ships-with-nvHive profiles; user copies and
                       edits land under ``user_profiles`` and are mutable.

Profiles live as YAML under ``$NVH_HOME/agent-profiles/`` so they survive
reconnects. The built-in defaults below ship in code and are read-only —
users who want to tweak one save a copy under a new name.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentProfile:
    """A named persona + LLM mapping the user can switch the Wizard into."""

    name: str
    title: str
    description: str
    system_prompt: str
    provider: str = ""  # empty = let the router pick
    model: str = ""  # empty = let the router pick within provider
    temperature: float | None = None
    max_tokens: int | None = None
    tools_allowed: list[str] | None = None
    built_in: bool = False
    tags: list[str] = field(default_factory=list)
    # Optional avatar reference. For built-ins this is the URL of the
    # /v1/wizard/profiles/{name}/avatar endpoint (returns an SVG). For
    # user profiles it can be: a path under NVH_HOME/agent-profiles/
    # avatars/, an absolute URL the user pasted, or empty (initial fallback).
    avatar: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Ships-with-nvHive profiles. Each is a sane default for a common workflow.
# Users can copy + edit any of these into ``$NVH_HOME/agent-profiles/`` under
# a new name. The mapping uses provider "" / model "" to mean "let the router
# pick" — so a user without an OpenAI key still gets a working Coder profile
# routed through local Ollama.
_AVATAR_URL_PREFIX = "/v1/wizard/profiles/"


def _avatar_url_for(name: str) -> str:
    """Stable URL the WebUI can render directly without bundling assets."""
    return f"{_AVATAR_URL_PREFIX}{name}/avatar"


BUILT_IN_PROFILES: tuple[AgentProfile, ...] = (
    AgentProfile(
        name="wizard",
        title="AI Wizard (default)",
        description="The calm mission-control mentor. Reads live state, fixes things, walks you through setup.",
        system_prompt="",  # Inherits the global Wizard persona unchanged.
        provider="",
        model="",
        built_in=True,
        tags=["general", "setup", "support"],
        avatar=_avatar_url_for("wizard"),
    ),
    AgentProfile(
        name="coder",
        title="Code Reviewer",
        description="A careful code reviewer that prefers small fixes, names risks, and explains tradeoffs.",
        system_prompt=(
            "You are a careful code reviewer for the user's projects. "
            "Read the code or diff they share, identify real bugs and risks, "
            "name security concerns explicitly, and suggest the smallest fix "
            "that solves the problem. Prefer concrete patches over vague "
            "advice. If you're unsure, ask one clarifying question."
        ),
        provider="ollama",
        model="ollama/qwen2.5-coder:7b",
        temperature=0.2,
        built_in=True,
        tags=["code", "review"],
        avatar=_avatar_url_for("coder"),
    ),
    AgentProfile(
        name="researcher",
        title="Research Assistant",
        description="Web-search-aware research helper. Cites sources, summarizes deeply, flags uncertainty.",
        system_prompt=(
            "You are a research assistant. When the user asks about a topic "
            "that requires current information, call the web_search tool and "
            "cite sources by URL. When they ask about something in their own "
            "notes or files, prefer rag_ask_vault or rag_ask. Summarize "
            "carefully, distinguish primary sources from blog posts, and "
            "flag explicitly when sources disagree."
        ),
        provider="",
        model="",
        temperature=0.4,
        tools_allowed=["web_search", "rag_ask", "rag_ask_vault"],
        built_in=True,
        tags=["research", "search"],
        avatar=_avatar_url_for("researcher"),
    ),
    AgentProfile(
        name="writer",
        title="Long-form Writer",
        description="Drafts and edits long-form content with a warm, plainspoken voice.",
        system_prompt=(
            "You are a long-form writing collaborator. Help the user draft, "
            "revise, and tighten essays, documentation, and reports. Prefer "
            "plain language. Read the user's existing tone from anything "
            "they share and match it. Don't pad. When asked to edit, show "
            "the changed sentence first, then explain the change briefly."
        ),
        provider="",
        model="",
        temperature=0.6,
        built_in=True,
        tags=["writing"],
        avatar=_avatar_url_for("writer"),
    ),
    AgentProfile(
        name="ops",
        title="Workspace Operator",
        description="Hands-on operator that prefers running repair_workspace and refreshing models when things break.",
        system_prompt=(
            "You are a hands-on operator for this rootless nvHive workspace. "
            "When something is broken or off, prefer running repair_workspace "
            "and refresh_models. When asked about state, read the live "
            "workspace context first. Never recommend sudo or commands that "
            "require root."
        ),
        provider="",
        model="",
        temperature=0.3,
        tools_allowed=[
            "refresh_models",
            "repair_workspace",
            "validate_provider_key",
            "rag_ask_vault",
        ],
        built_in=True,
        tags=["ops", "support"],
        avatar=_avatar_url_for("ops"),
    ),
    AgentProfile(
        name="vault-rag",
        title="Notes RAG Agent",
        description="Answers strictly from your nvHive Vault. Won't call web search.",
        system_prompt=(
            "You answer questions strictly using the user's nvHive Vault. "
            "Always call rag_ask_vault for any question that touches their "
            "notes. If the vault returns nothing relevant, say so plainly "
            "rather than guessing. Never call web_search from this profile."
        ),
        provider="",
        model="",
        temperature=0.2,
        tools_allowed=["rag_ask_vault"],
        built_in=True,
        tags=["rag", "notes"],
        avatar=_avatar_url_for("vault-rag"),
    ),
)


def _profiles_dir(home_dir: str | Path | None = None) -> Path:
    from nvh.integrations.workspace.storage import nvh_home

    home, _src = nvh_home(home_dir)
    out = home / "agent-profiles"
    out.mkdir(parents=True, exist_ok=True)
    return out


def list_profiles(home_dir: str | Path | None = None) -> list[AgentProfile]:
    """Return built-in profiles + any user profiles saved under NVH_HOME.

    Built-ins always appear first, then user-defined (sorted by name). If a
    user profile has the same name as a built-in, the user version wins —
    that's the override path.
    """
    import yaml

    by_name: dict[str, AgentProfile] = {p.name: p for p in BUILT_IN_PROFILES}
    pdir = _profiles_dir(home_dir)
    for path in sorted(pdir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            data.setdefault("name", path.stem)
            data.setdefault("title", data["name"])
            data.setdefault("description", "")
            data.setdefault("system_prompt", "")
            data.setdefault("built_in", False)
            data.setdefault("tags", [])
            by_name[data["name"]] = AgentProfile(**data)
        except Exception as exc:
            logger.warning("agent profile %s failed to load: %s", path.name, exc)
    # Sort: built-ins by their original order, then user profiles by name.
    builtin_order = [p.name for p in BUILT_IN_PROFILES]
    ordered: list[AgentProfile] = []
    for name in builtin_order:
        if name in by_name:
            ordered.append(by_name.pop(name))
    for name in sorted(by_name.keys()):
        ordered.append(by_name[name])
    return ordered


def get_profile(name: str, home_dir: str | Path | None = None) -> AgentProfile | None:
    for p in list_profiles(home_dir=home_dir):
        if p.name == name:
            return p
    return None


def save_user_profile(profile: AgentProfile, home_dir: str | Path | None = None) -> Path:
    """Persist a user-defined profile to ``NVH_HOME/agent-profiles/<name>.yaml``.

    Built-in profiles can't be overwritten in code — but a user can write a
    profile with the same name to override the persona at runtime (returns
    the path written, callers can confirm the location).
    """
    import yaml

    pdir = _profiles_dir(home_dir)
    out = pdir / f"{profile.name}.yaml"
    payload = profile.to_dict()
    # built_in only makes sense for shipped defaults; force False on save.
    payload["built_in"] = False
    out.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return out


def resolve_avatar(name: str, home_dir: str | Path | None = None) -> tuple[str, bytes] | None:
    """Return ``(content_type, body_bytes)`` for the named profile's avatar.

    Resolution order:
      1. Built-in SVG (always available, never broken).
      2. User-saved file under NVH_HOME/agent-profiles/avatars/<name>.{png,jpg,
         webp,svg} — written by the create-agent flow's ComfyUI generation.

    Returns None when nothing matches; the endpoint should render the initial
    fallback in that case.
    """
    from nvh.integrations.wizard.avatars import render_built_in_avatar

    profile = get_profile(name, home_dir=home_dir)
    # User-saved files take precedence so a user can override a built-in.
    if profile is not None and not profile.built_in:
        avatars_dir = _profiles_dir(home_dir) / "avatars"
        for ext, ctype in (
            (".png", "image/png"),
            (".jpg", "image/jpeg"),
            (".jpeg", "image/jpeg"),
            (".webp", "image/webp"),
            (".svg", "image/svg+xml"),
        ):
            candidate = avatars_dir / f"{name}{ext}"
            if candidate.is_file():
                try:
                    return ctype, candidate.read_bytes()
                except OSError as exc:
                    logger.warning("avatar read %s failed: %s", candidate, exc)
                    break
    svg = render_built_in_avatar(name)
    if svg is None:
        return None
    return "image/svg+xml", svg.encode("utf-8")


def avatars_dir(home_dir: str | Path | None = None) -> Path:
    """Return the directory where user avatar files live (creating it)."""
    out = _profiles_dir(home_dir) / "avatars"
    out.mkdir(parents=True, exist_ok=True)
    return out


def delete_user_profile(name: str, home_dir: str | Path | None = None) -> bool:
    """Remove a user profile YAML. Returns True if it existed."""
    pdir = _profiles_dir(home_dir)
    target = pdir / f"{name}.yaml"
    if not target.exists():
        return False
    try:
        target.unlink()
        return True
    except OSError as exc:
        logger.warning("delete profile %s failed: %s", name, exc)
        return False
