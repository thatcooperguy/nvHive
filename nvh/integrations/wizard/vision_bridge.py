"""The Wizard's eyes — ``analyze_image`` / ``read_text_from_image`` behind a path allowlist.

Phase 3 of the Spark concierge (docs/proposals/SPARK_CONCIERGE_2026-09.md):
the single Wizard chat can read images. The two tools are a *narrow* bridge
to :mod:`nvh.core.vision_tools` — the same providers the agentic tool
registry has used since 0.30 — registered on the ``WizardToolRegistry`` as
``auto`` (no card: they read a file and answer a question, they change
nothing). Because there is no card, every decision about what may be read
and where the bytes may go is made here, in code, before anything is read.

What the bridge adds is what the core tool never had:

1. A **path allowlist**. The core ``analyze_image`` base64-encodes *any*
   readable path and may ship it to a cloud vision API, so a model that is
   asked "what's in ~/.ssh/id_rsa.png" would happily oblige. Here a path is
   accepted only when, after resolving symlinks, it sits under one of

     - ``$NVH_HOME/rag/uploads/`` — where ``/v1/rag/upload-ingest`` lands
       files and where the Wizard chat lands its own image attachments
       (``rag/uploads/wizard/<conversation id>/``);
     - the agent workspace (``NVH_PROJECTS``, default ``$NVH_HOME/projects``
       — :func:`nvh.integrations.wizard.sandbox_tools.workspace_dir`, the
       same directory ``shell`` mounts);
     - the screenshot temp dir (``tempfile.gettempdir()``), *direct children
       only* — the core ``screenshot`` tool writes ``tempfile.mktemp(suffix=
       ".png")`` there and nothing else of ours does.

   The check reuses :func:`nvh.core.agent_guardrails.check_path` (workspace
   boundary + system directory blocklist) per root, and the *resolved* path
   is what gets read, so the check and the read cannot follow two different
   symlink targets.
2. An **image-only rule**. Containment alone would still let a prompt
   injection name ``projects/app/.env`` or an ingested ``contract.pdf``. So
   the basename must not be one of :data:`nvh.core.agent_guardrails.BLOCKED_FILES`
   (``check_file_read``), the suffix must be one of :data:`IMAGE_SUFFIXES`,
   and — when the file exists — its first bytes must carry a PNG / JPEG /
   GIF / WEBP / BMP signature (:func:`sniff_image_suffix`). A missing file is
   admitted so the answer can say "Image not found"; nothing is read.
3. A **cloud rule**. Only an image the user attached in this chat (a path
   under ``rag/uploads/wizard/``) may fall back to the configured cloud
   vision API; every other allowlisted path is analysed by a local Ollama
   vision model or not at all. Without a local model such a path is refused
   in band (``local_only: True``) with the ``ollama pull`` hint — the bytes
   never leave the machine on the model's own initiative.

Anything refused is an in-band ``{ok: False, refused: True, error}`` naming
the rule, and no provider is called.

The provider calls themselves (``_detect_ollama_vision_model``,
``_analyze_with_ollama``, ``_analyze_with_cloud``, ``_suggested_vision_pull``)
stay single-sourced in :mod:`nvh.core.vision_tools` and are looked up on the
module at call time (tests patch them there). The core ``analyze_image``
closure is *not* borrowed: it decides local-vs-cloud internally and cannot
express rule 3, so the local-then-cloud sequence lives here with the rule
applied.

Result shape (both tools): ``{ok, text, model, provider, bytes, image_path}``
plus ``error`` when ``ok`` is False. ``text`` keeps the core tool's
``[Vision: <model>, N KB]`` first line so the trace can show local-vs-cloud
at a glance; ``model`` / ``provider`` are parsed off it (``provider`` is
``"ollama"`` for an installed tag, ``"cloud"`` for the litellm fallback).
``bytes`` is the file size. The 20 MB cap is reported as a not-ok result,
never an exception. Secrets are redacted from ``text`` before anything is cut
downstream.

Registration: ``register_wizard_tools(reg)`` — ``tools.default_registry()``
calls it after the sandbox bridge and before the entry-point / plugin passes.
Tests build a fresh ``WizardToolRegistry`` and call it themselves.
"""

