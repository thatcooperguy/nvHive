"""Marker plumbing shared by the doc generators (``gen_models_doc.py``, ``gen_providers_doc.py``).

A generated doc keeps hand-written prose around blocks delimited by
``<!-- BEGIN GENERATED: <name> -->`` / ``<!-- END GENERATED: <name> -->``.
Each generator owns its renderers (``BLOCKS``) and its ``DOC_PATH``; the
span finding, the block replacement and the ``--check`` contract live here
once, so a guard tightened for one doc holds for every doc.

Every function takes the generator's ``doc_path`` / ``blocks`` explicitly
rather than binding them at import, so a test can monkeypatch the
generator's ``DOC_PATH`` and call its ``main()`` as before.

:func:`check_or_write` is the ``--check`` / rewrite contract on its own
(relative path in the messages, CRLF-normalised read, the exit-1 stale
branch, the LF-only write) for a generator that renders a whole file rather
than marker blocks (``gen_capabilities.py``); :func:`main` is that contract
applied to a marker-block doc.
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


def rel_path(path: Path, root: Path) -> str:
    """``path`` relative to ``root`` with forward slashes (the name every message prints), else the path as given."""
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


def check_or_write(
    argv: list[str] | None,
    *,
    path: Path,
    root: Path,
    script: str,
    render: Callable[[str, bool], str],
    summary: Callable[[str], str] | None = None,
) -> int:
    """The ``--check`` / rewrite contract every generator shares (marker-block docs and whole files alike).

    ``render(actual_text, check)`` returns the text the file should hold;
    ``check`` is true under ``--check``, for a generator whose expected text
    differs between the two modes (gen_capabilities.py compares against the
    LiteLLM version its header records, but writes the installed one). The
    file is read with CRLF normalised so a Windows checkout compares equal.
    Under ``--check`` a differing file prints ``<rel> is stale — run: python
    <script>`` on stderr and exits 1, else ``<rel> is current`` and 0.
    Otherwise the file is rewritten with LF line endings and ``wrote <rel>``
    is printed, with ``summary(written_text)`` in parentheses when given.
    """
    argv = sys.argv[1:] if argv is None else argv
    rel = rel_path(path, root)
    actual = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    check = "--check" in argv
    expected = render(actual, check)
    if check:
        if actual != expected:
            print(f"{rel} is stale — run: python {script}", file=sys.stderr)
            return 1
        print(f"{rel} is current")
        return 0
    path.write_text(expected, encoding="utf-8", newline="\n")
    print(f"wrote {rel} ({summary(expected)})" if summary else f"wrote {rel}")
    return 0


def main(
    argv: list[str] | None,
    *,
    doc_path: Path,
    root: Path,
    blocks: dict[str, Callable[[], str]],
    script: str,
) -> int:
    """The ``--check`` / rewrite entry point: exit 1 when a block is stale, else rewrite (LF) and exit 0."""
    rel = rel_path(doc_path, root)
    return check_or_write(
        argv,
        path=doc_path,
        root=root,
        script=script,
        render=lambda actual, _check: render(actual, blocks, rel),
        summary=lambda _text: f"{len(blocks)} generated blocks",
    )
