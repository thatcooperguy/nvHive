#!/usr/bin/env python
"""Verify every tag in nvh.core.local_models is published on the Ollama registry.

    python scripts/verify_local_model_tags.py             # table; exit 1 on any failure
    python scripts/verify_local_model_tags.py --json      # machine-readable
    python scripts/verify_local_model_tags.py --timeout 10 --tag qwen3:8b --tag moondream

For each distinct tag in the table this GETs
``https://registry.ollama.ai/v2/library/<name>/manifests/<tag>`` (10 s timeout).
A 200 means the tag is pullable; the manifest's layer sizes give the on-disk
size, which is compared with the table's ``weights_gb`` (a >10% gap fails, so a
republished tag cannot silently drift from the table). A 404 is a phantom tag
-- the class of bug (``nemotron-omni``, ``nemotron-3-nano-omni``) this script
exists to keep out of the tree. Network errors are reported as ``error`` and
also fail, so CI cannot pass on a flaky link.

tests/test_local_models.py runs :func:`verify` under ``NVH_NETWORK_TESTS=1``.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nvh.core import local_models  # noqa: E402

REGISTRY = "https://registry.ollama.ai/v2/library"
ACCEPT = (
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.oci.image.manifest.v1+json"
)
DEFAULT_TIMEOUT = 10.0
SIZE_TOLERANCE = 0.10  # fraction of the table's weights_gb


@dataclass
class TagCheck:
    tag: str
    status: str                 # "ok" | "missing" | "error"
    http_status: int | None
    table_gb: float
    registry_gb: float | None = None
    detail: str = ""

    @property
    def size_ok(self) -> bool | None:
        """None until the registry size is known; then whether it is within tolerance."""
        if self.registry_gb is None:
            return None
        allowed = max(self.table_gb * SIZE_TOLERANCE, 0.1)
        return abs(self.registry_gb - self.table_gb) <= allowed

    @property
    def passed(self) -> bool:
        return self.status == "ok" and self.size_ok is not False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["size_ok"] = self.size_ok
        data["passed"] = self.passed
        return data


def split_tag(tag: str) -> tuple[str, str]:
    """``"qwen3:8b"`` -> ``("qwen3", "8b")``; an untagged name means ``latest``."""
    name, _, version = tag.partition(":")
    return name, version or "latest"


def manifest_url(tag: str) -> str:
    name, version = split_tag(tag)
    return f"{REGISTRY}/{name}/manifests/{version}"


def fetch_manifest(tag: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, dict[str, Any] | None]:
    """``(http_status, manifest_json_or_None)``; raises ``OSError`` on transport failure."""
    request = urllib.request.Request(manifest_url(tag), headers={"Accept": ACCEPT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, None


def manifest_size_gb(manifest: dict[str, Any] | None) -> float | None:
    """Sum of the manifest layers in decimal GB (what ``ollama list`` prints)."""
    if not manifest:
        return None
    total = sum(int(layer.get("size", 0)) for layer in manifest.get("layers", []))
    return round(total / 1e9, 2) if total else None


def check_tag(tag: str, table_gb: float, timeout: float = DEFAULT_TIMEOUT) -> TagCheck:
    try:
        status, manifest = fetch_manifest(tag, timeout)
    except Exception as exc:  # DNS, timeout, TLS -- anything that is not an HTTP answer
        return TagCheck(tag, "error", None, table_gb, detail=f"{type(exc).__name__}: {exc}")
    if status == 200:
        return TagCheck(tag, "ok", status, table_gb, registry_gb=manifest_size_gb(manifest))
    if status == 404:
        return TagCheck(tag, "missing", status, table_gb, detail="not on registry.ollama.ai")
    return TagCheck(tag, "error", status, table_gb, detail=f"unexpected HTTP {status}")


def verify(
    tags: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[TagCheck]:
    """Check every table tag (or the given subset) against the registry."""
    picks = {p.tag: p for p in local_models.all_picks()}
    wanted = tags or sorted(picks)
    checks: list[TagCheck] = []
    for tag in wanted:
        pick = picks.get(tag)
        table_gb = pick.weights_gb if pick is not None else 0.0
        checks.append(check_tag(tag, table_gb, timeout))
    return checks


def render_table(checks: list[TagCheck]) -> str:
    rows = [("tag", "status", "http", "table GB", "registry GB", "verdict")]
    for c in checks:
        verdict = "PASS" if c.passed else ("SIZE" if c.status == "ok" else c.status.upper())
        rows.append((
            c.tag,
            c.status,
            "" if c.http_status is None else str(c.http_status),
            f"{c.table_gb:g}",
            "" if c.registry_gb is None else f"{c.registry_gb:g}",
            verdict + (f"  {c.detail}" if c.detail else ""),
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for n, row in enumerate(rows):
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    failed = [c for c in checks if not c.passed]
    out.append("")
    out.append(
        f"{len(checks) - len(failed)}/{len(checks)} tags verified"
        + (f"; {len(failed)} FAILED: {', '.join(c.tag for c in failed)}" if failed else "")
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-request seconds")
    parser.add_argument("--tag", action="append", default=None, help="check only this tag (repeatable)")
    args = parser.parse_args(argv)

    checks = verify(args.tag, args.timeout)
    if args.json:
        print(json.dumps([c.as_dict() for c in checks], indent=2))
    else:
        print(render_table(checks))
    return 0 if all(c.passed for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
