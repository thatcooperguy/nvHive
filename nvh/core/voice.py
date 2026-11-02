"""NVHive Voice I/O — speak to NVHive, hear responses.

Speech-to-text (design 0.46 §8) is local-first: ``faster-whisper`` in-process
on the CPU (int8, default ``small.en``) when the optional ``nvhive[voice]``
extra is installed and the weights sit under :func:`download_root`; else
Groq Whisper when a Groq key resolves (env, then keyring); the old
openai-whisper CLI stays reachable as ``provider="whisper-cli"``.
Text-to-speech: Edge TTS (``edge-tts`` on PATH, undeclared and opt-in) or the
system voice (``espeak`` / ``say``).

Nothing here is imported eagerly from the extra: ``faster_whisper`` is imported
inside the functions that need it, so the module imports (and ``nvh voice
--help`` works) on a box without the wheel — Windows on Arm has none.

Not on the event loop: :func:`speech_to_text` and :func:`download_model` are
synchronous CPU/network work. ``nvh.api.routes.voice`` runs them under
``asyncio.to_thread`` behind a per-process semaphore; the first model download
is never implicit — it is the ``voice-model-download`` job that
``POST /v1/voice/download`` or ``nvh voice --download`` starts.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from nvh.integrations.workspace.storage import storage_layout

DEFAULT_STT_MODEL = "small.en"
"""faster-whisper model the local path loads when nothing else is named."""

STT_MODEL_ENV = "NVH_VOICE_STT_MODEL"
"""Environment override for :func:`default_stt_model`; read when a config is built, not at import."""

DEFAULT_TTS_VOICE = "en-US-AriaNeural"

GROQ_STT_MODEL = "whisper-large-v3"
GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_KEY_ENV = ("COUNCIL_GROQ_API_KEY", "GROQ_API_KEY", "HIVE_GROQ_API_KEY")
"""The registry's order for the ``groq`` provider (``nvh/providers/registry.py``), then the keyring."""

STT_PROVIDERS = ("auto", "local", "groq", "whisper-cli")

MIME_SUFFIX: dict[str, str] = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".mp4",
    "audio/mpeg": ".mp3",
}
"""Audio types the API accepts, with the suffix the temp file (and Groq's filename) gets."""

ACCEPTED_MIME_TYPES = tuple(MIME_SUFFIX)

GROQ_KEY_HINT = "Groq API key needed for voice input. Run: nvh advisor add groq"
REASON_NOT_INSTALLED = "install nvhive[voice]"
REASON_NO_WHEEL = "no faster-whisper wheel for this platform"
REASON_MODEL_MISSING = "model not downloaded"

_APPROX_MODEL_BYTES: dict[str, int] = {
    "tiny": 75_000_000,
    "tiny.en": 75_000_000,
    "base": 145_000_000,
    "base.en": 145_000_000,
    "small": 485_000_000,
    "small.en": 485_000_000,
    "medium": 1_530_000_000,
    "medium.en": 1_530_000_000,
    "large-v1": 3_090_000_000,
    "large-v2": 3_090_000_000,
    "large-v3": 3_090_000_000,
    "large": 3_090_000_000,
    "large-v3-turbo": 1_620_000_000,
    "turbo": 1_620_000_000,
    "distil-small.en": 330_000_000,
    "distil-medium.en": 790_000_000,
    "distil-large-v2": 1_510_000_000,
    "distil-large-v3": 1_510_000_000,
}
"""Rough snapshot sizes, for a download job's progress bar only (re-measured at pin time)."""

_LOCAL_MODELS: dict[str, Any] = {}
_LOCAL_MODELS_LOCK = threading.Lock()


def default_stt_model() -> str:
    """``NVH_VOICE_STT_MODEL`` when set, else :data:`DEFAULT_STT_MODEL` — read now, not at import."""
    return os.environ.get(STT_MODEL_ENV, "").strip() or DEFAULT_STT_MODEL


@dataclass
class VoiceConfig:
    stt_provider: str = "groq"     # "auto" (local, then Groq), "local" (faster-whisper), "groq", "whisper-cli"
    tts_provider: str = "edge"     # "edge" (free), "system" (OS TTS)
    tts_voice: str = DEFAULT_TTS_VOICE  # Edge TTS voice
    auto_listen: bool = False      # continuously listen
    silence_timeout: float = 2.0   # seconds of silence to stop recording
    stt_model: str = field(default_factory=default_stt_model)  # faster-whisper model for the local path


