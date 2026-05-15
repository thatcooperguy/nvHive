"""Opt-in, local-only install telemetry.

Three product-health events get appended to ``$NVH_HOME/telemetry/events.jsonl``
when telemetry is enabled:

* ``install_completed``       — first run of ``nvh init`` / setup wizard.
* ``first_wizard_turn``       — first successful end-to-end Wizard reply.
* ``reconnect_survived``      — workspace resumed after a disconnect.

This module **never sends data over the network**. The file is a breadcrumb
trail the user can read, redact, or send to support manually. A future PR may
add an opt-in HTTPS shipper that reads this same file; until then, this is
purely local.

Enabling / disabling:

* Default: **off**. Set ``NVH_TELEMETRY=1`` in the environment, or call
  :func:`set_enabled` to flip the flag in ``$NVH_HOME/telemetry/config.json``.
* When disabled, :func:`emit` is a no-op and returns ``None`` immediately.

Privacy:

* No prompts, no completions, no file contents, no API keys.
* Anonymous ``install_id`` (UUID4) generated on first emit and cached.
* All values are coerced through :func:`_redact` which drops obvious secrets.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


VALID_EVENTS = frozenset({
    "install_completed",
    "first_wizard_turn",
    "reconnect_survived",
})

_TELEMETRY_ENV = "NVH_TELEMETRY"
_INSTALL_ID_FILE = "install_id"
_CONFIG_FILE = "config.json"
_EVENTS_FILE = "events.jsonl"

_REDACT_KEYS = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|bearer|authorization)",
)

_lock = threading.Lock()


def _telemetry_dir(home_dir: str | Path | None = None) -> Path:
    """Return ``$NVH_HOME/telemetry``, lazily resolving via the storage layer."""
    # Import here so test code can monkeypatch ``nvh_home`` and so this module
    # stays importable in environments where ``nvh.integrations`` is missing
    # extras (it shouldn't, but the cost of the late import is negligible).
    from nvh.integrations.workspace.storage import nvh_home

    home, _ = nvh_home(home_dir)
    return Path(home) / "telemetry"


def is_enabled(home_dir: str | Path | None = None) -> bool:
    """Telemetry is enabled iff the env var is truthy OR the config flag is set."""
    env_val = os.environ.get(_TELEMETRY_ENV, "").strip().lower()
    if env_val in {"1", "true", "yes", "on"}:
        return True
    if env_val in {"0", "false", "no", "off"}:
        return False
    config = _read_config(home_dir)
    return bool(config.get("enabled", False))


def set_enabled(value: bool, home_dir: str | Path | None = None) -> Path:
    """Persist the opt-in flag and return the config path written.

    The env var still wins if set — :func:`is_enabled` checks it first.
    """
    config = _read_config(home_dir)
    config["enabled"] = bool(value)
    config["updated_at"] = _now()
    return _write_config(config, home_dir)


def install_id(home_dir: str | Path | None = None) -> str:
    """Return a stable anonymous UUID4 install identifier.

    Generated and persisted on first call. Same file is read on every
    subsequent call so the value is stable across runs.
    """
    path = _telemetry_dir(home_dir) / _INSTALL_ID_FILE
    with _lock:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        new_id = str(uuid.uuid4())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id + "\n", encoding="utf-8")
        return new_id


def emit(
    event: str,
    properties: dict[str, Any] | None = None,
    *,
    home_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Append a single event to the local JSONL log.

    Returns the written record, or ``None`` if telemetry is disabled or the
    event name is not in :data:`VALID_EVENTS`. **Never raises** — any IO or
    serialization error is logged at debug level and swallowed, because
    telemetry must not destabilize the host product.
    """
    if event not in VALID_EVENTS:
        logger.debug("telemetry: rejecting unknown event %r", event)
        return None
    if not is_enabled(home_dir):
        return None
    try:
        record = {
            "event": event,
            "ts": _now(),
            "install_id": install_id(home_dir),
            "nvh_version": _safe_version(),
            "properties": _redact(properties or {}),
        }
        path = _telemetry_dir(home_dir) / _EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, sort_keys=True, ensure_ascii=False)
        with _lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return record
    except Exception:  # noqa: BLE001 — telemetry must never break the caller
        logger.debug("telemetry: emit failed for %r", event, exc_info=True)
        return None


def read_events(
    home_dir: str | Path | None = None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return parsed JSONL events (best-effort; bad lines are skipped)."""
    path = _telemetry_dir(home_dir) / _EVENTS_FILE
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if limit is not None and limit >= 0:
        out = out[-limit:]
    return out


def summary(home_dir: str | Path | None = None) -> dict[str, Any]:
    """High-level event counts for the diagnostic bundle / selfcheck report."""
    events = read_events(home_dir)
    counts: dict[str, int] = dict.fromkeys(VALID_EVENTS, 0)
    for ev in events:
        name = ev.get("event")
        if name in counts:
            counts[name] += 1
    return {
        "enabled": is_enabled(home_dir),
        "events_total": len(events),
        "events_by_name": counts,
        "first_seen": events[0]["ts"] if events else None,
        "last_seen": events[-1]["ts"] if events else None,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_version() -> str:
    try:
        from nvh import __version__
        return str(__version__)
    except Exception:
        return "unknown"


def _read_config(home_dir: str | Path | None = None) -> dict[str, Any]:
    path = _telemetry_dir(home_dir) / _CONFIG_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_config(config: dict[str, Any], home_dir: str | Path | None = None) -> Path:
    path = _telemetry_dir(home_dir) / _CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _redact(value: Any) -> Any:
    """Recursively drop obvious secret-shaped key/value pairs."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _REDACT_KEYS.search(k):
                out[k] = "[redacted]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
