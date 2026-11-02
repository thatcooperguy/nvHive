"""Image generation (nvh/integrations/imagegen.py) and ``GET /v1/outputs/images/{scope}/{filename}``.

Hermetic: ``NVAPI_KEY`` and the ``NVH_NVIDIA_IMAGE_*`` / ``NVH_COMFYUI_CHECKPOINT``
knobs are cleared per test, ``detect_comfyui`` is replaced by a fake that
reports a *recorded* runtime host/port (never ComfyUI's default), and every
HTTP call goes through an ``httpx.MockTransport`` handed in via
``imagegen._http_client`` — no socket, no ComfyUI, no NVIDIA call. ``NVH_HOME``
is the conftest's throwaway home.

Pinned (docs/proposals/ADDS_0_46_DESIGN_2026-11.md §3, O2, risk 16):

  - precedence: ``NVAPI_KEY`` → NVIDIA-hosted (``cost_usd`` None, ``cost_known``
    False, model from the endpoint tail or ``NVH_NVIDIA_IMAGE_MODEL``); else
    ComfyUI at the recorded runtime host/port (``$0``, ``cost_known`` True);
    else no backend with the plain-words reason. Only ``NVAPI_KEY`` is read.
  - ceiling rule: a finite profile ceiling plus an unknown hosted price skips
    the paid path — ComfyUI with the exact sentence, or the exact refusal when
    ComfyUI is down. ``requested`` local/nvidia force a backend; the ceiling
    still wins.
  - files land under ``outputs_dir/images/<scope>/<ts>-<8 hex><suffix>`` and
    ``url`` is the route's path; the four NVIDIA response shapes decode.
  - ``comfyui.generate_portrait`` still returns ``(bytes, suffix)`` and still
    raises ``NotImplementedError`` with the four-bullet hint; the moved
    renderers are re-exported under their old names.
  - the route: 200 with the suffix's media type and a private cache header,
    404 for ``..`` / a foreign scope / a missing file, 401 when keyed without
    a header, 200 in open mode.
"""

from __future__ import annotations

import base64
import json
import math
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nvh.integrations import imagegen
from nvh.integrations.installs import comfyui
from nvh.integrations.workspace.storage import storage_layout

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
KEY = "nvapi-test-key"
RECORDED_HOST = "127.0.0.1"
RECORDED_PORT = 8199  # what comfyui-service.json recorded; not the 8188 default
FILENAME_RE = re.compile(r"^\d{14}-[0-9a-f]{8}\.(png|jpg|webp)$")
ROUTES_INIT = Path(__file__).resolve().parents[1] / "nvh" / "api" / "routes" / "__init__.py"


@pytest.fixture(autouse=True)
def _no_image_env(monkeypatch):
    for name in (
        "NVAPI_KEY",
        "NVIDIA_API_KEY",
        "NIM_API_KEY",
        "NVH_NVIDIA_IMAGE_ENDPOINT",
        "NVH_NVIDIA_IMAGE_MODEL",
        "NVH_COMFYUI_CHECKPOINT",
        "HIVE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeDetect:
    """``detect_comfyui`` that reports the recorded runtime pair and records its calls."""

    def __init__(self, running: bool) -> None:
        self.running = running
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        host: str = comfyui.DEFAULT_HOST,
        port: int = comfyui.DEFAULT_PORT,
        home_dir: Any = None,
        *,
        check_http: bool = True,
    ) -> dict[str, Any]:
        self.calls.append({"host": host, "port": port, "home_dir": home_dir})
        return {
            "installed": True,
            "running": self.running,
            "host": RECORDED_HOST,
            "port": RECORDED_PORT,
            "url": f"http://{RECORDED_HOST}:{RECORDED_PORT}",
        }


@pytest.fixture()
def comfy_up(monkeypatch) -> FakeDetect:
    fake = FakeDetect(running=True)
    monkeypatch.setattr(comfyui, "detect_comfyui", fake)
    return fake


