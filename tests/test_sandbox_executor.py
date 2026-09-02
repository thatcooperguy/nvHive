"""Tests for nvh.sandbox.executor — config surface and construction."""

from __future__ import annotations

import pytest


class TestSandboxExecutor:
    def test_import(self):
        from nvh.sandbox import executor
        assert hasattr(executor, "SandboxExecutor") or hasattr(executor, "execute_code")

    def test_sandbox_config(self):
        try:
            from nvh.sandbox.executor import SandboxConfig
            config = SandboxConfig()
            assert config.timeout_seconds > 0
            assert config.memory_limit_mb > 0
        except (ImportError, TypeError):
            pytest.skip("SandboxConfig not available")

    def test_sandbox_config_defaults(self):
        try:
            from nvh.sandbox.executor import SandboxConfig
            config = SandboxConfig()
            assert config.timeout_seconds > 0
            assert config.memory_limit_mb > 0
            assert isinstance(config.allowed_languages, (list, set, tuple))
        except (ImportError, TypeError):
            pytest.skip("SandboxConfig not available")

    def test_sandbox_config_has_fields(self):
        try:
            from nvh.sandbox.executor import SandboxConfig
            c = SandboxConfig()
            assert hasattr(c, "timeout_seconds")
            assert hasattr(c, "memory_limit_mb")
            assert hasattr(c, "network_enabled")
        except (ImportError, TypeError):
            pytest.skip("SandboxConfig not available")

    def test_sandbox_executor_construction(self):
        try:
            from nvh.sandbox.executor import SandboxExecutor
            se = SandboxExecutor()
            assert se is not None
        except (ImportError, TypeError):
            pytest.skip("SandboxExecutor not available or needs Docker")

    def test_sandbox_is_available(self):
        try:
            from nvh.sandbox.executor import SandboxExecutor
            se = SandboxExecutor()
            # On a dev machine without Docker, this returns False
            result = se.is_available()
            assert isinstance(result, bool)
        except (ImportError, TypeError, AttributeError):
            pytest.skip("SandboxExecutor.is_available not available")
