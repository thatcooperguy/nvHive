#!/usr/bin/env python
"""Compare NVIDIA's dgx-spark-playbooks folders with the snapshot nvHive's playbooks were extracted from.

    python scripts/check_playbook_upstream.py --check     # network: diff today's upstream against the snapshot; exit 1 on drift
    python scripts/check_playbook_upstream.py --update    # network: rewrite scripts/playbook_upstream_snapshot.json
    python scripts/check_playbook_upstream.py --show      # offline: print the committed snapshot

Every shipped and deferred playbook in :mod:`nvh.integrations.installs.playbooks`
names an upstream folder (``nvidia/playbook-<name>/``). This script fetches each
folder's README from raw.githubusercontent.com, hashes it, reads its
``Last Updated`` line and checks the repository's root README still lists the
folder; it also hashes every upstream script a playbook step runs
(``UPSTREAM_ASSETS``) and compares it with the sha256 pinned in the module. The
snapshot lives in the repository (not under NVH_HOME) so CI can diff it:
``.github/workflows/model-currency.yml`` runs ``--check`` weekly.

Exit codes: 0 the upstream matches the snapshot; 1 drift (a README changed, a
folder vanished or was unlisted, an asset no longer matches its pin -- the
playbook's own verify step would stop the run); 2 the network did not answer
(a connection error, or any HTTP status other than 200 and 404 -- a 503 from
raw.githubusercontent.com is not eighteen unlisted folders, and ``--update``
never writes such an answer into the snapshot). Network access is opt-in:
only ``--check`` and ``--update`` fetch anything, and ``tests/test_playbooks.py``
drives both through a fake fetcher.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nvh.integrations.installs import playbooks as pb  # noqa: E402

SNAPSHOT_PATH = ROOT / "scripts" / "playbook_upstream_snapshot.json"
UPSTREAM_REPO = "https://github.com/NVIDIA/dgx-spark-playbooks"
ROOT_README_URL = "https://raw.githubusercontent.com/NVIDIA/dgx-spark-playbooks/refs/heads/main/README.md"
DEFAULT_TIMEOUT = 30.0
SCHEMA = 1

_LAST_UPDATED = re.compile(r"\*\*Last Updated:?\*\*:?\s*([0-9]{2}/[0-9]{2}/[0-9]{4})")


class UpstreamUnavailableError(OSError):
    """The upstream answered with a status that is neither content (200) nor a clean 404.

    A 5xx, a 403 or a 429 says nothing about the playbooks, so :func:`observe`
    raises instead of recording an empty body: ``--check`` exits 2 (not 1 with
    every folder "unlisted") and ``--update`` leaves the snapshot alone.
    """


def fetch_bytes(url: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, bytes]:
    """``(http_status, body)``; a 404 is ``(404, b"")``. Raises ``OSError`` when the network does not answer."""
    request = urllib.request.Request(url, headers={"User-Agent": "nvhive-playbook-drift-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _answered(fetch, url: str, timeout: float) -> tuple[int, bytes]:
    """``fetch(url, timeout)`` with every status but 200 and 404 raised as :class:`UpstreamUnavailableError`."""
    status, body = fetch(url, timeout)
    if status not in (200, 404):
        raise UpstreamUnavailableError(f"HTTP {status} from {url}")
    return status, body


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def readme_url(folder: str) -> str:
    return f"{pb.UPSTREAM_RAW}{folder}/README.md"


def last_updated(text: str) -> str | None:
    match = _LAST_UPDATED.search(text)
    return match.group(1) if match else None


def title_line(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def observe(timeout: float = DEFAULT_TIMEOUT, fetch=None) -> dict[str, Any]:
    """Today's upstream, in the snapshot's shape. ``fetch`` defaults to this module's ``fetch_bytes`` at call time (tests swap it)."""
    fetch = fetch if fetch is not None else fetch_bytes
    shipped = {playbook.id for playbook in pb.PLAYBOOKS}
    status, root_body = _answered(fetch, ROOT_README_URL, timeout)
    root_text = root_body.decode("utf-8", errors="replace") if status == 200 else ""
    folders: dict[str, Any] = {}
    for playbook_id, folder in sorted(pb.upstream_folders().items()):
        status, body = _answered(fetch, readme_url(folder), timeout)
        text = body.decode("utf-8", errors="replace") if status == 200 else ""
        folders[playbook_id] = {
            "upstream_folder": folder,
            "shipped": playbook_id in shipped,
            "http_status": status,
            "readme_sha256": sha256_hex(body) if status == 200 else None,
            "readme_bytes": len(body) if status == 200 else None,
            "title": title_line(text),
            "last_updated": last_updated(text),
            "listed_in_root_readme": f"nvidia/{folder}/" in root_text,
        }
    assets: dict[str, Any] = {}
    for url, pinned in sorted(pb.UPSTREAM_ASSETS.items()):
        status, body = _answered(fetch, url, timeout)
        digest = sha256_hex(body) if status == 200 else None
        assets[url] = {
            "http_status": status,
            "sha256": digest,
            "bytes": len(body) if status == 200 else None,
            "pinned_sha256": pinned,
            "matches_pin": digest == pinned,
        }
    return {
        "schema": SCHEMA,
        "observed": datetime.now(UTC).strftime("%Y-%m-%d"),
        "upstream": UPSTREAM_REPO,
        "root_readme_sha256": sha256_hex(root_body) if root_text else None,
        "folders": folders,
        "assets": assets,
    }