@pytest.fixture()
def comfy_down(monkeypatch) -> FakeDetect:
    fake = FakeDetect(running=False)
    monkeypatch.setattr(comfyui, "detect_comfyui", fake)
    return fake


class FakeHTTP:
    """The NVIDIA endpoint, a CDN and ComfyUI's three routes behind ``httpx.MockTransport``."""

    def __init__(
        self,
        *,
        nvidia_shape: str = "image",
        nvidia_status: int = 200,
        comfy_filename: str = "nvh_portrait_00001_.png",
    ) -> None:
        self.nvidia_shape = nvidia_shape
        self.nvidia_status = nvidia_status
        self.comfy_filename = comfy_filename
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        path = request.url.path
        if host == "integrate.api.nvidia.com":
            if self.nvidia_status != 200:
                return httpx.Response(self.nvidia_status, text="upstream exploded")
            b64 = base64.b64encode(PNG).decode("ascii")
            body: dict[str, Any]
            if self.nvidia_shape == "image":
                body = {"image": b64}
            elif self.nvidia_shape == "artifacts":
                body = {"artifacts": [{"base64": b64, "seed": 1}]}
            elif self.nvidia_shape == "b64_json":
                body = {"data": [{"b64_json": b64}]}
            elif self.nvidia_shape == "url":
                body = {"data": [{"url": "https://cdn.example.test/out.png"}]}
            else:
                body = {"weird": 1, "shape": self.nvidia_shape}
            return httpx.Response(200, json=body)
        if host == "cdn.example.test":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        # ComfyUI
        if path == "/prompt" and request.method == "POST":
            return httpx.Response(200, json={"prompt_id": "pid-1"})
        if path == "/history/pid-1":
            image = {"filename": self.comfy_filename, "subfolder": "", "type": "output"}
            return httpx.Response(200, json={"pid-1": {"outputs": {"7": {"images": [image]}}}})
        if path == "/view":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404, text=f"unexpected {request.method} {request.url}")

    def install(self, monkeypatch) -> FakeHTTP:
        transport = httpx.MockTransport(self.handler)
        monkeypatch.setattr(
            imagegen,
            "_http_client",
            lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
        )
        return self


@pytest.fixture()
def fake_http(monkeypatch) -> FakeHTTP:
    return FakeHTTP().install(monkeypatch)


# ---------------------------------------------------------------------------
# plan_image_backend
# ---------------------------------------------------------------------------


