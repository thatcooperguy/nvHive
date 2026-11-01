"""One provider table: display names, signup/docs URLs and key variables live in nvh/providers/specs.py only.

The guard parses every Python file under ``nvh/`` and ``scripts/`` (the spec
table itself excepted) plus every page under ``web/app`` and ``web/lib``, and
fails when

* a *site* -- one top-level assignment, or one function and the assignment
  inside it -- types facts for two or more providers (a display name, a
  signup / billing / docs / ``GET /models`` URL, a key variable, also as a
  ``${VAR}`` reference inside a template string);
* a dict literal keyed by three or more provider names carries such a fact
  or a spec's default / fallback model id (a provider table);
* a string constant lays out a YAML block keyed by three or more provider
  names (the shape the hand-typed ``advisors:`` template had).

Reading one provider's variable (``os.environ.get("OPENAI_API_KEY")``) is
using the fact, not copying it, and a site about a single provider is
feature code, so neither counts. Generated docs and tests are not scanned.
The allowlist below names the two tables that legitimately key on providers;
an entry that stops matching fails too, so it cannot go stale.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import pytest

from nvh.providers.specs import PROVIDER_FACTS

ROOT = Path(__file__).resolve().parents[1]
SPEC_FILE = "nvh/providers/specs.py"
PY_ROOTS = ("nvh", "scripts")
# Every page and layout under web/app plus web/lib's modules (tests, .mjs, are not scanned).
WEB_FILES = tuple(sorted(
    p.relative_to(ROOT).as_posix()
    for pattern in ("web/app/**/*.tsx", "web/lib/*.ts")
    for p in ROOT.glob(pattern)
))

# (path, site, kind) -> the exact providers the site may name, or None for any.
ALLOWED: dict[tuple[str, str, str], frozenset[str] | None] = {
    # old model id -> new id per provider; the targets are today's defaults by
    # construction and tests/test_cli_config_migrate.py pins them.
    ("nvh/cli/setup.py", "RETIRED_MODEL_RENAMES", "provider-table"): None,
    # council default_weights names the three providers it weights.
    ("nvh/config/settings.py", "generate_default_config().template", "yaml-table"): frozenset({"anthropic", "google", "openai"}),
}

PROVIDER_NAMES = frozenset(PROVIDER_FACTS)
ENV_READERS = frozenset({"get", "getenv", "pop", "setdefault"})


def _norm_url(url: str) -> str:
    return url.rstrip("/").lower()


def _facts() -> tuple[dict[str, str], dict[str, str], dict[str, str], frozenset[str]]:
    """``(exact -> provider, url_prefix -> provider, env_name -> provider, model ids)`` from the spec rows."""
    exact: dict[str, str] = {}
    urls: dict[str, str] = {}
    envs: dict[str, str] = {}
    models: set[str] = set()
    for name, spec in PROVIDER_FACTS.items():
        if spec.display_name != spec.name:
            exact[spec.display_name] = name
        for url in (spec.signup_url, spec.billing_url, spec.docs_url, spec.models_probe[0] if spec.models_probe else ""):
            if url:
                urls[_norm_url(url)] = name
        for env in (spec.key_env, *spec.env_keys):
            if env:
                envs[env] = name
        models.update(m for m in (spec.default_model, spec.fallback_model) if m)
    return exact, urls, envs, frozenset(models)


EXACT, URLS, ENVS, MODEL_IDS = _facts()
ENV_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)|\b(" + "|".join(sorted(ENVS, key=len, reverse=True)) + r")\b")
YAML_KEY_RE = re.compile(r"^\s*([a-z0-9_]+):", re.M)


def providers_named(text: str) -> set[str]:
    """The providers whose facts ``text`` carries (exact display name, URL prefix, env name)."""
    found: set[str] = set()
    if text in EXACT:
        found.add(EXACT[text])
    if text.startswith(("http://", "https://")):
        norm = _norm_url(text)
        for url, name in URLS.items():
            if norm == url or norm.startswith(url + "/"):
                found.add(name)
    for match in ENV_RE.finditer(text):
        env = match.group(1) or match.group(2)
        if env in ENVS:
            found.add(ENVS[env])
    return found


def yaml_table_keys(text: str) -> frozenset[str]:
    return frozenset(key for key in YAML_KEY_RE.findall(text) if key in PROVIDER_NAMES)


# --- Python ------------------------------------------------------------------


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _site(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """``func().target`` / ``func()`` / ``TARGET`` / ``Class`` / ``<module>``: the statement that owns ``node``."""
    target = func = ""
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Assign) and current.targets and isinstance(current.targets[0], ast.Name) and not target:
            target = current.targets[0].id
        elif isinstance(current, ast.AnnAssign) and isinstance(current.target, ast.Name) and not target:
            target = current.target.id
        elif isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = current.name + "()"
            break
        elif isinstance(current, ast.ClassDef) and not target:
            target = current.name
    return ".".join(part for part in (func, target) if part) or "<module>"


def _is_env_read(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    if isinstance(parent, ast.Subscript):
        return isinstance(parent.value, ast.Attribute) and parent.value.attr == "environ"
    if isinstance(parent, ast.Call) and node in parent.args and isinstance(parent.func, ast.Attribute):
        if parent.func.attr in ENV_READERS and isinstance(parent.func.value, (ast.Name, ast.Attribute)):
            owner = parent.func.value
            owner_name = owner.id if isinstance(owner, ast.Name) else owner.attr
            return owner_name in {"environ", "os"}
    return False


def _is_docstring(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    expr = parents.get(node)
    owner = parents.get(expr) if isinstance(expr, ast.Expr) else None
    return isinstance(owner, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and owner.body[0] is expr


def _dict_strings(node: ast.Dict) -> Iterable[str]:
    for value in node.values:
        if value is None:
            continue
        for sub in ast.walk(value):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                yield sub.value


def scan_python(path: Path) -> dict[tuple[str, str], set[str]]:
    """``{(site, kind): providers}`` for every finding in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = _parents(tree)
    findings: dict[tuple[str, str], set[str]] = defaultdict(set)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _is_env_read(node, parents) or _is_docstring(node, parents):
                continue
            site = _site(node, parents)
            findings[(site, "facts")] |= providers_named(node.value)
            keys = yaml_table_keys(node.value)
            if len(keys) >= 3:
                findings[(site, "yaml-table")] |= keys
        elif isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)} & PROVIDER_NAMES
            if len(keys) >= 3 and any(providers_named(s) or s in MODEL_IDS for s in _dict_strings(node)):
                findings[(_site(node, parents), "provider-table")] |= keys
    return {key: names for key, names in findings.items() if key[1] != "facts" or len(names) >= 2}


