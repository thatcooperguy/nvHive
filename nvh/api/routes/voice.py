"""``/v1/voice`` — transcribe, status, download, speak (design 0.46 §8).

Four routes over :mod:`nvh.core.voice`, every one behind ``require_auth``
(the router-level dependency), mounted by ``nvh.api.server`` with one
``include_router`` line:

- ``POST /v1/voice/transcribe`` — multipart ``file`` (``audio/webm``, ``ogg``,
  ``wav``, ``x-wav``, ``mp4``, ``mpeg``; anything else 415) plus optional
  ``provider`` (``auto`` default; ``auto``, ``local`` or ``groq`` — the
  ``whisper-cli`` path is ``nvh voice``'s alone) and ``model`` (a faster-whisper
  size or a ``Systran/`` / ``mobiuslabsgmbh/`` repo id) form fields. The
  handler reads the request stream itself through python-multipart's
  streaming parser — not FastAPI's ``UploadFile``, which would have parsed and
  spooled the whole body to a temp file before the handler ran — so the
  declared ``Content-Length`` is checked first, the part's type the moment its
  headers arrive, and the 20 MB cap the moment the running total passes it:
  an oversized clip is 413 mid-body, with nothing spooled beyond the cap. The
  clip goes to a ``NamedTemporaryFile`` that is unlinked on every path; the
  transcription runs under ``asyncio.to_thread`` behind a per-process
  ``Semaphore(1)`` so the uvicorn loop keeps answering (``/v1/health``, the
  Wizard stream, job consumers) while the CPU works. Answers ``{text,
  provider, model, latency_ms, cost_usd, cost_known}``; 503 ``{message,
  status, downloading, hint}`` when no path exists (the local weights absent
  and no Groq key); 502 when the chosen path failed.
- ``GET /v1/voice/status`` — :func:`nvh.core.voice.voice_status` plus
  ``downloading`` / ``download_job_id`` for the one download this process may
  be running, and ``hint``.
- ``POST /v1/voice/download`` — starts the one ``voice-model-download`` job
  (202 ``{job_id, model}``; 409 with the running job's id while one is in
  flight; 503 without the ``voice`` extra). The first download is never
  implicit: this route, or ``nvh voice --download``, is how the weights
  arrive. Watch it through ``/v1/jobs/{id}/events`` like any install job.
- ``POST /v1/voice/speak`` — ``{text ≤ 4000, voice?}`` → ``audio/mpeg``
  through ``edge-tts`` when it is on PATH, else 501: the browser's
  ``speechSynthesis`` is the Read-aloud path and server TTS is optional.

Audio is never stored: the temp file lives for the request only, and no
request can name a path on disk.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from nvh.api.deps import _response_envelope, require_auth
from nvh.core import voice
from nvh.integrations.services import jobs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/voice", tags=["voice"], dependencies=[Depends(require_auth)])

VOICE_DOWNLOAD_KIND = "voice-model-download"
"""``kind`` of the job ``POST /v1/voice/download`` starts (one per process at a time)."""

MAX_AUDIO_BYTES = 20 * 1024 * 1024
CONTENT_LENGTH_SLACK = 64 * 1024
"""Multipart framing allowed on top of ``MAX_AUDIO_BYTES`` before the declared length alone is a 413."""
FIELD_MAX_BYTES = 4096
"""Cap on a text form field (``provider``, ``model``); anything longer is refused as it arrives."""
TOO_LARGE_DETAIL = f"Audio must be under {MAX_AUDIO_BYTES // (1024 * 1024)} MB"

STT_PROVIDERS = ("auto", "local", "groq")
"""What the route accepts as ``provider``. ``whisper-cli`` (openai-whisper's CLI, which fetches
its own weights on first use) stays reachable from ``nvh voice --stt whisper-cli`` only."""

SPEAK_TEXT_MAX = 4000
SPEAK_NOT_AVAILABLE = "browser speech synthesis is used; server TTS is optional"

MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}(/[A-Za-z0-9][A-Za-z0-9._-]{0,79})?$")
"""The shape of a ``model`` value; :func:`_check_model` then requires :func:`nvh.core.voice.is_catalog_model`."""
MODEL_DETAIL = (
    "model must be a faster-whisper size (small.en) or a repo id under "
    + " / ".join(voice.STT_MODEL_NAMESPACES)
)

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

_STT_LOCK: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
_DOWNLOAD_JOB_ID: str | None = None


# ---------------------------------------------------------------------------
# Process-wide state: the one semaphore and the one download
# ---------------------------------------------------------------------------

def _stt_semaphore() -> asyncio.Semaphore:
    """The per-process ``Semaphore(1)`` every transcription waits on, built inside the running loop.

    Re-created when the loop changes (a TestClient per request, a restarted
    server in one process), since a primitive bound to a closed loop is useless.
    """
    global _STT_LOCK
    loop = asyncio.get_running_loop()
    if _STT_LOCK is None or _STT_LOCK[0] is not loop:
        _STT_LOCK = (loop, asyncio.Semaphore(1))
    return _STT_LOCK[1]


def download_in_flight() -> str | None:
    """The id of the ``voice-model-download`` job still running in this process, else ``None``."""
    global _DOWNLOAD_JOB_ID
    job_id = _DOWNLOAD_JOB_ID
    if job_id is None:
        return None
    task = jobs._TASKS.get(job_id)
    if task is None or task.done():
        _DOWNLOAD_JOB_ID = None
        return None
    return job_id


async def _status_payload(model: str | None = None) -> dict[str, Any]:
    status = await asyncio.to_thread(voice.voice_status, model)
    job_id = download_in_flight()
    status["downloading"] = job_id is not None
    status["download_job_id"] = job_id
    status["hint"] = voice.stt_hint(status, downloading=job_id is not None)
    return status


def _unavailable(message: str, status: dict[str, Any]) -> HTTPException:
    downloading = download_in_flight() is not None
    return HTTPException(
        status_code=503,
        detail={
            "message": message,
            "status": status,
            "downloading": downloading,
            "hint": voice.stt_hint(status, downloading=downloading),
        },
    )


# ---------------------------------------------------------------------------
# Temp files
# ---------------------------------------------------------------------------

def _spool_path(suffix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(prefix="nvh-voice-", suffix=suffix, delete=False)
    handle.close()
    return Path(handle.name)


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:  # a stray handle on Windows; the temp dir is swept, say so and move on
        logger.debug("voice temp file %s not removed: %s", path, exc)


def _check_mime(content_type: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in voice.MIME_SUFFIX:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported audio type {ctype or '(none)'}; send one of {', '.join(voice.ACCEPTED_MIME_TYPES)}",
        )
    return ctype


def _check_model(model: str | None) -> str | None:
    """``None`` (the server default) or an allow-listed name; anything else is 422."""
    if model is None or model == "":
        return None
    if not MODEL_NAME.match(model) or not voice.is_catalog_model(model):
        raise HTTPException(status_code=422, detail=MODEL_DETAIL)
    return model


def _safe_filename(original: str | None, suffix: str) -> str:
    name = Path(original or "").name
    if _SAFE_FILENAME.match(name) and name.lower().endswith(suffix):
        return name
    return f"audio{suffix}"


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


class _ClipReader:
    """python-multipart callbacks: the ``file`` part to disk under the cap, the small fields to memory.

    Refusals happen as the bytes arrive: the part's type when its headers are
    seen (415), the cap the moment the running total passes it (413), a text
    field past :data:`FIELD_MAX_BYTES` (413). The file bytes of each parsed
    chunk collect in ``pending`` and the route writes them off the loop; the
    temp file is created when the first audio byte is known, with the suffix
    the part's type dictates, and ``path`` names it for the route to unlink.
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self.pending: list[bytes] = []
        self.total = 0
        self.mime: str | None = None
        self.filename: str | None = None
        self.saw_file = False
        self._fields: dict[str, bytearray] = {}
        self._headers: list[tuple[bytes, bytes]] = []
        self._name = b""
        self._value = b""
        self._in_file = False
        self._field: str | None = None

    def callbacks(self) -> dict[str, Any]:
        names = (
            "on_part_begin", "on_part_data", "on_part_end", "on_header_field",
            "on_header_value", "on_header_end", "on_headers_finished", "on_end",
        )
        return {name: getattr(self, name) for name in names}

    def field(self, name: str) -> str | None:
        raw = self._fields.get(name)
        if raw is None:
            return None
        return _decode(bytes(raw)).strip() or None

    def on_part_begin(self) -> None:
        self._headers = []
        self._in_file = False
        self._field = None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._value += data[start:end]

    def on_header_end(self) -> None:
        self._headers.append((self._name.lower(), self._value))
        self._name = b""
        self._value = b""

    def on_headers_finished(self) -> None:
        headers = dict(self._headers)
        _, options = parse_options_header(headers.get(b"content-disposition", b""))
        field = _decode(options.get(b"name", b""))
        if field == "file":
            if self.saw_file:
                raise HTTPException(status_code=400, detail="send exactly one `file` part")
            self.saw_file = True
            self._in_file = True
            self.filename = _decode(options.get(b"filename", b"")) or None
            self.mime = _check_mime(_decode(headers.get(b"content-type", b"")))
            return
        self._field = field
        self._fields.setdefault(field, bytearray())

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._in_file:
            self.total += len(chunk)
            if self.total > MAX_AUDIO_BYTES:
                raise HTTPException(status_code=413, detail=TOO_LARGE_DETAIL)
            self.pending.append(chunk)
        elif self._field is not None:
            buffer = self._fields[self._field]
            if len(buffer) + len(chunk) > FIELD_MAX_BYTES:
                raise HTTPException(status_code=413, detail=f"form field {self._field} is too long")
            buffer.extend(chunk)

    def on_part_end(self) -> None:
        self._in_file = False
        self._field = None

    def on_end(self) -> None:
        pass