class TestPlan:
    def test_key_set_prefers_nvidia_without_probing_comfyui(self, monkeypatch, comfy_down) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        plan = imagegen.plan_image_backend()
        assert plan["backend"] == "nvidia"
        assert plan["model"] == "sdxl-turbo"  # the default endpoint's last path segment
        assert plan["cost_usd"] is None and plan["cost_known"] is False
        assert plan["summary"] == "NVIDIA-hosted (sdxl-turbo) · cost unknown · billed to your NVIDIA account"
        assert plan["reason"] == "" and plan["paid_skipped"] is False
        assert comfy_down.calls == []

    def test_model_name_follows_the_env(self, monkeypatch, comfy_down) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        monkeypatch.setenv("NVH_NVIDIA_IMAGE_ENDPOINT", "https://api.nvcf.example/v1/genai/vendor/my-model/")
        assert imagegen.plan_image_backend()["model"] == "my-model"
        monkeypatch.setenv("NVH_NVIDIA_IMAGE_MODEL", "named-model")
        plan = imagegen.plan_image_backend()
        assert plan["model"] == "named-model"
        assert "NVIDIA-hosted (named-model)" in plan["summary"]

    def test_only_nvapi_key_is_read(self, monkeypatch, comfy_down) -> None:
        # O2: a NIM chat key must not flip the box onto the paid hosted path.
        monkeypatch.setenv("NVIDIA_API_KEY", "nim-chat-key")
        monkeypatch.setenv("NIM_API_KEY", "nim-chat-key")
        plan = imagegen.plan_image_backend()
        assert plan["backend"] is None
        assert plan["reason"] == imagegen.NO_BACKEND_REASON

    def test_no_key_uses_comfyui_at_the_recorded_port(self, comfy_up, tmp_path) -> None:
        plan = imagegen.plan_image_backend(home_dir=tmp_path)
        assert plan["backend"] == "comfyui"
        assert (plan["host"], plan["port"]) == (RECORDED_HOST, RECORDED_PORT)
        assert plan["cost_usd"] == 0.0 and plan["cost_known"] is True
        assert plan["model"] == imagegen.DEFAULT_COMFYUI_CHECKPOINT
        assert plan["summary"] == (
            f"local ComfyUI on {RECORDED_HOST}:{RECORDED_PORT} · $0 · uses GPU memory while it runs"
        )
        assert comfy_up.calls[0]["home_dir"] == tmp_path  # detect_comfyui honours the home

    def test_a_recorded_host_that_is_not_loopback_is_never_used(self, monkeypatch, comfy_up) -> None:
        """A ``comfyui-service.json`` naming another machine (planted under a request-supplied
        ``home_dir``) must not make the renderer POST the prompt there: the status reads as not
        running, so the plan has no backend and ``requested="local"`` refuses too."""
        def foreign(host=comfyui.DEFAULT_HOST, port=comfyui.DEFAULT_PORT, home_dir=None, *, check_http=True):
            return {"installed": True, "running": True, "host": "internal-host", "port": 80}

        monkeypatch.setattr(comfyui, "detect_comfyui", foreign)
        status = imagegen._comfyui_status(None)
        assert status["running"] is False and status["host"] == "internal-host"
        plan = imagegen.plan_image_backend()
        assert plan["backend"] is None and plan["reason"] == imagegen.NO_BACKEND_REASON
        local = imagegen.plan_image_backend(requested="local")
        assert local["backend"] is None and "internal-host:80" in local["reason"]
        assert comfyui.LOOPBACK_HOSTS == frozenset({"127.0.0.1", "localhost"})

    def test_nothing_available(self, comfy_down) -> None:
        plan = imagegen.plan_image_backend()
        assert plan["backend"] is None
        assert plan["reason"] == imagegen.NO_BACKEND_REASON
        assert plan["summary"] == imagegen.NO_BACKEND_SUMMARY
        assert plan["cost_known"] is False and plan["model"] == ""
        assert "NVAPI_KEY" in plan["reason"] and "ComfyUI" in plan["reason"]

    def test_ceiling_with_unknown_price_uses_comfyui(self, monkeypatch, comfy_up) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        plan = imagegen.plan_image_backend(cost_ceiling_usd=0.05)
        assert plan["backend"] == "comfyui"
        assert plan["paid_skipped"] is True and plan["cost_known"] is True
        assert plan["reason"] == (
            "This profile has a $0.05/turn ceiling and the NVIDIA-hosted image price is not known, "
            "so nvHive used local ComfyUI"
        )
        assert plan["reason"] == imagegen.ceiling_reason(0.05, comfyui_reachable=True)

    def test_ceiling_with_comfyui_down_is_a_refusal(self, monkeypatch, comfy_down) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        plan = imagegen.plan_image_backend(cost_ceiling_usd=0.05)
        assert plan["backend"] is None and plan["paid_skipped"] is True
        assert plan["reason"] == imagegen.ceiling_reason(0.05, comfyui_reachable=False)
        assert plan["reason"].startswith(
            "This profile has a $0.05/turn ceiling and the NVIDIA-hosted image price is not known"
        )
        assert "start ComfyUI (Setup → ComfyUI) or run without a specialist" in plan["reason"]
        assert "$0.05" in plan["summary"]

    @pytest.mark.parametrize("ceiling", [0, 0.0, 1, 2.5])
    def test_a_zero_or_positive_ceiling_counts(self, monkeypatch, comfy_up, ceiling) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        plan = imagegen.plan_image_backend(cost_ceiling_usd=ceiling)
        assert plan["backend"] == "comfyui" and plan["paid_skipped"] is True
        assert plan["cost_ceiling_usd"] == float(ceiling)

    @pytest.mark.parametrize("ceiling", [None, math.inf, math.nan, -1, "abc", True])
    def test_a_missing_or_nonsense_ceiling_is_no_ceiling(self, monkeypatch, comfy_up, ceiling) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        plan = imagegen.plan_image_backend(cost_ceiling_usd=ceiling)
        assert plan["backend"] == "nvidia" and plan["cost_ceiling_usd"] is None

    def test_format_usd(self) -> None:
        assert imagegen.format_usd(0.05) == "$0.05"
        assert imagegen.format_usd(1) == "$1.00"
        assert imagegen.format_usd(0.005) == "$0.005"
        assert imagegen.format_usd(0) == "$0.00"

    def test_requested_local_forces_comfyui_even_with_a_key(self, monkeypatch, comfy_up) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        plan = imagegen.plan_image_backend(requested="local")
        assert plan["backend"] == "comfyui" and plan["port"] == RECORDED_PORT
        assert plan["requested"] == "local"

    def test_requested_local_with_comfyui_down(self, monkeypatch, comfy_down) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        plan = imagegen.plan_image_backend(requested="local")
        assert plan["backend"] is None
        assert f"{RECORDED_HOST}:{RECORDED_PORT}" in plan["reason"]
        assert "Setup → ComfyUI" in plan["reason"]

    def test_requested_nvidia(self, monkeypatch, comfy_up) -> None:
        plan = imagegen.plan_image_backend(requested="nvidia")
        assert plan["backend"] is None and "NVAPI_KEY" in plan["reason"]
        monkeypatch.setenv("NVAPI_KEY", KEY)
        assert imagegen.plan_image_backend(requested="NVIDIA")["backend"] == "nvidia"
        # The model may ask for the paid path; the ceiling still wins.
        capped = imagegen.plan_image_backend(requested="nvidia", cost_ceiling_usd=0.05)
        assert capped["backend"] == "comfyui" and capped["paid_skipped"] is True

    def test_requested_unknown(self, monkeypatch, comfy_up) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        plan = imagegen.plan_image_backend(requested="dalle")
        assert plan["backend"] is None
        assert "auto, local or nvidia" in plan["reason"]
        assert comfy_up.calls == []