@dataclass
class TranscriptResult:
    """What :func:`speech_to_text` returns — the shape ``POST /v1/voice/transcribe`` sends back.

    ``cost_known`` is ``False`` on the Groq path (no catalog row prices Groq
    Whisper, so the pill reads "cost unknown"); the local and CLI paths cost
    nothing and say so.
    """

    text: str
    provider: str
    model: str
    latency_ms: int
    cost_usd: float | None
    cost_known: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class VoiceUnavailableError(ValueError):
    """No speech-to-text path can serve this request (the API answers 503).

    A ``ValueError`` because the 0.45 contract raised one for a missing Groq
    key and ``nvh voice`` callers match on it. ``status`` is the
    :func:`voice_status` snapshot taken when the decision was made, when the
    caller had one.
    """

    def __init__(self, message: str, status: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status


VoiceUnavailable = VoiceUnavailableError
"""The design's name for :class:`VoiceUnavailableError` (ruff N818 wants the suffix)."""


class VoiceBackendError(RuntimeError):
    """The chosen path exists but failed: Groq answered an error, the decoder choked, a download broke (502)."""


# ---------------------------------------------------------------------------
# Where the weights live, and whether they do
# ---------------------------------------------------------------------------

def download_root() -> Path:
    """``storage_layout().cache_dir / "huggingface"`` — the layout's Hugging Face cache folder.

    Passed to faster-whisper as ``download_root`` / ``cache_dir`` explicitly;
    this process exports no Hugging Face environment variable.
    """
    return storage_layout().cache_dir / "huggingface"


def model_repo(model: str) -> str:
    """The Hugging Face repo faster-whisper resolves ``model`` to (a repo id passes through)."""
    if "/" in model:
        return model
    if model in {"large-v3-turbo", "turbo"}:
        return "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    if model == "large":
        return "Systran/faster-whisper-large-v3"
    if model.startswith("distil-"):
        return f"Systran/faster-distil-whisper-{model[len('distil-'):]}"
    return f"Systran/faster-whisper-{model}"


def _snapshot_dir(model: str) -> Path:
    return download_root() / f"models--{model_repo(model).replace('/', '--')}"


STT_MODEL_NAMESPACES = ("Systran/", "mobiuslabsgmbh/")
"""The Hugging Face owners faster-whisper's own size table resolves to; the API accepts repo ids there only."""

REQUIRED_MODEL_FILES = ("model.bin", "config.json")
VOCABULARY_FILES = ("vocabulary.json", "vocabulary.txt")
"""A CTranslate2 Whisper directory loads only with the weights, the config and one vocabulary file."""


def is_catalog_model(model: str) -> bool:
    """A faster-whisper size name, or a repo id under :data:`STT_MODEL_NAMESPACES`.

    What ``/v1/voice`` accepts as ``model``: a bare name is never a directory
    (see :func:`_model_dir`) and an arbitrary Hugging Face repo would be an
    unbounded download of someone else's ``model.bin`` into the cache, then a
    load of it. The CLI's ``--stt-model`` may also name a converted model
    directory spelled as a path.
    """
    return model in _APPROX_MODEL_BYTES or model.startswith(STT_MODEL_NAMESPACES)


def _model_dir(name: str) -> Path | None:
    """``name`` as a converted-model directory when it is spelled as a path, else ``None``.

    Only ``./x``, ``../x``, ``~/x`` and absolute paths count — a bare word
    (``small.en``) or a repo id (``Systran/x``) is never read as a directory
    relative to the server's working directory, so no API request can name a
    folder on disk.
    """
    if not (name.startswith((".", "~", "/", "\\")) or os.path.isabs(name)):
        return None
    folder = Path(name).expanduser()
    return folder if folder.is_dir() else None


def _model_dir_complete(folder: Path) -> bool:
    return all((folder / item).is_file() for item in REQUIRED_MODEL_FILES) and any(
        (folder / item).is_file() for item in VOCABULARY_FILES
    )


def model_present(model: str | None = None) -> bool:
    """Whether a complete snapshot of ``model`` is under :func:`download_root` (or ``model`` is a complete model directory).

    Complete means :data:`REQUIRED_MODEL_FILES` plus a vocabulary, not just
    ``model.bin``: a snapshot that lost its small files reads as "not
    downloaded", so the status says so and ``nvh voice --download`` (or the
    download job) repairs it instead of refusing as already present.
    """
    name = model or default_stt_model()
    local = _model_dir(name)
    if local is not None:
        return _model_dir_complete(local)
    snapshots = _snapshot_dir(name) / "snapshots"
    if not snapshots.is_dir():
        return False
    return any(_model_dir_complete(snap) for snap in snapshots.iterdir() if snap.is_dir())


def model_bytes_on_disk(model: str | None = None) -> int:
    """Bytes already under the model's cache folder — what a download job reports as progress."""
    name = model or default_stt_model()
    folder = _model_dir(name) or _snapshot_dir(name)
    if not folder.is_dir():
        return 0
    total = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def approx_model_bytes(model: str | None = None) -> int:
    """Rough full size of ``model``'s snapshot (0 when unknown); progress-bar arithmetic only."""
    return _APPROX_MODEL_BYTES.get(model or default_stt_model(), 0)


# ---------------------------------------------------------------------------
# What is installed, and what resolves
# ---------------------------------------------------------------------------

def _import_faster_whisper() -> Any | None:
    try:
        import faster_whisper
    except ImportError:
        return None
    return faster_whisper


def local_installed() -> bool:
    """Whether ``faster_whisper`` imports (the ``voice`` extra is installed and has a wheel here)."""
    return _import_faster_whisper() is not None


def platform_has_wheel() -> bool:
    """False on Windows on Arm: faster-whisper's CTranslate2 and PyAV ship no win_arm64 wheels."""
    return not (sys.platform == "win32" and platform.machine().upper() == "ARM64")


def _keyring_groq_key() -> str | None:
    try:
        import keyring

        return keyring.get_password("nvhive", "groq_api_key") or None
    except Exception:
        return None


def groq_api_key() -> str | None:
    """``GROQ_KEY_ENV`` in the registry's order, then the keyring.

    The keyring read is unconditional, unlike the registry's (gated on
    ``NVH_USE_KEYRING`` as a startup-latency guard): ``nvh advisor add groq``
    writes only the keyring, and the ``nvh`` entry point copies that key into
    ``GROQ_API_KEY`` before any command runs, so under ``nvh serve`` both
    lookups agree; this keeps the hint's own instruction working under a bare
    uvicorn launch too.
    """
    for name in GROQ_KEY_ENV:
        value = os.environ.get(name, "")
        if value:
            return value
    return _keyring_groq_key()


def edge_tts_available() -> bool:
    return shutil.which("edge-tts") is not None


def system_tts_available() -> bool:
    if sys.platform == "linux":
        return shutil.which("espeak") is not None
    if sys.platform == "darwin":
        return shutil.which("say") is not None
    return False


def voice_status(model: str | None = None) -> dict[str, Any]:
    """The facts the status line, the 503 and ``nvh voice`` print.

    ``stt.reason`` names the missing piece when the local path is not
    available (``install nvhive[voice]``, ``no faster-whisper wheel for this
    platform``, ``model not downloaded``) and is ``None`` when it is.
    """
    name = model or default_stt_model()
    installed = local_installed()
    present = model_present(name)
    if installed and present:
        reason: str | None = None
    elif installed:
        reason = REASON_MODEL_MISSING
    elif not platform_has_wheel():
        reason = REASON_NO_WHEEL
    else:
        reason = REASON_NOT_INSTALLED
    return {
        "stt": {
            "local_installed": installed,
            "local_model_present": present,
            "local_available": installed and present,
            "groq_available": groq_api_key() is not None,
            "model": name,
            "reason": reason,
            "download_root": str(download_root()),
        },
        "tts": {
            "edge_available": edge_tts_available(),
            "system_available": system_tts_available(),
        },
    }


def stt_hint(status: dict[str, Any], *, downloading: bool = False) -> str:
    """One sentence telling the user how to get a speech-to-text path; empty when one exists."""
    stt = status.get("stt", {})
    if downloading:
        return "the speech model is downloading; try again when the job completes"
    if stt.get("local_available") or stt.get("groq_available"):
        return ""
    if stt.get("local_installed"):
        return (
            f"download the speech model ({stt.get('model', default_stt_model())}): "
            "nvh voice --download, or the Download model button"
        )
    if stt.get("reason") == REASON_NO_WHEEL:
        return f"{REASON_NO_WHEEL}; add a Groq key instead: nvh advisor add groq"
    return "pip install 'nvhive[voice]' for local speech-to-text, or add a Groq key: nvh advisor add groq"


# ---------------------------------------------------------------------------
# Speech-to-text
# ---------------------------------------------------------------------------

def _load_local_model(name: str, faster_whisper: Any) -> Any:
    with _LOCAL_MODELS_LOCK:
        cached = _LOCAL_MODELS.get(name)
        if cached is not None:
            return cached
        try:
            loaded = faster_whisper.WhisperModel(
                name,
                device="cpu",
                compute_type="int8",
                download_root=str(download_root()),
                local_files_only=True,
            )
        except Exception as exc:
            raise VoiceUnavailable(
                f"{REASON_MODEL_MISSING}: {name} could not be loaded from {download_root()} ({exc})",
                voice_status(name),
            ) from exc
        _LOCAL_MODELS[name] = loaded
        return loaded


def _segments_of(result: Any) -> Any:
    # faster-whisper returns ``(segments, info)``; a plain iterable of segments is tolerated.
    if isinstance(result, tuple) and len(result) == 2:
        return result[0]
    return result


def _transcribe_local(audio_path: str, name: str) -> TranscriptResult:
    faster_whisper = _import_faster_whisper()
    if faster_whisper is None:
        status = voice_status(name)
        raise VoiceUnavailable(status["stt"]["reason"] or REASON_NOT_INSTALLED, status)
    if not model_present(name):
        raise VoiceUnavailable(f"{REASON_MODEL_MISSING}: {name}", voice_status(name))
    model = _load_local_model(name, faster_whisper)
    started = time.perf_counter()
    try:
        segments = _segments_of(model.transcribe(audio_path))
        text = " ".join(str(getattr(seg, "text", seg)).strip() for seg in segments).strip()
    except Exception as exc:
        raise VoiceBackendError(f"local transcription failed: {exc}") from exc
    return TranscriptResult(
        text=text,
        provider="local",
        model=name,
        latency_ms=int(round((time.perf_counter() - started) * 1000)),
        cost_usd=0.0,
        cost_known=True,
    )


def _transcribe_groq(audio_path: str, mime_type: str, filename: str | None) -> TranscriptResult:
    key = groq_api_key()
    if not key:
        raise VoiceUnavailable(GROQ_KEY_HINT, None)
    mime = (mime_type or "audio/wav").split(";")[0].strip().lower() or "audio/wav"
    name = filename or f"audio{MIME_SUFFIX.get(mime, '.wav')}"
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=60) as client, open(audio_path, "rb") as fh:
            resp = client.post(
                GROQ_TRANSCRIPTIONS_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (name, fh, mime)},
                data={"model": GROQ_STT_MODEL},
            )
            resp.raise_for_status()
            text = str(resp.json().get("text", "") or "")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        body = exc.response.text[:200] if exc.response is not None else ""
        raise VoiceBackendError(f"Groq answered HTTP {code}: {body}") from exc
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise VoiceBackendError(f"Groq transcription failed: {exc}") from exc
    return TranscriptResult(
        text=text.strip(),
        provider="groq",
        model=GROQ_STT_MODEL,
        latency_ms=int(round((time.perf_counter() - started) * 1000)),
        cost_usd=None,
        cost_known=False,
    )


