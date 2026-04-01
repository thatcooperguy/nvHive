"""HIVE.md and context file injection for multi-LLM prompts.

Searches for context files (HIVE.md, .hive/context/*.md) and injects
them into system prompts so all LLMs — local and cloud — share the same
project context, rules, and constraints.

Usage:
    Place a HIVE.md in your project root or ~/HIVE.md for global context.
    The content is automatically prepended to every system prompt.

Supports:
    - HIVE.md — primary context file (like CLAUDE.md but for all LLMs)
    - .hive/context/*.md — additional context files (modular)
    - ~/.hive/global_context.md — global context applied to all projects
    - Frontmatter parsing (optional: name, scope, priority)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ContextFile:
    """A loaded context file with metadata."""
    path: str
    name: str
    content: str
    scope: str = "all"     # "all", "hive", "query", "code", etc.
    priority: int = 0      # higher = injected first
    source: str = ""       # "project", "user", "global"


# File names we search for (in priority order)
CONTEXT_FILE_NAMES = [
    "HIVE.md",
    "hive.md",
    ".hive.md",
]

# Directory for modular context files
CONTEXT_DIR_NAMES = [
    ".hive/context",
    ".hive/rules",
]


def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    """Parse optional YAML-like frontmatter from a markdown file.

    Returns (metadata_dict, body_without_frontmatter).

    Frontmatter format:
    ---
    name: Project Rules
    scope: all
    priority: 10
    ---
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}, content

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()

    body = content[match.end():]
    return meta, body


def find_context_files(
    project_dir: Path | None = None,
    home_dir: Path | None = None,
) -> list[ContextFile]:
    """Find all context files from project directory up to home directory.

    Search order (later files have higher priority):
    1. ~/.hive/global_context.md (global, lowest priority)
    2. ~/HIVE.md (user-level)
    3. Project directory HIVE.md (project-level, highest priority)
    4. Project .hive/context/*.md (modular context files)
    """
    home = home_dir or Path.home()
    files: list[ContextFile] = []

    # 1. Global context
    global_ctx = home / ".hive" / "global_context.md"
    if global_ctx.is_file():
        try:
            content = global_ctx.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(content)
            files.append(ContextFile(
                path=str(global_ctx),
                name=meta.get("name", "Global Context"),
                content=body.strip(),
                scope=meta.get("scope", "all"),
                priority=int(meta.get("priority", "0")),
                source="global",
            ))
        except Exception:
            pass

    # 2. User-level HIVE.md (in home dir)
    for name in CONTEXT_FILE_NAMES:
        user_ctx = home / name
        if user_ctx.is_file():
            try:
                content = user_ctx.read_text(encoding="utf-8")
                meta, body = _parse_frontmatter(content)
                files.append(ContextFile(
                    path=str(user_ctx),
                    name=meta.get("name", "User Context"),
                    content=body.strip(),
                    scope=meta.get("scope", "all"),
                    priority=int(meta.get("priority", "5")),
                    source="user",
                ))
            except Exception:
                pass
            break  # Use first found

    # 3. Project directory search (walk upward from cwd)
    search_dir = project_dir or Path.cwd()
    for _ in range(10):  # limit depth
        for name in CONTEXT_FILE_NAMES:
            project_ctx = search_dir / name
            if project_ctx.is_file():
                try:
                    content = project_ctx.read_text(encoding="utf-8")
                    meta, body = _parse_frontmatter(content)
                    files.append(ContextFile(
                        path=str(project_ctx),
                        name=meta.get("name", "Project Context"),
                        content=body.strip(),
                        scope=meta.get("scope", "all"),
                        priority=int(meta.get("priority", "10")),
                        source="project",
                    ))
                except Exception:
                    pass
                break

        # 4. Modular context files from .hive/context/
        for dir_name in CONTEXT_DIR_NAMES:
            ctx_dir = search_dir / dir_name
            if ctx_dir.is_dir():
                for md_file in sorted(ctx_dir.glob("*.md")):
                    try:
                        content = md_file.read_text(encoding="utf-8")
                        meta, body = _parse_frontmatter(content)
                        files.append(ContextFile(
                            path=str(md_file),
                            name=meta.get("name", md_file.stem),
                            content=body.strip(),
                            scope=meta.get("scope", "all"),
                            priority=int(meta.get("priority", "10")),
                            source="project",
                        ))
                    except Exception:
                        continue

        parent = search_dir.parent
        if parent == search_dir:
            break
        search_dir = parent

    # Sort by priority (higher first)
    files.sort(key=lambda f: f.priority, reverse=True)
    return files


def build_context_prompt(
    context_files: list[ContextFile] | None = None,
    scope: str = "all",
    user_system_prompt: str = "",
) -> str:
    """Build a system prompt by combining context files with the user's system prompt.

    Args:
        context_files: Pre-loaded context files (or auto-discovers if None)
        scope: Filter context files by scope ("all" matches everything)
        user_system_prompt: The user's explicit system prompt (appended last)

    Returns:
        Combined system prompt with all applicable context injected.
    """
    if context_files is None:
        context_files = find_context_files()

    parts: list[str] = []

    for cf in context_files:
        if cf.scope == "all" or cf.scope == scope:
            parts.append(
                f"<context source=\"{cf.source}\" name=\"{cf.name}\">\n"
                f"{cf.content}\n"
                f"</context>"
            )

    if user_system_prompt:
        parts.append(user_system_prompt)

    if not parts:
        return user_system_prompt

    return "\n\n".join(parts)


def get_context_summary(context_files: list[ContextFile] | None = None) -> list[dict]:
    """Return a summary of loaded context files for display."""
    if context_files is None:
        context_files = find_context_files()

    return [
        {
            "name": cf.name,
            "source": cf.source,
            "scope": cf.scope,
            "priority": cf.priority,
            "path": cf.path,
            "size": len(cf.content),
        }
        for cf in context_files
    ]
