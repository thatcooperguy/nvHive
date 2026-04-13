"""Tests for vision + desktop control tools."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from nvh.core.tools import ToolRegistry


@pytest.fixture()
def registry():
    reg = ToolRegistry(include_system=False)
    from nvh.core.vision_tools import register_vision_tools
    register_vision_tools(reg)
    return reg


class TestVisionToolsRegistration:
    def test_all_tools_registered(self, registry):
        names = {t.name for t in registry.list_tools()}
        assert "capture_screenshot" in names
        assert "analyze_image" in names
        assert "read_text_from_image" in names
        assert "mouse_move" in names
        assert "mouse_click" in names
        assert "keyboard_type" in names
        assert "keyboard_press" in names
        assert "scroll" in names

    def test_safe_flags(self, registry):
        safe_tools = {"capture_screenshot", "analyze_image", "read_text_from_image"}
        unsafe_tools = {"mouse_move", "mouse_click", "keyboard_type", "keyboard_press", "scroll"}
        for t in registry.list_tools():
            if t.name in safe_tools:
                assert t.safe is True, f"{t.name} should be safe"
            elif t.name in unsafe_tools:
                assert t.safe is False, f"{t.name} should be unsafe"


class TestAnalyzeImage:
    @pytest.mark.asyncio
    async def test_analyze_missing_image(self, registry):
        tool = registry.get("analyze_image")
        result = await tool.handler(image_path="/nonexistent/image.png")
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_analyze_existing_image(self, tmp_path):
        # Create a tiny PNG (1x1 pixel)
        img = tmp_path / "test.png"
        # Minimal valid PNG
        import struct
        import zlib
        def create_minimal_png():
            sig = b'\x89PNG\r\n\x1a\n'
            ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
            ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff
            ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)
            raw = b'\x00\x00\x00\x00'
            compressed = zlib.compress(raw)
            idat_crc = zlib.crc32(b'IDAT' + compressed) & 0xffffffff
            idat = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', idat_crc)
            iend_crc = zlib.crc32(b'IEND') & 0xffffffff
            iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)
            return sig + ihdr + idat + iend
        img.write_bytes(create_minimal_png())

        from nvh.core.vision_tools import register_vision_tools
        reg = ToolRegistry(include_system=False)
        register_vision_tools(reg)
        tool = reg.get("analyze_image")
        result = await tool.handler(image_path=str(img), question="What is this?")
        assert "Image loaded" in result
        assert "test.png" in result


class TestDesktopControl:
    @pytest.mark.asyncio
    async def test_mouse_move_no_pyautogui(self, registry):
        with patch.dict("sys.modules", {"pyautogui": None}):
            tool = registry.get("mouse_move")
            result = await tool.handler(x="100", y="200")
            # Either works (pyautogui installed) or gives helpful error
            assert "100" in result or "pyautogui" in result.lower()

    @pytest.mark.asyncio
    async def test_keyboard_type_no_pyautogui(self, registry):
        with patch.dict("sys.modules", {"pyautogui": None}):
            tool = registry.get("keyboard_type")
            result = await tool.handler(text="hello")
            assert "hello" in result.lower() or "pyautogui" in result.lower()

    @pytest.mark.asyncio
    async def test_scroll_no_pyautogui(self, registry):
        with patch.dict("sys.modules", {"pyautogui": None}):
            tool = registry.get("scroll")
            result = await tool.handler(direction="down", amount="3")
            assert "scroll" in result.lower() or "pyautogui" in result.lower()