from __future__ import annotations

import base64
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from nvh.integrations.wizard.sandbox_tools import workspace_dir

logger = logging.getLogger(__name__)

#: The two tool names this module registers (auto-class, allowlisted).
WIZARD_VISION_TOOLS: tuple[str, ...] = ("analyze_image", "read_text_from_image")

#: The core tool's own ceiling, stated here so the catalogue, the chat
#: attachment cap and the docs name the same number.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: The label the ``[Vision: …]`` prefix carries when the litellm fallback answered.
CLOUD_MODEL_LABEL = "cloud"

#: The image kinds the Wizard reads, by declared mime → the honest suffix.
#: Shared with ``/v1/wizard/chat``'s attachment landing, so the API accepts
#: exactly what the tools can name a mime for.
IMAGE_MIME_SUFFIXES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
#: Suffix → the mime a cloud provider is told (the encoding must be honest).
SUFFIX_MIMES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
IMAGE_SUFFIXES: frozenset[str] = frozenset(SUFFIX_MIMES)
IMAGE_KINDS_TEXT = "png, jpeg, gif, webp or bmp"

#: ``[Vision: qwen3-vl:4b, 12.3 KB]`` — the first line of a successful analysis.
_VISION_PREFIX_RE = re.compile(r"^\[Vision:\s*(?P<model>[^,\]]+),\s*(?P<kb>[\d.]+)\s*KB\]")
_NO_MODEL_PREFIX = "[Image loaded:"

_DEFAULT_QUESTION = "Describe what you see in this image."
#: The core ``read_text_from_image`` prompt, verbatim.
OCR_QUESTION = (
    "Read ALL visible text from this image. Return the text exactly "
    "as it appears, preserving formatting and line breaks."
)


# ---------------------------------------------------------------------------
# Image signatures
# ---------------------------------------------------------------------------


