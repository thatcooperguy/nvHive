"""One-shot import of the pre-0.44 homes into the ``NVH_HOME`` layout.

Before 0.44 nvHive scattered per-user files over three roots: ``~/.hive``
(config.yaml, .env, user.json, plugins, workflows, schedules, the routing
explanation, the REPL memories, the pre-0.42 knowledge store), ``~/.council``
(the SQLite state database) and ``~/nvh`` (the git-clone install root of the
early installers). Since 0.44 :func:`nvh.integrations.workspace.storage.storage_layout`
is the only path oracle, and this module is the **only** place that still
knows the old names: :func:`migrate_legacy_homes` copies whatever it finds
into the layout exactly once (a marker under ``state/`` records what moved),
never deletes or writes to a legacy location, and skips anything a one-shot
importer (``nvh rag import-legacy``, the REPL memory import) has already
consumed.

Where the layout already has a file of its own the legacy copy is not
allowed to overwrite it — but two of them are *merged* instead of skipped,
because on 0.43 both copies were live at once: ``nvh setup`` wrote
``~/.hive/.env`` and ``~/.hive/config.yaml`` while the web wizard wrote the
layout's ``config/.env`` and ``config/config.yaml``, and the key loader read
both. ``KEY=VALUE`` lines the layout ``.env`` lacks are appended to it, and
provider stanzas / top-level sections the layout ``config.yaml`` lacks are
added to it (the layout copy wins every conflict, and a ``.pre-0.44-merge``
backup of it is left beside it). Nothing else is merged.

Symlinks under a legacy tree are copied *as links* (``copytree(...,
symlinks=True)``), so an edit-in-place ``~/.hive/plugins/dev.py ->
~/src/dev.py`` stays a link and a ``~/.hive/knowledge -> /mnt/archive`` link
is re-created rather than the archive duplicated. A target whose copy fails
is cleaned up and reported under ``failed``; the marker is written only when
every present target copied or was deliberately skipped, so the next run
retries the failed ones instead of forgetting them.

It runs from the init path (``nvh config init`` via ``get_config_dir()``),
from ``load_config()`` when the user config is missing while a legacy one
exists, from the CLI's key loading (``nvh.cli.setup._env_key_files``) so an
upgraded install sees its keys on the very first command, and from
``nvh.storage.repository.init_db`` when the state database is absent.

Kill switch: ``NVH_LEGACY_MIGRATION=0`` (``false`` / ``no`` / ``off``) makes
:func:`migrate_legacy_homes` a no-op that reads nothing under ``$HOME``.
tests/conftest.py sets it for every suite, so no test reads the developer's
real ``~/.hive`` by accident; a test of the migration itself sets it back to
``1``. Reset hook for tests: the marker lives under ``storage_layout().state_dir``,
so pointing ``NVH_HOME`` at a fresh ``tmp_path`` and monkeypatching
``Path.home`` gives a clean slate; ``migrate_legacy_homes(force=True)`` re-runs
over an existing marker.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nvh.integrations.workspace.storage import StorageLayout, storage_layout

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The legacy names. Nothing outside this module spells them.
# ---------------------------------------------------------------------------

LEGACY_HOME_DIRNAME = ".hive"  # ~/.hive — config, keys, plugins, workflows, state files
LEGACY_DB_DIRNAME = ".council"  # ~/.council — council.db (pre-0.41 SQLite state)
LEGACY_INSTALL_DIRNAME = "nvh"  # ~/nvh — repo/ + venv/ of the early installers
LEGACY_DATA_DIR_ENV = "HIVE_DATA_DIR"  # moved only the database; superseded by NVH_STATE

# Project-local overlay names (a checkout's own ``.hive/``). The ``.nvh`` names
# are primary since 0.44; these are searched after them, at every level.
LEGACY_PROJECT_DIRNAME = ".hive"
LEGACY_PROJECT_CONFIG_NAMES: tuple[str, ...] = (".hive.yaml", ".hive/config.yaml")
LEGACY_CONTEXT_FILE_NAMES: tuple[str, ...] = (".hive.md",)
LEGACY_CONTEXT_DIR_NAMES: tuple[str, ...] = (".hive/context", ".hive/rules")
LEGACY_WORKFLOW_DIRNAME = ".hive/workflows"

MARKER_NAME = "legacy-migration.json"
#: Kill switch (``0`` / ``false`` / ``no`` / ``off`` disables the migration).
LEGACY_MIGRATION_ENV = "NVH_LEGACY_MIGRATION"
_FALSY = frozenset({"0", "false", "no", "off"})
_SQLITE_SIDECARS = ("-wal", "-shm")
#: Suffix of the backup left beside a layout file the migration merged into.
MERGE_BACKUP_SUFFIX = ".pre-0.44-merge"
#: Sections of ``config.yaml`` whose *entries* are merged one by one.
_MERGED_CONFIG_SECTIONS = ("advisors", "providers", "profiles", "webhooks")


def migration_enabled(environ: dict[str, str] | None = None) -> bool:
    """``False`` only when :data:`LEGACY_MIGRATION_ENV` is set to a falsy word."""
    env = os.environ if environ is None else environ
    return env.get(LEGACY_MIGRATION_ENV, "").strip().lower() not in _FALSY


def legacy_hive_home() -> Path:
    """``~/.hive`` — where every pre-0.44 per-user file except the database lived."""
    return Path.home() / LEGACY_HOME_DIRNAME


def legacy_council_home() -> Path:
    """``~/.council`` — where the pre-0.41 SQLite state database lived."""
    return Path.home() / LEGACY_DB_DIRNAME


def legacy_install_home() -> Path:
    """``~/nvh`` — the install root of install.ps1 / install-mac.sh before 0.44."""
    return Path.home() / LEGACY_INSTALL_DIRNAME


def legacy_roots_present() -> dict[str, Path]:
    """The legacy roots that exist on this machine, by label."""
    roots = {
        "hive": legacy_hive_home(),
        "council": legacy_council_home(),
        "install": legacy_install_home(),
    }
    return {label: path for label, path in roots.items() if path.is_dir()}


# ---------------------------------------------------------------------------
# What moves where
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyTarget:
    """One legacy file or directory and its place in the layout."""

    label: str
    source: Path
    destination: Path
    # An existing marker meaning a one-shot importer already consumed the
    # source (the RAG knowledge import, the REPL memory import): nothing to copy.
    skip_if: Path | None = None
    # SQLite databases travel with their -wal / -shm sidecars so no committed
    # row that only lives in the write-ahead log is lost.
    sqlite: bool = False
    # How an existing destination is treated: ``"skip"`` (never touched),
    # ``"env"`` (KEY=VALUE lines it lacks are appended) or ``"yaml"``
    # (sections / provider stanzas it lacks are added; it wins conflicts).
    merge: str = "skip"


def legacy_targets(layout: StorageLayout | None = None) -> list[LegacyTarget]:
    """Every legacy location and the layout path it maps to, in copy order."""
    layout = layout or storage_layout()
    hive = legacy_hive_home()
    council = legacy_council_home()
    install = legacy_install_home()
    return [
        LegacyTarget("user config", hive / "config.yaml", layout.config_dir / "config.yaml", merge="yaml"),
        LegacyTarget("API keys (.env)", hive / ".env", layout.config_dir / ".env", merge="env"),
        LegacyTarget("user profile", hive / "user.json", layout.config_dir / "user.json"),
        LegacyTarget(
            "global context", hive / "global_context.md", layout.config_dir / "global_context.md"
        ),
        LegacyTarget("workflows", hive / "workflows", layout.config_dir / "workflows"),
        LegacyTarget("plugins", hive / "plugins", layout.plugins_dir),
        LegacyTarget("schedules", hive / "schedules.json", layout.state_dir / "schedules.json"),
        LegacyTarget(
            "routing explanation", hive / "last_query.json", layout.state_dir / "last_query.json"
        ),
        LegacyTarget(
            "REPL memories",
            hive / "memory" / "memories.json",
            layout.state_dir / "memory" / "memories.json",
            skip_if=layout.state_dir / "legacy-memories-imported.json",
        ),
        LegacyTarget(
            "knowledge store",
            hive / "knowledge",
            layout.home / "knowledge",
            skip_if=layout.home / "rag" / "legacy-import.json",
        ),
        LegacyTarget(
            "benchmark results",
            hive / "benchmark_results.md",
            layout.outputs_dir / "benchmark_results.md",
        ),
        LegacyTarget(
            "state database", council / "council.db", layout.state_dir / "nvhive.db", sqlite=True
        ),
        LegacyTarget(
            "NVIDIA bug report",
            install / "nvidia-bug-report.log.gz",
            layout.support_dir / "nvidia-bug-report.log.gz",
        ),
    ]


@dataclass
class LegacyMigration:
    """What :func:`migrate_legacy_homes` did (or found already done)."""

    layout_home: str
    marker: str
    ran_at: str | None = None
    already_done: bool = False
    disabled: bool = False
    legacy_roots: dict[str, str] = field(default_factory=dict)
    moved: list[dict[str, str]] = field(default_factory=list)
    merged: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def legacy_env_warnings(environ: dict[str, str] | None = None) -> list[str]:
    """Warnings for legacy environment knobs that are no longer read."""
    env = os.environ if environ is None else environ
    warnings: list[str] = []
    data_dir = env.get(LEGACY_DATA_DIR_ENV)
    if data_dir:
        warnings.append(
            f"{LEGACY_DATA_DIR_ENV}={data_dir} is no longer read (0.44); the state database "
            f"lives in $NVH_STATE (default $NVH_HOME/state). Export NVH_STATE={data_dir}/state "
            "to keep using that location."
        )
    return warnings


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------


def _relink(source: Path, destination: Path) -> bool:
    """Re-create the link at ``source`` as ``destination`` (absolute target); ``False`` when this user cannot make links."""
    link_target = Path(os.readlink(source))
    if not link_target.is_absolute():
        link_target = (source.parent / link_target).resolve()
    try:
        os.symlink(link_target, destination, target_is_directory=source.is_dir())
    except OSError:  # Windows without the symlink privilege, a filesystem without links
        return False
    return True


def _copy(target: LegacyTarget, report: LegacyMigration | None = None) -> None:
    """Copy one target; links stay links, databases bring their sidecars. Raises ``OSError``."""
    target.destination.parent.mkdir(parents=True, exist_ok=True)
    source = target.source
    if source.is_symlink():
        # The legacy entry itself is a link (``~/.hive/knowledge -> /mnt/archive``):
        # re-create the link, never duplicate what it points at — unless this
        # user cannot create links, in which case the content is copied and
        # the report says so.
        if _relink(source, target.destination):
            return
        if report is not None:
            report.warnings.append(
                f"{target.label}: {source} is a symlink and this user cannot create links; "
                f"its content was copied to {target.destination} instead"
            )
        source = source.resolve()
    if source.is_dir():
        shutil.copytree(source, target.destination, symlinks=True)
        return
    shutil.copy2(source, target.destination)
    if target.sqlite:
        for suffix in _SQLITE_SIDECARS:
            sidecar = target.source.with_name(target.source.name + suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, target.destination.with_name(target.destination.name + suffix))


def _discard_partial(destination: Path) -> None:
    """Remove whatever a failed copy left at ``destination`` so the next run can retry."""
    try:
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        elif destination.is_dir():
            shutil.rmtree(destination, ignore_errors=True)
    except OSError as exc:  # pragma: no cover — best effort
        _log.debug("legacy migration: could not clean up %s: %s", destination, exc)


# ---------------------------------------------------------------------------
# Merging (the two files 0.43 kept live in both homes)
# ---------------------------------------------------------------------------


def _env_lines(text: str) -> dict[str, str]:
    """``{VAR: line}`` for every ``KEY=VALUE`` line (comments and blanks dropped)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        var = line.partition("=")[0].strip()
        if var and var not in out:
            out[var] = line
    return out