async def _receive_clip(request: Request, reader: _ClipReader) -> None:
    """Feed the request stream through ``reader`` chunk by chunk, writing audio bytes off the loop.

    415 when the request is not ``multipart/form-data`` with a boundary, 400
    when the body is malformed. Whatever ``reader`` refused mid-body propagates
    as its ``HTTPException`` with the rest of the body unread.
    """
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    if ctype.lower() != b"multipart/form-data" or b"boundary" not in params:
        raise HTTPException(status_code=415, detail="send multipart/form-data with an audio `file` part")
    parser = MultipartParser(params[b"boundary"], reader.callbacks())
    handle: Any = None
    try:
        try:
            async for chunk in request.stream():
                if chunk:
                    parser.write(chunk)
                if reader.pending:
                    if handle is None:
                        reader.path = await asyncio.to_thread(_spool_path, voice.MIME_SUFFIX[reader.mime or "audio/wav"])
                        handle = await asyncio.to_thread(open, reader.path, "wb")
                    data = b"".join(reader.pending)
                    reader.pending = []
                    await asyncio.to_thread(handle.write, data)
            parser.finalize()
        except FormParserError as exc:
            raise HTTPException(status_code=400, detail="Invalid multipart body") from exc
    finally:
        if handle is not None:
            await asyncio.to_thread(handle.close)


