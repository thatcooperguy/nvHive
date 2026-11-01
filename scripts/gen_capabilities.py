#!/usr/bin/env python
"""Normalise nvh/config/capabilities.yaml, the hand-maintained half of the model catalog.

    python scripts/gen_capabilities.py          # rewrite the catalog in canonical form
    python scripts/gen_capabilities.py --check  # exit 1 when the committed file is not canonical
    python scripts/gen_capabilities.py --drift  # print every override beside today's LiteLLM (informational)

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
installed one. The ``--check`` / rewrite contract itself (messages, exit
codes, CRLF-normalised read, LF write) is ``scripts/_gendoc.check_or_write``,
shared with the doc generators.

``--drift`` is the weekly model-currency workflow's informational step
(:func:`print_override_drift`): every explicit ``source:`` override beside
what the installed LiteLLM table says today, every model_overrides.json
payload whose id LiteLLM now carries, and every active row that no source
prices any more -- the weekly counterpart of the spec-default release gate
in tests/test_capabilities_catalog.py, so a LiteLLM release that drops a
row is seen without turning CI red. It prints and never fails.

tests/test_capabilities_catalog.py runs the ``--check`` form;
tests/test_model_currency.py pins the drift report's shape offline.
"""

from __future__ import annotations

import logging
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _gendoc  # noqa: E402 — the --check / rewrite contract shared with the doc generators

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


# --- drift: the catalog's overrides against today's LiteLLM (--drift) ---------


def override_drift() -> list[str]:
    """One line per explicit capabilities.yaml override and per model_overrides.json payload.

    Each override is judged against the LiteLLM table this process imported:
    ``redundant`` when LiteLLM now says the same, ``differs`` when it says
    something else (the override still wins at load), ``no LiteLLM value``
    when LiteLLM has nothing to compare. Then one ``cost unknown`` line per
    active row that nothing prices any more (a ``metered_upstream``
    provider's rows are unknown by design and not listed). The comparison
    reuses the registry's own row composition, so no field mapping is typed
    here. Informational: nothing in it fails.
    """
    sys.path.insert(0, str(ROOT))
    from nvh.providers import registry

    rows = (yaml.safe_load(registry.CATALOG_PATH.read_text(encoding="utf-8")) or {}).get("models") or {}
    table = registry._litellm_table() or {}
    payloads = registry.load_model_overrides() if registry.MODEL_OVERRIDES_PATH.exists() else {}
    # LiteLLM's own rows, before nvHive registers its payloads into the same table (as the loader does next).
    carried_by_litellm = {model_id for model_id in payloads if model_id in table and model_id not in registry._OVERRIDES_APPLIED}
    if table and payloads:
        registry._apply_model_overrides(table, payloads)
    # Composing a row without its overrides is a what-if, not a load: keep the
    # loader's once-per-model "cost unknown" log and its memory out of it.
    registry_logger = logging.getLogger(registry.__name__)
    previous_level, logged_before = registry_logger.level, set(registry._UNPRICED_LOGGED)
    registry_logger.setLevel(logging.ERROR)
    try:
        return _drift_lines(rows, table, payloads, carried_by_litellm)
    finally:
        registry_logger.setLevel(previous_level)
        registry._UNPRICED_LOGGED.clear()
        registry._UNPRICED_LOGGED.update(logged_before)


def _drift_lines(rows: dict, table: dict, payloads: dict, carried_by_litellm: set[str]) -> list[str]:
    from nvh.providers import registry
    from nvh.providers.specs import PROVIDER_FACTS

    lines: list[str] = []
    for model_id, row in rows.items():
        row = row or {}
        if row.get("status") != "retired":
            info = registry._row_to_model_info(model_id, row, table)
            if not info.cost_known and not PROVIDER_FACTS[info.provider].metered_upstream:
                lines.append(
                    f"{model_id}: cost unknown -- no source prices this active row any more "
                    f"(retire it or add a sourced override) [{row.get('source', 'no source')}]"
                )
        overrides = {k: row[k] for k in registry.CATALOG_OVERRIDE_FIELDS if k in row}
        if not overrides:
            continue
        stripped = {k: v for k, v in row.items() if k not in overrides}
        ours = registry._row_to_model_info(model_id, row, table)
        theirs = registry._row_to_model_info(model_id, stripped, table)
        source = row.get("source", "no source")
        for field in overrides:
            ours_value, litellm_value = getattr(ours, field), getattr(theirs, field)
            unpriced = field.endswith("_cost_per_1m_tokens") and not theirs.cost_known
            if unpriced or litellm_value in (0, None):
                verdict = "no LiteLLM value"
            elif ours_value == litellm_value:
                verdict = "redundant (LiteLLM agrees)"
            else:
                verdict = f"differs (LiteLLM says {litellm_value})"
            lines.append(f"{model_id}.{field} = {ours_value}: {verdict} [{source}]")
    for model_id, payload in payloads.items():
        state = (
            "LiteLLM now carries this id (payload redundant)"
            if model_id in carried_by_litellm
            else "LiteLLM still lacks this id"
        )
        lines.append(f"model_overrides.json {model_id}: {state} [{payload.get('source', 'no source')}]")
    return lines


def print_override_drift() -> None:
    """Print :func:`override_drift` under a header naming the installed LiteLLM (the workflow's ``--drift`` step)."""
    print(f"# capabilities.yaml overrides vs LiteLLM {_dist_version('litellm')} (informational)")
    for line in override_drift():
        print(line)


# --- entry point ---------------------------------------------------------------


def _expected_text(actual: str, check: bool) -> str:
    """The canonical file: under ``--check`` against the header's recorded LiteLLM version, else the installed one."""
    version = (recorded_litellm_version(actual) or installed_litellm_version()) if check else installed_litellm_version()
    return canonical_text(actual, version)


def _written_summary(text: str) -> str:
    rows = yaml.safe_load(text)["models"]
    retired = sum(1 for row in rows.values() if row.get("status") == "retired")
    sourced = sum(1 for row in rows.values() if "source" in row)
    return f"{len(rows)} rows, {retired} retired, {sourced} with explicit overrides"


def main(argv: list[str] | None = None, *, catalog_path: Path | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--drift" in argv:
        print_override_drift()
        return 0
    return _gendoc.check_or_write(
        argv,
        path=catalog_path or CATALOG_PATH,
        root=ROOT,
        script=SCRIPT,
        render=_expected_text,
        summary=_written_summary,
    )


if __name__ == "__main__":
    raise SystemExit(main())