def sniff_image_suffix(head: bytes) -> str | None:
    """The suffix for the image signature ``head`` starts with, or ``None``.

    PNG, JPEG, GIF (87a/89a), WEBP (RIFF + WEBP) and BMP — the five kinds
    the tools and the chat attachments accept. Twelve bytes are enough.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"BM"):
        return ".bmp"
    return None


# ---------------------------------------------------------------------------
# Allowed roots
# ---------------------------------------------------------------------------


def uploads_root(home_dir: str | Path | None = None) -> Path:
    """``$NVH_HOME/rag/uploads`` — RAG uploads and, under ``wizard/``, chat attachments."""
    from nvh.integrations.workspace.storage import nvh_home

    home, _ = nvh_home(home_dir)
    return home / "rag" / "uploads"


def wizard_uploads_root(home_dir: str | Path | None = None) -> Path:
    """Where ``/v1/wizard/chat`` lands this turn's image attachments — the only
    root whose images may fall back to a cloud vision API."""
    return uploads_root(home_dir) / "wizard"


def _screenshot_dir() -> Path:
    """Where the core ``screenshot`` tool writes (``tempfile.mktemp(suffix=".png")``)."""
    return Path(tempfile.gettempdir())


def allowed_image_roots() -> list[Path]:
    """The resolved directories an image may be read from, in allowlist order.

    A root whose lookup fails (no home, broken env) is skipped, never
    substituted; duplicates (workspace inside the home) collapse.
    """
    roots: list[Path] = []
    for lookup in (uploads_root, workspace_dir, _screenshot_dir):
        try:
            root = Path(lookup()).expanduser().resolve()
        except Exception as exc:  # pragma: no cover - defensive, env-specific
            logger.debug("vision bridge: root %s unavailable: %s", lookup.__name__, exc)
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _refusal(error: str, roots: list[Path] | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "refused": True,
        "error": error,
        "allowed_roots": [str(r) for r in (roots if roots is not None else allowed_image_roots())],
        **extra,
    }


def _contained(resolved: Path, roots: list[Path]) -> bool:
    """Is ``resolved`` under one of ``roots`` (``check_path`` rules; temp root: direct children only)?"""
    from nvh.core.agent_guardrails import GuardrailError, check_path

    try:
        temp_root = _screenshot_dir().resolve()
    except Exception:  # pragma: no cover - defensive
        temp_root = None
    for root in roots:
        try:
            check_path(str(resolved), root)
        except GuardrailError:
            continue
        if root == temp_root and resolved.parent != root:
            # The screenshot tool writes straight into the temp dir; a
            # subfolder there belongs to some other program.
            continue
        return True
    return False


def admit_image_path(image_path: Any) -> tuple[Path | None, dict[str, Any] | None]:
    """``(resolved path, None)`` when ``image_path`` may be read; ``(None, refusal)`` otherwise.

    Symlinks are resolved *before* every check and the resolved path is what
    the caller must read. Refused, in order: an empty or unresolvable path;
    a path outside the roots (or a temp-dir subfolder); a basename on the
    guardrails' ``BLOCKED_FILES`` list; a suffix that is not an image's; an
    existing path that is not a regular file or whose first bytes are not a
    png/jpeg/gif/webp/bmp signature. A missing file is admitted (the answer
    says so; nothing is read).
    """
    from nvh.core.agent_guardrails import GuardrailError, check_file_read

    text = str(image_path or "").strip()
    roots = allowed_image_roots()
    if not text:
        return None, _refusal("image_path is required", roots)
    try:
        resolved = Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return None, _refusal(f"invalid image_path {text!r}: {exc}", roots)
    if not _contained(resolved, roots):
        listed = ", ".join(str(r) for r in roots) or "(none configured)"
        return None, _refusal(
            f"refused: {text} is outside the folders the Wizard may read images from. "
            f"Allowed roots: {listed} (the temp folder only directly, where screenshots land). "
            "Attach the image in the chat (it lands under the upload folder) or copy it into "
            "the workspace first.",
            roots,
        )
    try:
        check_file_read(resolved.name)
    except GuardrailError as exc:
        first = str(exc).splitlines()[0] if str(exc) else "BLOCKED: sensitive file"
        return None, _refusal(f"refused: {first} ({resolved.name} may hold secrets and is never read)", roots)
    if resolved.suffix.lower() not in IMAGE_SUFFIXES:
        return None, _refusal(
            f"refused: {text} is not an image file ({IMAGE_KINDS_TEXT} by extension); "
            "the vision tools read images only.",
            roots,
        )
    if resolved.exists():
        if not resolved.is_file():
            return None, _refusal(f"refused: {text} is not a regular file", roots)
        try:
            with resolved.open("rb") as fh:
                head = fh.read(16)
        except OSError as exc:
            return None, _refusal(f"refused: {text} could not be opened ({exc})", roots)
        if sniff_image_suffix(head) is None:
            return None, _refusal(
                f"refused: {text} does not carry a {IMAGE_KINDS_TEXT} signature — "
                "not an image, whatever its name says.",
                roots,
            )
    return resolved, None


def image_path_refusal(image_path: Any) -> dict[str, Any] | None:
    """``None`` when ``image_path`` may be read; else the in-band refusal (see :func:`admit_image_path`)."""
    _, refusal = admit_image_path(image_path)
    return refusal


def cloud_allowed(resolved: Path) -> bool:
    """May this image's bytes leave the machine? Only a chat attachment's.

    The user handed those over in this conversation; the Wizard's cloud tier
    is theirs to configure. A workspace file, an ingested upload or a
    screenshot the model picked on its own stays local.
    """
    try:
        root = wizard_uploads_root().resolve()
    except Exception:  # pragma: no cover - defensive
        return False
    return root == resolved or root in resolved.parents


# ---------------------------------------------------------------------------
# The analysis: local first, cloud only when allowed
# ---------------------------------------------------------------------------


def _no_model_text(name: str, size_kb: float, *, allow_cloud: bool) -> str:
    from nvh.core import vision_tools as vt

    head = f"{_NO_MODEL_PREFIX} {name}, {size_kb:.1f} KB]\n"
    pull = f"  ollama pull {vt._suggested_vision_pull()}\n"
    if allow_cloud:
        return (
            head + "No vision model available. Install one locally:\n" + pull
            + "Or configure a cloud API key (OpenAI, Google, Anthropic)."
        )
    return (
        head + "No local vision model is installed, and only images attached in the chat may "
        "go to a cloud vision API — this one stays on this machine. Install a local model:\n" + pull
        + "Or attach the image in the chat to allow the configured cloud vision API."
    )


async def analyze_image_file(resolved: Path, question: str, *, allow_cloud: bool) -> str:
    """The core tool's answer text for an admitted path, with the cloud rule applied.

    Same shape as ``nvh.core.vision_tools.analyze_image``: ``Image not
    found``, ``Image too large (… MB). Max 20 MB.``, ``[Vision: <model>, N
    KB]\\n<answer>`` or the ``[Image loaded: …]`` no-model hint. The mime a
    cloud provider is told comes from the bytes' signature (falling back to
    the suffix), never from a default.
    """
    from nvh.core import vision_tools as vt

    if not resolved.exists():
        return f"Image not found: {resolved}"
    size = resolved.stat().st_size
    if size > MAX_IMAGE_BYTES:
        return f"Image too large ({size / 1024 / 1024:.1f} MB). Max {MAX_IMAGE_BYTES // (1024 * 1024)} MB."
    try:
        data = resolved.read_bytes()
        size_kb = size / 1024
        image_data = base64.b64encode(data).decode("utf-8")
        mime = SUFFIX_MIMES.get(sniff_image_suffix(data[:16]) or resolved.suffix.lower(), "image/png")

        model = vt._detect_ollama_vision_model()
        if model:
            answer = await vt._analyze_with_ollama(image_data, question, model)
            if answer:
                return f"[Vision: {model}, {size_kb:.1f} KB]\n{answer}"
        if allow_cloud:
            answer = await vt._analyze_with_cloud(image_data, mime, question)
            if answer:
                return f"[Vision: {CLOUD_MODEL_LABEL}, {size_kb:.1f} KB]\n{answer}"
        return _no_model_text(resolved.name, size_kb, allow_cloud=allow_cloud)
    except Exception as exc:
        return f"Failed to analyze image: {exc}"


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def parse_vision_prefix(text: str) -> tuple[str | None, str | None]:
    """``(model, provider)`` from the answer's first line; ``(None, None)`` when it is not an analysis."""
    match = _VISION_PREFIX_RE.match(text or "")
    if not match:
        return None, None
    model = match.group("model").strip()
    provider = CLOUD_MODEL_LABEL if model == CLOUD_MODEL_LABEL else "ollama"
    return model, provider