def _transcribe_whisper_cli(audio_path: str) -> TranscriptResult:
    """The pre-0.46 ``local`` path: openai-whisper's ``whisper`` CLI on PATH (``nvh voice`` only).

    The CLI writes ``<clip>.txt`` into ``--output_dir`` (its default is the
    working directory), so it gets a temporary directory that is removed with
    the transcript, and the text is read from that file rather than from the
    tool's verbose, timestamped stdout.
    """
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="nvh-whisper-cli-") as out_dir:
        try:
            result = subprocess.run(
                [
                    "whisper", audio_path, "--model", "base", "--output_format", "txt",
                    "--output_dir", out_dir,
                ],
                capture_output=True, text=True, timeout=300,
            )
        except FileNotFoundError as exc:
            raise VoiceUnavailable("the `whisper` CLI is not on PATH", voice_status()) from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise VoiceBackendError(f"whisper CLI failed: {exc}") from exc
        if result.returncode != 0:
            raise VoiceBackendError(f"whisper CLI exited {result.returncode}: {(result.stderr or '')[:200]}")
        transcript = Path(out_dir) / f"{Path(audio_path).stem}.txt"
        text = (
            transcript.read_text(encoding="utf-8", errors="replace")
            if transcript.is_file()
            else result.stdout
        )
    return TranscriptResult(
        text=text.strip(),
        provider="whisper-cli",
        model="base",
        latency_ms=int(round((time.perf_counter() - started) * 1000)),
        cost_usd=0.0,
        cost_known=True,
    )


