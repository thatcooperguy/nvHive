"""``nvh/core/scheduler.py``: the ``nvh-schedules/2`` store.

Every test builds its ``Scheduler`` on an explicit file under ``tmp_path`` so
nothing here reads ``$NVH_HOME`` or leaves state behind for the one-process
CI run. tests/test_storage_layout.py pins the default path and the legacy
migration; this file pins the record shape, read-compat, atomic writes and the
run bookkeeping.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import fields
from decimal import Decimal
from pathlib import Path

import pytest

from nvh.core import scheduler as sched
from nvh.core.scheduler import (
    MAX_ERROR_CHARS,
    RUN_STATUSES,
    SCHEDULE_SCHEMA,
    ScheduledTask,
    Scheduler,
)

LEGACY_RECORD = {
    "id": "t1", "prompt": "p", "interval_seconds": 60, "advisor": "", "mode": "ask",
    "last_run": "", "next_run": 0.0, "enabled": True, "created_at": "",
}
LEGACY_KEYS = tuple(LEGACY_RECORD)
HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _write(path: Path, data: object) -> bytes:
    raw = json.dumps(data).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "state" / "schedules.json"


# ---------------------------------------------------------------------------
# record shape
# ---------------------------------------------------------------------------


def test_every_field_has_a_doc_sentence_for_the_generated_block() -> None:
    names = [f.name for f in fields(ScheduledTask)]
    assert names[:9] == list(LEGACY_KEYS), "the nine pre-0.46 fields keep their order"
    assert names[9:] == [
        "name", "profile", "conversation_id", "updated_at", "last_status", "last_error",
        "last_job_id", "last_cost_usd", "last_cost_known", "run_count", "extra",
    ]
    for f in fields(ScheduledTask):
        doc = f.metadata.get("doc")
        assert isinstance(doc, str) and doc.strip(), f"{f.name} needs metadata['doc']"
        assert doc.endswith("."), f"{f.name} doc should be one sentence"
    assert fields(ScheduledTask)[-1].metadata["doc"] == "Unknown keys, round-tripped."


def test_legacy_positional_construction_still_works() -> None:
    task = ScheduledTask("abcd1234", "p", 60, "", "ask", "", 0.0, True, "")
    assert task.id == "abcd1234"
    assert task.name == "" and task.profile == "" and task.conversation_id == ""
    assert task.last_status == "" and task.last_error == "" and task.last_job_id == ""
    assert task.last_cost_usd is None and task.last_cost_known is None
    assert task.run_count == 0 and task.extra == {}


def test_schema_constant_and_statuses() -> None:
    assert SCHEDULE_SCHEMA == "nvh-schedules/2"
    assert RUN_STATUSES == {"ok", "error", "skipped", "deferred"}
    assert MAX_ERROR_CHARS == 300


# ---------------------------------------------------------------------------
# read-compat: the pre-0.46 bare array
# ---------------------------------------------------------------------------


def test_legacy_bare_array_loads_intact_and_is_not_rewritten_on_load(store: Path) -> None:
    store.parent.mkdir(parents=True)
    raw = _write(store, [LEGACY_RECORD])

    s = Scheduler(schedule_file=store)

    assert [t.id for t in s._tasks] == ["t1"]
    task = s.list_tasks()[0]
    for key in LEGACY_KEYS:
        assert getattr(task, key) == LEGACY_RECORD[key], key
    assert task.extra == {}
    assert task.last_cost_usd is None
    assert store.read_bytes() == raw, "loading never rewrites the file"
    assert sorted(p.name for p in store.parent.iterdir()) == ["schedules.json"]


def test_legacy_bare_array_is_rewritten_as_v2_on_the_next_save(store: Path) -> None:
    store.parent.mkdir(parents=True)
    _write(store, [LEGACY_RECORD])
    s = Scheduler(schedule_file=store)

    added = s.add("ping", 60)

    data = _read(store)
    assert set(data) == {"schema", "updated_at", "schedules"}
    assert data["schema"] == SCHEDULE_SCHEMA
    assert data["updated_at"].startswith("20") and "+00:00" in data["updated_at"]
    assert [r["id"] for r in data["schedules"]] == ["t1", added.id], "legacy ids kept verbatim"
    legacy = data["schedules"][0]
    assert {k: legacy[k] for k in LEGACY_KEYS} == LEGACY_RECORD
    assert legacy["extra"] == {} and legacy["run_count"] == 0 and legacy["last_cost_usd"] is None


def test_file_is_written_with_sorted_keys_and_a_trailing_newline(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    s.add("ping", 60, name="Ping")
    text = store.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# v2 round-trip, unknown keys, missing keys
# ---------------------------------------------------------------------------


def test_v2_round_trip_preserves_every_field(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    a = s.add("first", 60, "groq", "convene", name="A", profile="research")
    b = s.add("second", 3600)
    s.mark_run(a.id, status="ok", job_id="j1", cost_usd=Decimal("0.0125"), cost_known=True, conversation_id="c1")

    reloaded = Scheduler(schedule_file=store)

    assert [t.to_dict() for t in reloaded.list_tasks()] == [a.to_dict(), b.to_dict()]
    again = reloaded.get(a.id)
    assert again is not None
    assert (again.name, again.profile, again.advisor, again.mode) == ("A", "research", "groq", "convene")
    assert (again.last_job_id, again.last_cost_usd, again.last_cost_known, again.conversation_id) == ("j1", 0.0125, True, "c1")


def test_unknown_record_key_survives_in_extra_and_is_written_back(store: Path) -> None:
    store.parent.mkdir(parents=True)
    record = dict(LEGACY_RECORD, colour="blue", extra={"nested": 1})
    _write(store, {"schema": SCHEDULE_SCHEMA, "updated_at": "", "schedules": [record]})

    s = Scheduler(schedule_file=store)
    assert s.list_tasks()[0].extra == {"nested": 1, "colour": "blue"}

    s.set_enabled("t1", False)
    written = _read(store)["schedules"][0]
    assert written["extra"] == {"nested": 1, "colour": "blue"}
    assert "colour" not in written, "unknown keys live under extra, not at the top level"
    assert Scheduler(schedule_file=store).list_tasks()[0].extra == {"nested": 1, "colour": "blue"}


def test_missing_keys_are_defaulted_instead_of_dropping_the_record(store: Path) -> None:
    store.parent.mkdir(parents=True)
    partial = {"prompt": "p", "interval_seconds": 60, "next_run": 0.0, "created_at": "2026-01-01T00:00:00+00:00"}
    _write(store, {"schema": SCHEDULE_SCHEMA, "schedules": [partial, {"id": "t2", "prompt": "q"}]})

    tasks = Scheduler(schedule_file=store).list_tasks()

    assert len(tasks) == 2
    first, second = tasks
    assert HEX32.match(first.id), "a record without an id gets a derived 32-hex id"
    assert (first.advisor, first.mode, first.last_run, first.enabled) == ("", "ask", "", True)
    assert (first.name, first.last_status, first.run_count, first.last_cost_usd, first.extra) == ("", "", 0, None, {})
    assert second.id == "t2" and second.interval_seconds == 86400 and second.next_run == 0.0


def test_non_object_records_are_skipped_not_fatal(store: Path, caplog: pytest.LogCaptureFixture) -> None:
    store.parent.mkdir(parents=True)
    _write(store, [LEGACY_RECORD, "junk", 7])
    with caplog.at_level(logging.WARNING, logger="nvh.core.scheduler"):
        s = Scheduler(schedule_file=store)
    assert [t.id for t in s.list_tasks()] == ["t1"]
    assert sum("Skipping schedules.json record" in r.getMessage() for r in caplog.records) == 2


# ---------------------------------------------------------------------------
# corrupt file: quarantined, not emptied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [b"{not json", b'"a string"', b"42", b'{"schema": "x", "schedules": "nope"}'])
def test_unparseable_file_is_moved_aside_once_and_the_store_starts_empty(
    store: Path, payload: bytes, caplog: pytest.LogCaptureFixture,
) -> None:
    store.parent.mkdir(parents=True)
    store.write_bytes(payload)

    with caplog.at_level(logging.WARNING, logger="nvh.core.scheduler"):
        s = Scheduler(schedule_file=store)

    assert s.list_tasks() == []
    assert not store.exists(), "the corrupt file is renamed, not emptied in place"
    quarantined = sorted(store.parent.glob("schedules.json.corrupt-*"))
    assert len(quarantined) == 1
    assert re.fullmatch(r"schedules\.json\.corrupt-\d{14}", quarantined[0].name)
    assert quarantined[0].read_bytes() == payload, "the bytes are kept for a human to look at"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert quarantined[0].name in warnings[0].getMessage()

    # the next save writes a fresh v2 file beside the quarantined copy
    s.add("ping", 60)
    assert _read(store)["schema"] == SCHEDULE_SCHEMA
    assert len(sorted(store.parent.glob("schedules.json.corrupt-*"))) == 1


def test_second_corrupt_copy_in_the_same_second_is_not_overwritten(store: Path) -> None:
    store.parent.mkdir(parents=True)
    store.write_bytes(b"{first")
    Scheduler(schedule_file=store)
    first = sorted(store.parent.glob("schedules.json.corrupt-*"))
    store.write_bytes(b"{second")
    Scheduler(schedule_file=store)
    copies = sorted(store.parent.glob("schedules.json.corrupt-*"))
    assert len(copies) == 2
    assert first[0].read_bytes() == b"{first"
    assert {p.read_bytes() for p in copies} == {b"{first", b"{second"}


def test_empty_or_missing_file_is_an_empty_store_without_quarantine(store: Path) -> None:
    assert Scheduler(schedule_file=store).list_tasks() == []
    assert store.parent.is_dir() and not store.exists()
    store.write_bytes(b"  \n")
    assert Scheduler(schedule_file=store).list_tasks() == []
    assert store.exists() and list(store.parent.glob("*.corrupt-*")) == []


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------


def test_failed_replace_leaves_the_previous_file_whole_and_no_tmp(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Scheduler(schedule_file=store)
    s.add("keep me", 60)
    before = store.read_bytes()

    def boom(src, dst):
        raise OSError("disk says no")

    monkeypatch.setattr(sched.os, "replace", boom)
    with pytest.raises(OSError, match="disk says no"):
        s.add("never lands", 60)

    assert store.read_bytes() == before
    assert sorted(p.name for p in store.parent.iterdir()) == ["schedules.json"], "no .tmp left behind"
    assert len(s.list_tasks()) == 2, "the in-memory record stays so the next save can retry"


def test_save_retries_once_when_replace_raises_permission_error(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Scheduler(schedule_file=store)
    real_replace = os.replace
    calls: list[str] = []
    naps: list[float] = []

    def flaky(src, dst):
        calls.append("replace")
        if len(calls) == 1:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(sched.os, "replace", flaky)
    monkeypatch.setattr(sched, "_sleep", naps.append)  # the module seam, not the process-global time.sleep

    task = s.add("ping", 60)

    assert calls == ["replace", "replace"]
    assert naps == [sched._SAVE_RETRY_DELAY_S]
    assert [r["id"] for r in _read(store)["schedules"]] == [task.id]
    assert sorted(p.name for p in store.parent.iterdir()) == ["schedules.json"]


def test_save_gives_up_after_a_bounded_number_of_permission_errors(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Scheduler(schedule_file=store)
    s.add("first", 60)
    before = store.read_bytes()
    attempts: list[str] = []
    naps: list[float] = []

    def locked(src, dst):
        attempts.append("replace")
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(sched.os, "replace", locked)
    monkeypatch.setattr(sched, "_sleep", naps.append)  # the module seam, not the process-global time.sleep

    with pytest.raises(PermissionError):
        s.add("second", 60)

    assert len(attempts) == sched._SAVE_RETRIES == 5
    assert len(naps) == sched._SAVE_RETRIES - 1
    assert store.read_bytes() == before
    assert sorted(p.name for p in store.parent.iterdir()) == ["schedules.json"]


# ---------------------------------------------------------------------------
# ids, add
# ---------------------------------------------------------------------------


def test_save_holds_the_store_lock_and_interleaved_writers_keep_both_records(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """0.47 (T3.9 b): two Scheduler instances loaded before either saves — the CLI and
    the API runner — each keep the other's record, and every save runs under the lock."""
    events: list[str] = []
    real = sched._file_lock

    import contextlib

    @contextlib.contextmanager
    def recording(path: Path):
        assert path == store
        events.append("acquire")
        with real(path):
            assert (store.parent / "schedules.json.lock").exists()
            yield
        events.append("release")

    monkeypatch.setattr(sched, "_file_lock", recording)
    first = Scheduler(store)
    second = Scheduler(store)  # both loaded the empty store
    a = first.add("from the cli", 60)
    b = second.add("from the api runner", 60)
    assert events == ["acquire", "release", "acquire", "release"]
    assert not (store.parent / "schedules.json.lock").exists()
    saved = {r["id"] for r in _read(store)["schedules"]}
    assert saved == {a.id, b.id}
    assert {t.id for t in second.list_tasks()} == {a.id, b.id}


