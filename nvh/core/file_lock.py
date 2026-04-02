"""File lock coordinator for multi-agent workflows.

Prevents multiple agents/LLMs from modifying the same file simultaneously.
Agents must acquire a lock before writing and release it when done.

Supports:
- Exclusive write locks (one writer at a time per file)
- Shared read locks (multiple readers allowed)
- Lock timeouts (auto-release if an agent hangs)
- Queue-based waiting (agents wait in order, not rejected)
- Agent identity tracking (which agent holds which lock)
- Deadlock prevention (no agent can hold more than N locks)

Usage in council mode:
    When multiple LLMs generate code changes, the orchestrator:
    1. Collects all proposed file modifications from each agent
    2. Detects conflicts (two agents want to edit the same file)
    3. Resolves conflicts before applying (merge, pick best, or sequential)
    4. Applies changes one file at a time with locks held

Usage in sandbox/tool execution:
    When an LLM uses tools to modify files:
    1. Tool acquires lock on target file
    2. Performs modification
    3. Releases lock
    4. If lock unavailable, waits (with timeout) or reports conflict
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)


class LockType(StrEnum):
    READ = "read"       # Shared — multiple readers allowed
    WRITE = "write"     # Exclusive — one writer, no readers


@dataclass
class FileLock:
    """A lock held on a file."""
    path: str
    lock_type: LockType
    agent_id: str
    acquired_at: float
    timeout_seconds: float = 60.0

    @property
    def is_expired(self) -> bool:
        return time.monotonic() - self.acquired_at > self.timeout_seconds


@dataclass
class FileConflict:
    """A detected conflict between two agents."""
    file_path: str
    agent_a: str  # current lock holder
    agent_b: str  # agent requesting access
    lock_type_held: LockType
    lock_type_requested: LockType


class FileLockCoordinator:
    """Manages file locks across multiple agents.

    Thread/async-safe — all operations go through an asyncio.Lock.
    """

    def __init__(
        self,
        default_timeout: float = 60.0,
        max_locks_per_agent: int = 20,
        max_wait_seconds: float = 30.0,
    ):
        self._locks: dict[str, list[FileLock]] = {}  # path -> active locks
        self._waiters: dict[str, asyncio.Event] = {}  # path -> release event
        self._mutex = asyncio.Lock()
        self.default_timeout = default_timeout
        self.max_locks_per_agent = max_locks_per_agent
        self.max_wait_seconds = max_wait_seconds

    async def acquire(
        self,
        path: str,
        agent_id: str,
        lock_type: LockType = LockType.WRITE,
        timeout: float | None = None,
        wait: bool = True,
    ) -> bool:
        """Acquire a lock on a file.

        Args:
            path: Normalized file path
            agent_id: Unique identifier for the agent (e.g., "openai:Architect")
            lock_type: READ (shared) or WRITE (exclusive)
            timeout: Lock timeout in seconds (auto-releases after this)
            wait: If True, wait for lock availability. If False, fail immediately.

        Returns:
            True if lock acquired, False if denied/timed out.
        """
        path = self._normalize_path(path)
        lock_timeout = timeout or self.default_timeout

        async with self._mutex:
            # Clean expired locks
            self._cleanup_expired()

            # Check agent lock limit (deadlock prevention)
            agent_lock_count = sum(
                1 for locks in self._locks.values()
                for lock in locks if lock.agent_id == agent_id
            )
            if agent_lock_count >= self.max_locks_per_agent:
                logger.warning(
                    f"Agent '{agent_id}' at lock limit ({self.max_locks_per_agent}). "
                    f"Release some locks before acquiring more."
                )
                return False

            # Try to acquire immediately
            if self._can_acquire(path, agent_id, lock_type):
                self._do_acquire(path, agent_id, lock_type, lock_timeout)
                return True

        if not wait:
            return False

        # Wait for the lock to become available
        return await self._wait_for_lock(path, agent_id, lock_type, lock_timeout)

    async def release(self, path: str, agent_id: str) -> bool:
        """Release a lock on a file.

        Returns True if a lock was released, False if no lock was held.
        """
        path = self._normalize_path(path)

        async with self._mutex:
            if path not in self._locks:
                return False

            before = len(self._locks[path])
            self._locks[path] = [
                lock for lock in self._locks[path]
                if lock.agent_id != agent_id
            ]
            after = len(self._locks[path])

            if not self._locks[path]:
                del self._locks[path]

            released = before > after
            if released:
                logger.debug(f"Lock released: {path} by {agent_id}")
                # Notify any waiters
                if path in self._waiters:
                    self._waiters[path].set()

            return released

    async def release_all(self, agent_id: str) -> int:
        """Release all locks held by an agent. Returns count of locks released."""
        async with self._mutex:
            count = 0
            paths_to_clean = []
            for path, locks in self._locks.items():
                before = len(locks)
                self._locks[path] = [lk for lk in locks if lk.agent_id != agent_id]
                released = before - len(self._locks[path])
                count += released
                if not self._locks[path]:
                    paths_to_clean.append(path)

            for path in paths_to_clean:
                del self._locks[path]
                if path in self._waiters:
                    self._waiters[path].set()

            if count:
                logger.debug(f"Released {count} locks for agent {agent_id}")
            return count

    async def check_conflicts(
        self,
        proposed_changes: dict[str, str],
    ) -> list[FileConflict]:
        """Check if proposed file changes would conflict with current locks.

        Args:
            proposed_changes: dict of {agent_id: file_path} pairs

        Returns:
            List of conflicts detected.
        """
        async with self._mutex:
            self._cleanup_expired()
            conflicts: list[FileConflict] = []

            # Group paths by agent
            path_agents: dict[str, list[str]] = {}
            for agent_id, file_path in proposed_changes.items():
                path = self._normalize_path(file_path)
                path_agents.setdefault(path, []).append(agent_id)

            # Check for multi-agent conflicts on same file
            for path, agents in path_agents.items():
                if len(agents) > 1:
                    for i in range(len(agents)):
                        for j in range(i + 1, len(agents)):
                            conflicts.append(FileConflict(
                                file_path=path,
                                agent_a=agents[i],
                                agent_b=agents[j],
                                lock_type_held=LockType.WRITE,
                                lock_type_requested=LockType.WRITE,
                            ))

                # Also check against existing locks
                if path in self._locks:
                    for lock in self._locks[path]:
                        if lock.agent_id not in agents:
                            conflicts.append(FileConflict(
                                file_path=path,
                                agent_a=lock.agent_id,
                                agent_b=agents[0],
                                lock_type_held=lock.lock_type,
                                lock_type_requested=LockType.WRITE,
                            ))

            return conflicts

    async def get_status(self) -> dict:
        """Get current lock status for monitoring."""
        async with self._mutex:
            self._cleanup_expired()
            return {
                "active_locks": sum(len(locks) for locks in self._locks.values()),
                "locked_files": len(self._locks),
                "waiting": len(self._waiters),
                "by_file": {
                    path: [
                        {
                            "agent": lock.agent_id,
                            "type": lock.lock_type.value,
                            "age_seconds": round(time.monotonic() - lock.acquired_at, 1),
                            "expires_in": round(
                                lock.timeout_seconds - (time.monotonic() - lock.acquired_at), 1
                            ),
                        }
                        for lock in locks
                    ]
                    for path, locks in self._locks.items()
                },
            }

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _normalize_path(self, path: str) -> str:
        """Normalize a file path for consistent lock keys."""
        return str(Path(path).resolve())

    def _can_acquire(self, path: str, agent_id: str, lock_type: LockType) -> bool:
        """Check if a lock can be acquired without waiting."""
        if path not in self._locks or not self._locks[path]:
            return True

        existing = self._locks[path]

        # Same agent already holds a lock — allow upgrade/re-entry
        if any(lock.agent_id == agent_id for lock in existing):
            return True

        # Read locks are shared — allow if all existing are reads
        if lock_type == LockType.READ:
            return all(lock.lock_type == LockType.READ for lock in existing)

        # Write lock requires no other locks
        return False

    def _do_acquire(
        self, path: str, agent_id: str, lock_type: LockType, timeout: float
    ) -> None:
        """Actually acquire the lock (caller must hold self._mutex)."""
        lock = FileLock(
            path=path,
            lock_type=lock_type,
            agent_id=agent_id,
            acquired_at=time.monotonic(),
            timeout_seconds=timeout,
        )
        self._locks.setdefault(path, []).append(lock)
        logger.debug(f"Lock acquired: {path} ({lock_type.value}) by {agent_id}")

    async def _wait_for_lock(
        self, path: str, agent_id: str, lock_type: LockType, lock_timeout: float
    ) -> bool:
        """Wait for a lock to become available."""
        deadline = time.monotonic() + self.max_wait_seconds

        while time.monotonic() < deadline:
            # Create or reset the event for this path
            async with self._mutex:
                if path not in self._waiters:
                    self._waiters[path] = asyncio.Event()
                event = self._waiters[path]
                event.clear()

            # Wait for a release signal (with timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 2.0))
            except TimeoutError:
                pass

            # Try again
            async with self._mutex:
                self._cleanup_expired()
                if self._can_acquire(path, agent_id, lock_type):
                    self._do_acquire(path, agent_id, lock_type, lock_timeout)
                    # Clean up waiter
                    if path in self._waiters:
                        del self._waiters[path]
                    return True

        logger.warning(
            f"Lock wait timeout: {path} for agent {agent_id} "
            f"(waited {self.max_wait_seconds}s)"
        )
        return False

    def _cleanup_expired(self) -> None:
        """Remove expired locks (caller must hold self._mutex)."""
        paths_to_clean = []
        for path, locks in self._locks.items():
            self._locks[path] = [lk for lk in locks if not lk.is_expired]
            if not self._locks[path]:
                paths_to_clean.append(path)

        for path in paths_to_clean:
            del self._locks[path]
            if path in self._waiters:
                self._waiters[path].set()


# ---------------------------------------------------------------------------
# Council-specific conflict resolution
# ---------------------------------------------------------------------------

@dataclass
class AgentFileChange:
    """A proposed file change from a council agent."""
    agent_id: str
    file_path: str
    action: str          # "create", "modify", "delete"
    content: str = ""    # new content (for create/modify)
    explanation: str = "" # why this change is needed


def detect_council_conflicts(
    changes: list[AgentFileChange],
) -> dict[str, list[AgentFileChange]]:
    """Detect when multiple council agents want to modify the same file.

    Returns a dict of {file_path: [conflicting changes]} for files
    that have more than one agent proposing changes.
    """
    by_file: dict[str, list[AgentFileChange]] = {}
    for change in changes:
        path = str(Path(change.file_path).resolve())
        by_file.setdefault(path, []).append(change)

    # Return only conflicts (2+ agents on same file)
    return {path: agents for path, agents in by_file.items() if len(agents) > 1}


def plan_sequential_changes(
    changes: list[AgentFileChange],
    priority_order: list[str] | None = None,
) -> list[AgentFileChange]:
    """Order file changes to avoid conflicts.

    Strategy:
    - Group changes by file
    - For files with conflicts, pick the highest-priority agent's change
    - For non-conflicting files, preserve original order
    - Returns an ordered list of changes that can be applied sequentially

    Args:
        changes: All proposed changes from all agents
        priority_order: Agent IDs in priority order (first = highest)
    """
    conflicts = detect_council_conflicts(changes)
    result: list[AgentFileChange] = []
    handled_files: set[str] = set()

    # First, handle conflicting files — pick winner by priority
    for file_path, conflicting in conflicts.items():
        if priority_order:
            # Pick the change from the highest-priority agent
            winner = None
            for agent_id in priority_order:
                for change in conflicting:
                    if change.agent_id == agent_id:
                        winner = change
                        break
                if winner:
                    break
            if winner is None:
                winner = conflicting[0]
        else:
            # No priority — pick first
            winner = conflicting[0]

        result.append(winner)
        handled_files.add(file_path)

        # Log the conflict resolution
        losers = [c.agent_id for c in conflicting if c is not winner]
        logger.info(
            f"File conflict on {file_path}: "
            f"'{winner.agent_id}' wins over {losers}"
        )

    # Then add non-conflicting changes
    for change in changes:
        path = str(Path(change.file_path).resolve())
        if path not in handled_files:
            result.append(change)
            handled_files.add(path)

    return result


# Module-level singleton
_coordinator: FileLockCoordinator | None = None


def get_file_lock_coordinator() -> FileLockCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = FileLockCoordinator()
    return _coordinator
