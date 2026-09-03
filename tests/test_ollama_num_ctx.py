"""OllamaProvider sends the VRAM tier's ``num_ctx`` (ROADMAP 0.43).

Both native option dicts (``_direct_complete`` and ``_direct_stream``) carry
``num_ctx`` from ``local_models.num_ctx_for(tier_budget(...))``; the figure is
resolved once per provider instance, ``NVH_OLLAMA_NUM_CTX`` (or the ``num_ctx``
kwarg) overrides it, ``0`` disables it, a failed detection sends no option at
all, and the value never exceeds the model's own context when ``/api/show``
exposes one. The model-preference ladders in the same module are also checked
against the table here.

Two cases send *no* ``num_ctx`` rather than a guess: a box where no GPU is
seen at all (``TierBudget.total_gpus == 0`` -- the table's CPU-only 2048 sits
below Ollama's own default, so pushing it would shrink a daemon that may have
a GPU the client cannot see) and a daemon that is not on loopback (the
client's VRAM says nothing about the remote box). ``NVH_OLLAMA_NUM_CTX`` still
applies to both.

The detected tier figure is sent only to a *table pick*
(``local_models.pick_for_tag``): the tier sized its context for those models.
A custom Modelfile, an imported GGUF or a family member the auto-pick accepted
(``qwen3:14b-q8_0``) gets no ``num_ctx`` at all -- its own Modelfile figure or
Ollama's default applies -- and detection is not even run for it. An explicit
``NVH_OLLAMA_NUM_CTX`` or ``num_ctx`` kwarg applies to every model. It used to
be attached to every native request, overriding any Modelfile ``num_ctx``.

Detection is stubbed: this box has a real GPU and the tests must not depend on
it.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from nvh.core import local_models
from nvh.providers import ollama_provider as op
from nvh.providers.base import Message

TIER_GB = 24.0
TIER_CTX = local_models.num_ctx_for(TIER_GB)
MODEL = f"ollama/{local_models.pick(8.0, 'chat').tag}"

RETIRED_TAGS = (
    "nemotron-omni",
    "nemotron-3-nano-omni",
    "nemotron",
    "nemotron:70b",
    "nemotron-3-super",
    "llama3.3:70b",
    "qwen2.5-coder:32b",
    "qwen2.5-coder:7b",
    "deepseek-r1:8b",
    "llama3.1:8b",
    "llama3.1",
    "llava:7b",
    "llava",
    "bakllava",
    "minicpm-v",
    "nemotron-mini",
)


# --- fake daemon -------------------------------------------------------------


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _StreamResponse:
    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        yield json.dumps({"message": {"content": "po"}, "done": False})
        yield json.dumps(
            {"message": {"content": "ng"}, "done": True, "prompt_eval_count": 1, "eval_count": 1}
        )


class _FakeOllama:
    """Stands in for ``httpx.AsyncClient``: records every request, answers /api/show and /api/chat."""

    def __init__(self, calls: list[tuple[str, dict]], *, show: dict | None):
        self.calls = calls
        self.show = show

    async def __aenter__(self) -> _FakeOllama:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> _Response:
        self.calls.append((url, json or {}))
        if url.endswith("/api/show"):
            if self.show is None:
                raise RuntimeError("no /api/show on this daemon")
            return _Response(self.show)
        return _Response({"message": {"content": "pong"}, "done": True})

    def stream(self, method: str, url: str, json: dict | None = None):
        self.calls.append((url, json or {}))

        @asynccontextmanager
        async def _cm():
            yield _StreamResponse()

        return _cm()


@pytest.fixture
def daemon(monkeypatch):
    """The fake daemon behind ``op.httpx.AsyncClient`` with the env override cleared; detection is real."""
    monkeypatch.delenv(op.NUM_CTX_ENV, raising=False)
    calls: list[tuple[str, dict]] = []
    state: dict = {"show": None}
    monkeypatch.setattr(
        op.httpx, "AsyncClient", lambda *a, **kw: _FakeOllama(calls, show=state["show"])
    )
    return SimpleNamespace(calls=calls, state=state)


@pytest.fixture
def ollama(daemon, monkeypatch):
    """``daemon`` plus a counting stub in place of ``_detect_num_ctx``."""
    daemon.state.update({"detect_calls": 0, "detected": TIER_CTX})

    def _detect() -> int | None:
        daemon.state["detect_calls"] += 1
        return daemon.state["detected"]

    monkeypatch.setattr(op, "_detect_num_ctx", _detect)
    return daemon


@pytest.fixture
def gpu_rows(monkeypatch):
    """Stub ``nvh.utils.gpu`` detection with the given GPU rows (real ``_detect_num_ctx`` runs on them)."""
    import nvh.utils.gpu as gpu

    mem = SimpleNamespace(effective_for_llm_gb=14.8, available_ram_gb=21.2)
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: mem)

    def _install(rows: list) -> None:
        monkeypatch.setattr(gpu, "detect_gpus", lambda: list(rows))

    return _install


GPU_24GB = SimpleNamespace(vram_mb=24 * 1024, vram_gb=24.0, unified_memory=False, compute_capability=(8, 6))
GPU_UNREADABLE = SimpleNamespace(vram_mb=0, vram_gb=0.0, unified_memory=False, compute_capability=(8, 6))
REMOTE_URL = "http://spark.local:11434"


def _chat_options(calls: list[tuple[str, dict]]) -> list[dict]:
    return [body["options"] for url, body in calls if url.endswith("/api/chat")]


def _show_calls(calls: list[tuple[str, dict]]) -> list[dict]:
    return [body for url, body in calls if url.endswith("/api/show")]


async def _complete(provider: op.OllamaProvider, model: str = MODEL):
    return await provider.complete(
        [Message(role="user", content="ping")], model=model, temperature=0.2, max_tokens=64
    )


async def _stream(provider: op.OllamaProvider, model: str = MODEL):
    return [
        chunk
        async for chunk in provider.stream(
            [Message(role="user", content="ping")], model=model, temperature=0.2, max_tokens=64
        )
    ]


# --- both option dicts carry num_ctx -----------------------------------------


async def test_complete_sends_the_tier_num_ctx(ollama):
    provider = op.OllamaProvider()

    resp = await _complete(provider)

    assert resp.content == "pong"
    (options,) = _chat_options(ollama.calls)
    assert options == {"temperature": 0.2, "num_predict": 64, "num_ctx": TIER_CTX}


async def test_stream_sends_the_tier_num_ctx(ollama):
    provider = op.OllamaProvider()

    chunks = await _stream(provider)

    assert chunks[-1].is_final and chunks[-1].accumulated_content == "pong"
    (options,) = _chat_options(ollama.calls)
    assert options == {"temperature": 0.2, "num_predict": 64, "num_ctx": TIER_CTX}


# --- resolved once per instance ----------------------------------------------


def test_construction_does_not_detect(ollama):
    op.OllamaProvider()
    assert ollama.state["detect_calls"] == 0


async def test_num_ctx_is_resolved_once_per_instance(ollama):
    provider = op.OllamaProvider()

    await _complete(provider)
    await _stream(provider)
    await _complete(provider, model="ollama/gemma3:4b")

    assert ollama.state["detect_calls"] == 1
    assert [o["num_ctx"] for o in _chat_options(ollama.calls)] == [TIER_CTX] * 3
    # a second instance resolves for itself, once
    assert op.OllamaProvider()._resolved_num_ctx() == TIER_CTX
    assert ollama.state["detect_calls"] == 2


# --- overrides ---------------------------------------------------------------


async def test_env_override_replaces_detection(ollama, monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "8192")
    provider = op.OllamaProvider()

    await _complete(provider)

    assert _chat_options(ollama.calls)[0]["num_ctx"] == 8192
    assert ollama.state["detect_calls"] == 0


async def test_constructor_kwarg_beats_env(ollama, monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "8192")
    provider = op.OllamaProvider(num_ctx=4096)

    await _stream(provider)

    assert _chat_options(ollama.calls)[0]["num_ctx"] == 4096
    assert ollama.state["detect_calls"] == 0


async def test_env_zero_disables_num_ctx(ollama, monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "0")
    provider = op.OllamaProvider()

    await _complete(provider)
    await _stream(provider)

    for options in _chat_options(ollama.calls):
        assert "num_ctx" not in options
    assert ollama.state["detect_calls"] == 0
    assert not _show_calls(ollama.calls)


async def test_env_garbage_falls_back_to_detection(ollama, monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "plenty")
    provider = op.OllamaProvider()

    await _complete(provider)

    assert _chat_options(ollama.calls)[0]["num_ctx"] == TIER_CTX
    assert ollama.state["detect_calls"] == 1


# --- only a table pick gets the tier figure ----------------------------------

CUSTOM = "ollama/my-assistant:latest"  # a Modelfile of the user's own: not in the table
MEMBER = "ollama/qwen3:14b-q8_0"  # a family member the auto-pick accepts: not a table tag either


async def test_a_custom_model_gets_no_tier_num_ctx(ollama):
    provider = op.OllamaProvider()

    await _complete(provider, model=CUSTOM)
    await _stream(provider, model=CUSTOM)

    options = _chat_options(ollama.calls)
    assert len(options) == 2
    for opt in options:
        assert "num_ctx" not in opt
        assert opt["temperature"] == 0.2 and opt["num_predict"] == 64
    assert not _show_calls(ollama.calls)  # nothing to cap, so the model is never asked
    assert ollama.state["detect_calls"] == 0  # ...and the tier is not even detected for it


async def test_a_family_member_is_not_a_table_pick(ollama):
    provider = op.OllamaProvider()

    await _complete(provider, model=MEMBER)

    assert "num_ctx" not in _chat_options(ollama.calls)[0]
    assert ollama.state["detect_calls"] == 0


async def test_a_table_pick_next_to_a_custom_model_still_gets_the_tier_num_ctx(ollama):
    provider = op.OllamaProvider()

    await _complete(provider, model=MODEL)
    await _complete(provider, model=CUSTOM)
    await _complete(provider, model="ollama/moondream")  # an untagged table pick is a pick

    assert [o.get("num_ctx") for o in _chat_options(ollama.calls)] == [TIER_CTX, None, TIER_CTX]
    assert ollama.state["detect_calls"] == 1


async def test_env_override_applies_to_a_custom_model(ollama, monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "8192")
    provider = op.OllamaProvider()

    await _complete(provider, model=CUSTOM)
    await _stream(provider, model=MEMBER)

    assert [o["num_ctx"] for o in _chat_options(ollama.calls)] == [8192, 8192]
    assert ollama.state["detect_calls"] == 0


async def test_env_override_on_a_custom_model_is_still_capped_by_the_model(ollama, monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "65536")
    ollama.state["show"] = {"model_info": {"llama.context_length": 4096}}
    provider = op.OllamaProvider()

    await _complete(provider, model=CUSTOM)

    assert _chat_options(ollama.calls)[0]["num_ctx"] == 4096


async def test_constructor_kwarg_applies_to_a_custom_model(ollama):
    provider = op.OllamaProvider(num_ctx=4096)

    await _stream(provider, model=CUSTOM)

    assert _chat_options(ollama.calls)[0]["num_ctx"] == 4096
    assert ollama.state["detect_calls"] == 0


async def test_env_zero_disables_num_ctx_for_a_custom_model_too(ollama, monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "0")
    provider = op.OllamaProvider()

    await _complete(provider, model=CUSTOM)

    assert "num_ctx" not in _chat_options(ollama.calls)[0]
    assert ollama.state["detect_calls"] == 0


def test_num_ctx_for_is_the_table_pick_gate(ollama):
    provider = op.OllamaProvider()

    assert provider._num_ctx_for(MODEL.removeprefix("ollama/")) == TIER_CTX
    assert provider._num_ctx_for("moondream") == TIER_CTX
    assert provider._num_ctx_for("my-assistant:latest") is None
    assert provider._num_ctx_for("qwen3:14b-q8_0") is None
    assert provider._resolved_num_ctx() == TIER_CTX
    assert ollama.state["detect_calls"] == 1


# --- detection failure -> no option at all -----------------------------------


async def test_detection_failure_sends_no_num_ctx(ollama):
    ollama.state["detected"] = None
    provider = op.OllamaProvider()

    await _complete(provider)
    await _stream(provider)

    options = _chat_options(ollama.calls)
    assert len(options) == 2
    for opt in options:
        assert "num_ctx" not in opt
        assert opt["temperature"] == 0.2 and opt["num_predict"] == 64
    assert ollama.state["detect_calls"] == 1  # the failure is cached too
    assert not _show_calls(ollama.calls)  # nothing to cap, so the model is never asked


def test_detect_num_ctx_reads_the_tier_table(gpu_rows):
    import nvh.utils.gpu as gpu

    gpu_rows([GPU_24GB])

    budget = local_models.tier_budget([GPU_24GB], gpu.detect_system_memory())
    assert op._detect_num_ctx() == local_models.num_ctx_for(budget) == TIER_CTX


def test_detect_num_ctx_is_none_when_no_gpu_is_seen(gpu_rows):
    """An empty GPU list is not a 2048 tier: the daemon's own default must apply."""
    gpu_rows([])

    budget = local_models.tier_budget([], None)
    assert budget.total_gpus == 0
    assert local_models.num_ctx_for(budget) == local_models.CPU_ONLY_NUM_CTX  # the table still says 2048
    assert op._detect_num_ctx() is None  # ...and the provider declines to send it


