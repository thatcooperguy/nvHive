"""Tests for nvh.sdk — public surface of the convenience wrappers."""

from __future__ import annotations


class TestSDK:
    def test_import_exports(self):
        import nvh.sdk as sdk
        # Should export the main convenience functions
        assert hasattr(sdk, "complete") or hasattr(sdk, "query")

    def test_sdk_has_version(self):
        import nvh
        assert hasattr(nvh, "__version__")
        assert isinstance(nvh.__version__, str)

    def test_public_async_functions_exist(self):
        import nvh.sdk as sdk

        for name in ("ask", "convene", "poll", "safe", "quick",
                     "complete", "route", "stream", "health"):
            assert callable(getattr(sdk, name)), f"sdk.{name} missing"

    def test_public_sync_functions_exist(self):
        import nvh.sdk as sdk

        for name in ("ask_sync", "convene_sync", "poll_sync",
                     "safe_sync", "quick_sync", "complete_sync",
                     "health_sync"):
            assert callable(getattr(sdk, name)), f"sdk.{name} missing"

    def test_messages_to_internal(self):
        from nvh.sdk import _messages_to_internal

        msgs = _messages_to_internal([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ])
        assert len(msgs) == 2
        assert msgs[0].role == "system"
        assert msgs[1].content == "Hi"
