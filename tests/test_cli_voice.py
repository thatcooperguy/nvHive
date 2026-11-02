"""`nvh voice` after 0.46 §8 (D1p): `--stt`, `--stt-model`, `--status`, `--download`
and the recording path that now runs the synchronous `speech_to_text` off the loop.

The Typer app runs in-process through CliRunner. ``faster_whisper`` is never
installed here (Windows on Arm has no wheel and the venv deliberately lacks
it), so the local path is a fake module placed in ``sys.modules`` through
``monkeypatch.setitem`` — ``None`` spells "not installed" — and the weights
marker is written under the hermetic home's ``download_root()`` (conftest
points ``NVH_HOME`` at a fresh directory and clears every ``NVH_*`` variable).
The recording tests patch ``record_audio`` / ``speech_to_text`` on
``nvh.core.voice`` (the command imports them at call time) and swap the Engine
for a stub, so nothing records, transcribes for real or reaches a provider.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import nvh.cli.main as cli_main
import nvh.core.engine as engine_module
from nvh.core import voice

MODEL = voice.DEFAULT_STT_MODEL


@pytest.fixture(autouse=True)
def _quiet_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Groq key from env or keyring, no leftover model handles, the default model,
    faster_whisper absent unless a test installs its fake, and a wide console so rich
    never wraps a sentence a test asserts on."""
    for name in voice.GROQ_KEY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(voice, "_keyring_groq_key", lambda: None)
    monkeypatch.setattr(voice, "_LOCAL_MODELS", {})
    monkeypatch.delenv(voice.STT_MODEL_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    monkeypatch.setenv("COLUMNS", "200")


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _write_weights(model: str = MODEL) -> Path:
    """The CTranslate2 trio a real snapshot carries — ``model_present`` wants all of it."""
    repo = voice.model_repo(model).replace("/", "--")
    snap = voice.download_root() / f"models--{repo}" / "snapshots" / "abc123"
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "model.bin").write_bytes(b"\0" * 2048)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    (snap / "vocabulary.txt").write_text("<|endoftext|>\n", encoding="utf-8")
    return snap


def _fake_faster_whisper(
    calls: list[tuple[str, dict[str, Any]]], *, fail: Exception | None = None
) -> types.ModuleType:
    """A ``faster_whisper`` stand-in whose ``download_model`` writes the snapshot marker
    the real one would (or fails the way a broken fetch does)."""
    mod = types.ModuleType("faster_whisper")

    def download_model(name: str, **kwargs: Any) -> str:
        calls.append((name, kwargs))
        if fail is not None:
            raise fail
        return str(_write_weights(name))

    mod.download_model = download_model  # type: ignore[attr-defined]
    mod.WhisperModel = object  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# --help and --stt validation
# ---------------------------------------------------------------------------


_ANSI = re.compile(r"\[[0-9;]*m")


def _plain(text: str) -> str:
    """Typer forces colour whenever ``GITHUB_ACTIONS`` (or ``FORCE_COLOR``) is set and its
    highlighter styles the first dash of an option on its own, so on CI ``--duration``
    arrives as two ANSI spans and a raw substring check misses it. Strip the codes."""
    return _ANSI.sub("", text)


def test_help_lists_the_new_flags_and_the_four_stt_paths(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["voice", "--help"])
    assert result.exit_code == 0
    output = _plain(result.output)
    for flag in ("--duration", "--stt", "--stt-model", "--tts", "--no-speak", "--download", "--status"):
        assert flag in output, flag
    for provider in voice.STT_PROVIDERS:
        assert provider in output, provider