def speech_to_text(
    audio_path: str,
    provider: str = "auto",
    *,
    mime_type: str = "audio/wav",
    filename: str | None = None,
    model: str | None = None,
) -> TranscriptResult:
    """Transcribe the audio file at ``audio_path``. Synchronous: run it in a thread from async code.

    ``auto`` is ``local`` when ``faster_whisper`` imports and the weights for
    ``model`` (default :func:`default_stt_model`) are under
    :func:`download_root`, else ``groq`` when a Groq key resolves, else
    :class:`VoiceUnavailable` carrying :func:`voice_status`. ``local`` loads
    ``WhisperModel(model, device="cpu", compute_type="int8",
    download_root=..., local_files_only=True)`` once per process; it never
    downloads. ``groq`` posts the real ``mime_type`` and ``filename`` (Groq
    reads the format from the name). ``whisper-cli`` is the old subprocess
    path. Any other ``provider`` is a :class:`ValueError`.
    """
    if provider not in STT_PROVIDERS:
        raise ValueError(f"Unknown speech-to-text provider {provider!r}; one of {', '.join(STT_PROVIDERS)}")
    name = model or default_stt_model()
    if provider == "auto":
        status = voice_status(name)
        if status["stt"]["local_available"]:
            provider = "local"
        elif status["stt"]["groq_available"]:
            provider = "groq"
        else:
            raise VoiceUnavailable(
                f"no speech-to-text path: {status['stt']['reason']} and no Groq key", status,
            )
    if provider == "local":
        return _transcribe_local(audio_path, name)
    if provider == "groq":
        return _transcribe_groq(audio_path, mime_type, filename)
    return _transcribe_whisper_cli(audio_path)


