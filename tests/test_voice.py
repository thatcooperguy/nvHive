"""nvh/core/voice.py and nvh/api/routes/voice.py (design 0.46 §8).

``faster_whisper`` is never installed here (the venv deliberately lacks it and
Windows on Arm has no wheel): every local-path test puts a fake module in
``sys.modules`` through ``monkeypatch.setitem`` and writes a weights marker
under the hermetic home's ``download_root()``; ``sys.modules["faster_whisper"]
= None`` is how "not installed" is spelled. The Groq path swaps
``httpx.Client`` so nothing leaves the box. The router is mounted on a minimal
FastAPI app — the server's ``include_router`` line is tests/test_api.py's
concern — and the loop test runs two requests through one shared portal
(``with TestClient(...)``) so a slow transcription and ``/v1/health`` really
share one event loop.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nvh.api.routes import voice as voice_routes
from nvh.core import voice
from nvh.integrations.services import jobs

MODEL = voice.DEFAULT_STT_MODEL
CLIP = b"\x1aE\xdf\xa3" + b"\0" * 256  # an EBML header, enough to look like webm


class _Segment:
    def __init__(self, text: str) -> None:
        self.text = text


def _fake_faster_whisper(
    *,
    text_parts: tuple[str, ...] = (" hello", " from the fake "),
    sleep_s: float = 0.0,
    calls: list[tuple[str, Any, dict[str, Any]]] | None = None,
    fail: Exception | None = None,
    download_gate: threading.Event | None = None,
    transcribe_started: threading.Event | None = None,
) -> types.ModuleType:
    """A ``faster_whisper`` stand-in: ``WhisperModel(...).transcribe`` returns ``(segments, info)``
    like the real one; ``download_model`` writes the weights marker the real snapshot would."""
    mod = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, name: str, **kwargs: Any) -> None:
            if calls is not None:
                calls.append(("init", name, kwargs))

        def transcribe(self, path: str, **kwargs: Any) -> Any:
            if calls is not None:
                calls.append(("transcribe", path, kwargs))
            if transcribe_started is not None:
                transcribe_started.set()
            if sleep_s:
                time.sleep(sleep_s)
            if fail is not None:
                raise fail
            info = types.SimpleNamespace(language="en", duration=1.2)
            return iter([_Segment(part) for part in text_parts]), info

    def download_model(name: str, **kwargs: Any) -> str:
        if calls is not None:
            calls.append(("download", name, kwargs))
        if download_gate is not None:
            download_gate.wait(timeout=10)
        return str(_write_weights(name))

    mod.WhisperModel = WhisperModel  # type: ignore[attr-defined]
    mod.download_model = download_model  # type: ignore[attr-defined]
    return mod


def _write_weights(model: str = MODEL) -> Path:
    """The CTranslate2 trio a real snapshot carries: weights, config and a vocabulary."""
    repo = voice.model_repo(model).replace("/", "--")
    snap = voice.download_root() / f"models--{repo}" / "snapshots" / "abc123"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "model.bin").write_bytes(b"\0" * 2048)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    (snap / "vocabulary.txt").write_text("<|endoftext|>\n", encoding="utf-8")
    return snap


def _installed(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> types.ModuleType:
    mod = _fake_faster_whisper(**kwargs)
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return mod


def _not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)


_REAL_KEYRING_GROQ_KEY = voice._keyring_groq_key  # before _clean_slate stubs it


@pytest.fixture(autouse=True)
def _clean_slate(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Groq key from this box's env or keyring, an empty per-process model cache,
    no download in flight, no semaphore from an earlier loop, the default model."""
    for name in voice.GROQ_KEY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(voice, "_keyring_groq_key", lambda: None)
    monkeypatch.setattr(voice, "_LOCAL_MODELS", {})
    monkeypatch.setattr(voice_routes, "_DOWNLOAD_JOB_ID", None)
    monkeypatch.setattr(voice_routes, "_STT_LOCK", None)
    monkeypatch.delenv(voice.STT_MODEL_ENV, raising=False)
    monkeypatch.delenv("HIVE_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# nvh/core/voice.py
# ---------------------------------------------------------------------------


def _fake_keyring(monkeypatch: pytest.MonkeyPatch, value: str | None) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    fake = types.ModuleType("keyring")

    def get_password(service: str, name: str) -> str | None:
        calls.append((service, name))
        return value

    fake.get_password = get_password  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)
    return calls


def test_groq_keyring_lookup_is_off_unless_nvh_use_keyring_is_set(monkeypatch) -> None:
    monkeypatch.delenv("NVH_USE_KEYRING", raising=False)
    monkeypatch.delitem(sys.modules, "keyring", raising=False)
    # A finder that fails the test if anything tries to import keyring.
    monkeypatch.setattr(sys, "meta_path", [_NoKeyring(), *sys.meta_path])
    assert _REAL_KEYRING_GROQ_KEY() is None
    assert "keyring" not in sys.modules


