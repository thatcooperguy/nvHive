"""Admission at public vision/benchmark boundaries; no real model or HTTP work."""

import asyncio
from contextlib import asynccontextmanager
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nvh.config.settings import AtohiConfig, CouncilConfig
from nvh.core import benchmark, vision_tools
from nvh.core.atohi import AtohiAdmission, ResourcePaused
from nvh.core.tools import Tool, ToolRegistry
from nvh.integrations.wizard import vision_bridge
from nvh.providers.base import CompletionResponse, StreamChunk, Usage
from tests.test_atohi_admission import fixture_model_lease, model_wrapper


class RawProvider:
    name = "ollama"

    def __init__(self):
        self.calls = 0

    async def stream(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.001)
        yield StreamChunk(delta="test", is_final=True)

    def estimate_tokens(self, text):
        return len(text)


@pytest.fixture(autouse=True)
def benchmark_clock(monkeypatch):
    # Deterministic benchmark measurements without altering asyncio's clock.
    clock = count(1.0, 0.1)
    monkeypatch.setattr(benchmark, "time", SimpleNamespace(monotonic=lambda: next(clock)))


@pytest.fixture
def shared_disk(monkeypatch):
    cfg = CouncilConfig(atohi=AtohiConfig(enabled=True))
    monkeypatch.setattr("nvh.config.settings.load_config", lambda: cfg)
    return cfg


@pytest.fixture
def fake_http(monkeypatch):
    response = SimpleNamespace(status_code=200, json=lambda: {"message": {"content": "synthetic"}})
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    monkeypatch.setattr("httpx.AsyncClient", lambda: client)
    return client


