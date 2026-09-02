"""Embedding client — local Ollama first, then any registered provider.

Default model is ``nomic-embed-text`` because it's already in the studio
pack catalog (so first-run users have it installed alongside their chat
model). The dimension is 768; we don't hard-code it because users can swap
the model via ``NVH_RAG_EMBED_MODEL``.

The embedder is intentionally tiny — it has one job: turn a list of strings
into a list of float vectors. If the default embedding model isn't pulled
yet, we auto-pull it on the first miss so first-time RAG users don't get a
dead-end "model not found" error.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from nvh.utils.ollama import ollama_base_url

logger = logging.getLogger(__name__)

# The embedder's historical override; wins over OLLAMA_BASE_URL / OLLAMA_HOST.
OLLAMA_URL_ENV = "OLLAMA_URL"
EMBED_MODEL_ENV = "NVH_RAG_EMBED_MODEL"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
AUTO_PULL_ENV = "NVH_RAG_AUTO_PULL"  # set to "0" to disable auto-pull

# Auto-pull can take a while on a slow connection (nomic-embed-text is ~270 MB).
# A generous ceiling keeps us from giving up on a real network that's just slow,
# while still hard-stopping in pathological cases.
_PULL_TIMEOUT_SECS = 600.0


class EmbeddingError(RuntimeError):
    """Raised when no embedding backend produced a vector."""


def _ollama_url() -> str:
    return ollama_base_url(os.environ.get(OLLAMA_URL_ENV))


def embed_model_name() -> str:
    return os.environ.get(EMBED_MODEL_ENV, DEFAULT_EMBED_MODEL)


def _auto_pull_enabled() -> bool:
    return os.environ.get(AUTO_PULL_ENV, "1").strip() not in ("0", "false", "no")


async def _pull_ollama_model(model: str, *, timeout: float = _PULL_TIMEOUT_SECS) -> bool:
    """Stream ``POST /api/pull`` until the model finishes downloading.

    Returns True if the pull completed, False if Ollama reported an error
    or never reached the "success" status. We're intentionally quiet about
    progress — the Wizard chat surface is text, and a progress bar would
    fight the chat stream. The user sees ingest just take longer the first
    time, then succeed.
    """
    url = f"{_ollama_url()}/api/pull"
    payload = {"name": model, "stream": True}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    logger.warning("auto-pull %s: status %s", model, resp.status_code)
                    return False
                last_status = ""
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "error" in data:
                        logger.warning("auto-pull %s reported error: %s", model, data["error"])
                        return False
                    last_status = data.get("status", last_status)
                # Ollama emits "success" as the final status on a clean pull.
                return last_status == "success"
    except httpx.HTTPError as exc:
        logger.warning("auto-pull %s failed: %s", model, exc)
        return False


async def embed_texts(texts: list[str], *, timeout: float = 30.0) -> list[list[float]]:
    """Return one embedding vector per input string via Ollama.

    If the configured embedding model isn't pulled and auto-pull is enabled
    (``NVH_RAG_AUTO_PULL`` != "0"), we trigger one pull and retry once.
    A "model not found" outcome after that retry surfaces as an
    ``EmbeddingError`` so the caller can show a real error to the user.
    """
    if not texts:
        return []

    model = embed_model_name()
    return await _embed_texts_with_retry(
        texts, model=model, timeout=timeout, allow_pull=_auto_pull_enabled(),
    )


async def _embed_texts_with_retry(
    texts: list[str],
    *,
    model: str,
    timeout: float,
    allow_pull: bool,
) -> list[list[float]]:
    url = f"{_ollama_url()}/api/embeddings"
    vectors: list[list[float]] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for text in texts:
                payload: dict[str, Any] = {"model": model, "prompt": text}
                resp = await client.post(url, json=payload)
                if resp.status_code == 404 and "not found" in resp.text.lower():
                    if allow_pull:
                        logger.info("auto-pulling embed model '%s' (first-use)", model)
                        ok = await _pull_ollama_model(model)
                        if ok:
                            # Retry the batch from scratch — we got 0 of N chunks
                            # done, so there's nothing partial to preserve.
                            return await _embed_texts_with_retry(
                                texts, model=model, timeout=timeout, allow_pull=False,
                            )
                    raise EmbeddingError(
                        f"Ollama doesn't have '{model}' yet. Run "
                        f"`ollama pull {model}` or set {EMBED_MODEL_ENV}.",
                    )
                resp.raise_for_status()
                data = resp.json()
                vec = data.get("embedding")
                if not isinstance(vec, list) or not vec:
                    raise EmbeddingError(f"Ollama returned no embedding for one chunk (model={model}).")
                vectors.append([float(x) for x in vec])
    except EmbeddingError:
        raise
    except httpx.ConnectError as exc:
        raise EmbeddingError(
            f"Can't reach Ollama at {_ollama_url()} — is the daemon running? ({exc})",
        ) from exc
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"Ollama embeddings call failed: {exc}") from exc
    return vectors


async def embed_one(text: str) -> list[float]:
    """Single-text convenience wrapper."""
    result = await embed_texts([text])
    if not result:
        raise EmbeddingError("Embedder returned no vectors.")
    return result[0]