def download_model(model: str | None = None, *, progress: Callable[[int], None] | None = None) -> Path:
    """Fetch ``model``'s weights into :func:`download_root` and return the snapshot path.

    The explicit download the ``voice-model-download`` job and ``nvh voice
    --download`` run; :func:`speech_to_text` never calls it. ``progress``, when
    given, is called with the bytes on disk so far about twice a second while
    the download runs, and once more when it is done. Raises
    :class:`VoiceUnavailable` when ``faster_whisper`` is not importable and
    :class:`VoiceBackendError` when the fetch itself fails.
    """
    name = model or default_stt_model()
    local = _model_dir(name)
    if local is not None:
        return local
    faster_whisper = _import_faster_whisper()
    if faster_whisper is None:
        status = voice_status(name)
        raise VoiceUnavailable(status["stt"]["reason"] or REASON_NOT_INSTALLED, status)
    root = download_root()
    root.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    watcher: threading.Thread | None = None
    if progress is not None:
        def _watch() -> None:
            while not stop.wait(0.5):
                try:
                    progress(model_bytes_on_disk(name))
                except Exception:
                    pass

        watcher = threading.Thread(target=_watch, name="nvh-voice-download-progress", daemon=True)
        watcher.start()
    try:
        path = faster_whisper.download_model(name, cache_dir=str(root), local_files_only=False)
    except Exception as exc:
        raise VoiceBackendError(f"download of {name} failed: {exc}") from exc
    finally:
        stop.set()
        if watcher is not None:
            watcher.join(timeout=2)
    with _LOCAL_MODELS_LOCK:
        _LOCAL_MODELS.pop(name, None)  # a fresh snapshot: forget any handle loaded from an older one
    if progress is not None:
        progress(model_bytes_on_disk(name))
    return Path(str(path))


