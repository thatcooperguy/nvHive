"""Tests for the file lock coordinator."""

import asyncio

import pytest

from nvh.core.file_lock import (
    AgentFileChange,
    FileLockCoordinator,
    LockType,
    detect_council_conflicts,
    plan_sequential_changes,
)


class TestFileLockCoordinator:
    @pytest.fixture
    def coordinator(self):
        return FileLockCoordinator(default_timeout=5.0, max_wait_seconds=2.0)

    @pytest.mark.asyncio
    async def test_acquire_and_release(self, coordinator):
        assert await coordinator.acquire("/tmp/test.py", "agent-1")
        status = await coordinator.get_status()
        assert status["active_locks"] == 1
        assert await coordinator.release("/tmp/test.py", "agent-1")
        status = await coordinator.get_status()
        assert status["active_locks"] == 0

    @pytest.mark.asyncio
    async def test_write_lock_is_exclusive(self, coordinator):
        assert await coordinator.acquire("/tmp/test.py", "agent-1", LockType.WRITE)
        # Second agent can't get write lock
        assert not await coordinator.acquire(
            "/tmp/test.py", "agent-2", LockType.WRITE, wait=False
        )

    @pytest.mark.asyncio
    async def test_read_locks_are_shared(self, coordinator):
        assert await coordinator.acquire("/tmp/test.py", "agent-1", LockType.READ)
        assert await coordinator.acquire("/tmp/test.py", "agent-2", LockType.READ)
        status = await coordinator.get_status()
        assert status["active_locks"] == 2

    @pytest.mark.asyncio
    async def test_write_blocked_by_read(self, coordinator):
        assert await coordinator.acquire("/tmp/test.py", "agent-1", LockType.READ)
        assert not await coordinator.acquire(
            "/tmp/test.py", "agent-2", LockType.WRITE, wait=False
        )

    @pytest.mark.asyncio
    async def test_same_agent_can_reenter(self, coordinator):
        assert await coordinator.acquire("/tmp/test.py", "agent-1", LockType.WRITE)
        # Same agent can acquire again
        assert await coordinator.acquire("/tmp/test.py", "agent-1", LockType.WRITE)

    @pytest.mark.asyncio
    async def test_release_all(self, coordinator):
        await coordinator.acquire("/tmp/a.py", "agent-1")
        await coordinator.acquire("/tmp/b.py", "agent-1")
        await coordinator.acquire("/tmp/c.py", "agent-1")
        count = await coordinator.release_all("agent-1")
        assert count == 3
        status = await coordinator.get_status()
        assert status["active_locks"] == 0

    @pytest.mark.asyncio
    async def test_expired_locks_cleaned(self, coordinator):
        coordinator.default_timeout = 0.1  # 100ms timeout
        await coordinator.acquire("/tmp/test.py", "agent-1")
        await asyncio.sleep(0.15)
        # Should be able to acquire — old lock expired
        assert await coordinator.acquire(
            "/tmp/test.py", "agent-2", LockType.WRITE, wait=False
        )

    @pytest.mark.asyncio
    async def test_max_locks_per_agent(self, coordinator):
        coordinator.max_locks_per_agent = 2
        assert await coordinator.acquire("/tmp/a.py", "agent-1")
        assert await coordinator.acquire("/tmp/b.py", "agent-1")
        assert not await coordinator.acquire("/tmp/c.py", "agent-1")

    @pytest.mark.asyncio
    async def test_conflict_detection(self, coordinator):
        await coordinator.acquire("/tmp/test.py", "agent-1")
        conflicts = await coordinator.check_conflicts(
            {"agent-2": "/tmp/test.py"}
        )
        assert len(conflicts) == 1
        assert conflicts[0].agent_a == "agent-1"


class TestCouncilConflicts:
    def test_no_conflicts(self):
        changes = [
            AgentFileChange(agent_id="a", file_path="/tmp/a.py", action="modify"),
            AgentFileChange(agent_id="b", file_path="/tmp/b.py", action="modify"),
        ]
        conflicts = detect_council_conflicts(changes)
        assert len(conflicts) == 0

    def test_two_agents_same_file(self):
        changes = [
            AgentFileChange(agent_id="a", file_path="/tmp/shared.py", action="modify"),
            AgentFileChange(agent_id="b", file_path="/tmp/shared.py", action="modify"),
        ]
        conflicts = detect_council_conflicts(changes)
        assert len(conflicts) == 1

    def test_three_agents_same_file(self):
        changes = [
            AgentFileChange(agent_id="a", file_path="/tmp/shared.py", action="modify"),
            AgentFileChange(agent_id="b", file_path="/tmp/shared.py", action="modify"),
            AgentFileChange(agent_id="c", file_path="/tmp/shared.py", action="modify"),
        ]
        conflicts = detect_council_conflicts(changes)
        assert "/tmp/shared.py" in str(conflicts) or len(conflicts) == 1

    def test_sequential_planning_with_priority(self):
        changes = [
            AgentFileChange(agent_id="architect", file_path="/tmp/main.py", action="modify", content="v1"),
            AgentFileChange(agent_id="security", file_path="/tmp/main.py", action="modify", content="v2"),
            AgentFileChange(agent_id="architect", file_path="/tmp/utils.py", action="modify", content="v3"),
        ]
        ordered = plan_sequential_changes(
            changes, priority_order=["security", "architect"]
        )
        # Security should win on main.py
        main_change = [c for c in ordered if "main.py" in c.file_path][0]
        assert main_change.agent_id == "security"

    def test_sequential_planning_no_conflict(self):
        changes = [
            AgentFileChange(agent_id="a", file_path="/tmp/a.py", action="modify"),
            AgentFileChange(agent_id="b", file_path="/tmp/b.py", action="modify"),
        ]
        ordered = plan_sequential_changes(changes)
        assert len(ordered) == 2
