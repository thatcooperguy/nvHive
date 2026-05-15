"""Embedding client — local Ollama first, then any registered provider.

Default model is ``nomic-embed-text`` because it's already in the studio
pack catalog (so first-run users have it installed alongside their chat
model). The dimension is 768; we don't hard-code it because users can swap
the model via ``NVH_RAG_EMBED_MODEL``.

The embedder is intentionally tiny — it has one job: turn a list of strings
into a list of float vectors, or fail with a clear error so the caller can
surface a helpful "ollama pull nomic-embed-text" hint.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

OLLAMA_URL_ENV = "OLLAMA_URL"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL_ENV = "NVH_RAG_EMBED_MODEL"
DEFAULT_EMBED_MODEL = "nomic-embed-text"


class EmbeddingError(RuntimeError):
    """Raised when no embedding backend produced a vector."""


def _ollama_url() -> str:
    return os.environ.get(OLLAMA_URL_ENV, DEFAULT_OLLAMA_URL).rstrip("/")


def embed_model_name() -> str:
    return os.environ.get(EMBED_MODEL_ENV, DEFAULT_EMBED_MODEL)


async def embed_texts(texts: list[str], *, timeout: float = 30.0) -> list[list[float]]:
    """Return one embedding vector per input string via Ollama.

    Raises ``EmbeddingError`` with an actionable message if Ollama is
    unreachable or the model isn't pulled.
    """
    if not texts:
        return []

    model = embed_model_name()
    url = f"{_ollama_url()}/api/embeddings"
    vectors: list[list[float]] = []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for text in texts:
                payload: dict[str, Any] = {"model": model, "prompt": text}
                resp = await client.post(url, json=payload)
                if resp.status_code == 404 and "not found" in resp.text.lower():
                    raise EmbeddingError(
                        f"Ollama doesn't have '{model}' yet. Run "
                        f"`ollama pull {model}` or set {EMBED_MODEL_ENV}."
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
            f"Can't reach Ollama at {_ollama_url()} — is the daemon running? ({exc})"
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