# --- TypeScript (web/app pages and web/lib) ----------------------------------

TS_STRING_RE = re.compile(r"""(['"`])((?:\\.|(?!\1).){3,200}?)\1""")
# The whitespace escapes a literal spells (``'A=1\nB=2'``): decoded before the
# name match so ``\b`` sees a boundary where the page shows a line break.
TS_WHITESPACE_ESCAPE_RE = re.compile(r"\\[ntr]")


def scan_typescript(path: Path) -> dict[tuple[str, str], set[str]]:
    names: set[str] = set()
    for match in TS_STRING_RE.finditer(path.read_text(encoding="utf-8")):
        names |= providers_named(TS_WHITESPACE_ESCAPE_RE.sub(" ", match.group(2)))
    return {("<file>", "facts"): names} if len(names) >= 2 else {}


# --- the guard ----------------------------------------------------------------


def python_files() -> list[Path]:
    files = [p for root in PY_ROOTS for p in sorted((ROOT / root).rglob("*.py"))]
    return [p for p in files if "__pycache__" not in p.parts and p.relative_to(ROOT).as_posix() != SPEC_FILE]


def findings() -> dict[tuple[str, str, str], set[str]]:
    found: dict[tuple[str, str, str], set[str]] = {}
    for path in python_files():
        rel = path.relative_to(ROOT).as_posix()
        for (site, kind), names in scan_python(path).items():
            found[(rel, site, kind)] = names
    for rel in WEB_FILES:
        for (site, kind), names in scan_typescript(ROOT / rel).items():
            found[(rel, site, kind)] = names
    return found