def _result_from_text(image_path: str, resolved: Path, text: Any, *, allow_cloud: bool) -> dict[str, Any]:
    """Wrap the answer text in the bridge's result shape.

    A ``[Vision: …]`` first line is an analysis (``ok``). Everything else —
    "Image not found", "Image too large", the "No vision model available"
    hint, "Failed to analyze" — is reported as not ok with the message in
    ``error`` and the full text kept so the model can relay the install
    hint. The local-only decline (no local model, cloud not allowed for this
    path) is additionally a refusal: nothing was analysed and nothing left.
    """
    from nvh.core.agent_guardrails import redact_secrets

    body = redact_secrets(str(text or ""))
    model, provider = parse_vision_prefix(body)
    result: dict[str, Any] = {
        "ok": model is not None,
        "text": body,
        "model": model,
        "provider": provider,
        "bytes": _file_size(resolved),
        "image_path": str(Path(image_path)),
    }
    if model is None:
        if body.startswith(_NO_MODEL_PREFIX) and "\n" in body:
            error = body.split("\n", 1)[1].strip()
            if not allow_cloud:
                result["refused"] = True
                result["local_only"] = True
        else:
            error = body.strip() or "vision tool returned nothing"
        result["error"] = error[:600]
    return result


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _analyze(args: dict[str, Any], question: str) -> dict[str, Any]:
    image_path = str(args.get("image_path") or "").strip()
    resolved, refusal = admit_image_path(image_path)
    if refusal is not None or resolved is None:
        return refusal or _refusal("image_path is required")
    allow_cloud = cloud_allowed(resolved)
    text = await analyze_image_file(resolved, question, allow_cloud=allow_cloud)
    return _result_from_text(image_path, resolved, text, allow_cloud=allow_cloud)