def test_a_record_removed_here_is_not_resurrected_by_the_merge(store: Path) -> None:
    first = Scheduler(store)
    keep = first.add("keep", 60)
    gone = first.add("gone", 60)
    other = Scheduler(store)
    assert other.remove(gone.id) is True
    assert {r["id"] for r in _read(store)["schedules"]} == {keep.id}


def test_a_stale_lock_file_is_broken(store: Path) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    lock = store.parent / "schedules.json.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    task = Scheduler(store).add("after a crash", 60)
    assert {r["id"] for r in _read(store)["schedules"]} == {task.id}
    assert not lock.exists()


def test_new_ids_are_32_hex_and_add_keeps_its_positional_signature(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    plain = s.add("ping", 60)
    full = s.add("pong", 120, "groq", "convene", name="Pong", profile="ops")

    assert HEX32.match(plain.id) and HEX32.match(full.id) and plain.id != full.id
    assert (plain.advisor, plain.mode, plain.name, plain.profile) == ("", "ask", "", "")
    assert (full.advisor, full.mode, full.name, full.profile) == ("groq", "convene", "Pong", "ops")
    assert plain.enabled and plain.last_run == "" and plain.run_count == 0
    assert plain.created_at == plain.updated_at != ""
    assert plain.next_run <= time.time()
    assert s.get_due_tasks() == [plain, full]


def test_remove_and_get(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 60)
    assert s.get(task.id) is task
    assert s.get("nope") is None
    assert s.remove(task.id) is True
    assert s.remove(task.id) is False
    assert s.get(task.id) is None
    assert _read(store)["schedules"] == []


# ---------------------------------------------------------------------------
# mark_run / mark_completed
# ---------------------------------------------------------------------------


def test_mark_run_ok_records_the_run_and_advances_next_run(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 600)
    t0 = time.time()

    out = s.mark_run(
        task.id, status="ok", job_id="20260101000000-abcdef01",
        cost_usd=Decimal("0.0005"), cost_known=True, conversation_id="conv-1",
    )

    assert out is task
    assert task.last_status == "ok" and task.last_error == ""
    assert task.last_job_id == "20260101000000-abcdef01"
    assert task.last_cost_usd == 0.0005 and isinstance(task.last_cost_usd, float)
    assert task.last_cost_known is True
    assert task.conversation_id == "conv-1"
    assert task.run_count == 1
    assert task.next_run >= t0 + 600
    assert task.last_run.startswith("20") and task.updated_at == task.last_run
    assert task not in s.get_due_tasks()

    saved = _read(store)["schedules"][0]
    assert saved["last_status"] == "ok" and saved["run_count"] == 1 and saved["last_cost_usd"] == 0.0005


def test_mark_run_error_truncates_the_message_and_keeps_cost_null(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 60)
    s.mark_run(task.id, status="ok", cost_usd=0.5, cost_known=False, conversation_id="conv-1")

    s.mark_run(task.id, status="error", error="x" * 500)

    assert task.last_status == "error"
    assert task.last_error == "x" * MAX_ERROR_CHARS and len(task.last_error) == 300
    assert task.last_cost_usd is None and task.last_cost_known is None, "a failed run has no figure"
    assert task.last_job_id == ""
    assert task.conversation_id == "conv-1", "an empty conversation_id never clears the thread"
    assert task.run_count == 2


@pytest.mark.parametrize("status", sorted(RUN_STATUSES))
def test_every_run_status_is_accepted_and_persisted(store: Path, status: str) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 60)
    assert s.mark_run(task.id, status=status) is task
    assert Scheduler(schedule_file=store).get(task.id).last_status == status


