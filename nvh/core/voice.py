"""NVHive Voice I/O — speak to NVHive, hear responses.

Uses:
- Speech-to-text: Whisper via Groq (free, fast) or local Whisper via Ollama
- Text-to-speech: Edge TTS (free, no API key) or system TTS

No additional dependencies required — uses httpx for API calls
and subprocess for system TTS fallback.
"""

import asyncio
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass
class VoiceConfig:
    stt_provider: str = "groq"     # "groq" (free whisper), "local" (system mic)
    tts_provider: str = "edge"     # "edge" (free), "system" (OS TTS)
    tts_voice: str = "en-US-AriaNeural"  # Edge TTS voice
    auto_listen: bool = False      # continuously listen
    silence_timeout: float = 2.0   # seconds of silence to stop recording


async def speech_to_text(audio_path: str, provider: str = "groq") -> str:
    """Convert audio file to text.

    Groq provides free Whisper API — fastest STT available.
    Falls back to local whisper if available.
    """
    import os

    if provider == "groq":
        import httpx
        key = os.environ.get("GROQ_API_KEY", "")
        if not key:
            # Try keyring
            try:
                import keyring
                key = keyring.get_password("nvhive", "groq_api_key") or ""
            except Exception:
                pass

        if not key:
            raise ValueError("Groq API key needed for voice input. Run: nvh groq")

        async with httpx.AsyncClient() as client:
            with open(audio_path, "rb") as f:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": ("audio.wav", f, "audio/wav")},
                    data={"model": "whisper-large-v3"},
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json().get("text", "")

    elif provider == "local":
        # Try local whisper binary
        result = subprocess.run(
            ["whisper", audio_path, "--model", "base", "--output_format", "txt"],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.strip()

    return ""


async def text_to_speech(
    text: str,
    output_path: str | None = None,
    provider: str = "edge",
    voice: str = "en-US-AriaNeural",
) -> str:
    """Convert text to speech audio.

    Edge TTS is free, high quality, no API key needed.
    Falls back to system TTS (espeak/say).

    Returns path to the audio file.
    """
    if output_path is None:
        output_path = tempfile.mktemp(suffix=".mp3")

    if provider == "edge":
        try:
            # Use edge-tts if installed, otherwise fall back
            proc = await asyncio.create_subprocess_exec(
                "edge-tts", "--voice", voice, "--text", text, "--write-media", output_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                return output_path
        except (TimeoutError, FileNotFoundError):
            pass

    # Fallback: system TTS
    import sys
    if sys.platform == "linux":
        try:
            proc = await asyncio.create_subprocess_exec(
                "espeak", "-w", output_path, text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return output_path
        except FileNotFoundError:
            pass
    elif sys.platform == "darwin":
        # macOS `say` writes AIFF; save to a temp AIFF then convert or just use it
        aiff_path = output_path.replace(".mp3", ".aiff") if output_path.endswith(".mp3") else output_path + ".aiff"
        try:
            proc = await asyncio.create_subprocess_exec(
                "say", "-o", aiff_path, text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            # Return the AIFF path — afplay can handle it directly
            return aiff_path
        except FileNotFoundError:
            pass

    return ""


async def play_audio(path: str) -> None:
    """Play an audio file using available system player."""
    import sys
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

    Uses sox (cross-platform), arecord (Linux/ALSA), or rec (macOS via sox).
    """
    import sys

    if output_path is None:
        output_path = tempfile.mktemp(suffix=".wav")

    if sys.platform == "darwin":
        # macOS: try sox first, then afrecord (not standard), fall back to error
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

    raise RuntimeError("No audio recording tool found. Install: sudo apt install alsa-utils")