# ---------------------------------------------------------------------------
# Text-to-speech and the CLI's microphone
# ---------------------------------------------------------------------------

async def text_to_speech(
    text: str,
    output_path: str | None = None,
    provider: str = "edge",
    voice: str = DEFAULT_TTS_VOICE,
) -> str:
    """Convert text to speech audio.

    Edge TTS is free, high quality, no API key needed.
    Falls back to system TTS (espeak/say).

    The text is caller-controlled (``POST /v1/voice/speak`` forwards it), so it
    never travels as a bare argv element: edge-tts takes ``--text=<text>`` (the
    ``=`` form keeps a leading ``-`` from being read as an option), and the
    system voices read it from stdin (``espeak --stdin``, ``say -f -``) — a text
    such as ``-w/some/path`` can neither pick the output file nor make the tool
    read and speak a file of its choosing.

    Returns path to the audio file.
    """
    if output_path is None:
        output_path = tempfile.mktemp(suffix=".mp3")
    spoken = text.encode("utf-8")

    if provider == "edge":
        try:
            # Use edge-tts if installed, otherwise fall back
            proc = await asyncio.create_subprocess_exec(
                "edge-tts", f"--voice={voice}", f"--text={text}", f"--write-media={output_path}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                return output_path
        except (TimeoutError, FileNotFoundError):
            pass

    # Fallback: system TTS — the text goes in on stdin, never as an argument.
    if sys.platform == "linux":
        try:
            proc = await asyncio.create_subprocess_exec(
                "espeak", "-w", output_path, "--stdin",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate(spoken)
            return output_path
        except FileNotFoundError:
            pass
    elif sys.platform == "darwin":
        # macOS `say` writes AIFF; save to a temp AIFF then convert or just use it
        aiff_path = output_path.replace(".mp3", ".aiff") if output_path.endswith(".mp3") else output_path + ".aiff"
        try:
            proc = await asyncio.create_subprocess_exec(
                "say", "-o", aiff_path, "-f", "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate(spoken)
            # Return the AIFF path — afplay can handle it directly
            return aiff_path
        except FileNotFoundError:
            pass

    return ""


async def play_audio(path: str) -> None:
    """Play an audio file using available system player."""
    players = ["mpv", "ffplay", "aplay", "paplay"] if sys.platform == "linux" else ["afplay"]
    for player in players:
        try:
            proc = await asyncio.create_subprocess_exec(
                player, path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return
        except FileNotFoundError:
            continue


async def record_audio(duration: float = 10.0, output_path: str | None = None) -> str:
    """Record audio from microphone. Returns path to WAV file.

    Uses sox (`sox -d`, macOS and Linux) or arecord (Linux/ALSA); nothing else is tried.
    """
    if output_path is None:
        output_path = tempfile.mktemp(suffix=".wav")

    if sys.platform == "darwin":
        # macOS: sox -d, else error (no other recorder is tried)
        try:
            proc = await asyncio.create_subprocess_exec(
                "sox", "-d", "-r", "16000", "-c", "1", output_path, "trim", "0", str(duration),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=duration + 5)
            return output_path
        except (TimeoutError, FileNotFoundError):
            pass
        raise RuntimeError(
            "No audio recording tool found on macOS. Install: brew install sox"
        )

    # Linux: try arecord (ALSA)
    try:
        proc = await asyncio.create_subprocess_exec(
            "arecord", "-d", str(int(duration)), "-f", "cd", "-t", "wav", output_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=duration + 5)
        return output_path
    except (TimeoutError, FileNotFoundError):
        pass

    # Try sox on Linux too
    try:
        proc = await asyncio.create_subprocess_exec(
            "sox", "-d", "-r", "16000", "-c", "1", output_path, "trim", "0", str(duration),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=duration + 5)
        return output_path
    except (TimeoutError, FileNotFoundError):
        pass

    raise RuntimeError("No audio recording tool found. Add arecord or sox to PATH; nvHive will not call apt.")