def test_mark_run_rejects_unknown_status_and_unknown_id(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 60)
    before = store.read_bytes()
    with pytest.raises(ValueError, match="Unknown run status 'done'"):
        s.mark_run(task.id, status="done")
    assert s.mark_run("missing", status="ok") is None
    assert store.read_bytes() == before and task.run_count == 0


def test_mark_completed_is_an_alias_of_mark_run_ok(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 60)
    s.mark_completed(task.id)
    assert task.last_status == "ok" and task.run_count == 1 and task.last_run != ""
    assert task.next_run > time.time() + 50
    s.mark_completed("missing")  # unknown id is still a no-op


# ---------------------------------------------------------------------------
# set_enabled / update
# ---------------------------------------------------------------------------


def test_set_enabled_toggles_due_ness_and_persists(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 60)
    assert s.get_due_tasks() == [task]

    assert s.set_enabled(task.id, False) is task
    assert task.enabled is False and task.updated_at != ""
    assert s.get_due_tasks() == []
    assert Scheduler(schedule_file=store).get(task.id).enabled is False

    assert s.set_enabled(task.id, True) is task and s.get_due_tasks() == [task]
    assert s.set_enabled("missing", True) is None


def test_update_changes_known_fields_and_stamps_updated_at(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 86400, name="Old")
    task.next_run = time.time() + 80000
    stamp_before = task.updated_at

    out = s.update(task.id, name="New", prompt="pong", advisor="groq", mode="convene", profile="ops", interval_seconds=60)

    assert out is task
    assert (task.name, task.prompt, task.advisor, task.mode, task.profile, task.interval_seconds) == (
        "New", "pong", "groq", "convene", "ops", 60,
    )
    assert task.next_run <= time.time() + 60, "a shorter interval pulls the next run in"
    assert task.updated_at >= stamp_before
    saved = Scheduler(schedule_file=store).get(task.id)
    assert saved.name == "New" and saved.interval_seconds == 60