# ---------------------------------------------------------------------------
# render_image
# ---------------------------------------------------------------------------


class TestRender:
    async def test_nvidia_render_lands_under_outputs_images(
        self, monkeypatch, comfy_down, fake_http, tmp_path
    ) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        home = tmp_path / "home"
        result = await imagegen.render_image("a hero image for the lab wiki", home_dir=home, width=768, height=512)

        expected_dir = storage_layout(home).outputs_dir / "images" / "wizard"
        path = Path(result.path)
        assert path.parent == expected_dir and path.read_bytes() == PNG
        assert FILENAME_RE.match(result.filename) and result.suffix == ".png"
        assert result.url == f"/v1/outputs/images/wizard/{result.filename}"
        assert result.scope == "wizard" and result.backend == "nvidia"
        assert result.model == "sdxl-turbo"
        assert result.cost_usd is None and result.cost_known is False
        assert result.latency_ms >= 0 and result.reason == ""
        assert (result.width, result.height) == (768, 512)
        assert not list(expected_dir.glob("*.tmp"))
        assert result.to_dict()["url"] == result.url

        [request] = fake_http.requests
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        payload = json.loads(request.content)
        assert payload["prompt"] == "a hero image for the lab wiki"
        assert (payload["width"], payload["height"]) == (768, 512)
        assert "model" not in payload
        assert comfy_down.calls == []

    async def test_nvidia_model_override_is_sent(self, monkeypatch, comfy_down, fake_http, tmp_path) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        monkeypatch.setenv("NVH_NVIDIA_IMAGE_MODEL", "named-model")
        result = await imagegen.render_image("p", home_dir=tmp_path)
        assert result.model == "named-model"
        assert json.loads(fake_http.requests[0].content)["model"] == "named-model"

    @pytest.mark.parametrize("shape", ["image", "artifacts", "b64_json", "url"])
    async def test_the_four_nvidia_response_shapes(self, monkeypatch, comfy_down, shape, tmp_path) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        fake = FakeHTTP(nvidia_shape=shape).install(monkeypatch)
        result = await imagegen.render_image("p", home_dir=tmp_path)
        assert Path(result.path).read_bytes() == PNG and result.suffix == ".png"
        hosts = [r.url.host for r in fake.requests]
        assert hosts == (
            ["integrate.api.nvidia.com", "cdn.example.test"] if shape == "url" else ["integrate.api.nvidia.com"]
        )

    async def test_unrecognised_nvidia_shape(self, monkeypatch, comfy_down, tmp_path) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        FakeHTTP(nvidia_shape="nonsense").install(monkeypatch)
        with pytest.raises(RuntimeError, match="shape not recognized"):
            await imagegen.render_image("p", home_dir=tmp_path)
        assert not (storage_layout(tmp_path).outputs_dir / "images").exists()

    async def test_nvidia_http_error_is_surfaced(self, monkeypatch, comfy_down, tmp_path) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        FakeHTTP(nvidia_status=500).install(monkeypatch)
        with pytest.raises(RuntimeError, match="NVIDIA image API 500"):
            await imagegen.render_image("p", home_dir=tmp_path)

    async def test_comfyui_render_uses_the_recorded_port_and_scope(self, comfy_up, fake_http, tmp_path) -> None:
        result = await imagegen.render_image("a hero image", home_dir=tmp_path, scope="conv-42", width=512, height=512)

        assert result.backend == "comfyui" and result.suffix == ".png"
        assert result.cost_usd == 0.0 and result.cost_known is True
        assert result.model == imagegen.DEFAULT_COMFYUI_CHECKPOINT
        assert Path(result.path).parent == storage_layout(tmp_path).outputs_dir / "images" / "conv-42"
        assert Path(result.path).read_bytes() == PNG
        assert result.url == f"/v1/outputs/images/conv-42/{result.filename}"

        assert [(r.method, r.url.path) for r in fake_http.requests] == [
            ("POST", "/prompt"),
            ("GET", "/history/pid-1"),
            ("GET", "/view"),
        ]
        assert {(r.url.host, r.url.port) for r in fake_http.requests} == {(RECORDED_HOST, RECORDED_PORT)}
        workflow = json.loads(fake_http.requests[0].content)["prompt"]
        assert workflow["3"]["inputs"]["text"] == "a hero image"
        assert workflow["1"]["inputs"]["ckpt_name"] == imagegen.DEFAULT_COMFYUI_CHECKPOINT
        assert workflow["2"]["inputs"] == {"width": 512, "height": 512, "batch_size": 1}

    async def test_comfyui_checkpoint_env(self, monkeypatch, comfy_up, fake_http, tmp_path) -> None:
        monkeypatch.setenv("NVH_COMFYUI_CHECKPOINT", "z-image-turbo.safetensors")
        result = await imagegen.render_image("p", home_dir=tmp_path)
        assert result.model == "z-image-turbo.safetensors"
        workflow = json.loads(fake_http.requests[0].content)["prompt"]
        assert workflow["1"]["inputs"]["ckpt_name"] == "z-image-turbo.safetensors"

    @pytest.mark.parametrize(
        ("filename", "suffix"),
        [("out_00001_.webp", ".webp"), ("out.jpeg", ".jpg"), ("out.JPG", ".jpg"), ("noext", ".png")],
    )
    async def test_comfyui_suffix_follows_the_output_filename(
        self, monkeypatch, comfy_up, filename, suffix, tmp_path
    ) -> None:
        FakeHTTP(comfy_filename=filename).install(monkeypatch)
        result = await imagegen.render_image("p", home_dir=tmp_path)
        assert result.suffix == suffix and result.filename.endswith(suffix)

    async def test_ceiling_render_carries_the_reason(self, monkeypatch, comfy_up, fake_http, tmp_path) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        result = await imagegen.render_image("p", home_dir=tmp_path, cost_ceiling_usd=0.05)
        assert result.backend == "comfyui"
        assert result.reason == imagegen.ceiling_reason(0.05, comfyui_reachable=True)
        assert all(r.url.host == RECORDED_HOST for r in fake_http.requests)  # the key was never used

    async def test_no_backend_raises_unavailable_and_writes_nothing(self, comfy_down, fake_http, tmp_path) -> None:
        with pytest.raises(imagegen.ImageGenerationUnavailable) as info:
            await imagegen.render_image("p", home_dir=tmp_path)
        assert str(info.value) == imagegen.NO_BACKEND_REASON
        assert isinstance(info.value, RuntimeError)
        assert fake_http.requests == []
        assert not (storage_layout(tmp_path).outputs_dir / "images").exists()

    async def test_ceiling_refusal_raises_the_exact_sentence(self, monkeypatch, comfy_down, fake_http, tmp_path) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        with pytest.raises(imagegen.ImageGenerationUnavailable) as info:
            await imagegen.render_image("p", home_dir=tmp_path, cost_ceiling_usd=0.05)
        assert str(info.value) == imagegen.ceiling_reason(0.05, comfyui_reachable=False)
        assert fake_http.requests == []

    @pytest.mark.parametrize("scope", ["../x", "", "a" * 65, "with space", "a/b", "dot.scope"])
    async def test_invalid_scope_is_refused_before_any_call(self, monkeypatch, comfy_up, fake_http, scope, tmp_path) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        with pytest.raises(ValueError, match="scope"):
            await imagegen.render_image("p", home_dir=tmp_path, scope=scope)
        assert fake_http.requests == [] and comfy_up.calls == []

    @pytest.mark.parametrize("kwargs", [{"width": 10}, {"height": 5000}, {"width": "abc"}, {"height": True}])
    async def test_bad_dimensions_are_refused(self, monkeypatch, comfy_up, fake_http, kwargs, tmp_path) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        with pytest.raises(ValueError):
            await imagegen.render_image("p", home_dir=tmp_path, **kwargs)
        assert fake_http.requests == []

    async def test_empty_prompt_is_refused(self, comfy_up, fake_http, tmp_path) -> None:
        with pytest.raises(ValueError, match="prompt"):
            await imagegen.render_image("   ", home_dir=tmp_path)
        assert fake_http.requests == []


