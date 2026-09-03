#!/usr/bin/env python
"""Regenerate the derived tables in docs/MODELS.md from the code that owns them.

    python scripts/gen_models_doc.py          # rewrite the generated blocks
    python scripts/gen_models_doc.py --check  # exit 1 when a block is stale

Five blocks sit between ``<!-- BEGIN GENERATED: <name> -->`` and
``<!-- END GENERATED: <name> -->`` markers; the prose around them is
hand-written and left alone:

* ``local-model-tiers`` -- ``nvh.core.local_models.tier_table_markdown()``:
  the budget-band ladder and the per-tag size table.
* ``unified-os-reserve`` -- the OS reserve a unified pool loses
  (``nvh.core.local_models.unified_os_reserve_gb``: an eighth of the pool,
  4-16 GB), the budget left and the tier it lands in, for the pool sizes
  the module's own comment tabulates.
* ``wizard-chat-matrix`` -- the Wizard chat / vision rows of the capability
  matrix: every tag the ladder picks as ``chat`` or ``vision``, with the
  first tier and the smallest budget that gets it. (The ComfyUI / speech /
  music rows come from studio_packs and stay hand-written.)
* ``installer-pull-ladder`` -- what ``install.sh`` pulls per tier (the chat
  pick, then each lower tier's chat pick, then the CPU fallback -- the same
  walk ``_nvwizard_fallback_chain`` does over the sourced snippet) next to
  the ``recommended()`` set for a discrete card and for a unified pool. The
  unified column is computed on the smallest pool whose budget after the
  OS reserve *is* the tier floor (:func:`unified_pool_gb`) and names it.
* ``gb10-baselines`` -- the measured DGX Spark rows of
  ``nvh.utils.gpu_emulation._MEASURED_BASELINES``.

tests/test_models_doc_parity.py runs the --check form, so a table edit, a
moved boundary or a retired tag fails CI instead of drifting in the doc.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "MODELS.md"

BEGIN = "<!-- BEGIN GENERATED: {name} -->"
END = "<!-- END GENERATED: {name} -->"


def _lm():
    sys.path.insert(0, str(ROOT))
    from nvh.core import local_models

    return local_models


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def _code(tag: str) -> str:
    return f"`{tag}`"


# --- renderers ---------------------------------------------------------------


def render_local_model_tiers() -> str:
    return _lm().tier_table_markdown().rstrip("\n") + "\n"


def install_sh_chain(tier_index: int) -> list[str]:
    """The pull chain install.sh's ``_nvwizard_fallback_chain`` builds for a tier.

    The tier's chat pick, then the chat pick of each lower tier, then the
    tier's CPU fallback -- de-duplicated in order, so it starts at the
    strongest fitting model and ends at the smallest.
    """
    lm = _lm()
    tier = lm.LOCAL_MODEL_TIERS[tier_index]
    chain: list[str] = []
    for lower in reversed(lm.LOCAL_MODEL_TIERS[: tier_index + 1]):
        tag = lower.picks["chat"].tag
        if tag not in chain:
            chain.append(tag)
    fallback = tier.picks["cpu_fallback"].tag
    if fallback not in chain:
        chain.append(fallback)
    return chain


# The pool sizes nvh.core.local_models tabulates above its reserve constants
# (8 .. 128 GB) plus 192 GB, where the reserve has long hit its 16 GB ceiling.
UNIFIED_POOLS_GB: tuple[int, ...] = (8, 16, 24, 32, 48, 64, 96, 128, 192)

# Granularity of the unified-pool search: half the tier snap's grain, and the
# step at which ``round(pool / 8)`` can move by at most one, so a search that
# crosses a tier floor stops *on* the floor rather than past it.
_POOL_STEP_GB = 0.5


def unified_budget_for_pool(pool_gb: float):
    """``tier_budget`` of a single unified GPU whose pool is ``pool_gb`` (no system RAM)."""
    lm = _lm()
    return lm.tier_budget([SimpleNamespace(vram_mb=pool_gb * 1024, unified_memory=True)], None)


def unified_pool_gb(budget_gb: float) -> float:
    """The smallest unified pool (0.5 GB steps) whose usable budget reaches ``budget_gb``.

    The OS reserve a unified pool loses scales with the pool
    (``local_models.unified_os_reserve_gb``: an eighth of it, floored at 4 GB
    and capped at the GB10's 16 GB), so ``budget + 16`` -- the flat GB10
    reserve -- overshoots every floor below 128 GB: a 16 GB pool keeps 4 GB,
    not 16, and plans against 12. The search starts at the reserve floor (a
    pool no larger than its own reserve has a 0 GB budget) and stops at the
    first pool whose ``tier_budget`` reaches the floor. Each half-gigabyte
    step moves the budget by exactly +-0.5 (the reserve is an integer that
    changes by at most one per step), so the pool found has a budget of
    *exactly* ``budget_gb`` -- never a tier above it.
    """
    lm = _lm()
    pool = float(lm.UNIFIED_OS_RESERVE_MIN_GB)
    ceiling = float(budget_gb) + lm.UNIFIED_OS_RESERVE_MAX_GB  # budget + 16 always reaches the floor
    while unified_budget_for_pool(pool).budget_gb < budget_gb and pool < ceiling:
        pool += _POOL_STEP_GB
    return pool


def _unified_budget(tier):
    """A unified pool whose usable budget is exactly the tier's floor (:func:`unified_pool_gb`)."""
    return unified_budget_for_pool(unified_pool_gb(tier.min_gb))


def render_unified_os_reserve() -> str:
    lm = _lm()
    rows = []
    for pool in UNIFIED_POOLS_GB:
        budget = unified_budget_for_pool(pool)
        tier = lm.tier_for(budget)
        rows.append([
            f"{pool:g}",
            f"{budget.os_reserve_gb:g}",
            f"{budget.budget_gb:g}",
            f"{tier.range_label} ({tier.label})",
            _code(lm.recommended(budget)[0].tag),
        ])
    return _table(
        ["Unified pool (GB)", "OS reserve (GB)", "Budget (GB)", "Tier", "First `recommended()` pick"],
        rows,
    )


def render_installer_pull_ladder() -> str:
    lm = _lm()
    rows = []
    for n, tier in enumerate(lm.LOCAL_MODEL_TIERS):
        pool = unified_pool_gb(tier.min_gb)
        rows.append([
            tier.range_label,
            tier.label,
            " → ".join(_code(t) for t in install_sh_chain(n)),
            ", ".join(_code(p.tag) for p in lm.recommended(float(tier.min_gb))),
            f"{pool:g} GB pool: " + ", ".join(_code(p.tag) for p in lm.recommended(_unified_budget(tier))),
        ])
    return _table(
        [
            "Budget (GB)", "Tier", "`install.sh` pull chain (first success wins)",
            "`recommended()` — discrete card", "`recommended()` — unified pool (MoE first)",
        ],
        rows,
    )


def render_wizard_chat_matrix() -> str:
    lm = _lm()
    seen: dict[str, dict] = {}
    for tier in lm.LOCAL_MODEL_TIERS:
        for use_case in ("chat", "vision"):
            pick = tier.picks[use_case]
            row = seen.setdefault(pick.tag, {"pick": pick, "roles": [], "first": tier, "chat_tier": None})
            if use_case not in row["roles"]:
                row["roles"].append(use_case)
            if use_case == "chat" and row["chat_tier"] is None:
                row["chat_tier"] = tier
    ordered = sorted(seen.values(), key=lambda r: (r["first"].min_gb, r["pick"].tag))
    rows = []
    for row in ordered:
        pick, first, chat_tier = row["pick"], row["first"], row["chat_tier"]
        pulled = (
            f"yes — chat pick from the {chat_tier.label} tier"
            if chat_tier is not None
            else f"no — `nvh models pull {pick.tag}`"
        )
        rows.append([
            _code(pick.tag),
            ", ".join(row["roles"]),
            first.label,
            f"{first.min_gb:g}",
            "yes" if pick.vision else "",
            pulled,
        ])
    return _table(
        ["Tag", "Ladder role", "First tier", "Min budget (GB)", "Sees images", "Pulled by `install.sh`"],
        rows,
    )


def render_gb10_baselines() -> str:
    lm = _lm()
    from nvh.utils.gpu_emulation import _MEASURED_BASELINES, _MODEL_MEMORY_GB

    measured = sorted(
        ((model, toks) for (gpu, model), toks in _MEASURED_BASELINES.items() if gpu == "gb10"),
        key=lambda item: (-item[1], item[0]),
    )
    rows = []
    for model, toks in measured:
        pick = lm.pick_for_tag(model)
        size = pick.weights_gb if pick is not None else _MODEL_MEMORY_GB.get(model)
        rows.append([_code(model), "—" if size is None else f"{size:g}", f"{toks:g}"])
    return _table(["Model", "Size (GB)", "tok/s"], rows)


BLOCKS: dict[str, Callable[[], str]] = {
    "local-model-tiers": render_local_model_tiers,
    "unified-os-reserve": render_unified_os_reserve,
    "wizard-chat-matrix": render_wizard_chat_matrix,
    "installer-pull-ladder": render_installer_pull_ladder,
    "gb10-baselines": render_gb10_baselines,
}


# --- marker plumbing -----------------------------------------------------------


def _span(text: str, name: str) -> tuple[int, int]:
    """(start of body, start of END marker) for a block; raises when a marker is missing or doubled."""
    begin, end = BEGIN.format(name=name), END.format(name=name)
    for marker in (begin, end):
        count = text.count(marker)
        if count != 1:
            raise SystemExit(f"docs/MODELS.md: expected exactly one {marker!r}, found {count}")
    i = text.index(begin) + len(begin)
    j = text.index(end)
    if j < i:
        raise SystemExit(f"docs/MODELS.md: END marker for {name!r} precedes its BEGIN marker")
    return i, j


def block_text(text: str, name: str) -> str:
    """The generated body currently between a block's markers."""
    i, j = _span(text, name)
    return text[i:j]


def render(text: str) -> str:
    """``text`` with every generated block replaced by its renderer's output."""
    for name, renderer in BLOCKS.items():
        i, j = _span(text, name)
        text = text[:i] + "\n" + renderer() + text[j:]
    return text


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    actual = DOC_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    expected = render(actual)
    rel = DOC_PATH.relative_to(ROOT).as_posix() if DOC_PATH.is_relative_to(ROOT) else str(DOC_PATH)
    if "--check" in argv:
        if actual != expected:
            print(f"{rel} is stale — run: python scripts/gen_models_doc.py", file=sys.stderr)
            return 1
        print(f"{rel} is current")
        return 0
    DOC_PATH.write_text(expected, encoding="utf-8", newline="\n")
    print(f"wrote {rel} ({len(BLOCKS)} generated blocks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