def test_update_rejects_unknown_and_protected_fields_before_writing(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 60)
    before = store.read_bytes()
    for bad in ({"bogus": 1}, {"id": "new"}, {"extra": {}}, {"created_at": "x"}, {"updated_at": "x"}):
        with pytest.raises(ValueError, match="Cannot update schedule field"):
            s.update(task.id, **bad)
    assert store.read_bytes() == before
    assert s.update("missing", name="x") is None
    assert s.update(task.id) is task, "no changes is a no-op"


def test_update_with_explicit_next_run_is_not_clamped(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 60)
    far = time.time() + 10_000
    s.update(task.id, interval_seconds=30, next_run=far)
    assert task.next_run == far


# ---------------------------------------------------------------------------
# 7a review fixes: private temp name, typed update, stable derived ids, half-records
# ---------------------------------------------------------------------------


def test_temp_file_is_private_to_the_writer_and_removed_on_a_failed_write(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = Scheduler(schedule_file=store)
    s.add("keep me", 60)
    seen: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(sched.os, "replace", spy)
    s.add("second", 60)
    assert len(seen) == 1
    # pid + random token: two writers (CLI and API) never share a temp file.
    assert re.fullmatch(rf"schedules\.json\.{os.getpid()}-[0-9a-f]{{8}}\.tmp", seen[0]), seen
    assert sorted(p.name for p in store.parent.iterdir()) == ["schedules.json"]

    # A write that fails half-way (disk full) leaves no .tmp and the store whole.
    before = store.read_bytes()
    real_write_text = Path.write_text

    def enospc(self, text, *args, **kwargs):
        if self.name.endswith(".tmp"):
            real_write_text(self, text[: len(text) // 2], *args, **kwargs)
            raise OSError(28, "No space left on device")
        return real_write_text(self, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", enospc)
    with pytest.raises(OSError, match="No space left"):
        s.add("never lands", 60)
    assert store.read_bytes() == before
    assert sorted(p.name for p in store.parent.iterdir()) == ["schedules.json"], "no .tmp left behind"


def test_update_coerces_values_and_refuses_wrong_types_before_touching_the_record(store: Path) -> None:
    s = Scheduler(schedule_file=store)
    task = s.add("ping", 60)
    before = store.read_bytes()
    for bad in (
        {"interval_seconds": "1h"}, {"interval_seconds": 0}, {"enabled": "maybe"},
        {"prompt": 7}, {"next_run": "soon"}, {"last_status": "done"},
    ):
        with pytest.raises(ValueError):
            s.update(task.id, **bad)
    assert (task.interval_seconds, task.enabled, task.prompt) == (60, True, "ping"), "memory untouched"
    assert store.read_bytes() == before, "nothing written for a refused value"
    assert Scheduler(schedule_file=store).get(task.id).interval_seconds == 60

    # The spellings a request body or a CLI flag arrives in are coerced.
    s.update(task.id, interval_seconds="120", enabled="false", next_run="5.5", name=None)
    assert (task.interval_seconds, task.enabled, task.next_run, task.name) == (120, False, 5.5, "")
    assert isinstance(task.interval_seconds, int)
    assert Scheduler(schedule_file=store).get(task.id).enabled is False
    assert s.get_due_tasks() == []
    s.update(task.id, enabled=1)
    assert task.enabled is True


def test_a_record_without_an_id_reads_the_same_derived_id_in_every_process(store: Path) -> None:
    store.parent.mkdir(parents=True)
    no_id = {k: v for k, v in LEGACY_RECORD.items() if k != "id"} | {"prompt": "Summarize"}
    raw = _write(store, [no_id, {**LEGACY_RECORD, "id": 42}])

    first = Scheduler(schedule_file=store).list_tasks()
    second = Scheduler(schedule_file=store).list_tasks()  # `nvh schedule remove` is a second process

    assert HEX32.match(first[0].id) and first[0].id == second[0].id
    assert first[0].id == sched.derived_schedule_id(json.loads(raw)[0])
    assert store.read_bytes() == raw, "loading never writes"
    assert first[1].id == "42", "a non-string id is coerced so the CLI's string argument matches"
    s = Scheduler(schedule_file=store)
    assert s.get("42") is not None and s.remove(first[0].id) is True
    assert [t.id for t in Scheduler(schedule_file=store).list_tasks()] == ["42"], "the first save persists the ids"


def test_a_record_missing_its_prompt_or_interval_loads_disabled(store: Path) -> None:
    store.parent.mkdir(parents=True)
    _write(store, {"schema": SCHEDULE_SCHEMA, "schedules": [
        {"id": "t9", "advisor": "", "mode": "ask"},           # half-written by hand
        {"id": "t2", "prompt": "q"},                            # no interval
        {"id": "t3", "interval_seconds": 60},                   # no prompt
        {"id": "t4", "prompt": "p", "interval_seconds": 60},    # runnable; enabled defaulted
    ]})

    s = Scheduler(schedule_file=store)
    by_id = {t.id: t for t in s.list_tasks()}

    assert [by_id[i].enabled for i in ("t9", "t2", "t3")] == [False, False, False]
    assert by_id["t4"].enabled is True and by_id["t4"].next_run == 0.0
    assert [t.id for t in s.get_due_tasks()] == ["t4"], "a half-written record is never due"
    assert (by_id["t9"].prompt, by_id["t9"].interval_seconds) == ("", 86400)
    s.set_enabled("t2", True)  # a human can still turn it on
    assert Scheduler(schedule_file=store).get("t2").enabled is True