# ---------------------------------------------------------------------------
# comfyui.generate_portrait shim and re-exports
# ---------------------------------------------------------------------------


class TestPortraitShim:
    async def test_generate_portrait_returns_bytes_and_suffix(self, comfy_up, fake_http, tmp_path) -> None:
        out = await comfyui.generate_portrait("a friendly robot", home_dir=tmp_path)
        assert isinstance(out, tuple) and len(out) == 2
        data, suffix = out
        assert data == PNG and suffix == ".png"
        # Portrait size and the home are honoured, and a copy is kept with the other renders.
        workflow = json.loads(fake_http.requests[0].content)["prompt"]
        assert workflow["2"]["inputs"]["width"] == 512 and workflow["2"]["inputs"]["height"] == 512
        assert comfy_up.calls[0]["home_dir"] == tmp_path
        kept = list((storage_layout(tmp_path).outputs_dir / "images" / "avatars").glob("*.png"))
        assert len(kept) == 1 and kept[0].read_bytes() == PNG

    async def test_generate_portrait_prefers_nvidia(self, monkeypatch, comfy_down, fake_http, tmp_path) -> None:
        monkeypatch.setenv("NVAPI_KEY", KEY)
        data, suffix = await comfyui.generate_portrait("p", home_dir=tmp_path)
        assert (data, suffix) == (PNG, ".png")
        assert [r.url.host for r in fake_http.requests] == ["integrate.api.nvidia.com"]

    async def test_generate_portrait_without_a_backend_keeps_the_four_bullet_hint(
        self, comfy_down, fake_http, tmp_path
    ) -> None:
        with pytest.raises(NotImplementedError) as info:
            await comfyui.generate_portrait("p", home_dir=tmp_path)
        text = str(info.value)
        assert text.startswith("No portrait generator available. Either:")
        assert text.count("•") == 4
        assert "NVAPI_KEY" in text and "avatar/upload" in text
        assert isinstance(info.value.__cause__, imagegen.ImageGenerationUnavailable)
        assert fake_http.requests == []

    def test_moved_renderers_are_reexported_under_their_old_names(self) -> None:
        assert comfyui._generate_portrait_nvidia is imagegen._generate_portrait_nvidia
        assert comfyui._generate_portrait_comfyui is imagegen._generate_portrait_comfyui
        assert comfyui._comfyui_portrait_workflow is imagegen._comfyui_portrait_workflow
        assert comfyui._DEFAULT_NVIDIA_IMAGE_ENDPOINT == imagegen.DEFAULT_NVIDIA_IMAGE_ENDPOINT
        assert comfyui._DEFAULT_COMFYUI_CHECKPOINT == imagegen.DEFAULT_COMFYUI_CHECKPOINT
        assert comfyui.NVIDIA_IMAGE_ENDPOINT_ENV == "NVH_NVIDIA_IMAGE_ENDPOINT"
        assert comfyui.NVIDIA_IMAGE_MODEL_ENV == "NVH_NVIDIA_IMAGE_MODEL"
        assert comfyui.COMFYUI_CHECKPOINT_ENV == "NVH_COMFYUI_CHECKPOINT"
        assert "_generate_portrait_nvidia" in dir(comfyui)
        with pytest.raises(AttributeError):
            comfyui.no_such_thing  # noqa: B018
        assert "not wired yet" not in (comfyui.generate_portrait.__doc__ or "")


