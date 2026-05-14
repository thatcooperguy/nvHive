"""Local model chat readiness checks for AI Wizard.

The setup wizard needs to distinguish "Ollama is installed" from "a local
model can actually answer." This module keeps that probe rootless, bounded,
and cacheable so the WebUI can be honest without hammering the model server.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from nvh.integrations.storage import storage_layout

PREFERRED_CHAT_MODELS = (
    "nemotron-3-nano-omni",
    "nemotron-omni",
    "nemotron",
    "llama3.3:70b",
    "qwen2.5-coder:32b",
    "llama3.2-vision",
    "qwen3:8b",
    "deepseek-r1:8b",
    "qwen2.5-coder:7b",
    "llama3.1:8b",
    "gemma3:4b",
    "llava:7b",
    "minicpm-v",
    "moondream",
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


def _rank_models(names: list[str]) -> list[str]:
    if not names:
        return []
    ranked: list[str] = []
    remaining = list(names)
    for preferred in PREFERRED_CHAT_MODELS:
        preferred_base = preferred.split(":")[0]
        for name in names:
            if name == preferred or name.split(":")[0] == preferred_base:
                if name not in ranked:
                    ranked.append(name)
    for name in remaining:
        if name not in ranked:
            ranked.append(name)
    return ranked


def _pick_model(names: list[str]) -> str | None:
    ranked = _rank_models(names)
    return ranked[0] if ranked else None


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
    attempted_models: list[str] | None = None,
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
        "attempted_models": attempted_models or [],
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
            ranked_models = _rank_models(names)
            if not ranked_models:
                result = _result(
                    ready=False,
                    status="no-models",
                    summary="Ollama is online, but no local chat model is installed yet.",
                    next_action_id="starter-models",
                )
                _write_cache(state_file, result)
                return result

            attempted: list[str] = []
            last_error: str | None = None
            for model in ranked_models:
                attempted.append(model)
                try:
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
                    if text:
                        result = _result(
                            ready=True,
                            status="ready",
                            summary=f"Local chat verified with {model}.",
                            model=model,
                            output_chars=len(text),
                            latency_ms=latency_ms,
                            attempted_models=attempted,
                        )
                        _write_cache(state_file, result)
                        return result
                    last_error = f"{model} returned no text"
                except Exception as exc:
                    last_error = f"{model}: {type(exc).__name__}: {str(exc)[:180]}"
                    continue

            latency_ms = int((time.monotonic() - start) * 1000)
            result = _result(
                ready=False,
                status="no-output",
                summary=(
                    "Ollama is online, but none of the installed local chat models "
                    "returned text before fallback was exhausted."
                ),
                model=attempted[-1] if attempted else None,
                error=last_error,
                latency_ms=latency_ms,
                next_action_id="starter-models",
                attempted_models=attempted,
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
