#!/usr/bin/env python
"""Normalise nvh/config/capabilities.yaml, the hand-maintained half of the model catalog.

    python scripts/gen_capabilities.py          # rewrite the catalog in canonical form
    python scripts/gen_capabilities.py --check  # exit 1 when the committed file is not canonical

The catalog is both this script's input and its output, the way a formatter
treats a source file. Each row keeps exactly the fields capabilities.yaml owns
(``nvh.providers.registry.CATALOG_OWN_FIELDS``: ``provider``, ``display_name``,
``status`` / ``successor``, ``typical_latency_ms``, ``capability_scores``) plus
any explicit override of a LiteLLM-read fact
(``nvh.providers.registry.CATALOG_OVERRIDE_FIELDS``), and a row that carries a
hand-typed override must carry a ``source:`` string saying where the number
comes from. Two things are derived rather than typed, rewritten on every run
and need no ``source:`` of their own (:data:`DERIVED_OVERRIDES`):

* ``status: retired`` and ``successor`` for every row whose id is a
  ``RETIRED_MODEL_RENAMES`` source for its provider (nvh/cli/setup.py);
* ``supports_vision`` on the ``ollama/<tag>`` rows, from the tier table
  (``nvh.core.local_models.pick_for_tag``), whose tags the rows must match
  one-for-one.

A ``ProviderSpec.zero_cost`` provider's rows are priced $0 by the loader from
the spec flag alone, so a price typed on one of them is dead text and is
dropped here rather than written. Price, context window, max output and the
tool / vision / JSON flags of every other row are read from LiteLLM's model
table (plus nvh/config/model_overrides.json) when the catalog loads and are
never written here, so a LiteLLM release cannot fail ``--check``. The header
records the LiteLLM version the file was last written against as
information only; ``--check`` compares the recorded version, not the
installed one.

tests/test_capabilities_catalog.py runs the ``--check`` form.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "nvh" / "config" / "capabilities.yaml"
SCRIPT = "scripts/gen_capabilities.py"

VERSION_LINE = re.compile(r"^# Generated against litellm (\S+)", re.MULTILINE)

#: Override fields the generator derives per provider (rewritten on every run,
#: no ``source:`` needed): the hand-typed overrides of a row are the rest.
DERIVED_OVERRIDES: dict[str, tuple[str, ...]] = {"ollama": ("supports_vision",)}
#: The price fields a ``ProviderSpec.zero_cost`` row never carries (the loader prices it from the flag).
PRICE_FIELDS: tuple[str, ...] = ("input_cost_per_1m_tokens", "output_cost_per_1m_tokens")

# Prose kept above a provider's rows (the rest of the file is derived).
PROVIDER_NOTES: dict[str, tuple[str, ...]] = {
    "ollama": (
        "Exactly the tier table's picks (nvh/core/local_models.py, `nvh models",
        "tiers`): one row per tag in local_models.all_tags(), keyed ollama/<tag>;",
        "supports_vision is rewritten from the table on every run.",
    ),
    "perplexity": (
        "Agent API presets (Responses shape) -- the only Perplexity rows. The Sonar",
        "Chat Completions ids (retired 2026-09-27) are renamed to these by `nvh",
        "config migrate` (nvh/cli/setup.py).",
    ),
}


def _nvh():
    """The nvHive modules the derivations read (imported on first use so ``--help`` stays cheap)."""
    sys.path.insert(0, str(ROOT))
    from nvh.cli.setup import RETIRED_MODEL_RENAMES
    from nvh.core import local_models
    from nvh.providers import registry
    from nvh.providers.base import TaskType
    from nvh.providers.specs import PROVIDER_FACTS

    return RETIRED_MODEL_RENAMES, local_models, registry, TaskType, PROVIDER_FACTS


def installed_litellm_version() -> str:
    try:
        return _dist_version("litellm")
    except PackageNotFoundError:
        return "unknown"


def recorded_litellm_version(text: str) -> str | None:
    """The version the committed file's header names, or ``None`` when it has no header yet."""
    match = VERSION_LINE.search(text)
    return match.group(1) if match else None