# ---------------------------------------------------------------------------
# Paths and the route
# ---------------------------------------------------------------------------


def _write_image(root: Path, scope: str, filename: str, data: bytes = PNG) -> Path:
    target = root / scope / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


class TestPaths:
    def test_outputs_images_root(self, monkeypatch, tmp_path) -> None:
        assert imagegen.outputs_images_root(tmp_path) == storage_layout(tmp_path).outputs_dir / "images"
        assert imagegen.outputs_images_root(tmp_path) == tmp_path.resolve() / "outputs" / "images"
        monkeypatch.setenv("NVH_OUTPUTS", str(tmp_path / "elsewhere"))
        assert imagegen.outputs_images_root() == (tmp_path / "elsewhere").resolve() / "images"

    def test_resolve_output_image(self, tmp_path) -> None:
        root = imagegen.outputs_images_root(tmp_path)
        good = _write_image(root, "wizard", "20260101000000-abcdef01.png")
        _write_image(root, "other", "20260101000000-abcdef02.png")
        _write_image(root, "wizard", "notes.txt", b"hello")
        (root / "secret.png").write_bytes(PNG)

        assert imagegen.resolve_output_image("wizard", good.name, home_dir=tmp_path) == good.resolve()
        for scope, name in [
            ("wizard", ".."),
            ("wizard", "../secret.png"),
            ("..", "secret.png"),
            ("wizard", "20260101000000-abcdef02.png"),  # lives under another scope
            ("other", good.name),
            ("wizard", "missing.png"),
            ("wizard", "notes.txt"),  # not a suffix the renderer writes
            ("wizard", "a/b.png"),
            ("", good.name),
            ("wizard", ""),
            ("wi zard", good.name),
        ]:
            assert imagegen.resolve_output_image(scope, name, home_dir=tmp_path) is None, (scope, name)

    def test_media_type_for(self) -> None:
        assert imagegen.media_type_for("x.png") == "image/png"
        assert imagegen.media_type_for("x.JPG") == "image/jpeg"
        assert imagegen.media_type_for("x.webp") == "image/webp"
        assert imagegen.media_type_for("x.txt") is None

    def test_routes_package_init_is_the_shared_one_liner(self) -> None:
        assert ROUTES_INIT.read_bytes() == (
            b'"""FastAPI routers mounted by nvh.api.server (one module per surface)."""\n'
        )