def test_detect_num_ctx_keeps_the_cpu_tier_for_an_unreadable_gpu(gpu_rows):
    """A GPU that was listed but could not be sized is still a GPU (total_gpus == 1, sized_gpus == 0)."""
    gpu_rows([GPU_UNREADABLE])

    budget = local_models.tier_budget([GPU_UNREADABLE], None)
    assert budget.total_gpus == 1 and budget.gpu_memory_unreadable
    assert op._detect_num_ctx() == local_models.LOCAL_MODEL_TIERS[0].num_ctx


def test_detect_num_ctx_never_raises(monkeypatch):
    import nvh.utils.gpu as gpu

    def boom():
        raise RuntimeError("NVML exploded")

    monkeypatch.setattr(gpu, "detect_gpus", boom)
    assert op._detect_num_ctx() is None


# --- no visible GPU -> no num_ctx (env still wins) ----------------------------


async def test_no_gpu_sends_no_num_ctx(daemon, gpu_rows):
    gpu_rows([])
    provider = op.OllamaProvider()

    await _complete(provider)
    await _stream(provider)

    options = _chat_options(daemon.calls)
    assert len(options) == 2
    for opt in options:
        assert "num_ctx" not in opt
        assert opt["temperature"] == 0.2 and opt["num_predict"] == 64
    assert not _show_calls(daemon.calls)  # nothing to cap, so the model is never asked


