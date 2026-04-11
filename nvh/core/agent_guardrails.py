"""Agent guardrails — prevent the coding agent from causing damage.

This module enforces safety boundaries on every tool call the agent
makes, regardless of whether the user passed --yes. The LLM does not
get a vote on these rules.

Three layers of protection:

1. **Command blocklist** — shell commands that match destructive
   patterns are rejected before execution. No `rm -rf /`, no
   `format C:`, no `git push --force`.

2. **Filesystem boundary** — write_file and shell commands are
   restricted to the workspace directory. The agent cannot write
   to /etc, C:\\Windows, or any path outside the project root.

3. **Secrets filter** — blocks tool results from leaking API keys,
   tokens, or passwords into the LLM context. Detected secrets
   are replaced with [REDACTED].

Usage:
    from nvh.core.agent_guardrails import GuardrailError, check_command, check_path, redact_secrets

    check_command("rm -rf /tmp/build")     # OK
    check_command("rm -rf /")              # raises GuardrailError
    check_path("/etc/passwd", workspace)   # raises GuardrailError
    clean = redact_secrets(tool_output)    # replaces sk-... with [REDACTED]
"""

from __future__ import annotations

import os
import re
from pathlib import Path


class GuardrailError(Exception):
    """Raised when a tool call violates a safety guardrail.

    This is NOT catchable by the agent — it terminates the tool call
    and the error message is shown to the user, not fed back to the
    LLM for retry.
    """


# ---------------------------------------------------------------------------
# Layer 1: Destructive command blocklist
# ---------------------------------------------------------------------------

# Patterns that match destructive shell commands. Each tuple is
# (compiled regex, human-readable description).
_BLOCKED_COMMANDS: list[tuple[re.Pattern, str]] = [
    # Recursive deletion of root or system directories
    (re.compile(r'\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?(-[a-zA-Z]*r[a-zA-Z]*\s+)?[/\\](\s|$)'), "recursive delete of root (/)"),
    (re.compile(r'\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+[/\\](\s|$)'), "recursive delete of root (/)"),
    (re.compile(r'\brm\s+-rf\s+/(?!tmp)(?!dev/null)'), "recursive force-delete outside /tmp"),
    (re.compile(r'\bdel\s+/[sS]\s+/[qQ]\s+[A-Za-z]:\\', re.IGNORECASE), "recursive delete of Windows drive"),
    (re.compile(r'\brmdir\s+/[sS]\s+/[qQ]\s+[A-Za-z]:\\', re.IGNORECASE), "recursive rmdir of Windows drive"),
    (re.compile(r'\bformat\s+[A-Za-z]:', re.IGNORECASE), "format drive"),

    # System file modification
    (re.compile(r'>\s*/etc/'), "write redirect to /etc"),
    (re.compile(r'>\s*[A-Za-z]:\\Windows\\', re.IGNORECASE), "write redirect to Windows system dir"),
    (re.compile(r'\bchmod\s+[0-7]*777\s+/'), "chmod 777 on root paths"),
    (re.compile(r'\bchown\s.*\s+/'), "chown on root paths"),

    # Git destructive operations
    (re.compile(r'\bgit\s+push\s+.*--force'), "git push --force"),
    (re.compile(r'\bgit\s+push\s+.*-f\b'), "git push -f"),
    (re.compile(r'\bgit\s+reset\s+--hard'), "git reset --hard"),
    (re.compile(r'\bgit\s+clean\s+-[a-zA-Z]*f'), "git clean -f"),

    # Process killing
    (re.compile(r'\bkill\s+-9\s+1\b'), "kill init process"),
    (re.compile(r'\btaskkill\s+.*\\explorer\.exe', re.IGNORECASE), "kill explorer.exe"),
    (re.compile(r'\btaskkill\s+.*\\svchost', re.IGNORECASE), "kill svchost"),
    (re.compile(r'\bshutdown\b'), "system shutdown"),
    (re.compile(r'\breboot\b'), "system reboot"),

    # Network exfiltration patterns
    (re.compile(r'\bcurl\b.*\|\s*\b(bash|sh|python|powershell)\b'), "pipe from network to shell"),
    (re.compile(r'\bwget\b.*\|\s*\b(bash|sh|python|powershell)\b'), "pipe from network to shell"),

    # Package installation (typosquatting risk)
    (re.compile(r'\bpip\s+install\s+(?!-e\s)(?!--upgrade\s)(?!-r\s)'), "pip install (use --confirm-install flag)"),
    (re.compile(r'\bnpm\s+install\s+(?!--save-dev)(?!-D)'), "npm install (use --confirm-install flag)"),

    # Disk fill
    (re.compile(r'\bdd\s+if=/dev/zero'), "dd from /dev/zero (disk fill)"),
    (re.compile(r'\byes\s*\|'), "yes pipe (infinite output)"),
    (re.compile(r'\b:bomb:\b|\bfork\s*bomb\b', re.IGNORECASE), "fork bomb reference"),
]


