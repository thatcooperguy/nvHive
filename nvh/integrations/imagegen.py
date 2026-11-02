"""Image generation for the Wizard and the profile portraits: one planner, two backends.

The Wizard's ``generate_image`` tool (nvh/integrations/wizard/tools.py) and the
Create-Agent "Generate Portrait" button (``comfyui.generate_portrait``, now a
shim over :func:`render_image`) both come through here, so the decision of
*which* backend renders and *what it costs* is made in one place:

1. ``NVAPI_KEY`` set → the NVIDIA-hosted endpoint (``NVH_NVIDIA_IMAGE_ENDPOINT``,
   default SDXL Turbo). Only that variable is read: a free-tier NIM chat key in
   either of the two chat-provider variables must not move a Spark owner onto
   the paid hosted path ahead of the ComfyUI they are running (design O2).
   ``cost_usd`` is ``None`` and ``cost_known`` is ``False``: there is no
   catalog row for the hosted image price and nothing here hand-types one.
2. else local ComfyUI at the host/port recorded when nvHive started it
   (``detect_comfyui()`` reads ``comfyui-service.json``), not the default-port
   probe the old portrait helper used. ``cost_usd`` is ``0`` and
   ``cost_known`` is ``True``.
3. else no backend: the plan says so before Run and :func:`render_image`
   raises :class:`ImageGenerationUnavailable` with the same words.

Ceiling rule: a finite ``cost_ceiling_usd`` never gets a backend whose price
is unknown, so under a ceiling the hosted path is skipped and ComfyUI is used,
or the request is refused with a sentence (:func:`ceiling_reason`) that names
both facts. The planner itself takes ``0`` as a ceiling ("no paid spend"); the
Wizard tool never passes one, because the profile field it reads treats ``0``
as "no limit" (``_image_profile_ceiling``), so a profile that wants no paid
spend sets a small positive ceiling.
``requested="local"`` forces ComfyUI; ``requested="nvidia"`` forces the hosted
path when the key exists and no ceiling applies. The model may ask for a
backend; the ceiling still wins.

Files land under ``storage_layout(home_dir).outputs_dir / "images" / <scope>``
as ``<UTC timestamp>-<8 hex><suffix>`` and are served by
``GET /v1/outputs/images/{scope}/{filename}`` (nvh/api/routes/outputs.py,
behind ``require_auth``). Nothing prunes that folder in 0.46.

Imports of :mod:`nvh.integrations.installs.comfyui` are lazy (that module
re-exports the two renderers from here), and all HTTP goes through
:func:`_http_client` so tests can hand in an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from nvh.integrations.workspace.storage import storage_layout

logger = logging.getLogger(__name__)

__all__ = [
    "BACKENDS",
    "DEFAULT_SCOPE",
    "FILENAME_RE",
    "IMAGE_MEDIA_TYPES",
    "ImageGenerationUnavailable",
    "ImageResult",
    "NO_BACKEND_REASON",
    "OUTPUTS_IMAGES_URL_PREFIX",
    "REQUESTED_BACKENDS",
    "RETENTION_NOTE",
    "SCOPE_RE",
    "ceiling_reason",
    "comfyui_checkpoint",
    "format_usd",
    "media_type_for",
    "nvidia_image_endpoint",
    "nvidia_image_model",
    "outputs_images_root",
    "plan_image_backend",
    "render_image",
    "resolve_output_image",
]

# ---------------------------------------------------------------------------
# Environment and constants
# ---------------------------------------------------------------------------

#: The only key the image path reads (O2): the two NIM chat-key variables are never consulted.
NVAPI_KEY_ENV = "NVAPI_KEY"
# NVIDIA-hosted image endpoints live under integrate.api.nvidia.com (or
# api.nvcf.nvidia.com depending on account tier). Both are env-driven so a
# user can swap to any image model they have access to without a code change.
NVIDIA_IMAGE_ENDPOINT_ENV = "NVH_NVIDIA_IMAGE_ENDPOINT"
NVIDIA_IMAGE_MODEL_ENV = "NVH_NVIDIA_IMAGE_MODEL"
#: Used when ``NVH_NVIDIA_IMAGE_ENDPOINT`` is unset. The plan's model name is
#: this URL's last path segment unless ``NVH_NVIDIA_IMAGE_MODEL`` names one.
DEFAULT_NVIDIA_IMAGE_ENDPOINT = "https://integrate.api.nvidia.com/v1/genai/stabilityai/sdxl-turbo"
COMFYUI_CHECKPOINT_ENV = "NVH_COMFYUI_CHECKPOINT"
DEFAULT_COMFYUI_CHECKPOINT = "sd_xl_base_1.0.safetensors"
# The spellings comfyui.py used before 0.46; it re-exports them from here.
_DEFAULT_NVIDIA_IMAGE_ENDPOINT = DEFAULT_NVIDIA_IMAGE_ENDPOINT
_DEFAULT_COMFYUI_CHECKPOINT = DEFAULT_COMFYUI_CHECKPOINT

BACKEND_NVIDIA = "nvidia"
BACKEND_COMFYUI = "comfyui"
#: What ``ImageResult.backend`` / ``plan["backend"]`` may hold (or ``None``).
BACKENDS: tuple[str, ...] = (BACKEND_NVIDIA, BACKEND_COMFYUI)
#: What a caller may ask for: ``auto`` (precedence above), ``local`` (ComfyUI
#: only), ``nvidia`` (hosted only, still subject to the ceiling rule).
REQUESTED_BACKENDS: tuple[str, ...] = ("auto", "local", "nvidia")

#: Where images land when the caller names no scope (a conversation id is the usual one).
DEFAULT_SCOPE = "wizard"
#: One folder level under ``outputs/images``: letters, digits, ``-``, ``_``; 1-64 chars.
SCOPE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: A file the route may serve: a safe stem plus exactly one short suffix.
FILENAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9]{1,5}$")
#: The suffixes the renderers write and the route serves, with their media types.
IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
OUTPUTS_IMAGES_URL_PREFIX = "/v1/outputs/images"

#: Appended to the card result's "saved to ..." line (design §3, risk 16).
RETENTION_NOTE = "nvHive does not prune this folder"

#: The plan's ``reason`` (and the exception text) when neither backend exists.
NO_BACKEND_REASON = (
    "No image generator available. Either:\n"
    "  • set NVAPI_KEY to use NVIDIA-hosted image generation,\n"
    "  • or start ComfyUI locally (Setup → ComfyUI) and re-try."
)
NO_BACKEND_SUMMARY = "no image backend · set NVAPI_KEY or start ComfyUI (Setup → ComfyUI)"
NVIDIA_KEY_MISSING_REASON = (
    "NVAPI_KEY is not set, so the NVIDIA-hosted image backend is unavailable; "
    "set it, or ask for the local backend."
)
#: The second half of :func:`ceiling_reason` when ComfyUI is down (design §3).
CEILING_NO_LOCAL_HINT = "start ComfyUI (Setup → ComfyUI) or run without a specialist"

#: Renders are written to ``<name>.tmp`` and ``os.replace``d into place.
_TMP_SUFFIX = ".tmp"


class ImageGenerationUnavailable(RuntimeError):  # noqa: N818 - the name is the 0.46 wave contract (C1)
    """No backend can render this request; the message is the plain-words reason."""


@dataclass
class ImageResult:
    """One rendered image on disk plus what rendered it and what it cost."""

    path: str
    suffix: str
    backend: str
    model: str
    width: int
    height: int
    cost_usd: float | None
    cost_known: bool
    latency_ms: int
    scope: str
    filename: str
    url: str
    #: Why this backend ran when it was not the first choice (the ceiling sentence).
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Environment readers
# ---------------------------------------------------------------------------


def _nvidia_key() -> str:
    return (os.environ.get(NVAPI_KEY_ENV) or "").strip()


def nvidia_image_endpoint() -> str:
    """The hosted endpoint URL (``NVH_NVIDIA_IMAGE_ENDPOINT`` or the SDXL Turbo default)."""
    return (os.environ.get(NVIDIA_IMAGE_ENDPOINT_ENV) or "").strip() or DEFAULT_NVIDIA_IMAGE_ENDPOINT


def nvidia_image_model() -> str:
    """The model name the plan shows: ``NVH_NVIDIA_IMAGE_MODEL`` or the endpoint's last path segment."""
    override = (os.environ.get(NVIDIA_IMAGE_MODEL_ENV) or "").strip()
    if override:
        return override
    tail = nvidia_image_endpoint().rstrip("/").rsplit("/", 1)[-1]
    return tail or "nvidia-hosted"