async def test_no_gpu_env_override_still_sizes_num_ctx(daemon, gpu_rows, monkeypatch):
    gpu_rows([])
    monkeypatch.setenv(op.NUM_CTX_ENV, "8192")
    provider = op.OllamaProvider()

    await _complete(provider)

    assert _chat_options(daemon.calls)[0]["num_ctx"] == 8192


async def test_no_gpu_env_override_is_still_capped_by_the_model(daemon, gpu_rows, monkeypatch):
    gpu_rows([])
    monkeypatch.setenv(op.NUM_CTX_ENV, "65536")
    daemon.state["show"] = {"model_info": {"qwen3.context_length": 4096}}
    provider = op.OllamaProvider()

    await _stream(provider)

    assert _chat_options(daemon.calls)[0]["num_ctx"] == 4096


async def test_no_gpu_constructor_kwarg_still_sizes_num_ctx(daemon, gpu_rows):
    gpu_rows([])
    provider = op.OllamaProvider(num_ctx=4096)

    await _complete(provider)

    assert _chat_options(daemon.calls)[0]["num_ctx"] == 4096


async def test_a_real_gpu_still_sends_the_tier_num_ctx(daemon, gpu_rows):
    """The no-GPU guard does not disturb the normal path."""
    gpu_rows([GPU_24GB])
    provider = op.OllamaProvider()

    await _complete(provider)

    assert _chat_options(daemon.calls)[0]["num_ctx"] == TIER_CTX