@pytest.fixture()
def outputs_client(sync_db):
    """A minimal app carrying only the outputs router (no lifespan, no engine)."""
    from nvh.api.routes.outputs import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=True)


class TestOutputsRoute:
    def test_serves_a_written_file_with_media_type_and_private_cache(self, outputs_client) -> None:
        root = imagegen.outputs_images_root()  # the process home: what the route reads
        png = _write_image(root, "wizard", "20260101000000-abcdef01.png")
        jpg = _write_image(root, "conv-1", "20260101000000-abcdef02.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 16)

        resp = outputs_client.get(f"/v1/outputs/images/wizard/{png.name}")
        assert resp.status_code == 200
        assert resp.content == PNG
        assert resp.headers["content-type"].startswith("image/png")
        assert resp.headers["cache-control"] == "private, max-age=300"

        resp = outputs_client.get(f"/v1/outputs/images/conv-1/{jpg.name}")
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("image/jpeg")

    @pytest.mark.parametrize(
        "url",
        [
            "/v1/outputs/images/wizard/..%2Fsecret.png",
            "/v1/outputs/images/wizard/%2e%2e%2fsecret.png",
            "/v1/outputs/images/..%2Fimages/secret.png",
            "/v1/outputs/images/other/20260101000000-abcdef01.png",  # foreign scope
            "/v1/outputs/images/wizard/missing.png",
            "/v1/outputs/images/wizard/notes.txt",
            "/v1/outputs/images/wi%20zard/20260101000000-abcdef01.png",
        ],
    )
    def test_bad_paths_are_404_never_500(self, outputs_client, url) -> None:
        root = imagegen.outputs_images_root()
        _write_image(root, "wizard", "20260101000000-abcdef01.png")
        _write_image(root, "wizard", "notes.txt", b"hello")
        (root / "secret.png").write_bytes(PNG)
        resp = outputs_client.get(url)
        assert resp.status_code == 404, (url, resp.status_code, resp.text)

    def test_keyed_mode_requires_a_credential(self, outputs_client, monkeypatch) -> None:
        png = _write_image(imagegen.outputs_images_root(), "wizard", "20260101000000-abcdef01.png")
        url = f"/v1/outputs/images/wizard/{png.name}"
        monkeypatch.setenv("HIVE_API_KEY", "s3cret")
        assert outputs_client.get(url).status_code == 401
        assert outputs_client.get(url, headers={"X-Hive-API-Key": "wrong"}).status_code == 401
        assert outputs_client.get(url, headers={"X-Hive-API-Key": "s3cret"}).status_code == 200
        assert outputs_client.get(url, headers={"Authorization": "Bearer s3cret"}).status_code == 200
        # A missing file is still a 404, not a disclosure of whether the folder exists.
        assert outputs_client.get(
            "/v1/outputs/images/wizard/missing.png", headers={"X-Hive-API-Key": "s3cret"}
        ).status_code == 404
        # ...but without a credential the auth gate answers first.
        assert outputs_client.get("/v1/outputs/images/wizard/missing.png").status_code == 401

    def test_open_mode_serves_without_a_header(self, outputs_client, monkeypatch) -> None:
        monkeypatch.delenv("HIVE_API_KEY", raising=False)
        png = _write_image(imagegen.outputs_images_root(), "wizard", "20260101000000-abcdef01.png")
        assert outputs_client.get(f"/v1/outputs/images/wizard/{png.name}").status_code == 200
