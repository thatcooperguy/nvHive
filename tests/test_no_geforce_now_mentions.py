"""No shipped text names GeForce NOW.

The Linux desktop product is not launched, so nothing a user can read —
README, docs, CLI strings, seeded vault notes, web UI, installer — may
mention GeForce NOW, GFN or CloudMatch. GPU product names ("GeForce RTX
4090") and the trademark notices that list "GeForce" are fine and are not
matched. CHANGELOG.md is history and is the only file allowed to keep the
name.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCANNED = [
    ROOT / "README.md",
    *sorted((ROOT / "docs").rglob("*.md")),
    *sorted((ROOT / "nvh").rglob("*.py")),
    *sorted(p for sub in ("app", "components", "lib") for p in (ROOT / "web" / sub).rglob("*.ts")),
    *sorted(p for sub in ("app", "components", "lib") for p in (ROOT / "web" / sub).rglob("*.tsx")),
    *sorted(ROOT.glob("install*.sh")),
]

BANNED = re.compile(r"geforce\s*now|\bgfn\b|cloudmatch", re.IGNORECASE)


def test_scan_covers_the_shipped_surfaces():
    assert ROOT / "README.md" in SCANNED
    assert any(p.name == "vault.py" for p in SCANNED)
    assert any(p.suffix == ".tsx" for p in SCANNED)
    assert any(p.name == "install.sh" for p in SCANNED)


def test_no_geforce_now_mentions():
    offenders = []
    for path in SCANNED:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if BANNED.search(line):
                rel = path.relative_to(ROOT).as_posix()
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "GeForce NOW / GFN / CloudMatch must not appear in shipped text "
        "(CHANGELOG.md is the only exception):\n  " + "\n  ".join(offenders)
    )
