"""The Wizard's vision bridge: ``analyze_image`` / ``read_text_from_image`` behind a path allowlist.

Hermetic: ``NVH_HOME`` is ``tmp_path``, the workspace root is ``NVH_PROJECTS``
(the same helper ``shell`` mounts, ``sandbox_tools.workspace_dir``), the
screenshot root is patched to a tmp folder, and the vision providers inside
:mod:`nvh.core.vision_tools` are monkeypatched (no Ollama, no litellm, no
network) — the same seams tests/test_vision_tools.py uses. The tools are
registered on a fresh ``WizardToolRegistry`` here; ``default_registry()``
wiring is pinned in tests/test_wizard_tools.py.

Pinned (docs/proposals/SPARK_CONCIERGE_2026-09.md Phase 3, design decision 4,
plus the review fixes):

  - both tools are ``auto`` and their parameters are the Wizard's
    ``{name: {type, description, required}}`` shape (``prompt``, not the core
    tool's ``question``);
  - a path is read only when, after resolving symlinks, it sits under
    ``$NVH_HOME/rag/uploads`` (incl. ``wizard/``), the workspace or — directly
    — the screenshot temp dir, AND is an image: not a guardrails-blocked
    basename, an image suffix, an image signature when it exists. Anything
    else is an in-band ``{ok: False, refused: True}`` naming the rule, and no
    provider is called;
  - only a chat attachment (``rag/uploads/wizard/``) may go to a cloud vision
    model; every other path is local-only and refused without a local model;
  - the result keeps the ``[Vision: <model>, N KB]`` first line and adds
    ``model`` / ``provider`` / ``bytes``; "no model", "not found" and the 20 MB
    cap are not-ok results, never exceptions;
  - secrets in the returned text are redacted before anything downstream cuts it.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nvh.core import vision_tools
from nvh.integrations.wizard import vision_bridge as vb
from nvh.integrations.wizard.chat import ATTACHED_IMAGES_NOTE, append_attached_images
from nvh.integrations.wizard.tools import WizardToolRegistry

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 8
BMP = b"BM" + b"\x00" * 16


@pytest.fixture()
def roots(monkeypatch, tmp_path: Path) -> SimpleNamespace:
    """NVH_HOME, workspace (NVH_PROJECTS) and screenshot roots all under tmp_path; ``outside`` is none of them."""
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    shots = tmp_path / "shots"
    outside = tmp_path / "outside"
    for folder in (home, workspace, shots, outside):
        folder.mkdir()
    monkeypatch.setenv("NVH_HOME", str(home))
    monkeypatch.setenv("NVH_PROJECTS", str(workspace))
    monkeypatch.setattr(vb, "_screenshot_dir", lambda: shots)
    uploads = home / "rag" / "uploads" / "wizard" / "conv-1"
    uploads.mkdir(parents=True)
    return SimpleNamespace(home=home, uploads=uploads, workspace=workspace, shots=shots, outside=outside)


@pytest.fixture()
def registry() -> WizardToolRegistry:
    reg = WizardToolRegistry()
    vb.register_wizard_tools(reg)
    return reg


@pytest.fixture()
def local_vision(monkeypatch) -> dict:
    """An installed ``qwen3-vl:4b`` answers; the cloud path must never be reached."""
    seen: dict = {}
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: "qwen3-vl:4b")

    async def fake_ollama(image_data, question, model):
        seen.update(question=question, model=model, image_data=image_data)
        return "A grey cat asleep on a keyboard."

    async def no_cloud(*_a, **_kw):
        raise AssertionError("cloud vision must not be called when a local model answered")

    monkeypatch.setattr(vision_tools, "_analyze_with_ollama", fake_ollama)
    monkeypatch.setattr(vision_tools, "_analyze_with_cloud", no_cloud)
    return seen


@pytest.fixture()
def no_provider_calls(monkeypatch) -> None:
    """Any provider lookup is a test failure — for the refusal paths."""

    def boom():
        raise AssertionError("a refused path must never reach the vision providers")

    async def aboom(*_a, **_kw):
        raise AssertionError("a refused path must never reach the vision providers")

    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", boom)
    monkeypatch.setattr(vision_tools, "_analyze_with_ollama", aboom)
    monkeypatch.setattr(vision_tools, "_analyze_with_cloud", aboom)


@pytest.fixture()
def no_local_model_cloud_forbidden(monkeypatch) -> dict:
    """No Ollama vision model; the cloud fallback records a call it must never get."""
    seen: dict = {"cloud_calls": 0}
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: None)
    monkeypatch.setattr(vision_tools, "_suggested_vision_pull", lambda: "qwen3-vl:4b")

    async def cloud(*_a, **_kw):
        seen["cloud_calls"] += 1
        return "leaked"

    monkeypatch.setattr(vision_tools, "_analyze_with_cloud", cloud)
    return seen


def _png(folder: Path, name: str = "shot.png") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(PNG)
    return path


# ───────────────────────────────────────────────────────────────────────────
# Registration
# ───────────────────────────────────────────────────────────────────────────


def test_registers_two_auto_tools_with_wizard_parameter_shape(registry) -> None:
    names = {t.name for t in registry.list_tools()}
    assert names == set(vb.WIZARD_VISION_TOOLS) == {"analyze_image", "read_text_from_image"}
    analyze = registry.get("analyze_image")
    ocr = registry.get("read_text_from_image")
    assert analyze.safety_class == "auto" and ocr.safety_class == "auto"
    assert analyze.planner is None and ocr.planner is None
    assert analyze.parameters["image_path"]["required"] is True
    assert analyze.parameters["prompt"]["required"] is False
    assert set(ocr.parameters) == {"image_path"}
    # The catalogue names the rules the handler enforces.
    assert "refused" in analyze.parameters["image_path"]["description"]
    assert "cloud" in analyze.parameters["image_path"]["description"]
    for tool in (analyze, ocr):
        pub = tool.as_public_dict()
        assert "handler" not in pub and pub["enabled"] is True


def test_default_roots_follow_nvh_home_and_the_shell_workspace(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "h"
    monkeypatch.setenv("NVH_HOME", str(home))
    monkeypatch.delenv("NVH_PROJECTS", raising=False)
    assert vb.uploads_root() == home / "rag" / "uploads"
    assert vb.wizard_uploads_root() == home / "rag" / "uploads" / "wizard"
    # One definition of "the workspace": the sandbox bridge's.
    from nvh.integrations.wizard.sandbox_tools import workspace_dir

    assert vb.workspace_dir is workspace_dir
    assert vb.workspace_dir() == (home / "projects").resolve()
    roots = vb.allowed_image_roots()
    assert roots[0] == (home / "rag" / "uploads").resolve()
    assert (home / "projects").resolve() in roots
    assert vb._screenshot_dir().resolve() in roots
    monkeypatch.setenv("NVH_PROJECTS", str(tmp_path / "elsewhere"))
    assert (tmp_path / "elsewhere").resolve() in vb.allowed_image_roots()


def test_image_kind_tables_agree() -> None:
    assert set(vb.IMAGE_MIME_SUFFIXES.values()) <= vb.IMAGE_SUFFIXES
    assert set(vb.SUFFIX_MIMES) == vb.IMAGE_SUFFIXES
    assert vb.sniff_image_suffix(PNG) == ".png"
    assert vb.sniff_image_suffix(JPEG) == ".jpg"
    assert vb.sniff_image_suffix(GIF) == ".gif"
    assert vb.sniff_image_suffix(b"GIF87a" + b"\x00" * 8) == ".gif"
    assert vb.sniff_image_suffix(WEBP) == ".webp"
    assert vb.sniff_image_suffix(BMP) == ".bmp"
    for junk in (b"", b"RIFF\x00\x00\x00\x00WAVE", b"SECRET=x\n", b"%PDF-1.7", b"<svg xmlns", b"\x89PNG\r\n"):
        assert vb.sniff_image_suffix(junk) is None, junk


# ───────────────────────────────────────────────────────────────────────────
# Analysis results
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_image_under_uploads_runs_the_local_model(roots, registry, local_vision) -> None:
    img = _png(roots.uploads)
    result = await registry.execute("analyze_image", arguments={"image_path": str(img), "prompt": "What animal?"})
    assert result["ok"] is True and result["safety_class"] == "auto"
    body = result["result"]
    assert body["ok"] is True
    assert body["text"].startswith("[Vision: qwen3-vl:4b, ")
    assert "grey cat" in body["text"]
    assert body["model"] == "qwen3-vl:4b"
    assert body["provider"] == "ollama"
    assert body["bytes"] == len(PNG)
    assert body["image_path"] == str(img)
    assert "error" not in body and "refused" not in body
    assert local_vision["question"] == "What animal?"
    assert base64.b64decode(local_vision["image_data"]) == PNG


@pytest.mark.asyncio
async def test_prompt_is_optional_and_defaults_to_a_description(roots, registry, local_vision) -> None:
    img = _png(roots.uploads)
    body = (await registry.execute("analyze_image", arguments={"image_path": str(img)}))["result"]
    assert body["ok"] is True
    assert local_vision["question"] == "Describe what you see in this image."
    # A blank prompt is the same as none.
    await registry.execute("analyze_image", arguments={"image_path": str(img), "prompt": "   "})
    assert local_vision["question"] == "Describe what you see in this image."


@pytest.mark.asyncio
async def test_cloud_fallback_for_a_chat_attachment_reports_provider_cloud(roots, registry, monkeypatch) -> None:
    img = _png(roots.uploads)  # rag/uploads/wizard/<conv>/ — the user attached it
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: None)

    async def fake_cloud(image_data, mime, question):
        assert mime == "image/png"  # from the bytes' signature, never a default
        assert base64.b64decode(image_data) == PNG
        return "A chart with three bars."

    monkeypatch.setattr(vision_tools, "_analyze_with_cloud", fake_cloud)
    body = (await registry.execute("analyze_image", arguments={"image_path": str(img)}))["result"]
    assert body["ok"] is True
    assert body["text"].startswith("[Vision: cloud, ")
    assert body["model"] == "cloud" and body["provider"] == "cloud"


@pytest.mark.asyncio
async def test_the_cloud_mime_follows_the_bytes_not_the_suffix(roots, registry, monkeypatch) -> None:
    img = roots.uploads / "photo.png"
    img.write_bytes(JPEG)  # a JPEG someone named .png
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: None)
    mimes: list[str] = []

    async def fake_cloud(image_data, mime, question):
        mimes.append(mime)
        return "ok"

    monkeypatch.setattr(vision_tools, "_analyze_with_cloud", fake_cloud)
    body = (await registry.execute("analyze_image", arguments={"image_path": str(img)}))["result"]
    assert body["ok"] is True and mimes == ["image/jpeg"]


@pytest.mark.asyncio
async def test_no_vision_model_at_all_is_not_ok_and_keeps_the_pull_hint(roots, registry, monkeypatch) -> None:
    img = _png(roots.uploads)
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: None)

    async def no_cloud(*_a, **_kw):
        return None

    monkeypatch.setattr(vision_tools, "_analyze_with_cloud", no_cloud)
    monkeypatch.setattr(vision_tools, "_suggested_vision_pull", lambda: "qwen3-vl:4b")
    body = (await registry.execute("analyze_image", arguments={"image_path": str(img)}))["result"]
    assert body["ok"] is False and "refused" not in body
    assert body["model"] is None and body["provider"] is None
    assert body["error"].startswith("No vision model available")
    assert "ollama pull qwen3-vl:4b" in body["error"]
    assert body["text"].startswith("[Image loaded: shot.png")
    assert body["bytes"] == len(PNG)


@pytest.mark.asyncio
async def test_read_text_from_image_uses_the_ocr_prompt(roots, registry, local_vision) -> None:
    img = _png(roots.uploads, "terminal.png")
    body = (await registry.execute("read_text_from_image", arguments={"image_path": str(img)}))["result"]
    assert body["ok"] is True and body["model"] == "qwen3-vl:4b"
    assert local_vision["question"].startswith("Read ALL visible text from this image.")
    assert local_vision["question"] == vb.OCR_QUESTION


@pytest.mark.asyncio
async def test_secrets_in_the_returned_text_are_redacted(roots, registry, monkeypatch) -> None:
    img = _png(roots.uploads)
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: "qwen3-vl:4b")

    async def leaky(image_data, question, model):
        return "OPENAI_API_KEY=sk-proj-" + "a" * 40 + "\nand a ghp_" + "b" * 40

    monkeypatch.setattr(vision_tools, "_analyze_with_ollama", leaky)
    body = (await registry.execute("read_text_from_image", arguments={"image_path": str(img)}))["result"]
    assert body["ok"] is True
    assert "sk-proj-" not in body["text"] and "ghp_b" not in body["text"]
    assert "[REDACTED" in body["text"]


@pytest.mark.asyncio
async def test_missing_file_under_an_allowed_root_is_not_ok_not_refused(roots, registry, no_provider_calls) -> None:
    body = (await registry.execute(
        "analyze_image", arguments={"image_path": str(roots.uploads / "gone.png")},
    ))["result"]
    assert body["ok"] is False and "refused" not in body
    assert "not found" in body["error"].lower()
    assert body["bytes"] is None


@pytest.mark.asyncio
async def test_oversized_image_is_refused_by_the_20mb_cap(roots, registry, no_provider_calls) -> None:
    big = roots.uploads / "huge.png"
    with big.open("wb") as fh:
        fh.write(PNG)  # a real signature, so only the size rule speaks
        fh.truncate(vb.MAX_IMAGE_BYTES + 1)
    body = (await registry.execute("analyze_image", arguments={"image_path": str(big)}))["result"]
    assert body["ok"] is False
    assert "too large" in body["error"].lower() and "20 MB" in body["error"]


@pytest.mark.asyncio
async def test_a_provider_exception_is_a_not_ok_result(roots, registry, monkeypatch) -> None:
    img = _png(roots.uploads)
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: "qwen3-vl:4b")

    async def broken(*_a, **_kw):
        raise RuntimeError("ollama exploded")

    monkeypatch.setattr(vision_tools, "_analyze_with_ollama", broken)
    body = (await registry.execute("analyze_image", arguments={"image_path": str(img)}))["result"]
    assert body["ok"] is False and body["error"].startswith("Failed to analyze image: ollama exploded")


# ───────────────────────────────────────────────────────────────────────────
# The cloud rule: only a chat attachment may leave the machine
# ───────────────────────────────────────────────────────────────────────────


def test_cloud_allowed_only_under_the_wizard_attachment_folder(roots) -> None:
    assert vb.cloud_allowed(roots.uploads / "a.png") is True
    assert vb.cloud_allowed((roots.home / "rag" / "uploads" / "wizard").resolve()) is True
    assert vb.cloud_allowed((roots.home / "rag" / "uploads" / "2026" / "a.png").resolve()) is False
    assert vb.cloud_allowed((roots.workspace / "a.png").resolve()) is False
    assert vb.cloud_allowed((roots.shots / "a.png").resolve()) is False


@pytest.mark.asyncio
async def test_without_a_local_model_a_non_attachment_path_is_refused_before_any_cloud_call(
    roots, registry, no_local_model_cloud_forbidden,
) -> None:
    for folder in (roots.workspace / "app", roots.home / "rag" / "uploads" / "2026", roots.shots):
        img = _png(folder, "diagram.png")
        body = (await registry.execute("analyze_image", arguments={"image_path": str(img)}))["result"]
        assert body["ok"] is False and body["refused"] is True and body["local_only"] is True, folder
        assert body["model"] is None and body["provider"] is None
        assert "ollama pull qwen3-vl:4b" in body["error"] and "attach" in body["error"].lower()
        assert body["text"].startswith("[Image loaded: diagram.png")
    assert no_local_model_cloud_forbidden["cloud_calls"] == 0


@pytest.mark.asyncio
async def test_a_local_model_that_fails_does_not_fall_through_to_the_cloud_for_a_workspace_file(
    roots, registry, monkeypatch,
) -> None:
    img = _png(roots.workspace, "diagram.png")
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: "qwen3-vl:4b")
    monkeypatch.setattr(vision_tools, "_suggested_vision_pull", lambda: "qwen3-vl:4b")

    async def ollama_down(*_a, **_kw):
        return None

    async def cloud(*_a, **_kw):
        raise AssertionError("a workspace file must never reach the cloud")

    monkeypatch.setattr(vision_tools, "_analyze_with_ollama", ollama_down)
    monkeypatch.setattr(vision_tools, "_analyze_with_cloud", cloud)
    body = (await registry.execute("analyze_image", arguments={"image_path": str(img)}))["result"]
    assert body["ok"] is False and body["refused"] is True and body["local_only"] is True


@pytest.mark.asyncio
async def test_with_a_local_model_every_allowed_root_is_readable(roots, registry, local_vision) -> None:
    for folder in (roots.uploads, roots.home / "rag" / "uploads", roots.workspace / "proj", roots.shots):
        img = _png(folder, "ok.png")
        body = (await registry.execute("analyze_image", arguments={"image_path": str(img)}))["result"]
        assert body["ok"] is True and body["provider"] == "ollama", folder


# ───────────────────────────────────────────────────────────────────────────
# The allowlist
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_path_outside_the_roots_is_refused_in_band_naming_them(roots, registry, no_provider_calls) -> None:
    img = _png(roots.outside, "secret.png")
    envelope = await registry.execute("analyze_image", arguments={"image_path": str(img)})
    assert envelope["ok"] is True  # the tool ran and answered; the refusal is its answer
    body = envelope["result"]
    assert body == {
        "ok": False,
        "refused": True,
        "error": body["error"],
        "allowed_roots": [str(r) for r in vb.allowed_image_roots()],
    }
    assert str(roots.uploads.parent.parent.resolve()) in body["error"]  # …/rag/uploads
    assert str(roots.workspace.resolve()) in body["error"]
    assert str(roots.shots.resolve()) in body["error"]
    assert "text" not in body


@pytest.mark.asyncio
async def test_home_itself_and_traversal_are_refused(roots, registry, no_provider_calls) -> None:
    home_img = _png(roots.home, "config.png")  # under NVH_HOME but not under rag/uploads
    for path in (
        str(home_img),
        str(roots.uploads / ".." / ".." / ".." / "config.png"),
        "/etc/passwd",
        "",
        "   ",
    ):
        body = (await registry.execute("read_text_from_image", arguments={"image_path": path}))["result"]
        assert body["ok"] is False and body["refused"] is True, path


@pytest.mark.asyncio
async def test_the_temp_root_admits_direct_children_only(roots, registry, no_provider_calls) -> None:
    nested = _png(roots.shots / "some-app" / "cache", "thumb.png")
    body = (await registry.execute("analyze_image", arguments={"image_path": str(nested)}))["result"]
    assert body["ok"] is False and body["refused"] is True
    assert "temp folder only directly" in body["error"]
    assert vb.image_path_refusal(str(roots.shots / "tmpabc123.png")) is None


@pytest.mark.asyncio
async def test_non_image_files_under_the_roots_are_refused_unread(roots, registry, no_provider_calls) -> None:
    (roots.workspace / "app").mkdir()
    cases = {
        roots.workspace / "app" / ".env": ("SECRET=x\n", "sensitive"),
        roots.workspace / "app" / "id_rsa": ("-----BEGIN OPENSSH PRIVATE KEY-----", "sensitive"),
        roots.home / "rag" / "uploads" / "2026" / "contract.pdf": ("%PDF-1.7", "not an image file"),
        roots.uploads / "notes.txt": ("hello", "not an image file"),
        roots.uploads / "logo.svg": ("<svg xmlns='http://www.w3.org/2000/svg'/>", "not an image file"),
    }
    for path, (content, fragment) in cases.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        body = (await registry.execute("analyze_image", arguments={"image_path": str(path)}))["result"]
        assert body["ok"] is False and body["refused"] is True, path
        assert fragment in body["error"], (path, body["error"])
        assert "text" not in body and "SECRET" not in body["error"]


@pytest.mark.asyncio
async def test_an_image_suffix_on_non_image_bytes_is_refused_by_the_signature(roots, registry, no_provider_calls) -> None:
    fake = roots.uploads / "innocent.png"
    fake.write_text("HIVE_API_KEY=abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
    body = (await registry.execute("analyze_image", arguments={"image_path": str(fake)}))["result"]
    assert body["ok"] is False and body["refused"] is True
    assert "signature" in body["error"] and "HIVE_API_KEY" not in body["error"]
    folder = roots.uploads / "dir.png"
    folder.mkdir()
    body = (await registry.execute("analyze_image", arguments={"image_path": str(folder)}))["result"]
    assert body["refused"] is True and "not a regular file" in body["error"]
    # Every accepted kind passes the signature check, whatever it is named.
    for name, blob in (("a.png", PNG), ("b.jpg", JPEG), ("c.jpeg", GIF), ("d.gif", WEBP), ("e.webp", BMP), ("f.bmp", PNG)):
        (roots.uploads / name).write_bytes(blob)
        assert vb.image_path_refusal(str(roots.uploads / name)) is None, name


@pytest.mark.asyncio
async def test_missing_image_path_argument_is_refused(roots, registry, no_provider_calls) -> None:
    body = (await registry.execute("analyze_image", arguments={}))["result"]
    assert body["refused"] is True and body["error"] == "image_path is required"


def test_admit_image_path_hands_back_the_resolved_path_that_gets_read(roots) -> None:
    img = _png(roots.uploads, "a.png")
    dotted = roots.uploads / "sub" / ".." / "a.png"
    resolved, refusal = vb.admit_image_path(str(dotted))
    assert refusal is None and resolved == img.resolve()
    resolved, refusal = vb.admit_image_path(str(roots.outside / "a.png"))
    assert resolved is None and refusal["refused"] is True


@pytest.mark.asyncio
async def test_symlink_under_uploads_pointing_outside_is_refused(roots, registry, no_provider_calls) -> None:
    target = _png(roots.outside, "real.png")
    link = roots.uploads / "innocent.png"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:  # unprivileged Windows
        pytest.skip(f"symlinks unavailable here: {exc}")
    body = (await registry.execute("analyze_image", arguments={"image_path": str(link)}))["result"]
    assert body["ok"] is False and body["refused"] is True
    assert vb.image_path_refusal(str(link)) is not None
    assert vb.image_path_refusal(str(target)) is not None


def test_image_path_refusal_accepts_a_path_in_the_roots(roots) -> None:
    assert vb.image_path_refusal(str(roots.uploads / "a.png")) is None
    assert vb.image_path_refusal(str(roots.workspace / "b.png")) is None
    assert vb.image_path_refusal(str(roots.shots / "c.png")) is None
    refusal = vb.image_path_refusal(str(roots.outside / "d.png"))
    assert refusal["refused"] is True and refusal["ok"] is False


def test_parse_vision_prefix() -> None:
    assert vb.parse_vision_prefix("[Vision: qwen3-vl:4b, 12.3 KB]\nhello") == ("qwen3-vl:4b", "ollama")
    assert vb.parse_vision_prefix("[Vision: cloud, 0.5 KB]\nhello") == ("cloud", "cloud")
    assert vb.parse_vision_prefix("[Image loaded: a.png, 1.0 KB]\nNo vision model available.") == (None, None)
    assert vb.parse_vision_prefix("Image not found: x") == (None, None)
    assert vb.parse_vision_prefix("") == (None, None)


# ───────────────────────────────────────────────────────────────────────────
# The user-turn note (chat.py helper the HTTP layer calls)
# ───────────────────────────────────────────────────────────────────────────


def test_append_attached_images_note() -> None:
    assert append_attached_images("what is this?", []) == "what is this?"
    assert append_attached_images("what is this?", ["", "  "]) == "what is this?"
    out = append_attached_images("what is this?\n", ["/h/rag/uploads/wizard/c1/a.png", "/h/rag/uploads/wizard/c1/b.jpg"])
    assert out == (
        "what is this?\n\n"
        f"{ATTACHED_IMAGES_NOTE} /h/rag/uploads/wizard/c1/a.png, /h/rag/uploads/wizard/c1/b.jpg"
    )
    assert ATTACHED_IMAGES_NOTE.startswith("Attached images (use analyze_image or read_text_from_image on these paths):")