# --- remote daemon -> the client's GPUs are not consulted --------------------


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:11434", "http://localhost:11434", "http://LOCALHOST:11434", "http://[::1]:11434"],
)
def test_loopback_spellings_are_local(base_url):
    assert op._daemon_is_local(base_url)


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:11434", "http://0.0.0.0:11434", "0.0.0.0:11434", "http://[::1]:11434", "[::1]:11434", "::1"],
)
def test_normalised_loopback_aliases_are_local(base_url):
    """What the provider actually sees: ``ollama_base_url`` spells every alias as 127.0.0.1 and
    keeps an IPv6 loopback literal bracketed (it used to come out as ``http://::1:11434`` and
    read as remote)."""
    assert op._daemon_is_local(op.ollama_base_url(base_url))


async def test_ipv6_loopback_daemon_still_detects(ollama):
    provider = op.OllamaProvider(base_url="http://[::1]:11434")

    await _complete(provider)

    assert provider._base_url == "http://[::1]:11434"
    assert ollama.state["detect_calls"] == 1
    assert _chat_options(ollama.calls)[0]["num_ctx"] == TIER_CTX


@pytest.mark.parametrize(
    "base_url",
    [REMOTE_URL, "http://10.0.0.5:11434", "http://host.docker.internal:11434", "https://ollama.example.com", "", "::::"],
)
def test_other_hosts_are_remote(base_url):
    assert not op._daemon_is_local(base_url)