@pytest.mark.asyncio
async def test_direct_local_vision_refuses_unavailable_authority(shared_disk, fake_http):
    with pytest.raises(ResourcePaused):
        await vision_tools._analyze_with_ollama("synthetic", "question", "vision-test")
    fake_http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_wizard_vision_pause_never_falls_through_to_cloud(shared_disk, fake_http, monkeypatch, tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: "vision-test")
    cloud = AsyncMock(return_value="cloud response")
    monkeypatch.setattr(vision_tools, "_analyze_with_cloud", cloud)
    with pytest.raises(ResourcePaused):
        await vision_bridge.analyze_image_file(image, "question", allow_cloud=True)
    fake_http.post.assert_not_awaited()
    cloud.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_benchmark_refuses_raw_provider_in_shared_mode(shared_disk, monkeypatch):
    monkeypatch.setattr("nvh.utils.gpu.detect_gpus", lambda: [])
    clock = iter((1.0, 1.1, 1.2))
    monkeypatch.setattr(benchmark, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    provider = RawProvider()
    with pytest.raises(ResourcePaused):
        await benchmark.run_single_benchmark(provider, "test", "question")
    assert provider.calls == 0


class Broker:
    """Inert lifecycle authority, not a real grant or physical release proof."""

    def __init__(self, *, revoke_at_exit=False):
        self.revoked = asyncio.Event()
        self.requests = []
        self.exits = 0
        self.revoke_at_exit = revoke_at_exit
        self.transports = {}

    async def wait_revoked(self):
        await self.revoked.wait()

    @asynccontextmanager
    async def admit(self, request):
        self.requests.append(request)
        try:
            yield fixture_model_lease(self, request)
        finally:
            self.exits += 1
            if self.revoke_at_exit:
                self.revoked.set()
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_disabled_local_vision_retains_response(shared_disk, fake_http):
    policy = AtohiAdmission(AtohiConfig(enabled=False))
    result = await vision_tools._analyze_with_ollama("data", "question", "test", admission=policy)
    assert result == "synthetic"
    fake_http.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_vision_policy_cannot_be_downgraded_by_disk(monkeypatch, fake_http):
    monkeypatch.setattr("nvh.config.settings.load_config", lambda: CouncilConfig())
    policy = AtohiAdmission(AtohiConfig(enabled=True))
    with pytest.raises(ResourcePaused):
        await vision_tools._analyze_with_ollama("data", "question", "test", admission=policy)
    with pytest.raises(ResourcePaused):
        await vision_tools._analyze_with_cloud("data", "image/png", "question", admission=policy)
    fake_http.post.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_vision_request_drains_owned_cleanup_on_revocation_or_cancel(fake_http, cancel):
    broker = Broker()
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def request(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    broker.transports["ollama"] = SimpleNamespace(complete=request)
    task = asyncio.create_task(vision_tools._analyze_with_ollama("data", "question", "test", admission=policy))
    await asyncio.wait_for(started.wait(), 2)
    if cancel:
        task.cancel()
    else:
        broker.revoked.set()
    with pytest.raises(asyncio.CancelledError if cancel else ResourcePaused):
        await asyncio.wait_for(task, 2)
    assert cleaned.is_set()
    assert broker.exits == 1
    fake_http.post.assert_not_awaited()
    assert [request.operation for request in broker.requests] == ["complete"]


@pytest.mark.asyncio
async def test_owned_cleanup_revocation_prevents_vision_success(fake_http):
    broker = Broker()
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)

    async def request(*args, **kwargs):
        try:
            return CompletionResponse(content="test", model="test", provider="ollama", usage=Usage())
        finally:
            broker.revoked.set()
            await asyncio.sleep(0)

    broker.transports["ollama"] = SimpleNamespace(complete=request)
    with pytest.raises(ResourcePaused):
        await vision_tools._analyze_with_ollama("data", "question", "test", admission=policy)
    assert broker.exits == 1
    fake_http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_registry_scopes_only_builtin_vision_without_mutating_source(monkeypatch, fake_http, tmp_path):
    monkeypatch.setattr(vision_tools, "_ensure_display", lambda: None)
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: "test")
    image = tmp_path / "image.png"
    image.write_bytes(b"synthetic")
    raw = ToolRegistry(include_system=False)
    vision_tools.register_vision_tools(raw)
    custom = Tool("custom", "custom", handler=AsyncMock(return_value="custom"))
    raw.register(custom)
    original = raw.get("analyze_image")
    denied = raw.with_admission(AtohiAdmission(AtohiConfig(enabled=True)))
    disabled = raw.with_admission(AtohiAdmission(AtohiConfig(enabled=False)))
    assert raw.get("analyze_image") is original
    assert denied.get("custom") is disabled.get("custom") is custom
    assert denied.get("capture_screenshot") is raw.get("capture_screenshot")
    for reg in (denied, denied.with_admission(AtohiAdmission(AtohiConfig(enabled=True)))):
        with pytest.raises(ResourcePaused):
            await reg.execute("analyze_image", {"image_path": str(image)})
    assert "synthetic" in (await disabled.execute("analyze_image", {"image_path": str(image)})).output
    with pytest.raises(ResourcePaused):
        await denied.execute("read_text_from_image", {"image_path": str(image)})
    fake_http.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_agent_loop_rebinds_supplied_registry_to_own_engine(monkeypatch, fake_http, tmp_path):
    from nvh.core.agent_loop import run_agent_loop
    from nvh.core.engine import Engine
    from nvh.providers.base import CompletionResponse, Usage
    from nvh.providers.registry import ProviderRegistry

    monkeypatch.setattr(vision_tools, "_ensure_display", lambda: None)
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: "test")
    image = tmp_path / "image.png"
    image.write_bytes(b"synthetic")
    raw = ToolRegistry(include_system=False)
    vision_tools.register_vision_tools(raw, admission=AtohiAdmission(AtohiConfig(enabled=False)))
    original = raw.get("analyze_image")
    engine = Engine(config=CouncilConfig(atohi=AtohiConfig(enabled=True)), registry=ProviderRegistry())
    engine.query = AsyncMock(return_value=CompletionResponse(
        content="", model="test", provider="test", usage=Usage(), tool_calls=[{
            "id": "vision", "type": "function", "function": {
                "name": "analyze_image", "arguments": {"image_path": str(image)},
            },
        }],
    ))
    with pytest.raises(ResourcePaused):
        await run_agent_loop("look", engine, raw, max_iterations=1)
    assert raw.get("analyze_image") is original
    fake_http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_benchmark_preserves_wrapped_authority_and_counts_one_lease(monkeypatch):
    monkeypatch.setattr("nvh.utils.gpu.detect_gpus", lambda: [])
    monkeypatch.setattr("nvh.config.settings.load_config", lambda: CouncilConfig())
    broker = Broker()
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    raw = RawProvider()
    wrapped = model_wrapper(raw, policy, "ollama")
    result = await benchmark.run_single_benchmark(wrapped, "test", "question")
    assert result.output_tokens == 1 and raw.calls == 1
    assert len(broker.requests) == broker.exits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_policy", [False, True])
