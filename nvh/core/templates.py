"""Prompt template system with variable substitution."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Template directory
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path.home() / ".council" / "templates"

# ---------------------------------------------------------------------------
# Built-in templates (created on first use)
# ---------------------------------------------------------------------------

BUILTIN_TEMPLATES: dict[str, str] = {
    "code_review": """\
---
name: code_review
description: Senior code reviewer analyzes code for quality, bugs, and improvements.
required_vars:
  - code
optional_vars:
  file: ""
system: "You are a senior code reviewer with expertise in software quality, security, and best practices."
---
Please review the following code and provide detailed feedback on:
1. Code quality and readability
2. Potential bugs or edge cases
3. Security concerns
4. Performance considerations
5. Suggested improvements

Code to review:
```
{{code}}
```
""",

    "summarize": """\
---
name: summarize
description: Summarize text in various lengths and formats.
required_vars:
  - text
optional_vars:
  length: medium
  format: prose
---
Please summarize the following text.
Length: {{length}} (short = 1-2 sentences, medium = 1 paragraph, long = several paragraphs)
Format: {{format}} (bullets = bullet points, prose = flowing text)

Text to summarize:
{{text}}
""",

    "explain_code": """\
---
name: explain_code
description: Explain code at various complexity levels.
required_vars:
  - code
optional_vars:
  level: intermediate
---
Please explain the following code at a {{level}} level.
(beginner = plain language with analogies, intermediate = technical but accessible, expert = deep implementation details)

Code:
```
{{code}}
```
""",

    "translate": """\
---
name: translate
description: Translate text into a target language.
required_vars:
  - text
  - target_lang
optional_vars: {}
---
Please translate the following text into {{target_lang}}. Preserve the original tone and meaning as closely as possible.

Text to translate:
{{text}}
""",

    "debug": """\
---
name: debug
description: Diagnose and fix errors.
required_vars:
  - error
optional_vars:
  language: ""
---
I'm encountering the following error{{language}} and need help debugging it:

Error:
```
{{error}}
```

Please:
1. Explain what is causing this error
2. Provide a solution or fix
3. Suggest how to prevent this in the future
""",

    "write_tests": """\
---
name: write_tests
description: Generate unit tests for code.
required_vars:
  - code
optional_vars:
  framework: pytest
---
Please write comprehensive unit tests for the following code using {{framework}}.

Include tests for:
- Happy path / normal usage
- Edge cases
- Error conditions

Code to test:
```
{{code}}
```
""",
}


# ---------------------------------------------------------------------------
# Template model
# ---------------------------------------------------------------------------

class Template:
    """A loaded prompt template."""

    def __init__(
        self,
        name: str,
        description: str,
        required_vars: list[str],
        optional_vars: dict[str, Any],
        body: str,
        system: str = "",
        path: Path | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.required_vars = required_vars
        self.optional_vars = optional_vars
        self.body = body
        self.system = system
        self.path = path

    def render(self, variables: dict[str, str]) -> tuple[str, str | None]:
        """Render the template with the given variables.

        Returns (rendered_body, system_prompt_or_None).
        Raises ValueError for missing required variables.
        """
        missing = [v for v in self.required_vars if v not in variables]
        if missing:
            raise ValueError(
                f"Template '{self.name}' requires variables: {', '.join(missing)}"
            )

        # Merge optional var defaults with provided values
        merged: dict[str, str] = {}
        for key, default in self.optional_vars.items():
            merged[key] = str(variables.get(key, default))
        for key, val in variables.items():
            merged[key] = str(val)

        body = _substitute(self.body, merged)
        system = _substitute(self.system, merged) if self.system else None
        return body, system


def _substitute(text: str, variables: dict[str, str]) -> str:
    """Replace {{var}} placeholders with values. Unknown placeholders are left as-is."""
    def replacer(m: re.Match) -> str:
        key = m.group(1).strip()
        return variables.get(key, m.group(0))

    return re.sub(r"\{\{([^}]+)\}\}", replacer, text)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_template(content: str, path: Path | None = None) -> Template:
    """Parse a template file with YAML frontmatter and body text."""
    if content.startswith("---"):
        # Split on second '---'
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter_raw = parts[1].strip()
            body = parts[2].lstrip("\n")
        else:
            frontmatter_raw = ""
            body = content
    else:
        frontmatter_raw = ""
        body = content

    meta: dict[str, Any] = yaml.safe_load(frontmatter_raw) or {} if frontmatter_raw else {}

    name = meta.get("name", path.stem if path else "unknown")
    description = meta.get("description", "")
    required_vars = meta.get("required_vars", [])
    optional_vars_raw = meta.get("optional_vars", {})
    system = meta.get("system", "")

    # optional_vars may be a list (no defaults) or a dict (with defaults)
    if isinstance(optional_vars_raw, list):
        optional_vars: dict[str, Any] = {k: "" for k in optional_vars_raw}
    elif isinstance(optional_vars_raw, dict):
        optional_vars = optional_vars_raw
    else:
        optional_vars = {}

    return Template(
        name=name,
        description=description,
        required_vars=required_vars,
        optional_vars=optional_vars,
        body=body,
        system=system,
        path=path,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _ensure_templates_dir() -> Path:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    return TEMPLATES_DIR


def get_builtin_templates() -> dict[str, str]:
    """Return raw built-in template content keyed by name."""
    return dict(BUILTIN_TEMPLATES)


def _install_builtins() -> None:
    """Write built-in templates to disk if they don't exist yet."""
    _ensure_templates_dir()
    for name, content in BUILTIN_TEMPLATES.items():
        path = TEMPLATES_DIR / f"{name}.yaml"
        if not path.exists():
            path.write_text(content)


def load_template(name: str) -> Template:
    """Load a template by name from disk (or built-in fallback).

    Raises FileNotFoundError if the template does not exist.
    """
    _install_builtins()
    path = TEMPLATES_DIR / f"{name}.yaml"
    if not path.exists():
        # Try .txt extension as well
        path_txt = TEMPLATES_DIR / f"{name}.txt"
        if path_txt.exists():
            path = path_txt
        else:
            raise FileNotFoundError(
                f"Template '{name}' not found. "
                f"Run `council template list` to see available templates."
            )
    content = path.read_text()
    return _parse_template(content, path=path)


def render_template(name: str, variables: dict[str, str]) -> tuple[str, str | None]:
    """Load and render a template.

    Returns (rendered_prompt, system_prompt_or_None).
    """
    template = load_template(name)
    return template.render(variables)


def list_templates() -> list[Template]:
    """List all available templates (installed + any user-created ones)."""
    _install_builtins()
    templates = []
    for path in sorted(TEMPLATES_DIR.glob("*.yaml")) + sorted(TEMPLATES_DIR.glob("*.txt")):
        try:
            content = path.read_text()
            t = _parse_template(content, path=path)
            templates.append(t)
        except Exception:
            pass
    return templates