class _NoKeyring:
    @staticmethod
    def find_spec(name, path=None, target=None):
        assert name != "keyring", "keyring was imported with NVH_USE_KEYRING off"
        return None


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_groq_keyring_lookup_reads_the_keyring_when_enabled(monkeypatch, value) -> None:
    monkeypatch.setenv("NVH_USE_KEYRING", value)
    calls = _fake_keyring(monkeypatch, "gsk-from-keyring")
    assert _REAL_KEYRING_GROQ_KEY() == "gsk-from-keyring"
    assert calls == [("nvhive", "groq_api_key")]


@pytest.mark.parametrize("value", ["0", "", "no", "off"])
def test_groq_keyring_lookup_stays_off_for_other_values(monkeypatch, value) -> None:
    monkeypatch.setenv("NVH_USE_KEYRING", value)
    calls = _fake_keyring(monkeypatch, "gsk-from-keyring")
    assert _REAL_KEYRING_GROQ_KEY() is None
    assert calls == []


def test_voice_config_keeps_its_defaults_and_reads_the_model_env_at_construction(monkeypatch) -> None:
    config = voice.VoiceConfig()
    assert config.stt_provider == "groq" and config.tts_provider == "edge"
    assert config.tts_voice == "en-US-AriaNeural" and config.auto_listen is False
    assert config.silence_timeout == 2.0 and config.stt_model == "small.en"
    monkeypatch.setenv(voice.STT_MODEL_ENV, " base ")
    assert voice.VoiceConfig().stt_model == "base" and voice.default_stt_model() == "base"
    assert config.stt_model == "small.en", "an instance built earlier keeps what it read"


def test_download_root_is_the_layouts_huggingface_folder_and_hf_home_is_not_exported() -> None:
    from nvh.integrations.workspace.storage import storage_layout

    assert voice.download_root() == storage_layout().cache_dir / "huggingface"
    assert "HF_HOME" not in voice.__dict__
    assert voice.model_repo("small.en") == "Systran/faster-whisper-small.en"
    assert voice.model_repo("distil-large-v3") == "Systran/faster-distil-whisper-large-v3"
    assert voice.model_repo("large") == "Systran/faster-whisper-large-v3"
    assert voice.model_repo("turbo") == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    assert voice.model_repo("someone/their-model") == "someone/their-model"
    assert voice.model_present(MODEL) is False and voice.model_bytes_on_disk(MODEL) == 0
    _write_weights()
    assert voice.model_present(MODEL) is True and voice.model_bytes_on_disk(MODEL) > 2048
    assert voice.model_present("base") is False


def test_a_snapshot_without_its_small_files_reads_as_not_downloaded(monkeypatch) -> None:
    """``model.bin`` alone cannot be loaded (CTranslate2 wants ``config.json`` and a vocabulary),
    so such a snapshot must read "not downloaded": the status says so, the mic stays off and
    ``nvh voice --download`` / the download job repair it instead of refusing as already present."""
    snap = _write_weights()
    (snap / "vocabulary.txt").unlink()
    assert voice.model_present(MODEL) is False
    _installed(monkeypatch)
    assert voice.voice_status()["stt"]["reason"] == voice.REASON_MODEL_MISSING
    (snap / "vocabulary.json").write_text("[]", encoding="utf-8")
    assert voice.model_present(MODEL) is True
    (snap / "config.json").unlink()
    assert voice.model_present(MODEL) is False