async def _tool_analyze_image(args: dict[str, Any]) -> dict[str, Any]:
    prompt = args.get("prompt")
    question = prompt.strip() if isinstance(prompt, str) and prompt.strip() else _DEFAULT_QUESTION
    return await _analyze(args, question)


async def _tool_read_text_from_image(args: dict[str, Any]) -> dict[str, Any]:
    return await _analyze(args, OCR_QUESTION)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_PATH_PARAM_DESCRIPTION = (
    f"Path to the image ({IMAGE_KINDS_TEXT}; at most 20 MB; the file's bytes must be an image). "
    "Use one of the paths from the 'Attached images' line of the user's message, or an image "
    "under the upload folder ($NVH_HOME/rag/uploads), the agent workspace or the screenshot temp "
    "folder — other paths, and non-image files anywhere, are refused. Only attached images may "
    "be analysed by a cloud model; everything else needs a local vision model."
)


def register_wizard_tools(reg: Any) -> None:
    """Register ``analyze_image`` and ``read_text_from_image`` (auto) on a ``WizardToolRegistry``.

    Both wrap the core vision providers behind :func:`admit_image_path` and
    the cloud rule. Registered unconditionally: without a vision model the
    handler answers ``ok: False`` with the ``ollama pull`` hint, so the
    Wizard can still explain how to get eyes.
    """
    from nvh.integrations.wizard.tools import WizardTool

    reg.register(WizardTool(
        name="analyze_image",
        description=(
            "Look at an image the user attached (or a screenshot) and describe it or "
            "answer a question about it — contents, UI elements, charts, error dialogs. "
            "Runs on the strongest installed local vision model; an image the user attached "
            "in the chat may fall back to a configured cloud vision API (the result's first "
            "line says which). Only image files under the upload, workspace or screenshot "
            "folders are accepted."
        ),
        safety_class="auto",
        parameters={
            "image_path": {"type": "string", "required": True, "description": _PATH_PARAM_DESCRIPTION},
            "prompt": {
                "type": "string",
                "required": False,
                "description": f"What to ask about the image. Default: '{_DEFAULT_QUESTION}'",
            },
        },
        handler=_tool_analyze_image,
        summary_template="Analyze the image at {image_path}.",
    ))

    reg.register(WizardTool(
        name="read_text_from_image",
        description=(
            "Read all visible text out of an image (OCR via the vision model) — "
            "terminal screenshots, error messages, photographed labels or documents. "
            "Same path and cloud rules as analyze_image."
        ),
        safety_class="auto",
        parameters={
            "image_path": {"type": "string", "required": True, "description": _PATH_PARAM_DESCRIPTION},
        },
        handler=_tool_read_text_from_image,
        summary_template="Read the text in the image at {image_path}.",
    ))