def test_provider_facts_are_typed_once():
    violations = {
        key: sorted(names)
        for key, names in findings().items()
        if key not in ALLOWED or (ALLOWED[key] is not None and names != ALLOWED[key])
    }
    assert not violations, (
        "provider facts typed outside nvh/providers/specs.py (derive them from PROVIDER_FACTS):\n"
        + "\n".join(f"  {path}: {site} [{kind}] -> {names}" for (path, site, kind), names in sorted(violations.items()))
    )


def test_allowlist_entries_still_match():
    found = findings()
    for key, expected in ALLOWED.items():
        assert key in found, f"stale allowlist entry {key}: nothing there any more, remove it"
        if expected is not None:
            assert found[key] == expected, key


# --- the scanner itself ---------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Google Gemini", {"google"}),
        ("https://console.groq.com/keys/", {"groq"}),
        ("https://platform.openai.com/docs/overview", {"openai"}),
        ("https://api.anthropic.com/v1/models", {"anthropic"}),
        ("api_key: ${HUGGINGFACE_API_KEY:-${HF_TOKEN}}", {"huggingface"}),
        ("export XAI_API_KEY=...", {"grok"}),
        ("openai", set()),  # a config name is not a fact
        ("https://example.invalid", set()),
    ],
)
def test_scanner_recognises_spec_facts(text: str, expected: set[str]):
    assert providers_named(text) == expected


def test_scanner_flags_a_hand_typed_table_and_passes_feature_code(tmp_path: Path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        'import os\n'
        'KEYS = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY", "google": "GOOGLE_API_KEY"}\n'
        'TEMPLATE = """\nadvisors:\n  openai:\n    a: 1\n  groq:\n    a: 1\n  google:\n    a: 1\n"""\n',
        encoding="utf-8",
    )
    found = scan_python(bad)
    assert found[("KEYS", "facts")] == {"openai", "groq", "google"}
    assert found[("KEYS", "provider-table")] == {"openai", "groq", "google"}
    assert found[("TEMPLATE", "yaml-table")] == {"openai", "groq", "google"}

    good = tmp_path / "good.py"
    good.write_text(
        '"""Docs may mention OPENAI_API_KEY and GROQ_API_KEY."""\n'
        'import os\n'
        'def f():\n'
        '    """Reads GROQ_API_KEY and OPENAI_API_KEY."""\n'
        '    return os.environ.get("OPENAI_API_KEY") or os.environ.get("GROQ_API_KEY")\n'
        'ADAPTERS = {"ollama": ("mod", "Cls"), "triton": ("mod", "Cls"), "mock": ("mod", "Cls")}\n'
        'URL = "https://console.groq.com/keys"\n',
        encoding="utf-8",
    )
    assert scan_python(good) == {}


def test_scanner_covers_every_web_page_and_lib():
    assert "web/app/setup/page.tsx" in WEB_FILES and "web/app/providers/page.tsx" in WEB_FILES
    assert "web/lib/api.ts" in WEB_FILES and "web/lib/types.ts" in WEB_FILES
    assert WEB_FILES == tuple(sorted(WEB_FILES)) and all(rel.startswith(("web/app/", "web/lib/")) for rel in WEB_FILES)
    # No web file needs an exemption: the providers page's .env example used
    # to type three key variables and now derives them from the fetched rows.
    for rel in WEB_FILES:
        assert scan_typescript(ROOT / rel) == {}, rel


def test_typescript_scanner_sees_names_across_escaped_line_breaks(tmp_path: Path):
    page = tmp_path / "page.tsx"
    page.write_text("const hint = 'OPENAI_API_KEY=sk-...\\nGROQ_API_KEY=gsk_...';\n", encoding="utf-8")
    assert scan_typescript(page) == {("<file>", "facts"): {"openai", "groq"}}
    page.write_text("const hint = rows.map(p => `${p.env_key}=${p.placeholder ?? ''}`).join('\\n');\n", encoding="utf-8")
    assert scan_typescript(page) == {}