def test_a_bare_name_is_never_a_directory_and_the_api_allow_list_is_narrow(tmp_path, monkeypatch) -> None:
    """``nvh/data`` or ``converted`` relative to the server's CWD must not become a model
    directory for an API caller; only a name spelled as a path (``./x``, ``~/x``, absolute) is,
    and the CLI is the only surface that passes one. The API accepts sizes and the two Hugging
    Face owners faster-whisper's own table maps to — never ``someone/anything``."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "nvh" / "data").mkdir(parents=True)
    converted = tmp_path / "converted"
    converted.mkdir()
    for item in ("model.bin", "config.json", "vocabulary.json"):
        (converted / item).write_bytes(b"x")
    for bare in ("nvh/data", "nvh", "converted", "small.en"):
        assert voice._model_dir(bare) is None, bare
        assert voice.model_present(bare) is False, bare
    assert voice._model_dir(str(converted)) == converted
    assert voice._model_dir("./converted") == Path("converted")
    assert voice.model_present(str(converted)) is True and voice.model_present("./converted") is True
    assert voice.model_bytes_on_disk(str(converted)) == 3
    _not_installed(monkeypatch)
    assert voice.download_model(str(converted)) == converted, "a directory needs no fetch and no faster_whisper"
    for ok in ("small.en", "base", "turbo", "distil-large-v3", "Systran/faster-whisper-base",
               "mobiuslabsgmbh/faster-whisper-large-v3-turbo"):
        assert voice.is_catalog_model(ok), ok
    for bad in ("someone/anything", "nvh/data", "converted", "./converted", "small.en/evil", ""):
        assert not voice.is_catalog_model(bad), bad


def test_auto_picks_local_when_the_extra_imports_and_the_weights_are_present(monkeypatch, tmp_path) -> None:
    calls: list = []
    _installed(monkeypatch, calls=calls)
    _write_weights()
    clip = tmp_path / "clip.wav"
    clip.write_bytes(CLIP)

    result = voice.speech_to_text(str(clip))
    assert result.text == "hello from the fake"
    assert result.provider == "local" and result.model == MODEL
    assert result.cost_usd == 0.0 and result.cost_known is True
    assert isinstance(result.latency_ms, int) and result.latency_ms >= 0
    init = [c for c in calls if c[0] == "init"]
    assert init == [("init", MODEL, {
        "device": "cpu", "compute_type": "int8",
        "download_root": str(voice.download_root()), "local_files_only": True,
    })]
    assert calls[-1][0] == "transcribe" and calls[-1][1] == str(clip)

    voice.speech_to_text(str(clip), "local")
    assert len([c for c in calls if c[0] == "init"]) == 1, "the model is loaded once per process"
    assert set(result.as_dict()) == {"text", "provider", "model", "latency_ms", "cost_usd", "cost_known"}


def test_auto_reports_download_needed_then_falls_to_groq_when_a_key_exists(monkeypatch, tmp_path) -> None:
    _installed(monkeypatch)
    status = voice.voice_status()
    assert status["stt"]["local_installed"] is True and status["stt"]["local_model_present"] is False
    assert status["stt"]["local_available"] is False and status["stt"]["reason"] == voice.REASON_MODEL_MISSING
    assert status["stt"]["groq_available"] is False and status["stt"]["model"] == MODEL
    assert status["stt"]["download_root"] == str(voice.download_root())
    assert "nvh voice --download" in voice.stt_hint(status)

    clip = tmp_path / "clip.wav"
    clip.write_bytes(CLIP)
    with pytest.raises(voice.VoiceUnavailable) as unavailable:
        voice.speech_to_text(str(clip))
    assert unavailable.value.status["stt"]["reason"] == voice.REASON_MODEL_MISSING
    with pytest.raises(voice.VoiceUnavailable, match="model not downloaded"):
        voice.speech_to_text(str(clip), "local")

    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    captured = _patch_groq(monkeypatch, httpx.Response(200, json={"text": " groq heard this "}))
    result = voice.speech_to_text(str(clip))
    assert result.provider == "groq" and result.text == "groq heard this"
    assert result.model == voice.GROQ_STT_MODEL
    assert result.cost_usd is None and result.cost_known is False
    assert captured and captured[0]["headers"]["Authorization"] == "Bearer gsk-test"
    assert voice.stt_hint(voice.voice_status()) == ""


def test_auto_raises_unavailable_with_the_install_reason_when_nothing_is_there(monkeypatch, tmp_path) -> None:
    _not_installed(monkeypatch)
    monkeypatch.setattr(voice, "platform_has_wheel", lambda: True)  # the module seam, not sys.platform
    status = voice.voice_status()
    assert status["stt"] == {
        "local_installed": False, "local_model_present": False, "local_available": False,
        "groq_available": False, "model": MODEL, "reason": voice.REASON_NOT_INSTALLED,
        "download_root": str(voice.download_root()),
    }
    assert set(status["tts"]) == {"edge_available", "system_available"}
    assert "nvhive[voice]" in voice.stt_hint(status) and "nvh advisor add groq" in voice.stt_hint(status)
    clip = tmp_path / "clip.wav"
    clip.write_bytes(CLIP)
    with pytest.raises(voice.VoiceUnavailable, match="install nvhive\\[voice\\]") as unavailable:
        voice.speech_to_text(str(clip))
    assert unavailable.value.status == status
    with pytest.raises(voice.VoiceUnavailable, match="install nvhive"):
        voice.speech_to_text(str(clip), "local")
    with pytest.raises(ValueError, match="Unknown speech-to-text provider"):
        voice.speech_to_text(str(clip), "telepathy")


def test_explicit_groq_without_a_key_is_still_the_old_valueerror(tmp_path) -> None:
    with pytest.raises(ValueError, match="Groq API key") as raised:
        voice.speech_to_text(str(tmp_path / "missing.wav"), provider="groq")
    assert isinstance(raised.value, voice.VoiceUnavailable), "the route maps it to 503 like every other no-path case"


def test_voice_status_names_the_missing_wheel_on_windows_on_arm(monkeypatch) -> None:
    _not_installed(monkeypatch)
    # voice.py reads only ``sys.platform``: swapping the module's ``sys`` name keeps the
    # interpreter's own sys.platform intact for everything else in this process.
    monkeypatch.setattr(voice, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(voice.platform, "machine", lambda: "ARM64")
    status = voice.voice_status()
    assert voice.platform_has_wheel() is False
    assert status["stt"]["local_installed"] is False and status["stt"]["reason"] == voice.REASON_NO_WHEEL
    assert voice.stt_hint(status).startswith(voice.REASON_NO_WHEEL)
    monkeypatch.setattr(voice.platform, "machine", lambda: "AMD64")
    assert voice.platform_has_wheel() is True
    assert voice.voice_status()["stt"]["reason"] == voice.REASON_NOT_INSTALLED
    _installed(monkeypatch)
    monkeypatch.setattr(voice.platform, "machine", lambda: "ARM64")
    assert voice.voice_status()["stt"]["local_installed"] is True, "an importable module beats the platform guess"


def _patch_groq(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> list[dict[str, Any]]:
    """Swap ``httpx.Client`` for a recorder that answers ``response`` (a real Response, so
    ``raise_for_status`` behaves)."""
    captured: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def post(self, url: str, *, headers: dict, files: dict, data: dict, **kwargs: Any) -> httpx.Response:
            name, handle, mime = files["file"]
            captured.append({
                "url": url, "headers": headers, "filename": name, "mime": mime,
                "data": data, "bytes": handle.read(),
            })
            response.request = httpx.Request("POST", url)
            return response

    monkeypatch.setattr(voice.httpx, "Client", _Client)
    return captured


def test_groq_forwards_the_real_mime_type_and_filename(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HIVE_GROQ_API_KEY", "gsk-from-hive")  # the registry's third env name
    captured = _patch_groq(monkeypatch, httpx.Response(200, json={"text": "ok"}))
    clip = tmp_path / "take1.webm"
    clip.write_bytes(CLIP)

    voice.speech_to_text(str(clip), "groq", mime_type="audio/webm;codecs=opus", filename="take1.webm")
    assert captured[-1]["url"] == voice.GROQ_TRANSCRIPTIONS_URL
    assert captured[-1]["filename"] == "take1.webm" and captured[-1]["mime"] == "audio/webm"
    assert captured[-1]["data"] == {"model": voice.GROQ_STT_MODEL} and captured[-1]["bytes"] == CLIP
    assert captured[-1]["headers"]["Authorization"] == "Bearer gsk-from-hive"

    voice.speech_to_text(str(clip), "groq", mime_type="audio/ogg")
    assert captured[-1]["filename"] == "audio.ogg" and captured[-1]["mime"] == "audio/ogg"
    voice.speech_to_text(str(clip), "groq")  # the 0.45 default: a wav
    assert captured[-1]["filename"] == "audio.wav" and captured[-1]["mime"] == "audio/wav"


def test_groq_http_error_is_a_backend_error_not_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    _patch_groq(monkeypatch, httpx.Response(401, json={"error": {"message": "bad key"}}))
    clip = tmp_path / "clip.wav"
    clip.write_bytes(CLIP)
    with pytest.raises(voice.VoiceBackendError, match="HTTP 401"):
        voice.speech_to_text(str(clip), "groq")


def test_local_decoder_failure_is_a_backend_error(monkeypatch, tmp_path) -> None:
    _installed(monkeypatch, fail=RuntimeError("decoder choked"))
    _write_weights()
    clip = tmp_path / "clip.wav"
    clip.write_bytes(CLIP)
    with pytest.raises(voice.VoiceBackendError, match="decoder choked"):
        voice.speech_to_text(str(clip), "local")


def test_whisper_cli_provider_still_shells_out_into_a_temp_dir(monkeypatch, tmp_path) -> None:
    """The CLI writes ``<clip>.txt`` into ``--output_dir`` (default: the working directory), so
    it gets a temporary directory that is removed with the transcript, and the text comes from
    that file rather than the tool's timestamped stdout."""
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        seen.append(cmd)
        out_dir = Path(cmd[cmd.index("--output_dir") + 1])
        assert out_dir.is_dir() and out_dir.resolve() != Path.cwd().resolve()
        (out_dir / "clip.txt").write_text(" from the file \n", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="[00:00.000 --> 00:02.000]  from the cli\n", stderr="")

    monkeypatch.setattr(voice.subprocess, "run", fake_run)
    result = voice.speech_to_text(str(tmp_path / "clip.wav"), "whisper-cli")
    assert result.text == "from the file" and result.provider == "whisper-cli"
    assert result.cost_usd == 0.0 and result.cost_known is True
    assert seen[0][:2] == ["whisper", str(tmp_path / "clip.wav")]
    assert not Path(seen[0][seen[0].index("--output_dir") + 1]).exists(), "the transcript dir is gone"
    assert not (Path.cwd() / "clip.txt").exists()

    monkeypatch.setattr(
        voice.subprocess, "run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=" stdout only \n", stderr=""),
    )
    assert voice.speech_to_text(str(tmp_path / "clip.wav"), "whisper-cli").text == "stdout only"

    monkeypatch.setattr(voice.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("whisper")))
    with pytest.raises(voice.VoiceUnavailable, match="not on PATH"):
        voice.speech_to_text(str(tmp_path / "clip.wav"), "whisper-cli")


