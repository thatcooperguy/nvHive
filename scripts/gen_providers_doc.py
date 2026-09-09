#!/usr/bin/env python
"""Regenerate the derived tables in docs/PROVIDERS.md from nvh.providers.specs.

    python scripts/gen_providers_doc.py          # rewrite the generated blocks
    python scripts/gen_providers_doc.py --check  # exit 1 when a block is stale

Two blocks sit between ``<!-- BEGIN GENERATED: <name> -->`` and
``<!-- END GENERATED: <name> -->`` markers; the prose around them is
hand-written and left alone:

* ``providers`` -- the provider table: one row per ``PROVIDER_FACTS`` spec
  (cloud adapters and the bespoke Ollama / Triton / Mock rows) with its
  ``config.yaml`` name, key variable(s), free-tier note and default model.
  Providers that need no key come first (Ollama, then the anonymous tier),
  then the keyed free tiers in ladder order, the paid providers in spec
  order, then Triton and Mock.
* ``free-tier-ladder`` -- ``nvh.core.free_tier.FREE_TIER_ADVISORS`` in the
  order the router tries them, with each tier's note and how it is detected.

tests/test_provider_docs_parity.py runs the --check form, so a new spec row,
a renamed key variable or a changed free-tier note fails CI instead of
drifting in the doc.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "PROVIDERS.md"
SCRIPT = "scripts/gen_providers_doc.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _gendoc  # noqa: E402 — the marker plumbing shared with gen_models_doc.py
from _gendoc import BEGIN, END  # noqa: E402,F401
from _gendoc import code as _code  # noqa: E402
from _gendoc import table as _table  # noqa: E402


def _specs():
    sys.path.insert(0, str(ROOT))
    from nvh.providers import specs

    return specs


# --- renderers ---------------------------------------------------------------


def doc_order() -> list:
    """The table's row order.

    Providers that need no key first (Ollama, then the anonymous LLM7 tier),
    the keyed free tiers by ladder rank, the paid providers in spec order,
    then the remaining bespoke adapters (Triton, Mock) in their own order.
    """
    specs = _specs()

    def key(item: tuple[int, object]) -> tuple[int, int]:
        index, spec = item
        if spec.name in specs.BESPOKE_SPECS and spec.name != "ollama":
            return (3, index)
        if spec.free_tier and (not spec.key_env or spec.anonymous_key):
            return (0, spec.free_tier_rank)
        if spec.free_tier:
            return (1, spec.free_tier_rank)
        return (2, index)

    return [spec for _, spec in sorted(enumerate(specs.PROVIDER_FACTS.values()), key=key)]


def key_cell(spec) -> str:
    """The key-variable column: the primary variable, the provider's own alternates, ``(optional)`` for an anonymous tier."""
    if not spec.key_env:
        return "—"
    cell = _code(spec.key_env)
    if spec.alt_key_envs:
        cell += " (" + ", ".join(_code(alt) for alt in spec.alt_key_envs) + ")"
    if spec.anonymous_key:
        cell += " (optional)"
    return cell


def free_tier_cell(spec) -> str:
    if spec.free_tier:
        return spec.free_info or "yes"
    return f"paid — {spec.free_info}" if spec.free_info else "paid"


def default_model_cell(spec) -> str:
    if spec.name == "ollama":
        # What `nvh config init` writes at the default budget; the tier table
        # (docs/MODELS.md) picks a larger model on a bigger card.
        from nvh.config.settings import _default_local_model

        return f"{_code(_default_local_model())} (VRAM-tiered)"
    return _code(spec.default_model) if spec.default_model else "—"


def render_providers() -> str:
    rows = [
        [spec.display_name, _code(spec.name), key_cell(spec), free_tier_cell(spec), default_model_cell(spec)]
        for spec in doc_order()
    ]
    return _table(["Provider", "`config.yaml` name", "Key variable", "Free tier", "Default model"], rows)


def render_free_tier_ladder() -> str:
    specs = _specs()
    from nvh.core.free_tier import FREE_TIER_ADVISORS

    lines = []
    for advisor in FREE_TIER_ADVISORS:
        spec = specs.PROVIDER_FACTS[advisor.name]
        if advisor.check_fn == "ollama":
            how = "a running local daemon"
        elif advisor.check_fn == "anonymous":
            how = "no key needed"
        else:
            how = _code(advisor.env_var)
        lines.append(f"{advisor.priority}. **{spec.display_name}** ({_code(advisor.name)}) — {advisor.daily_limit}; {how}")
    return "\n".join(lines) + "\n"


BLOCKS: dict[str, Callable[[], str]] = {
    "providers": render_providers,
    "free-tier-ladder": render_free_tier_ladder,
}


# --- marker plumbing (scripts/_gendoc.py, shared with gen_models_doc.py) ------


def block_text(text: str, name: str) -> str:
    """The generated body currently between a block's markers."""
    return _gendoc.block_text(text, name, "docs/PROVIDERS.md")


def render(text: str) -> str:
    """``text`` with every generated block replaced by its renderer's output."""
    return _gendoc.render(text, BLOCKS, "docs/PROVIDERS.md")


def main(argv: list[str] | None = None) -> int:
    return _gendoc.main(argv, doc_path=DOC_PATH, root=ROOT, blocks=BLOCKS, script=SCRIPT)


if __name__ == "__main__":
    raise SystemExit(main())