def check_command(command: str) -> None:
    """Validate a shell command against the blocklist.

    Raises GuardrailError if the command matches a destructive pattern.
    This check runs BEFORE the command is executed and cannot be
    bypassed by --yes or auto_approve_safe.
    """
    for pattern, description in _BLOCKED_COMMANDS:
        if pattern.search(command):
            raise GuardrailError(
                f"BLOCKED: {description}\n"
                f"Command: {command[:200]}\n"
                f"The agent attempted a destructive operation that guardrails "
                f"prevent regardless of --yes. If you need to run this command "
                f"manually, do so outside of `nvh agent`."
            )


# ---------------------------------------------------------------------------
# Layer 2: Filesystem boundary enforcement
# ---------------------------------------------------------------------------

# Directories that are NEVER writable, even inside the workspace
_SYSTEM_DIRS = {
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc",
    "/var/log", "/var/run", "/root",
}
_WINDOWS_SYSTEM_DIRS = {
    "windows", "program files", "program files (x86)",
    "programdata", "system32", "syswow64",
}


def check_path(path: str, workspace: Path) -> None:
    """Validate that a file path is inside the workspace.

    Raises GuardrailError if the path escapes the workspace boundary
    or targets a system directory.
    """
    try:
        resolved = Path(path).resolve()
        workspace_resolved = workspace.resolve()
    except (OSError, ValueError) as e:
        raise GuardrailError(f"Invalid path: {path} ({e})") from e

    # Check: is the path inside the workspace?
    try:
        resolved.relative_to(workspace_resolved)
    except ValueError:
        raise GuardrailError(
            f"BLOCKED: path outside workspace\n"
            f"Path: {resolved}\n"
            f"Workspace: {workspace_resolved}\n"
            f"The agent can only read/write files inside the working directory."
        )

    # Check: does the path target a system directory?
    path_str = str(resolved).lower()

    for sys_dir in _SYSTEM_DIRS:
        if path_str.startswith(sys_dir):
            raise GuardrailError(
                f"BLOCKED: system directory\n"
                f"Path: {resolved}\n"
                f"Writing to {sys_dir} is never allowed."
            )

    for win_dir in _WINDOWS_SYSTEM_DIRS:
        if win_dir in path_str.split(os.sep):
            raise GuardrailError(
                f"BLOCKED: Windows system directory\n"
                f"Path: {resolved}\n"
                f"Writing to {win_dir} is never allowed."
            )