def test_unknown_stt_value_exits_2_before_anything_records(runner: CliRunner, monkeypatch) -> None:
    def _no_record(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("record_audio must not run for a bad --stt value")

    monkeypatch.setattr(voice, "record_audio", _no_record)
    result = runner.invoke(cli_main.app, ["voice", "--stt", "bogus"])
    assert result.exit_code == 2
    assert "bogus" in result.output
    assert ", ".join(voice.STT_PROVIDERS) in result.output


# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------


def test_status_names_the_missing_pieces_without_the_extra_or_a_key(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["voice", "--status"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "faster-whisper" in out and "Groq Whisper" in out
    assert voice.REASON_NOT_INSTALLED in out, "rich markup must not eat the [voice] in the reason"
    assert "nvh advisor add groq" in out
    assert f"model: {MODEL}" in out and "huggingface" in out
    assert voice.stt_hint(voice.voice_status(MODEL)) in out


def test_status_reports_a_ready_local_path(runner: CliRunner, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_faster_whisper([]))
    _write_weights()
    result = runner.invoke(cli_main.app, ["voice", "--status"])
    assert result.exit_code == 0, result.output
    assert "speech-to-text is ready" in result.output
    assert voice.REASON_NOT_INSTALLED not in result.output
    assert voice.REASON_MODEL_MISSING not in result.output


def test_status_reports_the_download_still_needed(runner: CliRunner, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_faster_whisper([]))
    result = runner.invoke(cli_main.app, ["voice", "--status"])
    assert result.exit_code == 0, result.output
    assert voice.REASON_MODEL_MISSING in result.output
    assert "nvh voice --download" in result.output


def test_status_honours_stt_model_then_the_env(runner: CliRunner, monkeypatch) -> None:
    result = runner.invoke(cli_main.app, ["voice", "--status", "--stt-model", "base"])
    assert result.exit_code == 0 and "model: base" in result.output
    monkeypatch.setenv(voice.STT_MODEL_ENV, "tiny.en")
    result = runner.invoke(cli_main.app, ["voice", "--status"])
    assert result.exit_code == 0 and "model: tiny.en" in result.output
    result = runner.invoke(cli_main.app, ["voice", "--status", "--stt-model", "base"])
    assert result.exit_code == 0 and "model: base" in result.output, "the flag wins over the env"


# ---------------------------------------------------------------------------
# --download
# ---------------------------------------------------------------------------


def test_download_fetches_into_the_explicit_cache_dir_and_exits_0(runner: CliRunner, monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_faster_whisper(calls))
    assert not voice.model_present(MODEL)
    result = runner.invoke(cli_main.app, ["voice", "--download"])
    assert result.exit_code == 0, result.output
    assert calls == [(MODEL, {"cache_dir": str(voice.download_root()), "local_files_only": False})]
    assert voice.model_present(MODEL)
    assert "ready at" in result.output
    assert "HF_HOME" not in result.output


def test_download_honours_stt_model(runner: CliRunner, monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_faster_whisper(calls))
    result = runner.invoke(cli_main.app, ["voice", "--download", "--stt-model", "base"])
    assert result.exit_code == 0, result.output
    assert [name for name, _ in calls] == ["base"]
    assert voice.model_present("base") and not voice.model_present(MODEL)


def test_download_without_the_extra_exits_1_with_the_install_hint(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["voice", "--download"])
    assert result.exit_code == 1
    assert "nvhive[voice]" in result.output
    assert "Traceback" not in result.output
    assert not voice.model_present(MODEL)


def test_download_is_a_no_op_when_the_weights_are_present(runner: CliRunner) -> None:
    _write_weights()
    # faster_whisper is "not installed" here: presence is checked before anything imports.
    result = runner.invoke(cli_main.app, ["voice", "--download"])
    assert result.exit_code == 0, result.output
    assert "already downloaded" in result.output


def test_download_repairs_a_snapshot_that_lost_its_small_files(runner: CliRunner, monkeypatch) -> None:
    """``model.bin`` without ``config.json`` / a vocabulary is not "already downloaded": the one
    CLI path meant to fetch the weights must run the download, which fills in what is missing."""
    snap = _write_weights()
    (snap / "config.json").unlink()
    (snap / "vocabulary.txt").unlink()
    assert not voice.model_present(MODEL)
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setitem(sys.modules, "faster_whisper", _fake_faster_whisper(calls))
    result = runner.invoke(cli_main.app, ["voice", "--download"])
    assert result.exit_code == 0, result.output
    assert "already downloaded" not in result.output and "ready at" in result.output
    assert [name for name, _ in calls] == [MODEL]
    assert voice.model_present(MODEL)


def test_download_failure_is_a_one_line_error(runner: CliRunner, monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setitem(
        sys.modules, "faster_whisper", _fake_faster_whisper(calls, fail=OSError("network down"))
    )
    result = runner.invoke(cli_main.app, ["voice", "--download"])
    assert result.exit_code == 1
    assert "network down" in result.output
    assert "Traceback" not in result.output
    assert not voice.model_present(MODEL)


# ---------------------------------------------------------------------------
# The recording path
# ---------------------------------------------------------------------------


class _StubEngine:
    """Enough of `Engine` for `_run_voice`: initialise, answer, record the prompt."""

    prompts: list[str] = []

    def __init__(self, config: Any = None, **kwargs: Any) -> None:
        pass

    async def initialize(self) -> None:
        return None

    async def query(self, *, prompt: str, provider: Any = None, model: Any = None, stream: bool = False) -> Any:
        type(self).prompts.append(prompt)
        return types.SimpleNamespace(content="the answer")


@pytest.fixture()
def recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A stubbed Engine and a `record_audio` that hands back a wav path without a microphone."""
    monkeypatch.setattr(engine_module, "Engine", _StubEngine)
    monkeypatch.setattr(_StubEngine, "prompts", [])
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF" + b"\0" * 64)

    async def _record(duration: float = 10.0, output_path: str | None = None) -> str:
        return str(clip)

    monkeypatch.setattr(voice, "record_audio", _record)
    return clip


def _result(**overrides: Any) -> voice.TranscriptResult:
    base: dict[str, Any] = {
        "text": "  hello there ",
        "provider": "local",
        "model": MODEL,
        "latency_ms": 12,
        "cost_usd": 0.0,
        "cost_known": True,
    }
    base.update(overrides)
    return voice.TranscriptResult(**base)


def test_transcript_goes_through_speech_to_text_then_to_the_engine(
    runner: CliRunner, monkeypatch, recorded: Path
) -> None:
    seen: list[tuple[Any, ...]] = []

    def _stt(audio_path: str, provider: str = "auto", *, mime_type: str = "audio/wav",
             filename: str | None = None, model: str | None = None) -> voice.TranscriptResult:
        seen.append((audio_path, provider, mime_type, filename, model))
        return _result()

    monkeypatch.setattr(voice, "speech_to_text", _stt)
    result = runner.invoke(cli_main.app, ["voice", "--no-speak", "--stt", "local", "--stt-model", MODEL])
    assert result.exit_code == 0, result.output
    assert seen == [(str(recorded), "local", "audio/wav", "clip.wav", MODEL)]
    assert "You: hello there" in result.output
    assert "$0, on this device" in result.output
    assert _StubEngine.prompts == ["hello there"]
    assert "the answer" in result.output


def test_default_stt_is_auto_and_a_groq_result_says_cost_unknown(
    runner: CliRunner, monkeypatch, recorded: Path
) -> None:
    seen: list[str] = []

    def _stt(audio_path: str, provider: str = "auto", **kwargs: Any) -> voice.TranscriptResult:
        seen.append(provider)
        return _result(provider="groq", model=voice.GROQ_STT_MODEL, cost_usd=None, cost_known=False)

    monkeypatch.setattr(voice, "speech_to_text", _stt)
    result = runner.invoke(cli_main.app, ["voice", "--no-speak"])
    assert result.exit_code == 0, result.output
    assert seen == ["auto"]
    assert "cost unknown" in result.output
    assert voice.GROQ_STT_MODEL in result.output


def test_no_path_is_a_one_line_refusal_with_the_hint(runner: CliRunner, monkeypatch, recorded: Path) -> None:
    status = voice.voice_status(MODEL)

    def _stt(*args: Any, **kwargs: Any) -> voice.TranscriptResult:
        raise voice.VoiceUnavailable("no speech-to-text path: install nvhive[voice] and no Groq key", status)

    monkeypatch.setattr(voice, "speech_to_text", _stt)
    result = runner.invoke(cli_main.app, ["voice", "--no-speak"])
    assert result.exit_code == 1
    assert "no speech-to-text path" in result.output
    assert voice.stt_hint(status) in result.output
    assert "Traceback" not in result.output
    assert _StubEngine.prompts == []


def test_empty_transcript_exits_0_without_asking_the_engine(runner: CliRunner, monkeypatch, recorded: Path) -> None:
    monkeypatch.setattr(voice, "speech_to_text", lambda *a, **k: _result(text="   "))
    result = runner.invoke(cli_main.app, ["voice", "--no-speak"])
    assert result.exit_code == 0, result.output
    assert "No speech detected" in result.output
    assert _StubEngine.prompts == []


def test_backend_failure_is_reported_not_raised(runner: CliRunner, monkeypatch, recorded: Path) -> None:
    def _stt(*args: Any, **kwargs: Any) -> voice.TranscriptResult:
        raise voice.VoiceBackendError("Groq answered 500")

    monkeypatch.setattr(voice, "speech_to_text", _stt)
    result = runner.invoke(cli_main.app, ["voice", "--no-speak"])
    assert result.exit_code == 1
    assert "Transcription failed: Groq answered 500" in result.output
    assert _StubEngine.prompts == []