def _backup(destination: Path) -> None:
    backup = destination.with_name(destination.name + MERGE_BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(destination, backup)


def _merge_env(source: Path, destination: Path) -> list[str]:
    """Append the ``KEY=VALUE`` lines ``destination`` lacks; returns the variable names added."""
    legacy = _env_lines(source.read_text(encoding="utf-8"))
    current_text = destination.read_text(encoding="utf-8")
    current = _env_lines(current_text)
    missing = [var for var in legacy if var not in current]
    if not missing:
        return []
    _backup(destination)
    block = "\n".join(legacy[var] for var in missing)
    joiner = "" if not current_text or current_text.endswith("\n") else "\n"
    destination.write_text(
        f"{current_text}{joiner}# merged from the pre-0.44 home by the 0.44 legacy migration\n{block}\n",
        encoding="utf-8",
    )
    return missing


def _merge_yaml(source: Path, destination: Path) -> list[str]:
    """Add the sections / stanzas ``destination`` lacks; returns dotted names of what was added.

    The layout copy wins every conflict: only a top-level key it does not
    have, or an entry under ``advisors`` / ``providers`` / ``profiles`` /
    ``webhooks`` it does not have, is taken from the legacy file. An
    ``advisors:`` legacy section fills a ``providers:`` destination (and vice
    versa) — the two spellings are one section to the loader.
    """
    import yaml

    legacy = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    current = yaml.safe_load(destination.read_text(encoding="utf-8")) or {}
    if not isinstance(legacy, dict) or not isinstance(current, dict):
        return []
    added: list[str] = []
    aliases = {"advisors": "providers", "providers": "advisors"}
    for key, value in legacy.items():
        dest_key = key
        if key in aliases and key not in current and aliases[key] in current:
            dest_key = aliases[key]
        if dest_key not in current:
            current[dest_key] = value
            added.append(str(key))
            continue
        if key in _MERGED_CONFIG_SECTIONS and isinstance(value, dict) and isinstance(current[dest_key], dict):
            for name, stanza in value.items():
                if name not in current[dest_key]:
                    current[dest_key][name] = stanza
                    added.append(f"{key}.{name}")
    if not added:
        return []
    _backup(destination)
    destination.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8")
    return added


def _merge(target: LegacyTarget) -> list[str]:
    if target.merge == "env":
        return _merge_env(target.source, target.destination)
    if target.merge == "yaml":
        return _merge_yaml(target.source, target.destination)
    return []


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _read_marker(marker: Path, report: LegacyMigration) -> None:
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    report.ran_at = data.get("ran_at")
    report.legacy_roots = dict(data.get("legacy_roots") or {})
    report.moved = list(data.get("moved") or [])
    report.merged = list(data.get("merged") or [])
    report.skipped = list(data.get("skipped") or [])


def migrate_legacy_homes(
    layout: StorageLayout | None = None,
    *,
    force: bool = False,
) -> LegacyMigration:
    """Copy the pre-0.44 homes into ``layout`` once; never write back to them.

    Idempotent: a marker at ``state_dir/legacy-migration.json`` short-circuits
    later calls (``force=True`` re-runs and rewrites it). When no legacy root
    exists nothing is written at all, so a fresh install pays three ``stat``
    calls and leaves no trace. A copy goes only where the destination does
    not exist yet; an existing ``.env`` / ``config.yaml`` is merged (see the
    module docstring), anything else the user already has under ``NVH_HOME``
    is left alone. A target whose copy fails is reported under ``failed`` and
    the marker is *not* written, so the next run retries it.
    ``NVH_LEGACY_MIGRATION=0`` disables the whole thing (``disabled=True``).
    """
    layout = layout or storage_layout()
    marker = layout.state_dir / MARKER_NAME
    report = LegacyMigration(layout_home=str(layout.home), marker=str(marker))

    if not migration_enabled():
        report.disabled = True
        return report

    if marker.is_file() and not force:
        _read_marker(marker, report)
        report.already_done = True
        return report

    report.warnings.extend(legacy_env_warnings())
    for warning in report.warnings:
        _log.warning("%s", warning)

    present = legacy_roots_present()
    if not present:
        return report
    report.legacy_roots = {label: str(path) for label, path in present.items()}

    for target in legacy_targets(layout):
        if not target.source.exists() and not target.source.is_symlink():
            continue
        entry = {"label": target.label, "source": str(target.source), "destination": str(target.destination)}
        if _same_file(target.source, target.destination):
            continue
        if target.skip_if is not None and target.skip_if.exists():
            report.skipped.append({**entry, "reason": f"already imported ({target.skip_if.name})"})
            continue
        if target.destination.exists():
            if target.merge == "skip" or not target.destination.is_file():
                report.skipped.append({**entry, "reason": "destination exists"})
                continue
            try:
                added = _merge(target)
            except Exception as exc:  # noqa: BLE001 — a malformed file must not stop the run
                report.failed.append({**entry, "reason": f"merge failed: {exc}"})
                _log.warning("legacy migration: could not merge %s into %s: %s", target.source, target.destination, exc)
                continue
            if added:
                report.merged.append({**entry, "added": added})
                _log.info("legacy migration: %s merged %d item(s) from %s into %s", target.label, len(added), target.source, target.destination)
            else:
                report.skipped.append({**entry, "reason": "destination exists (nothing to merge)"})
            continue
        try:
            _copy(target, report)
        except OSError as exc:
            _discard_partial(target.destination)
            report.failed.append({**entry, "reason": f"copy failed: {exc}"})
            _log.warning("legacy migration: could not copy %s -> %s: %s", target.source, target.destination, exc)
            continue
        report.moved.append(entry)
        _log.info("legacy migration: %s copied %s -> %s", target.label, target.source, target.destination)

    report.ran_at = datetime.now(UTC).isoformat()
    if report.failed:
        _log.warning(
            "legacy migration: %d item(s) could not be copied; the run is not recorded and will be retried",
            len(report.failed),
        )
        return report
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    except OSError as exc:
        _log.warning("legacy migration: could not write marker %s: %s", marker, exc)
    return report