# --- normalisation -----------------------------------------------------------


def _score_order(task_type_enum) -> list[str]:
    return [task.value for task in task_type_enum]


def normalise(rows: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The canonical form of every row (see the module docstring); raises SystemExit on a defect."""
    RETIRED_MODEL_RENAMES, local_models, registry, TaskType, PROVIDER_FACTS = _nvh()
    own = set(registry.CATALOG_OWN_FIELDS)
    overridable = list(registry.CATALOG_OVERRIDE_FIELDS)
    score_order = _score_order(TaskType)
    problems: list[str] = []
    out: dict[str, dict[str, Any]] = {}

    for model_id, row in rows.items():
        row = dict(row or {})
        provider = row.get("provider")
        spec = PROVIDER_FACTS.get(provider) if isinstance(provider, str) else None
        if spec is None:
            problems.append(f"{model_id}: provider {provider!r} is not a PROVIDER_FACTS row")
            continue
        unknown = sorted(set(row) - own - set(overridable))
        if unknown:
            problems.append(f"{model_id}: unknown field(s) {unknown}")
            continue

        explicit = {key: row[key] for key in overridable if key in row}
        derived: dict[str, Any] = {}
        if provider == "ollama":
            pick = local_models.pick_for_tag(model_id.removeprefix("ollama/"))
            if pick is None:
                problems.append(f"{model_id}: not a tier-table tag (nvh/core/local_models.py)")
                continue
            derived["supports_vision"] = bool(pick.vision)
        if spec.zero_cost:
            for key in PRICE_FIELDS:
                explicit.pop(key, None)
        typed = {key: value for key, value in explicit.items() if key not in derived}
        source = row.get("source")
        if typed and not (isinstance(source, str) and source.strip()):
            problems.append(
                f"{model_id}: overrides {sorted(typed)} need a `source:` string saying where they come from"
            )
            continue
        if source and not typed:
            problems.append(f"{model_id}: `source:` without any hand-typed override field")
            continue
        explicit.update(derived)

        successor = RETIRED_MODEL_RENAMES.get(provider, {}).get(model_id)
        scores = dict(row.get("capability_scores") or {})
        ordered_scores = {key: scores[key] for key in score_order if key in scores}
        ordered_scores.update({key: scores[key] for key in sorted(scores) if key not in ordered_scores})

        canonical: dict[str, Any] = {"provider": provider, "display_name": row.get("display_name", "")}
        if successor is not None:
            canonical["status"] = "retired"
            canonical["successor"] = successor
        for key in overridable:
            if key in explicit:
                canonical[key] = explicit[key]
        if typed:
            canonical["source"] = source.strip()
        canonical["typical_latency_ms"] = row.get("typical_latency_ms", 1000)
        canonical["capability_scores"] = ordered_scores
        out[model_id] = canonical

    ollama_rows = {m.removeprefix("ollama/") for m in out if out[m]["provider"] == "ollama"}
    table_tags = set(local_models.all_tags())
    if ollama_rows != table_tags:
        problems.append(
            "ollama rows must be exactly the tier table's tags: "
            f"missing {sorted(table_tags - ollama_rows)}, extra {sorted(ollama_rows - table_tags)}"
        )
    if problems:
        raise SystemExit("capabilities.yaml:\n  " + "\n  ".join(problems))
    return out


# --- rendering ---------------------------------------------------------------


def _scalar(value: Any) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, width=10**6, allow_unicode=True).rstrip("\n")
    if text.endswith("\n..."):
        text = text[: -len("\n...")]
    return text


def header(litellm_version: str) -> str:
    return "\n".join([
        "# Model capability catalog -- the hand-maintained half; LiteLLM holds the rest.",
        "#",
        "# Hand-maintained per row: display_name, capability_scores (0-1 per task type),",
        "# typical_latency_ms, and explicit overrides of a LiteLLM-read fact, each row",
        "# of which carries a `source:` saying where the numbers come from.",
        "# Derived by scripts/gen_capabilities.py on every run: status/successor from",
        "# RETIRED_MODEL_RENAMES (nvh/cli/setup.py), ollama supports_vision from",
        "# nvh/core/local_models.py. A ProviderSpec.zero_cost provider's rows are $0",
        "# by that flag alone and carry no price here.",
        "# Read live when the catalog loads, never written here: price, context window,",
        "# max output and the tool/vision/JSON flags, from LiteLLM's model table plus",
        "# nvh/config/model_overrides.json (nvh/providers/registry.py). A row no source",
        "# prices loads as 'cost unknown', never as $0.",
        f"# Regenerate: python {SCRIPT}   (--check in CI compares only the fields above)",
        f"# Generated against litellm {litellm_version} (informational; --check ignores this line)",
        "",
    ])


def render(rows: Mapping[str, Mapping[str, Any]], litellm_version: str) -> str:
    """The canonical file text for already-normalised rows, grouped by provider in PROVIDER_FACTS order."""
    *_, PROVIDER_FACTS = _nvh()
    order = list(PROVIDER_FACTS)
    by_provider: dict[str, list[str]] = {}
    for model_id, row in rows.items():
        by_provider.setdefault(row["provider"], []).append(model_id)
    providers = [p for p in order if p in by_provider] + sorted(p for p in by_provider if p not in order)

    lines = [header(litellm_version), "models:"]
    for provider in providers:
        lines.append(f"  # --- {PROVIDER_FACTS[provider].display_name} ---")
        lines.extend(f"  # {note}" for note in PROVIDER_NOTES.get(provider, ()))
        for model_id in by_provider[provider]:
            row = rows[model_id]
            lines.append(f"  {_scalar(model_id)}:")
            for key, value in row.items():
                if key == "capability_scores":
                    lines.append("    capability_scores:")
                    lines.extend(f"      {_scalar(k)}: {_scalar(v)}" for k, v in value.items())
                else:
                    lines.append(f"    {_scalar(key)}: {_scalar(value)}")
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def canonical_text(text: str, litellm_version: str) -> str:
    """``text`` (a capabilities.yaml) rewritten in canonical form; the rendering must round-trip."""
    data = yaml.safe_load(text) or {}
    rows = normalise(data.get("models") or {})
    rendered = render(rows, litellm_version)
    reloaded = yaml.safe_load(rendered)["models"]
    if reloaded != rows:
        raise SystemExit("gen_capabilities: rendered catalog does not round-trip (emitter bug)")
    return rendered


def main(argv: list[str] | None = None, *, catalog_path: Path | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = catalog_path or CATALOG_PATH
    rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    actual = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    # The same --check / rewrite contract (messages, exit codes, LF writes) as
    # scripts/_gendoc.py's generators; this one renders a whole file rather
    # than marker blocks, so it is typed here until _gendoc grows that hook.
    if "--check" in argv:
        expected = canonical_text(actual, recorded_litellm_version(actual) or installed_litellm_version())
        if actual != expected:
            print(f"{rel} is stale — run: python {SCRIPT}", file=sys.stderr)
            return 1
        print(f"{rel} is current")
        return 0
    expected = canonical_text(actual, installed_litellm_version())
    path.write_text(expected, encoding="utf-8", newline="\n")
    rows = yaml.safe_load(expected)["models"]
    retired = sum(1 for row in rows.values() if row.get("status") == "retired")
    sourced = sum(1 for row in rows.values() if "source" in row)
    print(f"wrote {rel} ({len(rows)} rows, {retired} retired, {sourced} with explicit overrides)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
