"""No file under nvh/ names a local model tag the registry or the tier table retired.

Six ladders used to carry their own tag lists and drifted apart;
``nvh/core/local_models.py`` is now the single source and this guard keeps
stale tags from creeping back in *as values*: Python string constants
(docstrings and comments are not values), JSON values, YAML keys and values,
and plain text files. Exclusions are explicit and small, and each one names
its reason; a second test asserts every exclusion still hits, so the lists
cannot rot once a file is fixed.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import yaml

import nvh.core.local_models as lm

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "nvh"
SCANNED_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".md", ".txt", ".toml", ".sh"}

# Substrings that only ever occur inside retired tags. ``qwen2.5:`` keeps its
# colon so it names the chat family (``qwen2.5:7b``) while ``qwen2.5-coder`` is
# listed on its own; ``nemotron-mini`` is the retired 4B, not ``nemotron3``.
RETIRED_SUBSTRINGS = (
    "nemotron-omni", "nemotron-3-nano-omni", "nemotron:70b", "nemotron-3-super", "nemotron-mini",
    "qwen2.5-coder", "qwen2.5:", "minicpm-v", "llava", "bakllava", "llama3.3:70b", "deepseek-r1:8b",
    "codellama", "gemma2", "phi4", "mistral:7b", "llama3.2:3b",
)
# Whole values that are retired: the untagged Nemotron 70B as a model id, which
# a substring test could not tell apart from ``nemotron3:33b``. A bare
# ``nemotron`` is a family name (the concierge recognises it in user text) and
# is deliberately not matched.
RETIRED_EXACT = frozenset({"ollama/nemotron", "ollama/nemotron:latest"})

# Values allowed to carry a retired name, per file. Recognisers of what a user
# already has installed or typed must keep knowing the old names; a measurement
# is not a pick.
ALLOWED: dict[str, set[str]] = {
    # The vision tool's legacy-recognised set: an installed llava still answers.
    "nvh/core/vision_tools.py": {"minicpm-v", "llava", "bakllava"},
    # Model-family regex over the *user's* text ("is codellama any good?").
    "nvh/integrations/wizard/concierge.py": {"codellama"},
    # GB10 measured baselines (docs/MODELS.md: measurements, not picks).
    "nvh/utils/gpu_emulation.py": {"nemotron-mini", "nemotron-3-super"},
}

# Files another change owns that still carry a retired tag as a value. Each
# entry must still hit (``test_exclusions_are_still_needed``), so it has to be
# removed the moment the file is fixed; nothing joins this table without a
# reason. Fixing them is the follow-up, not widening this list. (The `nvh
# nvidia` pull hint and the blueprint's ``fast-local`` route left it when they
# started reading the table -- see the two tests at the bottom.)
KNOWN_STALE: dict[str, set[str]] = {
    # Vault note templates (built-in profiles, the Omni install story, the
    # per-VRAM vision ladder) still describe the pre-table world.
    "nvh/integrations/workspace/vault.py": {
        "qwen2.5-coder", "nemotron-omni", "nemotron-3-nano-omni", "minicpm-v",
    },
}


def retired_names_in(value: str) -> set[str]:
    """Every retired name ``value`` carries (a template can hold several). URLs are slugs, not tags."""
    if value.startswith(("http://", "https://")):
        return set()
    names = {name for name in RETIRED_SUBSTRINGS if name in value}
    if value in RETIRED_EXACT:
        names.add("ollama/nemotron")
    return names


def _python_values(path: Path) -> Iterator[tuple[int, str]]:
    """Every string constant in a module except docstrings (comments never parse)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def _structured_values(data: object, *, keys: bool) -> Iterator[str]:
    if isinstance(data, dict):
        for key, value in data.items():
            if keys and isinstance(key, str):
                yield key
            yield from _structured_values(value, keys=keys)
    elif isinstance(data, list):
        for item in data:
            yield from _structured_values(item, keys=keys)
    elif isinstance(data, str):
        yield data


def _line_of(text: str, value: str) -> int:
    needle = value.splitlines()[0] if value.strip() else value
    for lineno, line in enumerate(text.splitlines(), 1):
        if needle and needle in line:
            return lineno
    return 0


def _values(path: Path) -> Iterator[tuple[int, str]]:
    if path.suffix == ".py":
        yield from _python_values(path)
        return
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        for value in _structured_values(json.loads(text), keys=False):
            yield _line_of(text, value), value
    elif path.suffix in {".yaml", ".yml"}:
        # Keys count too: capabilities.yaml rows are keyed by model id.
        for value in _structured_values(yaml.safe_load(text), keys=True):
            yield _line_of(text, value), value
    else:
        yield from enumerate(text.splitlines(), 1)


