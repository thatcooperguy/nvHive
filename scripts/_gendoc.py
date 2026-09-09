"""Marker plumbing shared by the doc generators (``gen_models_doc.py``, ``gen_providers_doc.py``).

A generated doc keeps hand-written prose around blocks delimited by
``<!-- BEGIN GENERATED: <name> -->`` / ``<!-- END GENERATED: <name> -->``.
Each generator owns its renderers (``BLOCKS``) and its ``DOC_PATH``; the
span finding, the block replacement and the ``--check`` contract live here
once, so a guard tightened for one doc holds for every doc.

Every function takes the generator's ``doc_path`` / ``blocks`` explicitly
rather than binding them at import, so a test can monkeypatch the
generator's ``DOC_PATH`` and call its ``main()`` as before.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

BEGIN = "<!-- BEGIN GENERATED: {name} -->"
END = "<!-- END GENERATED: {name} -->"


def table(header: list[str], rows: list[list[str]]) -> str:
    """A GitHub-flavoured Markdown table (header, rule, rows), newline-terminated."""
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def code(text: str) -> str:
    return f"`{text}`"


def span(text: str, name: str, doc_label: str) -> tuple[int, int]:
    """(start of body, start of END marker) for a block; raises when a marker is missing or doubled."""
    begin, end = BEGIN.format(name=name), END.format(name=name)
    for marker in (begin, end):
        count = text.count(marker)
        if count != 1:
            raise SystemExit(f"{doc_label}: expected exactly one {marker!r}, found {count}")
    i = text.index(begin) + len(begin)
    j = text.index(end)
    if j < i:
        raise SystemExit(f"{doc_label}: END marker for {name!r} precedes its BEGIN marker")
    return i, j


def block_text(text: str, name: str, doc_label: str) -> str:
    """The generated body currently between a block's markers."""
    i, j = span(text, name, doc_label)
    return text[i:j]


def render(text: str, blocks: dict[str, Callable[[], str]], doc_label: str) -> str:
    """``text`` with every generated block replaced by its renderer's output."""
    for name, renderer in blocks.items():
        i, j = span(text, name, doc_label)
        text = text[:i] + "\n" + renderer() + text[j:]
    return text


def main(
    argv: list[str] | None,
    *,
    doc_path: Path,
    root: Path,
    blocks: dict[str, Callable[[], str]],
    script: str,
) -> int:
    """The ``--check`` / rewrite entry point: exit 1 when a block is stale, else rewrite (LF) and exit 0."""
    argv = sys.argv[1:] if argv is None else argv
    rel = doc_path.relative_to(root).as_posix() if doc_path.is_relative_to(root) else str(doc_path)
    actual = doc_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    expected = render(actual, blocks, rel)
    if "--check" in argv:
        if actual != expected:
            print(f"{rel} is stale — run: python {script}", file=sys.stderr)
            return 1
        print(f"{rel} is current")
        return 0
    doc_path.write_text(expected, encoding="utf-8", newline="\n")
    print(f"wrote {rel} ({len(blocks)} generated blocks)")
    return 0
