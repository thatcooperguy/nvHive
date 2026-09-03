"""``OllamaProvider``'s ``__auto__`` walk picks the strongest *installed* model without over-matching by family.

The ladders (``_FALLBACK_LADDER`` / ``_VISION_LADDER``) are table picks
strongest first, and one registry family spans several rungs (``gpt-oss:120b``
and ``gpt-oss:20b``; ``gemma3:4b`` and the text-only ``gemma3:1b``). The walk
used to accept any installed member of a rung's family, so ``gpt-oss:20b``
satisfied the 120b rung and beat an installed ``nemotron3:33b-q8``, and
``gemma3:1b`` satisfied the vision ladder's ``gemma3:4b`` rung ahead of
moondream. Now an installed tag stands in for a rung exactly (``name`` ==
``name:latest``) or as a family member that cannot be smaller than the rung --
a single-size family, or a parsed parameter count at least the rung's -- and
a llava-era install is recognised as a vision model after the ladder, never
recommended.

Standing in for a rung only says which family a tag belongs to. Candidates are
ranked by their own parsed size (an exact table tag first among equals) and,
when this machine's VRAM budget is known, one that fits it ranks above one that
does not: an installed ``gemma3:27b`` used to be pinned at the ``gemma3:4b``
rung and lose to an exact ``qwen3:8b``; it now wins on a 24 GB card and yields
to the 8B on a 12 GB one. The "nothing on the ladder" fallback never hands chat
to an embedding model (every tier pulls nomic-embed-text and the daemon lists
newest first), and Ollama's 400 ``does not support chat`` is treated as the
model being unavailable, so the retry swaps it out.

The daemon is faked: ``/api/tags`` lists what each test says is installed and
``/api/chat`` records the model it was asked for (and can refuse one with a
real httpx 400). ``NVH_OLLAMA_NUM_CTX=0`` keeps GPU detection out of the
options; budget detection is stubbed to what each test sets (None by default:
no fit check), so this box's own GPU never decides a test.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from nvh.core import local_models
from nvh.core.vision_tools import LEGACY_VISION_NAMES
from nvh.providers import ollama_provider as op
from nvh.providers.base import InvalidRequestError, Message

# --- fake daemon -------------------------------------------------------------


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeOllama:
    """``httpx.AsyncClient`` stand-in: ``/api/tags`` lists ``installed``, ``/api/chat`` records its model.

    A model named in ``refuse`` is answered the way Ollama answers a chat
    request to an embedding model: a real ``httpx.Response`` 400 whose body is
    ``{"error": "\\"<model>\\" does not support chat"}``.
    """

    def __init__(self, state: SimpleNamespace):
        self.state = state

    async def __aenter__(self) -> _FakeOllama:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get(self, url: str, timeout: float | None = None) -> _Response:
        assert url.endswith("/api/tags"), url
        return _Response({"models": [{"name": n} for n in self.state.installed]})

    async def post(self, url: str, json: dict | None = None, timeout: float | None = None):
        if url.endswith("/api/show"):
            raise RuntimeError("no /api/show on this daemon")
        assert url.endswith("/api/chat"), url
        model = str((json or {}).get("model"))
        self.state.chats.append(model)
        if model in self.state.refuse:
            return httpx.Response(
                400,
                json={"error": f'"{model}" does not support chat'},
                request=httpx.Request("POST", url),
            )
        return _Response({"message": {"content": "pong"}, "done": True})


@pytest.fixture
def daemon(monkeypatch):
    monkeypatch.setenv(op.NUM_CTX_ENV, "0")  # no num_ctx, so no GPU detection and no /api/show
    state = SimpleNamespace(installed=[], chats=[], refuse=set(), budget=None)
    monkeypatch.setattr(op, "_detect_tier_budget", lambda: state.budget)  # this box's GPU stays out
    monkeypatch.setattr(op.httpx, "AsyncClient", lambda *a, **kw: _FakeOllama(state))
    return state


def _budget(vram_gb: float) -> local_models.TierBudget:
    """A discrete card of ``vram_gb`` with no RAM to offload to: ``budget_gb`` is the VRAM figure."""
    gpu = SimpleNamespace(vram_mb=vram_gb * 1024, unified_memory=False, compute_capability=(8, 6))
    return local_models.tier_budget([gpu], None)


async def _auto(daemon, installed: list[str], *, vision: bool = False) -> str | None:
    daemon.installed[:] = installed
    return await op.OllamaProvider()._installed_model_fallback("__auto__", prefer_vision=vision)


# --- the four reported cases -------------------------------------------------


@pytest.mark.parametrize(
    "installed", [["gpt-oss:20b", "nemotron3:33b-q8"], ["nemotron3:33b-q8", "gpt-oss:20b"]]
)
async def test_small_family_member_does_not_satisfy_the_big_rung(daemon, installed):
    """A GB10 with gpt-oss:20b and nemotron3:33b-q8 pulled: 20b is not the gpt-oss:120b rung,
    so the installed Nemotron wins, whatever order the daemon lists them in."""
    assert await _auto(daemon, installed) == "ollama/nemotron3:33b-q8"


@pytest.mark.parametrize("moondream", ["moondream", "moondream:latest"])
async def test_text_only_gemma3_1b_is_not_the_vision_gemma3_4b(daemon, moondream):
    assert await _auto(daemon, ["gemma3:1b", moondream], vision=True) == f"ollama/{moondream}"


async def test_legacy_llava_is_recognised_as_vision_but_never_recommended(daemon):
    assert await _auto(daemon, ["llava:7b"], vision=True) == "ollama/llava:7b"
    assert await _auto(daemon, ["minicpm-v:latest"], vision=True) == "ollama/minicpm-v:latest"
    # a table vision pick still outranks the legacy install
    assert await _auto(daemon, ["llava:7b", "moondream"], vision=True) == "ollama/moondream"
    ladder_names = {p.name for p in op._VISION_LADDER} | {p.name for p in op._FALLBACK_LADDER}
    for legacy in LEGACY_VISION_NAMES:
        assert legacy not in ladder_names
    assert op._legacy_vision_names() == tuple(LEGACY_VISION_NAMES)


async def test_exact_tag_is_preferred_over_a_family_member_of_the_same_size(daemon):
    # Among equals the table's own tag wins over a family member of the same size...
    for listing in (["qwen3:8b-q8_0", "qwen3:8b"], ["qwen3:8b", "qwen3:8b-q8_0"]):
        assert await _auto(daemon, listing) == "ollama/qwen3:8b"
    for listing in (["gemma3:4b-it-q8_0", "gemma3:4b"], ["gemma3:4b", "gemma3:4b-it-q8_0"]):
        assert await _auto(daemon, listing, vision=True) == "ollama/gemma3:4b"
    # ...and a member with no size in its tag never jumps to a bigger rung.
    assert await _auto(daemon, ["qwen3:8b", "qwen3:latest"]) == "ollama/qwen3:8b"
    assert await _auto(daemon, ["qwen3:latest", "qwen3:8b"]) == "ollama/qwen3:8b"


# --- the family guard --------------------------------------------------------


async def test_bigger_family_member_stands_in_for_its_own_rung(daemon):
    """qwen3:14b-q8_0 is at least the qwen3:14b rung, which ranks above the exact qwen3:8b."""
    assert await _auto(daemon, ["qwen3:8b", "qwen3:14b-q8_0"]) == "ollama/qwen3:14b-q8_0"
    assert await _auto(daemon, ["qwen3:8b", "qwen3:32b"]) == "ollama/qwen3:32b"  # >= the 30b-a3b rung


async def test_single_size_family_matches_any_of_its_tags(daemon):
    assert await _auto(daemon, ["llama3.2-vision:11b"], vision=True) == "ollama/llama3.2-vision:11b"
    assert await _auto(daemon, ["qwen3-vl:4b", "moondream"], vision=True) == "ollama/qwen3-vl:4b"
    assert await _auto(daemon, ["nemotron3:latest", "gpt-oss:20b"]) == "ollama/nemotron3:latest"


async def test_sizeless_member_is_accepted_at_its_familys_smallest_rung(daemon):
    # qwen3:latest sits at the qwen3:1.7b rung, still above gemma3:1b
    assert await _auto(daemon, ["gemma3:1b", "qwen3:latest"]) == "ollama/qwen3:latest"
    # gemma3:latest is the 4b on the registry, so it is the vision ladder's gemma3 rung
    assert await _auto(daemon, ["gemma3:1b", "gemma3:latest"], vision=True) == "ollama/gemma3:latest"


async def test_the_model_that_just_failed_is_excluded_canonically(daemon):
    provider = op.OllamaProvider()
    daemon.installed[:] = ["qwen3:8b", "gemma3:4b"]
    assert await provider._installed_model_fallback("ollama/qwen3:8b") == "ollama/gemma3:4b"
    daemon.installed[:] = ["moondream:latest"]
    assert await provider._installed_model_fallback("ollama/moondream", prefer_vision=True) is None
    daemon.installed[:] = ["mystery:latest"]
    assert await provider._installed_model_fallback("ollama/mystery") is None


# --- ranked by the tag's own size, not by its family's top rung (O4) ---------


@pytest.mark.parametrize("bigger", ["gemma3:27b", "gemma3:12b"])
async def test_bigger_member_of_a_capped_family_beats_a_smaller_exact_tag(daemon, bigger):
    """gemma3 tops out at 4b in the table; an installed 12b / 27b used to be ranked at that rung and lose to qwen3:8b."""
    for listing in ([bigger, "qwen3:8b"], ["qwen3:8b", bigger]):
        assert await _auto(daemon, listing) == f"ollama/{bigger}"
    # every gemma3 from 4b up sees images, so the vision ladder ranks it the same way
    assert await _auto(daemon, [bigger, "gemma3:4b"], vision=True) == f"ollama/{bigger}"
    assert await _auto(daemon, ["gemma3:4b", bigger]) == f"ollama/{bigger}"


async def test_budget_fit_decides_between_gemma3_27b_and_qwen3_8b(daemon):
    listing = ["qwen3:8b", "gemma3:27b"]
    daemon.budget = _budget(24)  # gemma3:27b loads in ~20 GB: fits, and is the bigger model
    assert await _auto(daemon, listing) == "ollama/gemma3:27b"
    assert await _auto(daemon, list(reversed(listing))) == "ollama/gemma3:27b"
    daemon.budget = _budget(12)  # ...but not in 12 GB, so the exact 8B that does fit wins
    assert await _auto(daemon, listing) == "ollama/qwen3:8b"
    assert await _auto(daemon, list(reversed(listing))) == "ollama/qwen3:8b"
    # gemma3:12b (~9 GB loaded) still fits the 12 GB card and still beats the 8B
    assert await _auto(daemon, ["qwen3:8b", "gemma3:12b"]) == "ollama/gemma3:12b"


async def test_the_fit_applies_to_exact_table_tags_too(daemon):
    """nemotron3:33b loads in ~30 GB (the table's figure): a 24 GB card takes the 14B that fits."""
    daemon.budget = _budget(24)
    assert await _auto(daemon, ["nemotron3:33b", "qwen3:14b"]) == "ollama/qwen3:14b"
    daemon.budget = _budget(40)
    assert await _auto(daemon, ["nemotron3:33b", "qwen3:14b"]) == "ollama/nemotron3:33b"


async def test_when_nothing_fits_the_smallest_overflow_wins(daemon):
    daemon.budget = _budget(4)
    assert await _auto(daemon, ["qwen3:14b", "qwen3:8b"]) == "ollama/qwen3:8b"
    assert await _auto(daemon, ["qwen3:8b", "qwen3:14b"]) == "ollama/qwen3:8b"


async def test_no_budget_means_no_fit_check(daemon):
    assert daemon.budget is None
    assert await _auto(daemon, ["qwen3:8b", "nemotron3:33b-q8"]) == "ollama/nemotron3:33b-q8"


async def test_a_remote_daemon_is_not_fitted_to_this_machines_budget(daemon):
    daemon.budget = _budget(12)
    daemon.installed[:] = ["qwen3:8b", "gemma3:27b"]
    remote = op.OllamaProvider(base_url="http://spark.local:11434")
    assert remote._resolved_budget_gb() is None
    assert await remote._installed_model_fallback("__auto__") == "ollama/gemma3:27b"
    local = op.OllamaProvider()
    assert local._resolved_budget_gb() == 12.0
    assert await local._installed_model_fallback("__auto__") == "ollama/qwen3:8b"


async def test_text_capable_picks_still_lead_vision_only_ones_for_chat(daemon):
    """The fallback ladder's two halves survive the size ranking: the 1.7B chat pick answers chat
    before the bigger llama3.2-vision, which leads only when images are the point."""
    assert await _auto(daemon, ["llama3.2-vision", "qwen3:1.7b"]) == "ollama/qwen3:1.7b"
    assert await _auto(daemon, ["llama3.2-vision", "qwen3:1.7b"], vision=True) == "ollama/llama3.2-vision"
    assert op._VISION_ONLY_TAGS == {"llama3.2-vision", "qwen3-vl:8b", "moondream"}


@pytest.mark.parametrize(
    "name, size, moe, runtime",
    [
        ("gemma3:27b", 27.0, False, 20.4),  # Q4_K_M when the tag names no quant: 27 * 0.63 * 1.2
        ("gemma3:12b", 12.0, False, 9.1),
        ("qwen3:14b-q8_0", 14.0, False, 18.0),  # Q8_0 weighs ~1.07 GB per B
        ("llama3.2:3b-instruct-fp16", 3.0, False, 7.2),
        ("qwen3:30b-a3b", 30.0, True, 20.8),  # MoE: +10% headroom, not +20%
    ],
)
def test_estimated_runtime_follows_the_tables_maths(name, size, moe, runtime):
    assert op._estimate_runtime_gb(name, size, moe) == runtime


@pytest.mark.parametrize(
    "name, moe",
    [
        ("gpt-oss:70b", True),  # every gpt-oss pick in the table is MoE
        ("nemotron3:latest", True),
        ("qwen3:35b-a3b", True),  # mixed family: the 30b-a3b tag shape decides
        ("qwen3:32b", False),
        ("gemma3:27b", False),
        ("mystery:30b-a3b", True),
        ("mystery:30b", False),
    ],
)
def test_moe_is_read_from_the_table_then_from_the_tag(name, moe):
    assert op._is_moe(name) is moe


def test_family_moe_flags_come_from_the_table():
    assert op._FAMILY_MOE["gpt-oss"] is True
    assert op._FAMILY_MOE["gemma3"] is False
    assert op._FAMILY_MOE["qwen3"] is None  # dense 1.7b-14b next to the 30b-a3b MoE


def test_an_untagged_table_pick_is_sized_from_its_runtime():
    assert op._pick_size_b(local_models.pick_for_tag("qwen3:8b")) == 8.0
    assert 1.0 < op._pick_size_b(local_models.pick_for_tag("moondream")) < 4.0  # ~1.8B on the registry


# --- nothing on the ladder: never an embedding model (O1) --------------------


async def test_nothing_on_the_ladder_falls_back_to_the_biggest_chat_capable_model(daemon):
    assert await _auto(daemon, ["mystery:latest", "other:7b"]) == "ollama/other:7b"  # a parsed size first...
    assert await _auto(daemon, ["mystery:latest", "other"]) == "ollama/mystery:latest"  # ...the daemon's order among the sizeless
    assert await _auto(daemon, ["gemma3:1b"], vision=True) == "ollama/gemma3:1b"  # cannot see, but answers
    assert await _auto(daemon, []) is None


async def test_an_embedding_model_is_never_the_fallback(daemon):
    """Every tier pulls nomic-embed-text and the daemon lists newest first: it used to be handed to chat and vision."""
    assert await _auto(daemon, ["nomic-embed-text:latest", "mystery:latest"]) == "ollama/mystery:latest"
    assert await _auto(daemon, ["nomic-embed-text:latest", "mystery:latest"], vision=True) == "ollama/mystery:latest"
    assert await _auto(daemon, ["nomic-embed-text:latest"]) is None
    assert await _auto(daemon, ["nomic-embed-text:latest"], vision=True) is None
    only_embedders = [
        "nomic-embed-text:latest", "bge-m3:latest", "all-minilm:22m", "mxbai-embed-large",
        "bge-reranker-v2-m3", "e5-mistral:7b", "snowflake-arctic-embed2", "granite-embedding:30m",
    ]
    assert await _auto(daemon, only_embedders) is None
    assert await _auto(daemon, only_embedders, vision=True) is None


async def test_the_fallback_prefers_a_chat_model_that_fits_the_budget(daemon):
    daemon.budget = _budget(12)
    assert await _auto(daemon, ["mystery:70b", "other:7b"]) == "ollama/other:7b"
    daemon.budget = None
    assert await _auto(daemon, ["mystery:70b", "other:7b"]) == "ollama/mystery:70b"


@pytest.mark.parametrize(
    "name, chat",
    [
        ("nomic-embed-text:latest", False),  # the table's embed pick, by name
        ("nomic-embed-text:137m", False),
        ("mxbai-embed-large", False),
        ("snowflake-arctic-embed2", False),
        ("granite-embedding:30m", False),
        ("bge-m3:latest", False),
        ("bge-reranker-v2-m3", False),
        ("e5-mistral:7b", False),
        ("all-minilm:22m", False),
        ("qwen3:8b", True),
        ("gemma3n:e4b", True),  # "e4b" is not the "e5" embedding family
        ("deepseek-r1:8b", True),
        ("codellama:7b-code-q5_K_M", True),
        ("mystery:latest", True),
    ],
)
def test_is_chat_capable(name, chat):
    assert op._is_chat_capable(name) is chat


async def test_a_400_does_not_support_chat_swaps_the_model(daemon):
    """Ollama refuses chat on an embedding model with a 400 that used to surface as-is; now the
    model counts as unavailable and the retry picks the strongest installed chat model."""
    daemon.installed[:] = ["nomic-embed-text:latest", "qwen3:8b"]
    daemon.refuse.add("nomic-embed-text")

    resp = await op.OllamaProvider().complete(
        [Message(role="user", content="ping")], model="ollama/nomic-embed-text", max_tokens=8
    )

    assert resp.content == "pong"
    assert resp.model == "ollama/qwen3:8b"
    assert resp.metadata["fallback_model"] == "ollama/qwen3:8b"
    assert daemon.chats == ["nomic-embed-text", "qwen3:8b"]


async def test_only_embedders_installed_keeps_the_daemons_error(daemon, monkeypatch):
    daemon.installed[:] = ["nomic-embed-text:latest"]
    daemon.refuse.add("nomic-embed-text")

    async def no_litellm(**kwargs):
        raise RuntimeError('400 "nomic-embed-text" does not support chat')

    monkeypatch.setattr(op.litellm, "acompletion", no_litellm)

    with pytest.raises(InvalidRequestError):
        await op.OllamaProvider().complete(
            [Message(role="user", content="ping")], model="ollama/nomic-embed-text", max_tokens=8
        )
    assert daemon.chats == ["nomic-embed-text"]  # no doomed retry against the same model


def test_unsupported_model_counts_as_unavailable():
    err = RuntimeError('400 from Ollama: "nomic-embed-text" does not support chat')
    assert op.OllamaProvider._looks_like_unsupported_model(err)
    assert op.OllamaProvider._should_try_installed_fallback(err)
    assert op.OllamaProvider._should_try_installed_fallback(
        RuntimeError('400 from Ollama: "nomic-embed-text" does not support generate')
    )
    assert not op.OllamaProvider._should_try_installed_fallback(RuntimeError("400 from Ollama: invalid option"))


async def test_raise_for_ollama_status_keeps_the_daemons_error_text():
    request = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
    body = json.dumps({"error": '"nomic-embed-text" does not support chat'}).encode()

    with pytest.raises(RuntimeError, match=r"400 from Ollama: .*does not support chat"):
        await op._raise_for_ollama_status(httpx.Response(400, content=body, request=request))

    class _Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield body

    # a streamed response has not been read yet: the body is read once the status has failed
    with pytest.raises(RuntimeError, match="does not support chat"):
        await op._raise_for_ollama_status(httpx.Response(400, stream=_Body(), request=request))
    # no JSON error to quote -> httpx's own exception, unchanged
    with pytest.raises(httpx.HTTPStatusError):
        await op._raise_for_ollama_status(httpx.Response(500, content=b"boom", request=request))
    await op._raise_for_ollama_status(httpx.Response(200, content=b"{}", request=request))  # a good answer passes


# --- through complete() ------------------------------------------------------


async def test_complete_with_an_auto_model_chats_with_the_chosen_model(daemon):
    daemon.installed[:] = ["gpt-oss:20b", "nemotron3:33b-q8"]

    resp = await op.OllamaProvider().complete(
        [Message(role="user", content="ping")], model="auto", max_tokens=8
    )

    assert resp.content == "pong"
    assert resp.model == "ollama/nemotron3:33b-q8"
    assert daemon.chats == ["nemotron3:33b-q8"]


async def test_complete_prefer_vision_walks_the_vision_ladder(daemon):
    daemon.installed[:] = ["gemma3:1b", "moondream"]

    resp = await op.OllamaProvider().complete(
        [Message(role="user", content="what is this?")], model="auto", max_tokens=8, prefer_vision=True
    )

    assert resp.model == "ollama/moondream"
    assert daemon.chats == ["moondream"]


# --- list_models' vision flag (O3) -------------------------------------------


def test_legacy_vision_names_are_reported_vision_capable():
    """HEAD flagged the llava-era installs True; the table-derived flag dropped them while the auto-pick still used them."""
    for legacy in LEGACY_VISION_NAMES:
        assert op._supports_vision(legacy)
        assert op._supports_vision(f"{legacy}:13b")
    assert op._supports_vision("nemotron3:33b") and op._supports_vision("gemma3:4b")
    assert not op._supports_vision("gemma3:1b")
    assert not op._supports_vision("mistral:7b")


# --- the helpers behind the walk ---------------------------------------------


@pytest.mark.parametrize(
    "tag, size",
    [
        ("nemotron3:33b-q8", 33.0),
        ("qwen3:30b-a3b", 30.0),  # the total, not the 3B active count
        ("qwen3:1.7b", 1.7),
        ("gemma3:1b", 1.0),
        ("gpt-oss:120b", 120.0),
        ("llama3.2-vision:11b-instruct-q4_K_M", 11.0),
        ("nomic-embed-text:137m", 0.137),
        ("moondream", None),
        ("llama3.2-vision", None),
        ("qwen3:latest", None),
        ("gemma3n:e2b", None),  # "e2b" is an effective size, not a count this parser trusts
    ],
)
def test_param_size_reads_the_tag(tag, size):
    assert op._param_size_b(tag) == size


@pytest.mark.parametrize(
    "tag, gb_per_b",
    [
        ("gemma3:27b", 0.63),  # no quant named: Q4_K_M
        ("qwen3:14b-q8_0", 1.07),
        ("qwen3:8b-q4_K_M", 0.63),
        ("llama3.2:3b-instruct-fp16", 2.0),
        ("mystery:7b-q6_K", 0.82),
    ],
)
def test_gb_per_b_reads_the_quant_off_the_tag(tag, gb_per_b):
    assert op._gb_per_b(tag) == gb_per_b


def test_family_sizes_come_from_the_table():
    assert op._FAMILY_SIZES["gpt-oss"] == {20.0, 120.0}
    assert op._FAMILY_SIZES["gemma3"] == {1.0, 4.0}
    assert op._FAMILY_SIZES["nemotron3"] == {33.0}  # two quants, one size
    assert op._FAMILY_SIZES["moondream"] == frozenset()
    assert set(op._FAMILY_SIZES) == {p.name for p in local_models.all_picks()}


def test_ladders_are_the_tag_ladders_as_picks():
    assert [p.tag for p in op._FALLBACK_LADDER] == list(op._FALLBACK_MODEL_PREFERENCE)
    assert [p.tag for p in op._VISION_LADDER] == list(op._VISION_MODEL_PREFERENCE)
    assert all(p.vision for p in op._VISION_LADDER)
    assert "nomic-embed-text" not in {p.name for p in op._FALLBACK_LADDER}
    assert op._TABLE_EMBED_NAMES == {"nomic-embed-text"}


def test_canonical_tag_reads_a_bare_name_as_latest():
    assert op._canonical_tag("moondream") == "moondream:latest"
    assert op._canonical_tag("qwen3:8b") == "qwen3:8b"