async def test_benchmark_retains_managed_alias_lease(monkeypatch, replace_policy):
    from nvh.config.settings import ProviderConfig
    from nvh.providers.registry import ProviderRegistry

    monkeypatch.setattr("nvh.utils.gpu.detect_gpus", lambda: [])
    broker = Broker()
    cfg = CouncilConfig(atohi=AtohiConfig(enabled=True), providers={"spark": ProviderConfig(type="ollama")})
    raw = RawProvider()
    registry = ProviderRegistry(atohi_broker=broker)
    registry.register("spark", raw)
    scoped = registry.scoped(cfg)
    wrapped = scoped.get("spark")
    other = Broker()
    broker.transports["spark"] = raw
    other.transports["spark"] = raw
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=other) if replace_policy else scoped.admission
    result = await benchmark.run_single_benchmark(wrapped, "test", "question", admission=policy)
    assert result.output_tokens == raw.calls == 1
    selected = other if replace_policy else broker
    assert len(selected.requests) == selected.exits == 1
    assert selected.requests[0].provider == "spark"
    assert len((broker if replace_policy else other).requests) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name,env_name", [
    ("openai", "OPENAI_API_KEY"), ("google", "GOOGLE_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"),
])
async def test_managed_cloud_vision_uses_canonical_provider_policy(monkeypatch, provider_name, env_name):
    import sys

    for name in ("OPENAI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(env_name, "synthetic-test-value")
    completion = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="test"))]))
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    broker = Broker(revoke_at_exit=True)
    owned = AsyncMock(return_value=CompletionResponse(content="test", model="test", provider=provider_name, usage=Usage()))
    broker.transports[provider_name] = SimpleNamespace(complete=owned)
    policy = AtohiAdmission(AtohiConfig(enabled=True, managed_providers=[provider_name]), broker=broker)
    with pytest.raises(ResourcePaused):
        await vision_tools._analyze_with_cloud("data", "image/png", "question", admission=policy)
    assert len(broker.requests) == broker.exits == 1
    assert broker.requests[0].provider == provider_name
    owned.assert_awaited_once()
    completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_benchmark_broker_exit_revocation_never_returns_result(monkeypatch):
    monkeypatch.setattr("nvh.utils.gpu.detect_gpus", lambda: [])
    broker = Broker(revoke_at_exit=True)
    raw = RawProvider()
    policy = AtohiAdmission(AtohiConfig(enabled=True), broker=broker)
    broker.transports["ollama"] = raw
    with pytest.raises(ResourcePaused):
        await benchmark.run_single_benchmark(raw, "test", "question", admission=policy)
    assert raw.calls == broker.exits == 1


@pytest.mark.asyncio
async def test_disabled_benchmark_suite_keeps_existing_results(shared_disk, monkeypatch):
    monkeypatch.setattr("nvh.utils.gpu.detect_gpus", lambda: [])
    raw = RawProvider()
    suite = await benchmark.run_benchmark_suite(
        raw, "test", [{"prompt": "question", "max_tokens": 5}],
        admission=AtohiAdmission(AtohiConfig(enabled=False)),
    )
    assert len(suite.results) == raw.calls == 1
    assert suite.gpu_name == "CPU"


