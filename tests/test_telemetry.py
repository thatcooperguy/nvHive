"""Tests for the opt-in local-only install telemetry emitter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nvh import telemetry


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point nvh_home at a fresh tmp dir and clear the opt-in env var."""
    monkeypatch.delenv(telemetry._TELEMETRY_ENV, raising=False)
    nvh_home = tmp_path / "nvhive"
    monkeypatch.setenv("NVH_HOME", str(nvh_home))
    return nvh_home


def test_disabled_by_default_drops_events(home: Path) -> None:
    record = telemetry.emit("install_completed", {"platform": "linux"})
    assert record is None
    assert not (home / "telemetry" / "events.jsonl").exists()


def test_env_var_enables_emission(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(telemetry._TELEMETRY_ENV, "1")

    record = telemetry.emit("install_completed", {"platform": "linux"})
    assert record is not None
    assert record["event"] == "install_completed"

    log_path = home / "telemetry" / "events.jsonl"
    assert log_path.exists()
    line = log_path.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["event"] == "install_completed"
    assert parsed["properties"]["platform"] == "linux"
    assert parsed["install_id"]
    assert parsed["nvh_version"]


def test_env_var_off_overrides_persisted_opt_in(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    telemetry.set_enabled(True)
    monkeypatch.setenv(telemetry._TELEMETRY_ENV, "0")

    assert telemetry.is_enabled() is False
    assert telemetry.emit("first_wizard_turn") is None


def test_set_enabled_persists_and_emits(home: Path) -> None:
    telemetry.set_enabled(True)
    assert telemetry.is_enabled()

    record = telemetry.emit("first_wizard_turn", {"provider": "ollama"})
    assert record is not None

    telemetry.set_enabled(False)
    assert telemetry.is_enabled() is False
    assert telemetry.emit("first_wizard_turn") is None


def test_install_id_is_stable_across_calls(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(telemetry._TELEMETRY_ENV, "1")
    first = telemetry.install_id()
    second = telemetry.install_id()
    assert first == second
    # The persisted file is the source of truth.
    assert (home / "telemetry" / "install_id").read_text().strip() == first


def test_unknown_event_is_rejected(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(telemetry._TELEMETRY_ENV, "1")
    assert telemetry.emit("not_a_real_event") is None
    assert not (home / "telemetry" / "events.jsonl").exists()


def test_redaction_drops_secret_shaped_keys(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(telemetry._TELEMETRY_ENV, "1")
    telemetry.emit(
        "install_completed",
        {
            "api_key": "sk-very-secret",
            "OPENAI_TOKEN": "should-not-leak",
            "platform": "linux",
            "nested": {"bearer": "abc123", "harmless": True},
        },
    )
    parsed = json.loads((home / "telemetry" / "events.jsonl").read_text().strip())
    props = parsed["properties"]
    assert props["api_key"] == "[redacted]"
    assert props["OPENAI_TOKEN"] == "[redacted]"
    assert props["platform"] == "linux"
    assert props["nested"]["bearer"] == "[redacted]"
    assert props["nested"]["harmless"] is True


def test_emit_swallows_errors(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telemetry must never blow up the host product."""
    monkeypatch.setenv(telemetry._TELEMETRY_ENV, "1")

    # Force a serialization failure: an object with a non-JSON-serializable
    # value that the redactor doesn't unwrap.
    class NotSerializable:
        pass

    # The emitter swallows JSON errors at debug level; the caller is told
    # "I tried" via a None return.
    result = telemetry.emit(
        "install_completed", {"obj": NotSerializable()}
    )
    # We don't care about the return value — we care that nothing raised.
    _ = result


def test_summary_counts_events(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(telemetry._TELEMETRY_ENV, "1")
    telemetry.emit("install_completed")
    telemetry.emit("first_wizard_turn")
    telemetry.emit("first_wizard_turn")
    telemetry.emit("reconnect_survived")

    summary = telemetry.summary()
    assert summary["enabled"] is True
    assert summary["events_total"] == 4
    assert summary["events_by_name"]["install_completed"] == 1
    assert summary["events_by_name"]["first_wizard_turn"] == 2
    assert summary["events_by_name"]["reconnect_survived"] == 1
    assert summary["first_seen"]
    assert summary["last_seen"]


def test_read_events_skips_malformed_lines(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(telemetry._TELEMETRY_ENV, "1")
    telemetry.emit("install_completed")
    log_path = home / "telemetry" / "events.jsonl"
    # Append a garbage line — read_events should tolerate it.
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("this is not json\n")
    telemetry.emit("first_wizard_turn")

    events = telemetry.read_events()
    assert len(events) == 2
    assert {e["event"] for e in events} == {"install_completed", "first_wizard_turn"}


def test_home_dir_argument_overrides_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(telemetry._TELEMETRY_ENV, "1")
    # NVH_HOME in env points one place, but the explicit arg wins.
    env_home = tmp_path / "env-home"
    arg_home = tmp_path / "arg-home"
    monkeypatch.setenv("NVH_HOME", str(env_home))

    telemetry.emit("install_completed", home_dir=str(arg_home))

    assert (arg_home / "telemetry" / "events.jsonl").exists()
    assert not (env_home / "telemetry" / "events.jsonl").exists()
