"""Every relative link and image in README.md and docs/ resolves (issue #129).

24 of 33 docs had no inbound link and several linked to files that no longer
existed. This resolves each relative Markdown link, image and reference-style
target on disk — no network — and checks that every page under docs/ is
reachable from README.md or another page. docs/proposals/ are dated audit
records that deliberately cite files the plan deleted, so they are not
scanned for outbound links (they still count as link sources for reachability).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

INLINE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
HTML_SRC = re.compile(r"""<(?:img|a)\b[^>]*?\b(?:src|href)=["']([^"']+)["']""", re.IGNORECASE)
FENCE = re.compile(r"```.*?```", re.DOTALL)

SOURCES = [ROOT / "README.md", *sorted(p for p in DOCS.rglob("*.md") if "proposals" not in p.parts)]


def _targets(path: Path) -> list[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    return [
        *INLINE.findall(text),
        *REFERENCE.findall(text),
        *HTML_SRC.findall(text),
    ]


def _is_relative(target: str) -> bool:
    return not (
        target.startswith(("http://", "https://", "mailto:", "#"))
        or ":" in target.split("/")[0]
    )


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(source: Path):
    broken = []
    for target in _targets(source):
        if not _is_relative(target):
            continue
        rel = target.split("#", 1)[0]
        if not rel:
            continue
        if not (source.parent / rel).exists():
            broken.append(target)
    assert not broken, f"{source.relative_to(ROOT).as_posix()} links to missing files: {broken}"


def test_every_doc_has_an_inbound_link():
    pages = sorted(p for p in DOCS.glob("*.md"))
    linkers = [ROOT / "README.md", *sorted(DOCS.rglob("*.md"))]
    orphans = []
    for page in pages:
        inbound = False
        for other in linkers:
            if other == page:
                continue
            for target in _targets(other):
                if not _is_relative(target):
                    continue
                rel = target.split("#", 1)[0]
                if rel and (other.parent / rel).resolve() == page.resolve():
                    inbound = True
                    break
            if inbound:
                break
        if not inbound:
            orphans.append(page.relative_to(ROOT).as_posix())
    assert not orphans, f"docs with no inbound link: {orphans}"


def test_readme_links_every_doc_page():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in DOCS.glob("*.md") if f"docs/{p.name}" not in readme]
    assert not missing, f"README's documentation table is missing: {missing}"