def comfyui_checkpoint() -> str:
    """The checkpoint the local workflow loads (``NVH_COMFYUI_CHECKPOINT`` or the SDXL base)."""
    return (os.environ.get(COMFYUI_CHECKPOINT_ENV) or "").strip() or DEFAULT_COMFYUI_CHECKPOINT


def format_usd(value: float) -> str:
    """``$0.05`` style: two decimals, more only when the value needs them."""
    text = f"{value:.2f}"
    if abs(float(text) - value) > 1e-9:
        text = f"{value:.4f}".rstrip("0")
    return f"${text}"


def ceiling_reason(cost_ceiling_usd: float, *, comfyui_reachable: bool) -> str:
    """The exact sentence a card shows when a profile ceiling skipped the hosted path.

    With ComfyUI reachable it ends "so nvHive used local ComfyUI"; otherwise it
    says so and adds :data:`CEILING_NO_LOCAL_HINT`.
    """
    head = (
        f"This profile has a {format_usd(cost_ceiling_usd)}/turn ceiling and the "
        "NVIDIA-hosted image price is not known, so nvHive "
    )
    if comfyui_reachable:
        return head + "used local ComfyUI"
    return head + f"skipped it, and local ComfyUI is not reachable — {CEILING_NO_LOCAL_HINT}."