def _snapshot_path(path: Path | None) -> Path:
    # Read the module attribute at call time (not as a default argument) so a test can point it elsewhere.
    return path if path is not None else SNAPSHOT_PATH


def _snapshot_label(path: Path | None = None) -> str:
    path = _snapshot_path(path)
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    return json.loads(_snapshot_path(path).read_text(encoding="utf-8"))


def dump_snapshot(snapshot: dict[str, Any], path: Path | None = None) -> None:
    _snapshot_path(path).write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def diff(snapshot: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """Human-readable drift lines; empty when the upstream still matches the snapshot."""
    lines: list[str] = []
    old_folders, new_folders = snapshot.get("folders", {}), live.get("folders", {})
    for playbook_id in sorted(set(old_folders) | set(new_folders)):
        old, new = old_folders.get(playbook_id), new_folders.get(playbook_id)
        if old is None:
            lines.append(f"{playbook_id}: not in the snapshot (run --update after reviewing it)")
            continue
        if new is None:
            lines.append(f"{playbook_id}: in the snapshot but no longer in the module")
            continue
        folder = new["upstream_folder"]
        if new["upstream_folder"] != old["upstream_folder"]:
            lines.append(f"{playbook_id}: upstream_folder changed {old['upstream_folder']} -> {folder}")
        if new["http_status"] != 200:
            lines.append(f"{playbook_id}: nvidia/{folder}/README.md answered HTTP {new['http_status']}")
            continue
        if not new["listed_in_root_readme"]:
            lines.append(f"{playbook_id}: nvidia/{folder}/ is no longer linked from the repository README")
        if new["readme_sha256"] != old["readme_sha256"]:
            lines.append(
                f"{playbook_id}: nvidia/{folder}/README.md changed (Last Updated {old.get('last_updated')} -> "
                f"{new.get('last_updated')}; {old.get('readme_bytes')} -> {new.get('readme_bytes')} bytes; "
                f"title {new.get('title')!r}); re-read it against the playbook's steps"
            )
    old_assets, new_assets = snapshot.get("assets", {}), live.get("assets", {})
    for url in sorted(set(old_assets) | set(new_assets)):
        new = new_assets.get(url)
        if new is None:
            lines.append(f"asset {url}: in the snapshot but no longer pinned by the module")
            continue
        if new["http_status"] != 200:
            lines.append(f"asset {url}: answered HTTP {new['http_status']}")
        elif not new["matches_pin"]:
            lines.append(
                f"asset {url}: sha256 {new['sha256']} no longer matches the pin {new['pinned_sha256']} -- the playbook's "
                "verify step will stop the run; read the new file and re-pin UPSTREAM_ASSETS"
            )
    return lines


def render_snapshot(snapshot: dict[str, Any]) -> str:
    rows = [("id", "folder", "shipped", "last updated", "readme sha256")]
    for playbook_id, info in sorted(snapshot.get("folders", {}).items()):
        rows.append((
            playbook_id, info.get("upstream_folder", ""), "yes" if info.get("shipped") else "deferred",
            str(info.get("last_updated") or "-"), str(info.get("readme_sha256") or "-")[:16],
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]
    out.insert(1, "  ".join("-" * w for w in widths))
    out.append("")
    for url, info in sorted(snapshot.get("assets", {}).items()):
        verdict = "matches pin" if info.get("matches_pin") else "DOES NOT MATCH PIN"
        out.append(f"asset {url}: {str(info.get('sha256') or '-')[:16]}... {verdict}")
    out.append(f"observed {snapshot.get('observed')} from {snapshot.get('upstream')}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fetch today's upstream and diff it against the snapshot")
    mode.add_argument("--update", action="store_true", help="fetch today's upstream and rewrite the snapshot")
    mode.add_argument("--show", action="store_true", help="print the committed snapshot (no network)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-request seconds")
    args = parser.parse_args(argv)

    if args.show:
        print(render_snapshot(load_snapshot()))
        return 0
    try:
        live = observe(args.timeout)
    except OSError as exc:
        print(f"upstream unreachable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.update:
        dump_snapshot(live)
        print(f"wrote {_snapshot_label()}")
        print(render_snapshot(live))
        return 0
    snapshot = load_snapshot()
    drift = diff(snapshot, live)
    if drift:
        print(f"upstream drift against {_snapshot_label()} (observed {snapshot.get('observed')}):")
        for line in drift:
            print(f"  - {line}")
        return 1
    print(
        f"{len(live['folders'])} upstream folders and {len(live['assets'])} pinned assets match "
        f"{_snapshot_label()} (observed {snapshot.get('observed')})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