_TRANSCRIBE_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": f"{', '.join(voice.ACCEPTED_MIME_TYPES)}; {TOO_LARGE_DETAIL}",
                        },
                        "provider": {"type": "string", "enum": list(STT_PROVIDERS), "default": "auto"},
                        "model": {"type": "string", "description": MODEL_DETAIL},
                    },
                }
            }
        },
    }
}


def _sniff_media_type(data: bytes) -> str:
    if data[:4] == b"RIFF":
        return "audio/wav"
    if data[:4] == b"FORM":
        return "audio/aiff"
    return "audio/mpeg"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/transcribe",
    summary="Transcribe one audio clip (multipart `file`)",
    openapi_extra=_TRANSCRIBE_OPENAPI,
)
async def voice_transcribe(request: Request) -> dict[str, Any]:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_AUDIO_BYTES + CONTENT_LENGTH_SLACK:
        raise HTTPException(status_code=413, detail=TOO_LARGE_DETAIL)
    reader = _ClipReader()
    try:
        await _receive_clip(request, reader)
        if not reader.saw_file or reader.mime is None:
            raise HTTPException(status_code=422, detail="multipart field `file` is required")
        if reader.total == 0 or reader.path is None:
            raise HTTPException(status_code=400, detail="Empty audio clip")
        provider = reader.field("provider") or "auto"
        if provider not in STT_PROVIDERS:
            raise HTTPException(
                status_code=422,
                detail=f"provider must be one of {', '.join(STT_PROVIDERS)}",
            )
        model = _check_model(reader.field("model"))
        mime = reader.mime
        filename = _safe_filename(reader.filename, voice.MIME_SUFFIX[mime])
        async with _stt_semaphore():
            try:
                result = await asyncio.to_thread(
                    voice.speech_to_text,
                    str(reader.path),
                    provider,
                    mime_type=mime,
                    filename=filename,
                    model=model,
                )
            except voice.VoiceUnavailable as exc:
                status = exc.status or await asyncio.to_thread(voice.voice_status, model)
                raise _unavailable(str(exc), status) from exc
            except voice.VoiceBackendError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if reader.path is not None:
            await asyncio.to_thread(_unlink, reader.path)
    return _response_envelope(result.as_dict())