def _effective_ceiling(value: Any) -> float | None:
    """A finite, non-negative ceiling, else ``None`` (no ceiling)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        ceiling = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(ceiling) or math.isinf(ceiling) or ceiling < 0:
        return None
    return ceiling


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def outputs_images_root(home_dir: str | Path | None = None) -> Path:
    """``storage_layout(home_dir).outputs_dir / "images"`` (``NVH_OUTPUTS`` honoured)."""
    return storage_layout(home_dir).outputs_dir / "images"


def media_type_for(filename: str) -> str | None:
    """The media type for a file the route may serve, or ``None`` for a suffix it never writes."""
    return IMAGE_MEDIA_TYPES.get(Path(filename).suffix.lower())


def resolve_output_image(
    scope: str,
    filename: str,
    *,
    home_dir: str | Path | None = None,
) -> Path | None:
    """The on-disk file for ``/v1/outputs/images/{scope}/{filename}``, or ``None``.

    ``None`` (the route's 404) for anything but a regex-clean scope and
    filename with a known image suffix whose resolved path is a regular file
    directly inside ``outputs/images/<scope>`` — ``..``, a foreign scope, an
    escaped symlink and a missing file all answer the same way, never a 500.
    """
    if not SCOPE_RE.fullmatch(scope or "") or not FILENAME_RE.fullmatch(filename or ""):
        return None
    if media_type_for(filename) is None:
        return None
    root = outputs_images_root(home_dir)
    try:
        root_resolved = root.resolve()
        candidate = (root / scope / filename).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate.parent != root_resolved / scope or candidate.name != filename:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _output_url(scope: str, filename: str) -> str:
    return f"{OUTPUTS_IMAGES_URL_PREFIX}/{scope}/{filename}"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _comfyui_status(home_dir: str | Path | None) -> dict[str, Any]:
    """``detect_comfyui()`` at the recorded runtime host/port; a failure counts as not running.

    Only a loopback host is ever used (``comfyui.LOOPBACK_HOSTS``): nvHive
    starts ComfyUI on localhost alone, so a recorded host that is not
    loopback — a ``comfyui-service.json`` planted under a request-supplied
    ``home_dir`` — never becomes the target of the prompt POST and the
    ``/view`` GET the renderer makes; it reads as "not running".
    """
    from nvh.integrations.installs import comfyui as _comfyui

    try:
        status = _comfyui.detect_comfyui(home_dir=home_dir)
    except Exception as exc:  # pragma: no cover - env-specific
        logger.debug("imagegen: detect_comfyui failed: %s", exc)
        status = {}
    host = status.get("host") if isinstance(status.get("host"), str) else _comfyui.DEFAULT_HOST
    port = status.get("port") if isinstance(status.get("port"), int) else _comfyui.DEFAULT_PORT
    running = bool(status.get("running"))
    if host not in _comfyui.LOOPBACK_HOSTS:
        logger.warning("imagegen: recorded ComfyUI host %r is not loopback; not using it", host)
        running = False
    return {"running": running, "host": host, "port": port}


def _plan(
    backend: str | None,
    *,
    requested: str,
    cost_ceiling_usd: float | None,
    reason: str = "",
    model: str = "",
    cost_usd: float | None = None,
    cost_known: bool = False,
    host: str | None = None,
    port: int | None = None,
    summary: str = "",
    paid_skipped: bool = False,
) -> dict[str, Any]:
    return {
        "backend": backend,
        "reason": reason,
        "model": model,
        "cost_usd": cost_usd,
        "cost_known": cost_known,
        "host": host,
        "port": port,
        "summary": summary,
        "requested": requested,
        "cost_ceiling_usd": cost_ceiling_usd,
        "paid_skipped": paid_skipped,
    }


def _nvidia_plan(*, requested: str, cost_ceiling_usd: float | None) -> dict[str, Any]:
    model = nvidia_image_model()
    return _plan(
        BACKEND_NVIDIA,
        requested=requested,
        cost_ceiling_usd=cost_ceiling_usd,
        model=model,
        cost_usd=None,
        cost_known=False,
        summary=f"NVIDIA-hosted ({model}) · cost unknown · billed to your NVIDIA account",
    )


def _comfyui_plan(
    status: dict[str, Any],
    *,
    requested: str,
    cost_ceiling_usd: float | None,
    reason: str = "",
    paid_skipped: bool = False,
) -> dict[str, Any]:
    host, port = status["host"], status["port"]
    return _plan(
        BACKEND_COMFYUI,
        requested=requested,
        cost_ceiling_usd=cost_ceiling_usd,
        reason=reason,
        model=comfyui_checkpoint(),
        cost_usd=0.0,
        cost_known=True,
        host=host,
        port=port,
        summary=f"local ComfyUI on {host}:{port} · $0 · uses GPU memory while it runs",
        paid_skipped=paid_skipped,
    )


def plan_image_backend(
    *,
    home_dir: str | Path | None = None,
    cost_ceiling_usd: float | None = None,
    requested: str = "auto",
) -> dict[str, Any]:
    """Decide the backend for one request without rendering anything.

    Keys: ``backend`` (``"nvidia"`` | ``"comfyui"`` | ``None``), ``reason`` (the
    sentence the card shows when ``backend`` is ``None`` or when the paid path
    was skipped), ``model``, ``cost_usd``, ``cost_known``, ``host``, ``port``,
    ``summary`` (the card's plan line), plus ``requested``, ``cost_ceiling_usd``
    (normalised) and ``paid_skipped``. See the module docstring for the rules.
    """
    want = str(requested or "auto").strip().lower()
    ceiling = _effective_ceiling(cost_ceiling_usd)
    key = _nvidia_key()
    common = {"requested": want, "cost_ceiling_usd": ceiling}

    if want not in REQUESTED_BACKENDS:
        return _plan(
            None,
            reason=f"Unknown image backend {requested!r}; use auto, local or nvidia.",
            summary=f"unknown backend {requested!r}",
            **common,
        )

    if want == "local":
        status = _comfyui_status(home_dir)
        if status["running"]:
            return _comfyui_plan(status, **common)
        where = f"{status['host']}:{status['port']}"
        return _plan(
            None,
            reason=f"ComfyUI is not reachable at {where}; start ComfyUI (Setup → ComfyUI) and re-try.",
            summary=f"ComfyUI not reachable on {where} · start it (Setup → ComfyUI)",
            host=status["host"],
            port=status["port"],
            **common,
        )

    if want == "nvidia" and not key:
        return _plan(
            None,
            reason=NVIDIA_KEY_MISSING_REASON,
            summary="NVIDIA-hosted backend unavailable · NVAPI_KEY is not set",
            **common,
        )

    if key:
        if ceiling is None:
            return _nvidia_plan(**common)
        # The hosted price is unknown and this profile has a ceiling: never
        # the paid path. ComfyUI if it is up, else a refusal that says why.
        status = _comfyui_status(home_dir)
        if status["running"]:
            return _comfyui_plan(
                status,
                reason=ceiling_reason(ceiling, comfyui_reachable=True),
                paid_skipped=True,
                **common,
            )
        return _plan(
            None,
            reason=ceiling_reason(ceiling, comfyui_reachable=False),
            summary=(
                f"no backend within this profile's {format_usd(ceiling)}/turn ceiling · "
                f"{CEILING_NO_LOCAL_HINT}"
            ),
            host=status["host"],
            port=status["port"],
            paid_skipped=True,
            **common,
        )

    status = _comfyui_status(home_dir)
    if status["running"]:
        return _comfyui_plan(status, **common)
    return _plan(
        None,
        reason=NO_BACKEND_REASON,
        summary=NO_BACKEND_SUMMARY,
        host=status["host"],
        port=status["port"],
        **common,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _http_client(timeout: float) -> httpx.AsyncClient:
    """The client every render uses; tests replace it with a ``MockTransport`` one."""
    return httpx.AsyncClient(timeout=timeout)


def _validate_dimension(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number of pixels, got {value!r}") from None
    if not 64 <= value <= 4096:
        raise ValueError(f"{name} must be between 64 and 4096 pixels, got {value}")
    return value


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S")


async def render_image(
    prompt: str,
    *,
    home_dir: str | Path | None = None,
    width: int = 1024,
    height: int = 1024,
    backend: str = "auto",
    cost_ceiling_usd: float | None = None,
    scope: str = DEFAULT_SCOPE,
    timeout: float = 180.0,
) -> ImageResult:
    """Plan, render and save one image; raise :class:`ImageGenerationUnavailable` when no backend fits.

    ``scope`` is the folder under ``outputs/images`` (a conversation id, or
    :data:`DEFAULT_SCOPE`) and must match :data:`SCOPE_RE`; ``width`` /
    ``height`` are pixels in 64..4096. Backend failures (HTTP errors, a
    workflow that produced nothing) raise ``RuntimeError`` from the renderer
    with the upstream status so the caller's card can show it.
    """
    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt is required")
    if not SCOPE_RE.fullmatch(scope or ""):
        raise ValueError(
            f"invalid image scope {scope!r}: letters, digits, '-' and '_' only (1-64 characters)"
        )
    width = _validate_dimension("width", width)
    height = _validate_dimension("height", height)

    plan = plan_image_backend(home_dir=home_dir, cost_ceiling_usd=cost_ceiling_usd, requested=backend)
    chosen = plan["backend"]
    if chosen is None:
        raise ImageGenerationUnavailable(plan["reason"])

    started = time.monotonic()
    if chosen == BACKEND_NVIDIA:
        data, suffix = await _generate_portrait_nvidia(
            text, _nvidia_key(), width=width, height=height, timeout=timeout,
        )
    else:
        data, suffix = await _generate_portrait_comfyui(
            text, width=width, height=height, timeout=timeout, host=plan["host"], port=plan["port"],
        )
    latency_ms = int((time.monotonic() - started) * 1000)

    target_dir = outputs_images_root(home_dir) / scope
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_timestamp()}-{uuid.uuid4().hex[:8]}{suffix}"
    path = target_dir / filename
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    tmp.write_bytes(data)
    os.replace(tmp, path)

    return ImageResult(
        path=str(path),
        suffix=suffix,
        backend=chosen,
        model=plan["model"],
        width=width,
        height=height,
        cost_usd=plan["cost_usd"],
        cost_known=bool(plan["cost_known"]),
        latency_ms=latency_ms,
        scope=scope,
        filename=filename,
        url=_output_url(scope, filename),
        reason=plan["reason"],
    )


# ---------------------------------------------------------------------------
# Backends (moved from nvh/integrations/installs/comfyui.py in 0.46; it re-exports them)
# ---------------------------------------------------------------------------


def _comfyui_portrait_workflow(
    prompt: str,
    *,
    checkpoint: str,
    width: int,
    height: int,
    seed: int,
) -> dict[str, Any]:
    """Return a minimal SDXL ComfyUI workflow as a JSON-serializable dict.

    Five nodes — checkpoint, latent, positive prompt, negative prompt,
    sampler — plus VAE decode and SaveImage. Matches the default
    SDXL workflow ComfyUI ships in its examples, just trimmed.
    """
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "2": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": (
                    "blurry, lowres, text, watermark, signature, frame, "
                    "deformed, extra limbs, jpeg artifacts"
                ),
                "clip": ["1", 1],
            },
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["3", 0],
                "negative": ["4", 0],
                "latent_image": ["2", 0],
                "seed": seed,
                "steps": 28,
                "cfg": 6.5,
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "denoise": 1.0,
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": "nvh_portrait"},
        },
    }


async def _generate_portrait_comfyui(
    prompt: str,
    *,
    width: int,
    height: int,
    timeout: float,
    host: str | None = None,
    port: int | None = None,
) -> tuple[bytes, str]:
    """Submit the workflow to ComfyUI's HTTP API, poll until complete, fetch
    the image bytes via /view. Returns ``(image_bytes, suffix)``.

    ``host`` / ``port`` default to ComfyUI's stock ``127.0.0.1:8188``; the
    planner passes the recorded runtime pair instead. Polls
    ``/history/{prompt_id}`` at 1 Hz with a hard ``timeout`` ceiling. On any
    error we raise with the upstream status/message so the calling endpoint
    can show a clean inline error.
    """
    from nvh.integrations.installs import comfyui as _comfyui

    base = _comfyui._status_url(
        host or _comfyui.DEFAULT_HOST, port if port is not None else _comfyui.DEFAULT_PORT,
    )
    checkpoint = comfyui_checkpoint()
    seed = secrets.randbits(63)
    workflow = _comfyui_portrait_workflow(
        prompt, checkpoint=checkpoint, width=width, height=height, seed=seed,
    )
    client_id = f"nvh-{secrets.token_hex(4)}"

    async with _http_client(timeout) as client:
        # 1. Submit the prompt — ComfyUI returns a prompt_id we poll on.
        submit = await client.post(
            f"{base}/prompt",
            content=json.dumps({"prompt": workflow, "client_id": client_id}),
            headers={"Content-Type": "application/json"},
        )
        if submit.status_code >= 400:
            raise RuntimeError(f"ComfyUI submit failed: HTTP {submit.status_code} — {submit.text[:200]}")
        sub_data = submit.json()
        prompt_id = sub_data.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI did not return a prompt_id: {sub_data}")

        # 2. Poll /history/<id> until the workflow produces an output image.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        image_info: dict[str, Any] | None = None
        while loop.time() < deadline:
            hist = await client.get(f"{base}/history/{prompt_id}")
            if hist.status_code == 200:
                payload = hist.json()
                entry = payload.get(prompt_id) or {}
                outputs = entry.get("outputs") or {}
                # Find the first node that produced an image.
                for _node_id, node_out in outputs.items():
                    imgs = node_out.get("images") or []
                    if imgs:
                        image_info = imgs[0]
                        break
                if image_info is not None:
                    break
            await asyncio.sleep(1.0)
        if image_info is None:
            raise RuntimeError(f"ComfyUI workflow {prompt_id} produced no image within {int(timeout)}s")

        # 3. Fetch the raw image bytes via /view.
        params = {
            "filename": image_info.get("filename", ""),
            "subfolder": image_info.get("subfolder", ""),
            "type": image_info.get("type", "output"),
        }
        view = await client.get(f"{base}/view", params=params)
        if view.status_code >= 400:
            raise RuntimeError(f"ComfyUI /view failed: HTTP {view.status_code}")
        # ComfyUI typically returns PNG; the filename suffix backs that up.
        suffix = ".png"
        fname = image_info.get("filename") or ""
        if isinstance(fname, str) and "." in fname:
            ext = fname.rsplit(".", 1)[-1].lower()
            if ext in ("png", "jpg", "jpeg", "webp"):
                suffix = f".{'jpg' if ext == 'jpeg' else ext}"
        return view.content, suffix


async def _generate_portrait_nvidia(
    prompt: str,
    api_key: str,
    *,
    width: int,
    height: int,
    timeout: float,
) -> tuple[bytes, str]:
    """Call the NVIDIA-hosted image API and return ``(png_bytes, ".png")``.

    Tolerates the four common response shapes (raw base64 ``image``,
    Stability-style ``artifacts[0].base64``, OpenAI-style ``data[0].b64_json``
    and ``data[0].url``). Raises with the upstream status/body on failure so
    the caller's envelope surfaces it to the user.
    """
    endpoint = nvidia_image_endpoint()
    payload: dict[str, Any] = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "samples": 1,
        "steps": 30,
    }
    model_override = (os.environ.get(NVIDIA_IMAGE_MODEL_ENV) or "").strip()
    if model_override:
        payload["model"] = model_override
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with _http_client(timeout) as client:
        resp = await client.post(endpoint, headers=headers, content=json.dumps(payload))
        if resp.status_code >= 400:
            raise RuntimeError(f"NVIDIA image API {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        b64: str | None = None
        if isinstance(data.get("image"), str):
            b64 = data["image"]
        elif isinstance(data.get("artifacts"), list) and data["artifacts"]:
            first = data["artifacts"][0]
            if isinstance(first, dict):
                b64 = first.get("base64")
        elif isinstance(data.get("data"), list) and data["data"]:
            entry = data["data"][0]
            if isinstance(entry, dict):
                if isinstance(entry.get("b64_json"), str):
                    b64 = entry["b64_json"]
                elif isinstance(entry.get("url"), str):
                    url_resp = await client.get(entry["url"])
                    url_resp.raise_for_status()
                    return url_resp.content, ".png"
        if not b64:
            raise RuntimeError(
                f"NVIDIA image response shape not recognized: keys={list(data.keys())[:8]}",
            )
        return base64.b64decode(b64), ".png"
