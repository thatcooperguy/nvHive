"""Action Detector — determines if a query is an ACTION vs a QUESTION.

When the user types something like "install pandas" or "open firefox",
that's not a question for an LLM — it's a command to execute.

This module detects action intents and maps them to the right tool,
so the AI just DOES the thing without the user needing special commands.

Examples:
  "install pandas"           → pip_install("pandas")
  "open firefox"             → open("firefox")
  "open google.com"          → open("https://google.com")
  "what's using my CPU"      → list_processes(sort_by="cpu")
  "how much disk space"      → disk_usage("~")
  "find large files"         → find_files(min_size="100M")
  "kill python"              → kill_process(name="python") [confirm]
  "delete temp files"        → find + delete workflow [confirm]
  "copy this to clipboard"   → set_clipboard(content)
  "download this file"       → download(url)
  "what Python packages"     → pip_list()
  "show system info"         → system_info()
  "notify me when done"      → notify(title, message)

If no action is detected, returns None and the query goes to the LLM as usual.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DetectedAction:
    """A system action detected from natural language."""
    tool_name: str
    arguments: dict
    confidence: float       # 0-1
    requires_confirm: bool  # needs user approval before executing
    description: str        # human-readable description of what will happen


# Each pattern maps to a tool + argument extraction
_ACTION_PATTERNS: list[tuple[re.Pattern, str, callable]] = []


def _register(pattern: str, tool: str, arg_builder, confirm: bool = False, description: str = ""):
    """Register an action pattern."""
    _ACTION_PATTERNS.append((
        re.compile(pattern, re.IGNORECASE),
        tool,
        arg_builder,
        confirm,
        description,
    ))


# --- Application launching ---
_register(
    r"^open\s+(?:up\s+)?(.+)$",
    "open",
    lambda m: {"target": _normalize_url(m.group(1).strip())},
    description="Open application or URL",
)

_register(
    r"^(?:launch|start|run)\s+(.+)$",
    "open",
    lambda m: {"target": m.group(1).strip()},
    description="Launch application",
)

_register(
    r"^(?:go to|visit|browse)\s+(.+)$",
    "open",
    lambda m: {"target": _normalize_url(m.group(1).strip())},
    description="Open URL in browser",
)

# --- Package installation ---
_register(
    r"^(?:pip\s+)?install\s+(.+)$",
    "pip_install",
    lambda m: {"package": m.group(1).strip()},
    confirm=True,
    description="Install Python package",
)

_register(
    r"^(?:what|list|show)\s+(?:python\s+)?(?:packages|modules|libraries)\s*(?:installed|available)?",
    "pip_list",
    lambda m: {},
    description="List installed Python packages",
)

# --- Process management ---
_register(
    r"^(?:show|list|what)\s*(?:'s|is)?\s*(?:running|processes|using)\s*(?:my\s+)?(?:cpu|memory|ram|gpu)?",
    "list_processes",
    lambda m: {"sort_by": "memory" if "mem" in m.group(0).lower() or "ram" in m.group(0).lower() else "cpu"},
    description="Show running processes",
)

_register(
    r"^kill\s+(?:the\s+)?(?:process\s+)?(.+)$",
    "kill_process",
    lambda m: _parse_kill_target(m.group(1).strip()),
    confirm=True,
    description="Kill a process",
)

# --- File operations ---
_register(
    r"^(?:find|search\s+for|locate)\s+(?:all\s+)?(?:files?\s+)?(?:named?\s+|called\s+|with\s+)?(.+?)(?:\s+files?)?$",
    "find_files",
    lambda m: _parse_find_args(m.group(1).strip()),
    description="Find files",
)

_register(
    r"^(?:how much|check|show)\s+(?:disk\s+)?(?:space|usage|storage)",
    "disk_usage",
    lambda m: {"path": "~"},
    description="Check disk usage",
)

_register(
    r"^(?:find|show|list)\s+(?:the\s+)?(?:biggest|largest|huge)\s+files?",
    "find_files",
    lambda m: {"min_size": "100M", "directory": "~"},
    description="Find large files",
)

_register(
    r"^delete\s+(.+)$",
    "delete_file",
    lambda m: {"path": m.group(1).strip()},
    confirm=True,
    description="Delete file",
)

_register(
    r"^(?:move|rename)\s+(.+?)\s+(?:to|as)\s+(.+)$",
    "move_file",
    lambda m: {"source": m.group(1).strip(), "destination": m.group(2).strip()},
    confirm=True,
    description="Move/rename file",
)

# --- Downloads ---
_register(
    r"^download\s+(.+)$",
    "download",
    lambda m: {"url": _normalize_url(m.group(1).strip())},
    confirm=True,
    description="Download file",
)

# --- System info ---
_register(
    r"^(?:show|what|get)\s+(?:me\s+)?(?:the\s+)?system\s+(?:info|information|details|specs?)",
    "system_info",
    lambda m: {},
    description="Show system information",
)

# --- Clipboard ---
_register(
    r"^(?:copy|put)\s+(?:this|that|it)\s+(?:to|in|into)\s+(?:the\s+)?clipboard",
    "set_clipboard",
    lambda m: {},  # content filled by caller from context
    description="Copy to clipboard",
)

_register(
    r"^(?:what'?s?\s+(?:on|in)\s+(?:my|the)\s+clipboard|paste|show\s+clipboard)",
    "get_clipboard",
    lambda m: {},
    description="Read clipboard",
)

# --- Notifications ---
_register(
    r"^(?:notify|alert|remind)\s+(?:me\s+)?(?:that\s+|when\s+)?(.+)$",
    "notify",
    lambda m: {"title": "NVHive", "message": m.group(1).strip()},
    description="Send notification",
)

# --- Open terminal ---
_register(
    r"^(?:open|new)\s+terminal",
    "open_terminal",
    lambda m: {},
    description="Open new terminal",
)


def _normalize_url(text: str) -> str:
    """Add https:// if the text looks like a URL but has no scheme."""
    text = text.strip().strip('"').strip("'")
    if re.match(r'^[\w.-]+\.\w{2,}', text) and not text.startswith(("http://", "https://")):
        return f"https://{text}"
    return text


