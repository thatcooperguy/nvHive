"""Structured logging for production and rootless desktop deployments."""

import contextvars
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "nvh_request_id",
    default=None,
)


def set_request_id(request_id: str | None) -> contextvars.Token[str | None]:
    """Bind a request id to logs emitted by the current context."""
    return _request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token[str | None]) -> None:
    """Restore the previous request id context."""
    _request_id_var.reset(token)


def get_request_id() -> str | None:
    """Return the request id bound to the current context, if any."""
    return _request_id_var.get()


class RequestContextFilter(logging.Filter):
    """Attach request context fields so formatters never KeyError."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id() or ""
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        # Add extra fields
        for key in (
            "request_id",
            "error_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "client",
            "job_id",
            "kind",
            "provider",
            "model",
            "latency_ms",
            "tokens",
            "cost",
        ):
            if hasattr(record, key):
                value = getattr(record, key)
                if value not in ("", None):
                    log_entry[key] = value
        return json.dumps(log_entry)


def _log_file_from_env() -> Path | None:
    explicit = os.environ.get("HIVE_LOG_FILE")
    if explicit:
        return Path(explicit).expanduser()
    logs_dir = os.environ.get("NVH_LOGS")
    if logs_dir:
        return Path(logs_dir).expanduser() / "nvhive.log"
    home = os.environ.get("NVH_HOME")
    if home:
        return Path(home).expanduser() / "logs" / "nvhive.log"
    return None


def _make_handler(json_format: bool, *, stream: bool) -> logging.Handler:
    handler: logging.Handler
    if stream:
        handler = logging.StreamHandler(sys.stdout)
    else:
        log_file = _log_file_from_env()
        if log_file is None:
            raise RuntimeError("No log file configured")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")

    handler.addFilter(RequestContextFilter())
    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s [%(request_id)s]: %(message)s"
            )
        )
    return handler


def setup_logging(level: str = "INFO", json_format: bool = False) -> logging.Logger:
    """Configure application logging."""
    root = logging.getLogger("nvh")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    handlers = [_make_handler(json_format, stream=True)]
    try:
        handlers.append(_make_handler(json_format, stream=False))
    except Exception:
        # File logging is best-effort until NVH_HOME/NVH_LOGS has been activated.
        pass

    # Avoid adding duplicate handlers on repeated calls
    root.handlers = handlers

    return root