def scan() -> dict[str, dict[str, list[tuple[int, str]]]]:
    """``{relative path: {retired name: [(line, value), ...]}}`` for every hit under nvh/."""
    hits: dict[str, dict[str, list[tuple[int, str]]]] = {}
    for path in sorted(PKG.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        for lineno, value in _values(path):
            for name in retired_names_in(value):
                hits.setdefault(rel, {}).setdefault(name, []).append((lineno, value))
    return hits


def test_no_retired_tag_is_a_value_under_nvh() -> None:
    offenders = []
    for rel, by_name in sorted(scan().items()):
        allowed = ALLOWED.get(rel, set()) | KNOWN_STALE.get(rel, set())
        for name, sites in sorted(by_name.items()):
            if name in allowed:
                continue
            offenders += [
                f"{rel}:{lineno}: {value[:80]!r} names retired {name!r}" for lineno, value in sites
            ]
    assert not offenders, (
        "Retired local-model tags as values under nvh/ -- derive the tag from "
        "nvh.core.local_models instead:\n  " + "\n  ".join(offenders)
    )


def test_exclusions_are_still_needed() -> None:
    """Every allowance and every known-stale entry must still hit, or it is deleted."""
    hits = scan()
    stale = [
        f"{rel} no longer carries {name!r}: remove it from {table_name}"
        for table_name, table in (("ALLOWED", ALLOWED), ("KNOWN_STALE", KNOWN_STALE))
        for rel, names in table.items()
        for name in sorted(names)
        if name not in hits.get(rel, {})
    ]
    assert not stale, "\n".join(stale)


def test_the_tier_table_itself_names_no_retired_tag() -> None:
    for tag in lm.all_tags():
        assert not retired_names_in(tag), tag
        assert not retired_names_in(f"ollama/{tag}"), tag
    # The matcher tells the retired 70B id from the live Nemotron 3 tags and the
    # family name, reports every name a template carries, and skips URL slugs.
    assert retired_names_in("ollama/nemotron") == {"ollama/nemotron"}
    assert not retired_names_in("nemotron3:33b") and not retired_names_in("nemotron")
    assert retired_names_in("`nemotron-omni` or `nemotron-3-nano-omni`") == {"nemotron-omni", "nemotron-3-nano-omni"}
    assert retired_names_in("bakllava") == {"llava", "bakllava"}
    assert not retired_names_in("https://blogs.nvidia.com/blog/nemotron-3-nano-omni-multimodal-ai-agents/")


def test_capabilities_ollama_rows_are_exactly_the_table_picks() -> None:
    """One ``ollama/<tag>`` row per table pick, no more (phantom tags) and no fewer."""
    catalog = yaml.safe_load((PKG / "config" / "capabilities.yaml").read_text(encoding="utf-8"))
    rows = {key for key in catalog["models"] if key.startswith("ollama/")}
    assert rows == {f"ollama/{tag}" for tag in lm.all_tags()}
    for key in sorted(rows):
        info = catalog["models"][key]
        pick = lm.pick_for_tag(key.removeprefix("ollama/"))
        assert pick is not None, key
        assert info["provider"] == "ollama", key
        assert info["input_cost_per_1m_tokens"] == 0 and info["output_cost_per_1m_tokens"] == 0, key
        assert info["supports_vision"] is pick.vision, key
        assert info["display_name"].strip() and info["context_window"] > 0, key
        assert set(info["capability_scores"]) >= {"code_generation", "reasoning", "conversation"}, key


def test_generated_default_config_names_a_table_tag() -> None:
    """``hive config init`` fills the Ollama default from the table, never a literal."""
    from nvh.config.settings import _DEFAULT_LOCAL_BUDGET_GB, generate_default_config

    model = yaml.safe_load(generate_default_config())["advisors"]["ollama"]["default_model"]
    assert model == f"ollama/{lm.pick(_DEFAULT_LOCAL_BUDGET_GB, 'chat').tag}"
    assert not retired_names_in(model)
    assert "@OLLAMA_DEFAULT_MODEL@" not in generate_default_config()


def test_nvidia_hint_derives_its_pull_tag_from_the_table(monkeypatch) -> None:
    """``nvh nvidia``'s "No models installed. Run: ollama pull <tag>" names this machine's tier
    chat pick, the CPU fallback when no GPU is seen, and the CPU tier's pick when detection
    itself blows up -- never a literal (it used to say ``nemotron-mini``)."""
    from types import SimpleNamespace

    import nvh.utils.gpu as gpu
    from nvh.cli import main as cli_main

    monkeypatch.setattr(gpu, "detect_system_memory", lambda: None)
    cpu_tag = lm.pick(0.0, "cpu_fallback").tag

    monkeypatch.setattr(gpu, "detect_gpus", lambda: [])
    assert cli_main._starter_local_model_tag() == cpu_tag

    card = SimpleNamespace(vram_mb=24 * 1024, unified_memory=False, compute_capability=(8, 6))
    monkeypatch.setattr(gpu, "detect_gpus", lambda: [card])
    chat_tag = lm.pick(lm.tier_budget([card], None), "chat").tag
    assert cli_main._starter_local_model_tag() == chat_tag != cpu_tag

    def boom():
        raise RuntimeError("NVML exploded")

    monkeypatch.setattr(gpu, "detect_gpus", boom)
    assert cli_main._starter_local_model_tag() == cpu_tag

    for tag in (cpu_tag, chat_tag):
        assert tag in lm.all_tags() and not retired_names_in(tag)


def test_blueprint_fast_local_route_is_the_tables_small_chat_pick() -> None:
    """The NemoClaw blueprint's direct-Ollama route names the 4-8 GB tier's chat pick (the CPU
    fallback of every larger tier), so it runs on any box and is a tag the registry serves."""
    blueprint = yaml.safe_load((PKG / "config" / "nemoclaw-blueprint.yaml").read_text(encoding="utf-8"))
    model = blueprint["inference"]["profiles"]["fast-local"]["model"]
    assert model == lm.pick(4.0, "chat").tag
    assert model == lm.pick(8.0, "cpu_fallback").tag
    assert not retired_names_in(model)
