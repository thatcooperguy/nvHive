"""Tests for vision + desktop control tools."""

from __future__ import annotations

from types import SimpleNamespace
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
        # analyze_image now actually calls vision models — result may be
        # real analysis (cloud/local) or fallback "Image loaded" / "No vision model"
        assert result  # non-empty response
        assert "Image not found" not in result


class TestNoVisionModelMessage:
    """The "install one locally" hint names the table's vision pick for this box, never a retired tag."""

    @pytest.mark.asyncio
    async def test_hint_names_the_tables_vision_pick(self, registry, tmp_path, monkeypatch):
        import nvh.utils.gpu as gpu
        from nvh.core import local_models, vision_tools

        img = tmp_path / "blank.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        row = SimpleNamespace(vram_mb=24 * 1024, vram_gb=24.0, unified_memory=False, compute_capability=(8, 6))
        mem = SimpleNamespace(effective_for_llm_gb=14.8, available_ram_gb=21.2)
        monkeypatch.setattr(gpu, "detect_gpus", lambda: [row])
        monkeypatch.setattr(gpu, "detect_system_memory", lambda: mem)
        monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: None)

        async def _no_cloud(*_a, **_kw):
            return None

        monkeypatch.setattr(vision_tools, "_analyze_with_cloud", _no_cloud)

        result = await registry.get("analyze_image").handler(image_path=str(img))

        expected = local_models.pick(local_models.tier_budget([row], mem), "vision").tag
        assert "No vision model available" in result
        assert f"ollama pull {expected}" in result
        assert expected in local_models.all_tags()
        for retired in ("llava", "minicpm-v", "bakllava"):
            assert retired not in result


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


# ---------------------------------------------------------------------------
# The Wizard's vision bridge allow-lists the generated-image folder
# ---------------------------------------------------------------------------


class TestWizardVisionBridgeRoots:
    """``allowed_image_roots()`` (nvh/integrations/wizard/vision_bridge.py) includes ``outputs/images``.

    The Wizard's ``generate_image`` tool and the profile-portrait generator save
    under ``$NVH_HOME/outputs/images/<scope>/`` (nvh/integrations/imagegen.py);
    ``analyze_image`` must be able to look at what was just rendered. The
    upload root keeps first place, and a generated image is not a chat
    attachment, so the cloud rule keeps it local-only.
    """

    def test_outputs_images_root_is_allowlisted_and_admitted(self, monkeypatch, tmp_path):
        home = tmp_path / "h"
        monkeypatch.setenv("NVH_HOME", str(home))
        monkeypatch.delenv("NVH_OUTPUTS", raising=False)
        from nvh.integrations.imagegen import outputs_images_root
        from nvh.integrations.wizard import vision_bridge as vb

        roots = vb.allowed_image_roots()
        assert roots[0] == vb.uploads_root().resolve()
        assert outputs_images_root() == home.resolve() / "outputs" / "images"
        assert outputs_images_root().resolve() in roots

        target = outputs_images_root() / "wizard" / "20260101000000-abcdef01.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
        resolved, refusal = vb.admit_image_path(str(target))
        assert refusal is None
        assert resolved == target.resolve()
        assert vb.cloud_allowed(resolved) is False

    def test_outputs_root_follows_nvh_outputs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("NVH_HOME", str(tmp_path / "h"))
        monkeypatch.setenv("NVH_OUTPUTS", str(tmp_path / "elsewhere"))
        from nvh.integrations.wizard import vision_bridge as vb

        assert (tmp_path / "elsewhere" / "images").resolve() in vb.allowed_image_roots()