def _parse_kill_target(text: str) -> dict:
    """Parse kill target — could be a PID or process name."""
    try:
        pid = int(text)
        return {"pid": pid}
    except ValueError:
        return {"name": text}


def _parse_find_args(text: str) -> dict:
    """Parse find file arguments from natural language."""
    args: dict = {"directory": "~"}

    # Check for extension
    ext_match = re.search(r'\.(\w{1,5})\b', text)
    if ext_match:
        args["extension"] = ext_match.group(1)
        text = text[:ext_match.start()] + text[ext_match.end():]

    # Check for size
    size_match = re.search(r'(?:bigger|larger|over|above)\s+(\d+\s*[kmg]b?)', text, re.I)
    if size_match:
        args["min_size"] = size_match.group(1).strip().upper().replace(" ", "")

    # Check for recency
    days_match = re.search(r'(?:last|past|recent)\s+(\d+)\s+days?', text, re.I)
    if days_match:
        args["max_age_days"] = int(days_match.group(1))

    # Remaining text is the name pattern
    clean = re.sub(r'(?:bigger|larger|over|above)\s+\d+\s*[kmg]b?', '', text, flags=re.I)
    clean = re.sub(r'(?:last|past|recent)\s+\d+\s+days?', '', clean, flags=re.I)
    clean = clean.strip()
    if clean and clean not in ("files", "file", "all"):
        args["name"] = clean

    return args


def detect_action(query: str) -> DetectedAction | None:
    """Detect if a query is a system action rather than a question.

    Returns DetectedAction if an action is detected, None if it's a
    regular question that should go to the LLM.
    """
    query = query.strip()

    # Skip if it's clearly a question (starts with question words)
    question_starters = (
        "what is", "what are", "who is", "when did", "where is",
        "how does", "why does", "can you explain", "tell me about",
        "describe", "compare", "what's the difference",
    )
    query_lower = query.lower()
    if any(query_lower.startswith(q) for q in question_starters):
        return None

    # Skip if it ends with a question mark (probably a question, not an action)
    if query.endswith("?"):
        return None

    # Try each pattern
    for pattern, tool_name, arg_builder, confirm, desc in _ACTION_PATTERNS:
        match = pattern.match(query)
        if match:
            try:
                args = arg_builder(match)
                return DetectedAction(
                    tool_name=tool_name,
                    arguments=args,
                    confidence=0.85,
                    requires_confirm=confirm,
                    description=desc or f"Execute: {tool_name}",
                )
            except Exception:
                continue

    return None