# ---------------------------------------------------------------------------
# Layer 3: Secrets redaction
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # API keys and tokens
    (re.compile(r'(?:sk|pk|api[_-]?key)[_-][a-zA-Z0-9]{20,}'), "[REDACTED:api_key]"),
    (re.compile(r'ghp_[a-zA-Z0-9]{36,}'), "[REDACTED:github_pat]"),
    (re.compile(r'gho_[a-zA-Z0-9]{36,}'), "[REDACTED:github_oauth]"),
    (re.compile(r'pypi-[a-zA-Z0-9]{50,}'), "[REDACTED:pypi_token]"),
    (re.compile(r'(?:Bearer|token)\s+[a-zA-Z0-9._\-]{20,}', re.IGNORECASE), "[REDACTED:bearer_token]"),

    # AWS
    (re.compile(r'AKIA[A-Z0-9]{16}'), "[REDACTED:aws_key]"),
    (re.compile(r'(?:aws_secret|AWS_SECRET)[_A-Z]*\s*=\s*\S{20,}'), "[REDACTED:aws_secret]"),

    # Private keys
    (re.compile(r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----'), "[REDACTED:private_key]"),
    (re.compile(r'-----BEGIN\s+OPENSSH\s+PRIVATE\s+KEY-----'), "[REDACTED:ssh_key]"),

    # Generic secrets in env files
    (re.compile(r'(?:PASSWORD|SECRET|TOKEN|API_KEY)\s*=\s*\S{8,}', re.IGNORECASE), "[REDACTED:env_secret]"),

    # Connection strings
    (re.compile(r'(?:postgres|mysql|mongodb|redis)://\S+:\S+@'), "[REDACTED:connection_string]"),
]

# Files that should never be read by the agent
BLOCKED_FILES = {
    ".env", ".env.local", ".env.production", ".env.staging",
    "credentials.json", "service-account.json",
    "id_rsa", "id_ed25519", "id_ecdsa",
    ".npmrc",  # may contain auth tokens
    ".pypirc",  # PyPI tokens
    "keyring",
}


def redact_secrets(text: str) -> str:
    """Replace detected secrets in text with [REDACTED] markers.

    Applied to tool results before they're fed back to the LLM so
    API keys and passwords don't end up in the model's context
    (and potentially in generated code or logs).
    """
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def check_file_read(filename: str) -> None:
    """Block reads of known-sensitive files.

    Raises GuardrailError if the file basename matches a blocked
    pattern. Applied before read_file tool execution.
    """
    basename = Path(filename).name.lower()
    for blocked in BLOCKED_FILES:
        if basename == blocked.lower():
            raise GuardrailError(
                f"BLOCKED: sensitive file\n"
                f"File: {filename}\n"
                f"Reading {blocked} is blocked because it may contain "
                f"secrets that would leak into the LLM context. If you "
                f"need to read this file, do so manually outside the agent."
            )


# ---------------------------------------------------------------------------
# Layer 4: Resource limits
# ---------------------------------------------------------------------------

# Maximum file size the agent can write (10 MB)
MAX_WRITE_SIZE = 10 * 1024 * 1024

# Maximum number of files the agent can create in one session
MAX_FILES_CREATED = 50

# Maximum shell command output (1 MB) — prevents the LLM context
# from being blown up by a command that dumps a huge log
MAX_COMMAND_OUTPUT = 1 * 1024 * 1024


def check_write_size(content: str, path: str) -> None:
    """Block writes that exceed the size limit."""
    size = len(content.encode("utf-8", errors="replace"))
    if size > MAX_WRITE_SIZE:
        raise GuardrailError(
            f"BLOCKED: file too large\n"
            f"Path: {path}\n"
            f"Size: {size:,} bytes (limit: {MAX_WRITE_SIZE:,})\n"
            f"The agent cannot write files larger than 10 MB."
        )


def truncate_output(output: str) -> str:
    """Truncate command output to prevent context blowup."""
    if len(output) > MAX_COMMAND_OUTPUT:
        return (
            output[:MAX_COMMAND_OUTPUT]
            + f"\n\n[TRUNCATED — output exceeded {MAX_COMMAND_OUTPUT:,} byte limit]"
        )
    return output
