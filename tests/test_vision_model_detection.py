"""nvh.core.vision_tools picks its local vision model from the tier table.

The ladder is every ``nvh.core.local_models`` pick flagged ``vision``, largest
loaded size first, so the strongest installed image model wins and no retired
tag can ever be recommended. llava-era names are *recognised* when a user
already has one installed (an old install keeps working) but rank below every
table pick and are never suggested for a pull. ``/api/tags`` is faked here:
this box runs a live Ollama and the tests must not depend on what it has.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from nvh.core import local_models
from nvh.core import vision_tools as vt

RETIRED_TAGS = (
    "llava",
    "llava:7b",
    "llava-llama3",
    "llava-phi3",
    "bakllava",
    "minicpm-v",
    "nemotron-omni",
    "nemotron-3-nano-omni",
    "llama3.1:8b",
    "qwen2.5-coder:7b",
)

VISION_PICKS = {p.tag: p for p in local_models.all_picks() if p.vision}
LARGEST = vt._VISION_LADDER[0].tag
SMALLEST = vt._VISION_LADDER[-1].tag


@pytest.fixture(autouse=True)
def _fresh_cache():
    vt._reset_vision_model_cache()
    yield
    vt._reset_vision_model_cache()


def _tags(*names: str) -> dict:
    return {"models": [{"name": n} for n in names]}


# --- the ladder is the table --------------------------------------------------


def test_ladder_is_every_vision_pick_largest_first():
    sizes = local_models.size_table()
    tags = [p.tag for p in vt._VISION_LADDER]

    assert set(tags) == set(VISION_PICKS)
    assert set(tags) <= set(local_models.all_tags())
    assert tags == sorted(tags, key=lambda t: (-sizes[t], t))
    assert tags == [p.tag for p in local_models.ordered_picks(None) if p.vision]
    assert all(p.vision for p in vt._VISION_LADDER)
    # every dedicated vision pick (the table's "vision" column) is on the ladder,
    # in the same relative order
    dedicated = [p.tag for p in local_models.vision_picks()]
    assert set(dedicated) <= set(tags)
    assert [t for t in tags if t in dedicated] == dedicated


def test_every_dedicated_vision_pick_beats_every_legacy_tag():
    for pick in local_models.vision_picks():
        for legacy in ("llava:7b", "minicpm-v:latest", "bakllava"):
            assert vt.pick_vision_model([legacy, pick.tag]) == pick.tag


def test_ladder_never_carries_a_retired_or_text_tag():
    tags = {p.tag for p in vt._VISION_LADDER}
    for retired in RETIRED_TAGS:
        assert retired not in tags
    assert "nomic-embed-text" not in tags
    assert "gemma3:1b" not in tags
    assert set(vt.LEGACY_VISION_NAMES).isdisjoint(tags)


def test_vision_names_are_the_names_that_see_images_throughout():
    names = {p.name for p in local_models.all_picks()}
    expected = {n for n in names if all(p.vision for p in local_models.all_picks() if p.name == n)}
    assert vt._VISION_NAMES == expected
    assert "gemma3" not in vt._VISION_NAMES  # gemma3:1b is text-only
    for pick in vt._VISION_LADDER:
        if pick.name != "gemma3":
            assert pick.name in vt._VISION_NAMES


# --- pick_vision_model on fake listings ---------------------------------------


def test_nothing_installed_or_only_text_models_is_none():
    assert vt.pick_vision_model([]) is None
    assert vt.pick_vision_model(["", "  "]) is None
    assert vt.pick_vision_model(["qwen3:8b", "gemma3:1b", "nomic-embed-text:latest"]) is None


def test_strongest_installed_table_pick_wins():
    installed = [SMALLEST, "qwen3:8b", LARGEST]
    assert vt.pick_vision_model(installed) == LARGEST

    # the ladder order, not the listing order, decides
    two = [p.tag for p in vt._VISION_LADDER[:2]]
    assert vt.pick_vision_model(list(reversed(two))) == two[0]


def test_latest_suffix_is_the_same_tag():
    untagged = [p.tag for p in vt._VISION_LADDER if ":" not in p.tag]
    assert untagged, "the table carries at least one untagged vision pick"
    for tag in untagged:
        assert vt.pick_vision_model([f"{tag}:latest"]) == f"{tag}:latest"
        assert vt.pick_vision_model([tag]) == tag


def test_any_tag_of_a_vision_throughout_name_counts():
    assert vt.pick_vision_model(["llama3.2-vision:11b"]) == "llama3.2-vision:11b"
    assert vt.pick_vision_model(["qwen3-vl:4b", "qwen3:8b"]) == "qwen3-vl:4b"
    assert vt.pick_vision_model(["nemotron3:33b-q8"]) == "nemotron3:33b-q8"


def test_exact_tag_beats_a_sibling_of_the_same_name():
    assert vt.pick_vision_model(["qwen3-vl:4b", "qwen3-vl:8b"]) == "qwen3-vl:8b"


def test_gemma3_is_vision_only_at_its_exact_tag():
    assert vt.pick_vision_model(["gemma3:4b"]) == "gemma3:4b"
    assert vt.pick_vision_model(["gemma3:1b"]) is None


def test_a_name_prefix_does_not_over_match():
    # "llama3.2" is not "llama3.2-vision"; "moondream2" is not "moondream"
    assert vt.pick_vision_model(["llama3.2:3b", "moondream2:latest"]) is None


def test_legacy_install_keeps_working():
    assert vt.pick_vision_model(["llava:7b", "qwen3:8b"]) == "llava:7b"
    assert vt.pick_vision_model(["minicpm-v:latest"]) == "minicpm-v:latest"
    assert vt.pick_vision_model(["bakllava"]) == "bakllava"
    assert vt.pick_vision_model(["llava-llama3:8b"]) == "llava-llama3:8b"
    assert vt.pick_vision_model(["llava-phi3:latest"]) == "llava-phi3:latest"


def test_any_table_pick_beats_every_legacy_tag():
    for legacy in ("llava:7b", "minicpm-v:latest", "bakllava:latest"):
        assert vt.pick_vision_model([legacy, SMALLEST]) == SMALLEST
        assert vt.pick_vision_model([SMALLEST, legacy]) == SMALLEST


def test_legacy_ranking_follows_the_legacy_order():
    first, second = vt.LEGACY_VISION_NAMES[0], vt.LEGACY_VISION_NAMES[1]
    assert vt.pick_vision_model([f"{second}:latest", f"{first}:latest"]) == f"{first}:latest"


# --- _detect_ollama_vision_model against a fake /api/tags ---------------------


class _Resp:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


def _fake_get(monkeypatch, *, payload: dict | None, status_code: int = 200, boom: bool = False):
    calls: list[str] = []

    def get(url: str, timeout: float | None = None) -> _Resp:
        calls.append(url)
        if boom:
            raise httpx.ConnectError("refused")
        return _Resp(payload or {}, status_code)

    monkeypatch.setattr(httpx, "get", get)
    return calls


def test_detect_returns_the_tables_pick_from_the_listing(monkeypatch):
    calls = _fake_get(monkeypatch, payload=_tags("qwen3:8b", SMALLEST, LARGEST))

    assert vt._detect_ollama_vision_model() == LARGEST
    assert len(calls) == 1 and calls[0].endswith("/api/tags")


def test_detect_recognises_a_legacy_install(monkeypatch):
    _fake_get(monkeypatch, payload=_tags("llava:7b", "qwen3:8b"))
    assert vt._detect_ollama_vision_model() == "llava:7b"


def test_detect_is_none_for_text_only_daemon(monkeypatch):
    _fake_get(monkeypatch, payload=_tags("qwen3:8b", "gemma3:1b"))
    assert vt._detect_ollama_vision_model() is None


def test_detect_is_none_when_the_daemon_errors(monkeypatch):
    _fake_get(monkeypatch, payload=None, status_code=500)
    assert vt._detect_ollama_vision_model() is None

    vt._reset_vision_model_cache()
    _fake_get(monkeypatch, payload=None, boom=True)
    assert vt._detect_ollama_vision_model() is None


def test_detect_caches_within_the_ttl(monkeypatch):
    calls = _fake_get(monkeypatch, payload=_tags(SMALLEST))

    assert vt._detect_ollama_vision_model() == SMALLEST
    assert vt._detect_ollama_vision_model() == SMALLEST
    assert len(calls) == 1

    vt._reset_vision_model_cache()
    assert vt._detect_ollama_vision_model() == SMALLEST
    assert len(calls) == 2


def test_detect_cache_does_not_outlive_the_ttl(monkeypatch):
    calls = _fake_get(monkeypatch, payload=_tags(SMALLEST))
    vt._detect_ollama_vision_model()
    stamp, cached = vt._vision_model_cache
    monkeypatch.setattr(vt, "_vision_model_cache", (stamp - vt._VISION_CACHE_TTL - 1, cached))

    vt._detect_ollama_vision_model()

    assert len(calls) == 2


# --- the pull suggestion comes from the table ----------------------------------


def test_suggested_pull_is_the_tables_vision_pick_for_this_budget(monkeypatch):
    import nvh.utils.gpu as gpu

    row = SimpleNamespace(vram_mb=24 * 1024, vram_gb=24.0, unified_memory=False, compute_capability=(8, 6))
    mem = SimpleNamespace(effective_for_llm_gb=14.8, available_ram_gb=21.2)
    monkeypatch.setattr(gpu, "detect_gpus", lambda: [row])
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: mem)

    expected = local_models.pick(local_models.tier_budget([row], mem), "vision").tag
    assert vt._suggested_vision_pull() == expected
    assert expected in VISION_PICKS


def test_suggested_pull_without_a_gpu_is_the_cpu_tiers_pick(monkeypatch):
    import nvh.utils.gpu as gpu

    monkeypatch.setattr(gpu, "detect_gpus", lambda: [])
    monkeypatch.setattr(gpu, "detect_system_memory", lambda: None)

    assert vt._suggested_vision_pull() == local_models.LOCAL_MODEL_TIERS[0].picks["vision"].tag


def test_suggested_pull_never_raises_and_never_names_a_retired_tag(monkeypatch):
    import nvh.utils.gpu as gpu

    def boom():
        raise RuntimeError("NVML exploded")

    monkeypatch.setattr(gpu, "detect_gpus", boom)

    suggestion = vt._suggested_vision_pull()
    assert suggestion == local_models.LOCAL_MODEL_TIERS[0].picks["vision"].tag
    assert suggestion in local_models.all_tags()
    assert suggestion not in RETIRED_TAGS