def test_download_model_fetches_into_download_root_and_reports_progress(monkeypatch) -> None:
    calls: list = []
    _installed(monkeypatch, calls=calls)
    seen: list[int] = []
    path = voice.download_model(MODEL, progress=seen.append)
    assert path == _write_weights() and voice.model_present(MODEL) is True
    assert calls == [("download", MODEL, {"cache_dir": str(voice.download_root()), "local_files_only": False})]
    assert seen and seen[-1] == voice.model_bytes_on_disk(MODEL) > 0
    assert voice.approx_model_bytes(MODEL) > 0 and voice.approx_model_bytes("someone/odd") == 0

    _not_installed(monkeypatch)
    with pytest.raises(voice.VoiceUnavailable):
        voice.download_model(MODEL)


def test_system_tts_gets_the_text_on_stdin_never_as_an_argument(monkeypatch, tmp_path) -> None:
    """``POST /v1/voice/speak`` forwards caller text. As a bare argv element, ``-w/some/path``
    would make espeak overwrite that file and ``-f/etc/shadow`` would make espeak or ``say``
    read it back as speech; edge-tts rejects a ``--text`` value with a leading dash and falls
    through to those tools. So edge-tts gets ``--text=<text>`` and the system voices read stdin."""
    import asyncio

    spawned: list[dict[str, Any]] = []

    class _Proc:
        def __init__(self, code: int) -> None:
            self.returncode = code
            self.stdin_bytes: bytes | None = None

        async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
            self.stdin_bytes = data
            return b"", b""

        async def wait(self) -> int:
            return self.returncode

    async def fake_exec(*argv: str, **kwargs: Any) -> _Proc:
        proc = _Proc(1 if argv[0] == "edge-tts" else 0)  # edge-tts "fails": the fallback runs
        spawned.append({"argv": list(argv), "kwargs": kwargs, "proc": proc})
        return proc

    monkeypatch.setattr(voice.asyncio, "create_subprocess_exec", fake_exec)
    hostile = "-w/home/user/.bashrc"
    out = str(tmp_path / "out.mp3")

    monkeypatch.setattr(voice, "sys", types.SimpleNamespace(platform="linux"))
    assert asyncio.run(voice.text_to_speech(hostile, output_path=out, provider="edge")) == out
    edge, espeak = spawned[0], spawned[1]
    assert edge["argv"][0] == "edge-tts" and f"--text={hostile}" in edge["argv"]
    assert hostile not in edge["argv"], "the = form keeps a leading dash from being read as an option"
    assert espeak["argv"] == ["espeak", "-w", out, "--stdin"]
    assert espeak["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    assert espeak["proc"].stdin_bytes == hostile.encode("utf-8")

    spawned.clear()
    monkeypatch.setattr(voice, "sys", types.SimpleNamespace(platform="darwin"))
    aiff = asyncio.run(voice.text_to_speech(hostile, output_path=out, provider="system"))
    assert aiff.endswith(".aiff")
    say = spawned[-1]
    assert say["argv"] == ["say", "-o", aiff, "-f", "-"]
    assert say["proc"].stdin_bytes == hostile.encode("utf-8")
    for call in spawned:
        assert hostile not in call["argv"]


def test_download_model_wraps_a_failed_fetch(monkeypatch) -> None:
    mod = _installed(monkeypatch)

    def boom(name: str, **kwargs: Any) -> str:
        raise OSError("no route to host")

    mod.download_model = boom  # type: ignore[attr-defined]
    with pytest.raises(voice.VoiceBackendError, match="no route to host"):
        voice.download_model(MODEL)


# ---------------------------------------------------------------------------
# nvh/api/routes/voice.py on a minimal app
# ---------------------------------------------------------------------------


@pytest.fixture()
def voice_app() -> FastAPI:
    app = FastAPI()
    app.include_router(voice_routes.router)

    @app.get("/v1/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture()
def client(voice_app: FastAPI) -> TestClient:
    return TestClient(voice_app, raise_server_exceptions=True)


def _post_clip(
    client: TestClient,
    *,
    mime: str = "audio/webm",
    name: str = "clip.webm",
    body: bytes = CLIP,
    provider: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    data = {"provider": provider} if provider else None
    return client.post("/v1/voice/transcribe", files={"file": (name, body, mime)}, data=data, headers=headers)


def test_transcribe_route_answers_the_envelope_and_unlinks_the_clip(monkeypatch, client) -> None:
    calls: list = []
    _installed(monkeypatch, calls=calls)
    _write_weights()
    resp = _post_clip(client, mime="audio/webm;codecs=opus")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["text"] == "hello from the fake" and data["provider"] == "local" and data["model"] == MODEL
    assert data["cost_usd"] == 0.0 and data["cost_known"] is True and isinstance(data["latency_ms"], int)
    transcribed = Path(calls[-1][1])
    assert transcribed.suffix == ".webm" and not transcribed.exists(), "the temp clip is gone"

    resp = _post_clip(client, mime="audio/wav", name="../../etc/passwd.wav", provider="local")
    assert resp.status_code == 200 and Path(calls[-1][1]).suffix == ".wav"


def test_transcribe_route_unlinks_the_clip_when_the_decoder_fails(monkeypatch, client) -> None:
    calls: list = []
    _installed(monkeypatch, calls=calls, fail=RuntimeError("decoder choked"))
    _write_weights()
    resp = _post_clip(client)
    assert resp.status_code == 502 and "decoder choked" in resp.json()["detail"]
    assert not Path(calls[-1][1]).exists()


def test_transcribe_route_refuses_wrong_type_oversize_empty_and_unknown_provider(monkeypatch, client) -> None:
    _installed(monkeypatch)
    _write_weights()
    assert _post_clip(client, mime="text/plain", name="notes.txt", body=b"hello").status_code == 415
    assert _post_clip(client, mime="image/png", name="a.png").status_code == 415
    too_big = _post_clip(client, body=b"\0" * (voice_routes.MAX_AUDIO_BYTES + 1024))
    assert too_big.status_code == 413 and "20 MB" in too_big.json()["detail"]
    assert _post_clip(client, body=b"").status_code == 400
    assert _post_clip(client, provider="telepathy").status_code == 422
    assert _post_clip(client, provider="whisper-cli").status_code == 422, "the openai-whisper CLI is nvh voice's alone"
    for bad in ("../x", "someone/anything", "nvh/data", "x/y/z"):
        bad_model = client.post(
            "/v1/voice/transcribe", files={"file": ("a.webm", CLIP, "audio/webm")}, data={"model": bad},
        )
        assert bad_model.status_code == 422, (bad, bad_model.text)
        assert "Systran/" in bad_model.json()["detail"]
    assert client.post("/v1/voice/transcribe", json={"not": "multipart"}).status_code == 415
    assert client.post("/v1/voice/transcribe", files={"other": ("a.webm", CLIP, "audio/webm")}).status_code == 422


def test_oversized_clip_is_refused_mid_stream_before_the_rest_is_read(tmp_path) -> None:
    """The route reads the request stream itself: the 413 fires on the chunk that crosses the cap
    and the chunks after it are never pulled (FastAPI's ``UploadFile`` would have parsed and
    spooled the whole body to a temp file before the handler ran). TestClient hands the body
    over as one message, so this drives ``_receive_clip`` with a chunked fake request."""
    import asyncio

    boundary = b"nvh-test-boundary"
    head = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="clip.webm"\r\n'
        b"Content-Type: audio/webm\r\n\r\n"
    )
    tail = b"\r\n--" + boundary + b"--\r\n"
    chunk = b"\0" * (1024 * 1024)
    chunks_total = voice_routes.MAX_AUDIO_BYTES // len(chunk) + 8  # eight past the cap
    pulled: list[int] = []

    async def stream():
        yield head
        for index in range(chunks_total):
            pulled.append(index)
            yield chunk
        yield tail

    request = types.SimpleNamespace(
        headers={"content-type": f"multipart/form-data; boundary={boundary.decode()}"}, stream=stream,
    )
    reader = voice_routes._ClipReader()
    with pytest.raises(HTTPException) as refused:
        asyncio.run(voice_routes._receive_clip(request, reader))
    assert refused.value.status_code == 413
    assert len(pulled) <= chunks_total - 6, f"pulled {len(pulled)} of {chunks_total} chunks past the cap"
    assert reader.path is not None and reader.path.suffix == ".webm"
    assert reader.path.stat().st_size <= voice_routes.MAX_AUDIO_BYTES, "nothing beyond the cap was spooled"
    reader.path.unlink()

    # The part's type is judged when its headers arrive, before any audio byte is pulled.
    pulled.clear()
    text_head = head.replace(b"audio/webm", b"text/plain")

    async def text_stream():
        yield text_head
        pulled.append(0)
        yield b"hello"
        yield tail

    reader = voice_routes._ClipReader()
    request = types.SimpleNamespace(
        headers={"content-type": f"multipart/form-data; boundary={boundary.decode()}"}, stream=text_stream,
    )
    with pytest.raises(HTTPException) as unsupported:
        asyncio.run(voice_routes._receive_clip(request, reader))
    assert unsupported.value.status_code == 415 and pulled == [] and reader.path is None


def test_transcribe_route_503_carries_status_downloading_and_hint(monkeypatch, client) -> None:
    _not_installed(monkeypatch)
    monkeypatch.setattr(voice, "platform_has_wheel", lambda: True)
    resp = _post_clip(client)
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert set(detail) == {"message", "status", "downloading", "hint"}
    assert detail["downloading"] is False and detail["status"]["stt"]["reason"] == voice.REASON_NOT_INSTALLED
    assert "nvhive[voice]" in detail["hint"] and "install nvhive[voice]" in detail["message"]

    _installed(monkeypatch)  # installed, weights absent: the download-needed case
    detail = _post_clip(client).json()["detail"]
    assert detail["status"]["stt"]["reason"] == voice.REASON_MODEL_MISSING and "nvh voice --download" in detail["hint"]

    monkeypatch.setattr(voice, "speech_to_text", lambda *a, **k: (_ for _ in ()).throw(voice.VoiceUnavailable("x")))
    assert _post_clip(client).json()["detail"]["status"]["stt"]["model"] == MODEL, "status is computed when the error carried none"


def test_status_route_shape(monkeypatch, client) -> None:
    _installed(monkeypatch)
    data = client.get("/v1/voice/status").json()["data"]
    assert set(data) == {"stt", "tts", "downloading", "download_job_id", "hint"}
    assert data["downloading"] is False and data["download_job_id"] is None
    assert data["stt"]["model"] == MODEL and data["stt"]["reason"] == voice.REASON_MODEL_MISSING
    assert client.get("/v1/voice/status", params={"model": "base.en"}).json()["data"]["stt"]["model"] == "base.en"
    repo = "Systran/faster-whisper-base"
    assert client.get("/v1/voice/status", params={"model": repo}).json()["data"]["stt"]["model"] == repo
    for bad in ("../x", "someone/anything", "nvh/data"):
        assert client.get("/v1/voice/status", params={"model": bad}).status_code == 422, bad


def test_a_slow_transcription_does_not_block_the_event_loop(monkeypatch, voice_app) -> None:
    """The route awaits ``to_thread`` behind the semaphore: while the fake decoder sleeps a second,
    ``GET /v1/health`` on the same loop answers at once."""
    inside_decoder = threading.Event()
    _installed(monkeypatch, sleep_s=1.0, transcribe_started=inside_decoder)
    _write_weights()
    with TestClient(voice_app, raise_server_exceptions=True) as shared:
        with ThreadPoolExecutor(max_workers=1) as pool:
            slow = pool.submit(_post_clip, shared)
            # Time the health call only once the decoder is known to be inside ``transcribe``
            # (a fixed sleep would pass vacuously on a loaded runner where the POST lags).
            assert inside_decoder.wait(timeout=10), "the slow request never reached the decoder"
            started = time.perf_counter()
            health = shared.get("/v1/health")
            elapsed = time.perf_counter() - started
            resp = slow.result(timeout=15)
    assert health.status_code == 200 and elapsed < 0.6, f"health waited {elapsed:.2f}s behind the decoder"
    assert resp.status_code == 200 and resp.json()["data"]["provider"] == "local"


def _wait_terminal(job_id: str, timeout_s: float = 10.0) -> dict[str, Any]:
    """Poll the job record until it reaches a terminal status.

    The worker rewrites the record with ``Path.replace`` between polls; on Windows a read that
    lands mid-replace raises a transient ``OSError`` (``PermissionError``), so that tick is
    skipped and the next one reads the new file. CI (ubuntu) never sees it."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            job = jobs.load_job(job_id, reconcile=False)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)
            continue
        if job["status"] in jobs.TERMINAL_STATUSES or time.monotonic() > deadline:
            return job
        time.sleep(0.02)


def test_download_route_runs_one_job_and_refuses_a_second(monkeypatch, voice_app) -> None:
    gate = threading.Event()
    calls: list = []
    _installed(monkeypatch, calls=calls, download_gate=gate)
    with TestClient(voice_app, raise_server_exceptions=True) as shared:
        first = shared.post("/v1/voice/download", json={"model": MODEL})
        assert first.status_code == 202, first.text
        job_id = first.json()["data"]["job_id"]
        assert first.json()["data"]["model"] == MODEL and job_id
        second = shared.post("/v1/voice/download")
        assert second.status_code == 409 and second.json()["detail"]["job_id"] == job_id
        status = shared.get("/v1/voice/status").json()["data"]
        assert status["downloading"] is True and status["download_job_id"] == job_id
        assert "downloading" in status["hint"]
        refused = _post_clip(shared)
        assert refused.status_code == 503 and refused.json()["detail"]["downloading"] is True
        gate.set()
        job = _wait_terminal(job_id)
        assert job["status"] == "complete", job
        after = shared.get("/v1/voice/status").json()["data"]
        assert after["downloading"] is False and after["download_job_id"] is None
        assert after["stt"]["local_model_present"] is True and after["stt"]["local_available"] is True
    assert job["kind"] == voice_routes.VOICE_DOWNLOAD_KIND and job["request"]["model"] == MODEL
    assert job["request"]["download_root"] == str(voice.download_root()) and job["progress"] == 100
    events = jobs.read_events(job_id)
    assert events[0]["event"] == "step" and events[-1]["event"] == "complete"
    assert events[-1]["payload"]["path"] == str(_write_weights()) and events[-1]["payload"]["bytes"] > 0
    assert calls[-1] == ("download", MODEL, {"cache_dir": str(voice.download_root()), "local_files_only": False})
    assert voice_routes.download_in_flight() is None


def test_download_route_records_a_failed_fetch_on_the_job(monkeypatch, voice_app) -> None:
    mod = _installed(monkeypatch)

    def boom(name: str, **kwargs: Any) -> str:
        raise OSError("no route to host")

    mod.download_model = boom  # type: ignore[attr-defined]
    with TestClient(voice_app, raise_server_exceptions=True) as shared:
        job_id = shared.post("/v1/voice/download").json()["data"]["job_id"]
        job = _wait_terminal(job_id)
    assert job["status"] == "failed" and "no route to host" in job["message"]


def test_download_route_503_without_the_extra_and_422_on_a_bad_name(monkeypatch, client) -> None:
    _not_installed(monkeypatch)
    resp = client.post("/v1/voice/download")
    assert resp.status_code == 503 and resp.json()["detail"]["status"]["stt"]["local_installed"] is False
    assert client.post("/v1/voice/download", json={"model": "../../etc"}).status_code == 422
    assert client.post("/v1/voice/download", json={"model": "someone/anything"}).status_code == 422
    assert client.post("/v1/voice/download", json={"model": "nvh/data"}).status_code == 422
    assert voice_routes.download_in_flight() is None


def test_voice_download_job_wording_is_kind_aware(tmp_path, monkeypatch) -> None:
    """An orphaned or canceled ``voice-model-download`` job says to start the download again
    (the partial snapshot is reused) rather than "Retry to resume setup" / "Install job canceled"."""
    monkeypatch.setenv("NVH_HOME", str(tmp_path / "nvh"))
    kind = voice_routes.VOICE_DOWNLOAD_KIND
    job = jobs.create_job(kind=kind, title="Download speech model small.en", request={"model": MODEL})
    jobs.append_event(job["id"], {"event": "step", "status": "running", "message": "working"})
    reconciled = {j["id"]: j for j in jobs.list_jobs(limit=10)}[job["id"]]
    assert reconciled["status"] == "interrupted"
    assert reconciled["message"] == (
        "Server restarted or disconnected while the speech model was downloading; "
        "start the download again (files already fetched are reused)."
    )
    assert jobs._canceled_message(kind, by_user=True).startswith("Download canceled by user")
    stopped = jobs._canceled_message(kind, by_user=False)
    assert "server stopped" in stopped and "files already fetched are reused" in stopped


def test_speak_501_without_edge_tts(monkeypatch, client) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: None)
    resp = client.post("/v1/voice/speak", json={"text": "hello"})
    assert resp.status_code == 501 and resp.json()["detail"] == voice_routes.SPEAK_NOT_AVAILABLE


def test_speak_returns_edge_tts_audio_and_unlinks_it(monkeypatch, client) -> None:
    monkeypatch.setattr(voice, "edge_tts_available", lambda: True)
    produced: list[tuple] = []

    async def fake_tts(text: str, output_path: str | None = None, provider: str = "edge", voice: str = "") -> str:
        assert output_path is not None
        Path(output_path).write_bytes(b"ID3" + b"\0" * 32)
        produced.append((text, output_path, provider, voice))
        return output_path

    monkeypatch.setattr(voice, "text_to_speech", fake_tts)
    resp = client.post("/v1/voice/speak", json={"text": "hello there", "voice": "en-GB-SoniaNeural"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("audio/mpeg") and resp.content.startswith(b"ID3")
    assert resp.headers["cache-control"] == "no-store"
    assert produced[0][0] == "hello there" and produced[0][2] == "edge" and produced[0][3] == "en-GB-SoniaNeural"
    assert not Path(produced[0][1]).exists()
    assert client.post("/v1/voice/speak", json={"text": "x"}).status_code == 200
    assert produced[-1][3] == voice.DEFAULT_TTS_VOICE
    assert client.post("/v1/voice/speak", json={"text": "x" * 4001}).status_code == 422
    assert client.post("/v1/voice/speak", json={"text": ""}).status_code == 422
    assert client.post("/v1/voice/speak", json={"text": "x", "voice": "rm -rf"}).status_code == 422


def test_speak_502_when_edge_tts_produces_nothing(monkeypatch, client) -> None:
    monkeypatch.setattr(voice, "edge_tts_available", lambda: True)

    async def silent(text: str, output_path: str | None = None, provider: str = "edge", voice: str = "") -> str:
        return ""

    monkeypatch.setattr(voice, "text_to_speech", silent)
    assert client.post("/v1/voice/speak", json={"text": "hello"}).status_code == 502


def test_keyed_deployment_requires_the_key_on_every_route(monkeypatch) -> None:
    monkeypatch.setenv("HIVE_API_KEY", "s3cret")
    app = FastAPI()
    app.include_router(voice_routes.router)
    client = TestClient(app, raise_server_exceptions=True)
    assert client.get("/v1/voice/status").status_code == 401
    assert _post_clip(client).status_code == 401
    assert client.post("/v1/voice/download").status_code == 401
    assert client.post("/v1/voice/speak", json={"text": "hi"}).status_code == 401
    ok = client.get("/v1/voice/status", headers={"X-Hive-API-Key": "s3cret"})
    assert ok.status_code == 200 and {"stt", "tts", "downloading", "download_job_id", "hint"} <= set(ok.json()["data"])
    assert client.get("/v1/voice/status", headers={"Authorization": "Bearer s3cret"}).status_code == 200