@router.get("/status", summary="Which speech paths this server has, and whether a model download is running")
async def voice_status_route(
    model: str | None = Query(None, max_length=160, pattern=MODEL_NAME.pattern),
) -> dict[str, Any]:
    return _response_envelope(await _status_payload(_check_model(model)))


class DownloadRequest(BaseModel):
    model: str | None = Field(None, max_length=160, pattern=MODEL_NAME.pattern)


async def _download_source(model: str) -> AsyncIterator[dict[str, Any]]:
    """The ``voice-model-download`` job's events: a step, progress about once a second, then complete or error."""
    root = str(voice.download_root())
    approx = voice.approx_model_bytes(model)
    yield {
        "event": "step", "status": "running", "model": model, "progress": 2,
        "message": f"Downloading speech model {model} to {root}",
    }
    task = asyncio.ensure_future(asyncio.to_thread(voice.download_model, model))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=1.0)
            if done:
                break
            on_disk = await asyncio.to_thread(voice.model_bytes_on_disk, model)
            payload: dict[str, Any] = {
                "event": "progress", "status": "running", "model": model, "bytes": on_disk,
                "message": f"{on_disk / 1_000_000:.0f} MB so far",
            }
            if approx:
                payload["progress"] = 2 + int(88 * min(on_disk, approx) / approx)
            yield payload
        path = task.result()
    except asyncio.CancelledError:
        # The worker thread cannot be stopped; the partial snapshot is reused by the next download.
        task.cancel()
        raise
    except voice.VoiceUnavailable as exc:
        yield {"event": "error", "status": "failed", "model": model, "message": str(exc)}
        return
    except Exception as exc:
        yield {"event": "error", "status": "failed", "model": model, "message": f"download failed: {exc}"}
        return
    size = await asyncio.to_thread(voice.model_bytes_on_disk, model)
    yield {
        "event": "complete", "status": "complete", "model": model, "path": str(path), "bytes": size,
        "message": f"Speech model {model} ready ({size / 1_000_000:.0f} MB)",
    }


@router.post("/download", status_code=202, summary="Start the one voice-model-download job")
async def voice_download(body: DownloadRequest | None = None) -> dict[str, Any]:
    global _DOWNLOAD_JOB_ID
    # The server's own default (NVH_VOICE_STT_MODEL) is the operator's choice and is not
    # allow-listed; a client-supplied name is.
    model = _check_model(body.model if body is not None else None) or voice.default_stt_model()
    status = await asyncio.to_thread(voice.voice_status, model)
    if not status["stt"]["local_installed"]:
        raise _unavailable(status["stt"]["reason"] or voice.REASON_NOT_INSTALLED, status)
    existing = download_in_flight()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={"message": "a speech-model download is already in flight", "job_id": existing},
        )
    job = jobs.start_job(
        kind=VOICE_DOWNLOAD_KIND,
        title=f"Download speech model {model}",
        request={"model": model, "download_root": str(voice.download_root())},
        source_factory=lambda: _download_source(model),
    )
    _DOWNLOAD_JOB_ID = str(job["id"])
    return _response_envelope({"job_id": str(job["id"]), "model": model})


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=SPEAK_TEXT_MAX)
    voice: str | None = Field(None, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


@router.post("/speak", summary="Synthesize speech through edge-tts (501 when it is not on PATH)")
async def voice_speak(body: SpeakRequest) -> Response:
    if not voice.edge_tts_available():
        raise HTTPException(status_code=501, detail=SPEAK_NOT_AVAILABLE)
    out_path = await asyncio.to_thread(_spool_path, ".mp3")
    produced: str = ""
    try:
        produced = await voice.text_to_speech(
            body.text, output_path=str(out_path), provider="edge", voice=body.voice or voice.DEFAULT_TTS_VOICE,
        )
        source = Path(produced) if produced else out_path
        data = await asyncio.to_thread(_read_if_present, source)
        if not data:
            raise HTTPException(status_code=502, detail="edge-tts produced no audio")
    finally:
        await asyncio.to_thread(_unlink, out_path)
        if produced and Path(produced) != out_path:
            await asyncio.to_thread(_unlink, Path(produced))
    return Response(content=data, media_type=_sniff_media_type(data), headers={"Cache-Control": "no-store"})


def _read_if_present(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""
