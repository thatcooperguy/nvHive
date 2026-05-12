"""Local model chat readiness checks for AI Wizard.

The setup wizard needs to distinguish "Ollama is installed" from "a local
model can actually answer." This module keeps that probe rootless, bounded,
and cacheable so the WebUI can be honest without hammering the model server.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import time
from typing import Any

import httpx

from nvh.integrations.storage import storage_layout

PREFERRED_CHAT_MODELS = (
    "gemma3:4b",
    "llama3.1:8b",
    "qwen3:8b",
    "qwen3-8b",
    "nemotron-mini",
)


def _checked_at() -> str:
    return datetime.now(UTC).isoformat()


def _base_url() -> str:
    return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")


def _state_path(home_dir: str | Path | None = None) -> Path:
    layout = storage_layout(home_dir)
    return layout.home / "state" / "local-chat-smoke.json"


def _read_cache(path: Path, max_age_s: int) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        checked_at = data.get("checked_at")
        if not checked_at:
            return None
        age = datetime.now(UTC) - datetime.fromisoformat(str(checked_at))
        if age.total_seconds() <= max_age_s:
            data["cached"] = True
            return data
    except Exception:
        return None
    return None


def _write_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        # A cache failure should never make setup look broken.
        return


def _pick_model(names: list[str]) -> str | None:
    if not names:
        return None
    for preferred in PREFERRED_CHAT_MODELS:
        preferred_base = preferred.split(":")[0]
        for name in names:
            if name == preferred or name.split(":")[0] == preferred_base:
                return name
    return names[0]


def _result(
    *,
    ready: bool,
    status: str,
    summary: str,
    model: str | None = None,
    error: str | None = None,
    output_chars: int = 0,
    latency_ms: int | None = None,
    next_action_id: str | None = None,
    checked_at: str | None = None,
) -> dict[str, Any]:
    return {
        "ready": ready,
        "status": status,
        "summary": summary,
        "provider": "ollama",
        "model": model,
        "output_chars": output_chars,
        "latency_ms": latency_ms,
        "error": error,
        "next_action_id": next_action_id,
        "checked_at": checked_at or _checked_at(),
        "cached": False,
        "rootless": True,
    }


def local_chat_smoke_status(
    home_dir: str | Path | None = None,
    *,
    force: bool = False,
    max_age_s: int = 120,
    timeout_s: float = 35.0,
) -> dict[str, Any]:
    """Return whether a local Ollama model can answer a tiny prompt.

    This is intentionally a real token-producing probe. It does not install,
    download, or mutate model state; it only reads /api/tags and asks one
    installed model for a tiny response. Results are cached under NVH_HOME.
    """
    state_file = _state_path(home_dir)
    if not force:
        cached = _read_cache(state_file, max_age_s=max_age_s)
        if cached is not None:
            return cached

    base_url = _base_url()
    start = time.monotonic()
    try:
        timeout = httpx.Timeout(timeout_s, connect=2.0, read=timeout_s, write=10.0, pool=5.0)
        with httpx.Client(timeout=timeout) as client:
            tags = client.get(f"{base_url}/api/tags")
            tags.raise_for_status()
            names = [
                str(item.get("name", "")).strip()
                for item in tags.json().get("models", [])
                if item.get("name")
            ]
            model = _pick_model(names)
            if not model:
                result = _result(
                    ready=False,
                    status="no-models",
                    summary="Ollama is online, but no local chat model is installed yet.",
                    next_action_id="starter-models",
                )
                _write_cache(state_file, result)
                return result

            response = client.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": "Reply with exactly: NVHIVE_READY",
                        }
                    ],
                    "stream": False,
                    "options": {
                        "temperature": 0,
                        "num_predict": 16,
                    },
                },
            )
            response.raise_for_status()
            data = response.json()
            text = str((data.get("message") or {}).get("content") or "").strip()
            latency_ms = int((time.monotonic() - start) * 1000)
            if not text:
                result = _result(
                    ready=False,
                    status="no-output",
                    summary=f"Ollama answered from {model}, but returned no text.",
                    model=model,
                    latency_ms=latency_ms,
                    next_action_id="starter-models",
                )
            else:
                result = _result(
                    ready=True,
                    status="ready",
                    summary=f"Local chat verified with {model}.",
                    model=model,
                    output_chars=len(text),
                    latency_ms=latency_ms,
                )
            _write_cache(state_file, result)
            return result
    except httpx.ConnectError as exc:
        result = _result(
            ready=False,
            status="runtime-offline",
            summary="Ollama is installed, but the local model server is not reachable.",
            error=str(exc),
            next_action_id="rootless-ollama",
        )
    except httpx.TimeoutException as exc:
        result = _result(
            ready=False,
            status="stalled",
            summary="Ollama is online, but the local model did not return text before the timeout.",
            error=str(exc),
            next_action_id="starter-models",
        )
    except Exception as exc:
        result = _result(
            ready=False,
            status="error",
            summary="Local chat response test failed.",
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
            next_action_id="starter-models",
        )

    _write_cache(state_file, result)
    return result