async def test_remote_daemon_skips_client_gpu_detection(ollama):
    provider = op.OllamaProvider(base_url=REMOTE_URL)

    await _complete(provider)
    await _stream(provider)

    assert ollama.state["detect_calls"] == 0
    for opt in _chat_options(ollama.calls):
        assert "num_ctx" not in opt
    assert not _show_calls(ollama.calls)


async def test_remote_daemon_via_env_url_skips_detection(ollama, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", REMOTE_URL)
    provider = op.OllamaProvider()

    await _complete(provider)

    assert ollama.state["detect_calls"] == 0
    assert "num_ctx" not in _chat_options(ollama.calls)[0]


async def test_remote_daemon_env_override_applies(ollama, monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "8192")
    provider = op.OllamaProvider(base_url=REMOTE_URL)

    await _complete(provider)

    assert ollama.state["detect_calls"] == 0
    assert _chat_options(ollama.calls)[0]["num_ctx"] == 8192


async def test_remote_daemon_env_override_is_still_capped_by_the_model(ollama, monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "65536")
    ollama.state["show"] = {"model_info": {"llama.context_length": 8192}}
    provider = op.OllamaProvider(base_url=REMOTE_URL)

    await _stream(provider)

    assert _chat_options(ollama.calls)[0]["num_ctx"] == 8192


async def test_remote_daemon_constructor_kwarg_applies(ollama):
    provider = op.OllamaProvider(base_url=REMOTE_URL, num_ctx=4096)

    await _complete(provider)

    assert ollama.state["detect_calls"] == 0
    assert _chat_options(ollama.calls)[0]["num_ctx"] == 4096


async def test_loopback_daemon_still_detects(ollama):
    provider = op.OllamaProvider(base_url="http://localhost:11434")

    await _complete(provider)

    assert ollama.state["detect_calls"] == 1
    assert _chat_options(ollama.calls)[0]["num_ctx"] == TIER_CTX


# --- never larger than the model's own context -------------------------------


async def test_num_ctx_never_exceeds_the_models_context(ollama):
    ollama.state["show"] = {
        "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 4096}
    }
    provider = op.OllamaProvider()

    await _complete(provider)
    await _stream(provider)

    assert [o["num_ctx"] for o in _chat_options(ollama.calls)] == [4096, 4096]
    assert len(_show_calls(ollama.calls)) == 1  # the model's limit is cached per tag


async def test_roomier_model_keeps_the_tier_num_ctx(ollama):
    ollama.state["show"] = {"model_info": {"llama.context_length": 131072}}
    provider = op.OllamaProvider()

    await _complete(provider)

    assert _chat_options(ollama.calls)[0]["num_ctx"] == TIER_CTX


async def test_show_failure_keeps_the_tier_num_ctx(ollama):
    provider = op.OllamaProvider()  # state["show"] is None -> /api/show raises

    await _complete(provider)
    await _complete(provider)

    assert [o["num_ctx"] for o in _chat_options(ollama.calls)] == [TIER_CTX, TIER_CTX]
    assert len(_show_calls(ollama.calls)) == 1  # the failure is cached as well


async def test_env_override_is_still_capped_by_the_model(ollama, monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "65536")
    ollama.state["show"] = {"model_info": {"gemma3.context_length": 8192}}
    provider = op.OllamaProvider()

    await _complete(provider)

    assert _chat_options(ollama.calls)[0]["num_ctx"] == 8192


# --- the preference ladders come from the table ------------------------------


def test_fallback_preferences_come_from_the_table():
    tags = set(local_models.all_tags())
    sizes = local_models.size_table()
    picks = {p.tag: p for p in local_models.all_picks()}

    assert set(op._FALLBACK_MODEL_PREFERENCE) <= tags
    assert set(op._VISION_MODEL_PREFERENCE) <= tags
    assert "nomic-embed-text" not in op._FALLBACK_MODEL_PREFERENCE
    for retired in RETIRED_TAGS:
        assert retired not in op._FALLBACK_MODEL_PREFERENCE
        assert retired not in op._VISION_MODEL_PREFERENCE

    text = {
        tier.picks[u].tag for tier in local_models.LOCAL_MODEL_TIERS for u in ("chat", "code", "reasoning")
    }
    vision_only = {p.tag for p in picks.values() if p.vision} - text
    assert set(op._FALLBACK_MODEL_PREFERENCE) == text | vision_only
    head = op._FALLBACK_MODEL_PREFERENCE[: len(text)]
    tail = op._FALLBACK_MODEL_PREFERENCE[len(text):]
    assert set(head) == text and set(tail) == vision_only
    for part in (head, tail):
        assert [sizes[t] for t in part] == sorted((sizes[t] for t in part), reverse=True)

    vision = [p.tag for p in picks.values() if p.vision]
    assert list(op._VISION_MODEL_PREFERENCE) == sorted(vision, key=lambda t: (-sizes[t], t))


async def test_list_models_vision_flag_comes_from_the_table_and_the_legacy_names(monkeypatch):
    """Table picks by tag, vision-throughout names by family, and the llava-era installs the
    auto-pick still routes image questions to (HEAD flagged them True; the table-only flag lost them)."""
    names = [
        "nemotron3:33b",
        "gemma3:1b",
        "gemma3:4b",
        "qwen3-vl:8b",
        "llama3.2-vision:latest",
        "llava:7b",
        "minicpm-v:latest",
        "bakllava",
        "qwen3:8b",
        "mistral:7b",
    ]

    class _Tags:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, url: str, timeout: float | None = None) -> _Response:
            return _Response({"models": [{"name": n} for n in names]})

    monkeypatch.setattr(op.httpx, "AsyncClient", lambda *a, **kw: _Tags())

    models = await op.OllamaProvider().list_models()

    assert {m.display_name: m.supports_vision for m in models} == {
        "nemotron3:33b": True,
        "gemma3:1b": False,
        "gemma3:4b": True,
        "qwen3-vl:8b": True,
        "llama3.2-vision:latest": True,
        "llava:7b": True,
        "minicpm-v:latest": True,
        "bakllava": True,
        "qwen3:8b": False,
        "mistral:7b": False,
    }