def test_setup_writer_preserves_existing_shared_policy(monkeypatch, tmp_path):
    import yaml

    from nvh.cli.setup import _write_config

    config = tmp_path / "config.yaml"
    config.write_text("atohi:\n  enabled: true\n  managed_providers: [spark]\n", encoding="utf-8")
    monkeypatch.setattr("nvh.config.settings.DEFAULT_CONFIG_PATH", config)
    monkeypatch.setattr("nvh.config.settings.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("nvh.cli.setup._detect_tier_budget", lambda: None)
    _write_config({}, ollama_enabled=True)
    loaded = CouncilConfig(**yaml.safe_load(config.read_text(encoding="utf-8")))
    assert loaded.atohi.enabled is True
    assert loaded.atohi.managed_providers == ["spark"]


@pytest.mark.parametrize("original", [
    b'atohi:\n  enabled: "yes"\n', b"defaults:\n  temperature: invalid\n", b"false\n",
])
def test_setup_writer_refuses_invalid_policy_without_overwriting(monkeypatch, tmp_path, original):
    from nvh.cli.setup import _write_config

    config = tmp_path / "config.yaml"
    config.write_bytes(original)
    monkeypatch.setattr("nvh.config.settings.DEFAULT_CONFIG_PATH", config)
    monkeypatch.setattr("nvh.config.settings.get_config_dir", lambda: tmp_path)
    monkeypatch.setattr("nvh.cli.setup._detect_tier_budget", lambda: None)
    with pytest.raises(ValueError):
        _write_config({}, ollama_enabled=True)
    assert config.read_bytes() == original


def test_setup_writer_preserves_policy_arriving_during_home_migration(monkeypatch, tmp_path):
    import yaml

    from nvh.cli.setup import _write_config

    config = tmp_path / "config.yaml"
    assert not config.exists()

    def migrate_then_return_directory():
        config.write_text("atohi:\n  enabled: true\n  managed_providers: [spark]\n", encoding="utf-8")
        return tmp_path

    monkeypatch.setattr("nvh.config.settings.DEFAULT_CONFIG_PATH", config)
    monkeypatch.setattr("nvh.config.settings.get_config_dir", migrate_then_return_directory)
    monkeypatch.setattr("nvh.cli.setup._detect_tier_budget", lambda: None)
    _write_config({}, ollama_enabled=True)
    loaded = CouncilConfig(**yaml.safe_load(config.read_text(encoding="utf-8")))
    assert loaded.atohi.enabled is True
    assert loaded.atohi.managed_providers == ["spark"]


def test_guided_shared_setup_skips_screenshot_and_model_but_keeps_manual_path(shared_disk, monkeypatch, tmp_path):
    import io
    from unittest.mock import Mock

    from rich.console import Console

    from nvh.cli import setup

    monkeypatch.setenv("DISPLAY", ":synthetic")
    monkeypatch.setattr(setup, "load_env_keys", lambda: None)
    monkeypatch.setattr(setup, "_detect_gpu_info", lambda: ([], 24.0, "test", "test"))
    monkeypatch.setattr(setup, "_detect_tier_budget", lambda: None)
    monkeypatch.setattr(setup, "_ollama_running", lambda: (True, ["vision-test"]))
    monkeypatch.setattr(setup, "_get_recommended_models", lambda *a, **k: ["vision-test"])
    monkeypatch.setattr(setup, "CORE_PROVIDERS", [("test", "Test", "TEST_UNUSED_KEY", "https://example.invalid")])
    monkeypatch.setattr(setup, "_check_provider_key", lambda *a: None)
    monkeypatch.setattr(setup, "_get_clipboard", lambda: "")
    opened = Mock(return_value=True)
    clipboard = Mock(return_value="")
    monkeypatch.setattr(setup, "_open_in_browser", opened)
    monkeypatch.setattr(setup, "_watch_clipboard_for_key", clipboard)
    monkeypatch.setattr(setup, "_write_config", lambda *a, **k: tmp_path / "config.yaml")
    monkeypatch.setattr(setup, "_check_nvh_on_path", lambda: None)
    monkeypatch.setattr(vision_tools, "_detect_ollama_vision_model", lambda: "vision-test")
    display = Mock(side_effect=AssertionError("must not capture the desktop"))
    model = AsyncMock(side_effect=AssertionError("must not invoke a model"))
    monkeypatch.setattr(vision_tools, "_ensure_display", display)
    monkeypatch.setattr(vision_tools, "_analyze_with_ollama", model)
    console = Console(file=io.StringIO(), width=160, force_terminal=False)
    console.input = lambda *a, **k: ""
    setup.guided_setup(console)
    display.assert_not_called()
    model.assert_not_awaited()
    opened.assert_called_once()
    clipboard.assert_called_once()
    assert "Vision assist paused" in console.file.getvalue()
    assert "desktop agent ready" not in console.file.getvalue()


def test_benchmark_cli_reports_pause_before_metadata_or_model(shared_disk, monkeypatch):
    from unittest.mock import Mock

    import typer

    from nvh.cli.main import bench

    metadata = Mock(side_effect=AssertionError("must not query a provider"))
    monkeypatch.setattr("httpx.get", metadata)
    with pytest.raises(typer.Exit) as caught:
        bench(model=None, quick_mode=True, all_models=False, quality=False, speed_only=True)
    assert caught.value.exit_code != 0
    metadata.assert_not_called()
