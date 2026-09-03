"""nvh.integrations.local_chat -- the rootless "can a local model answer?" probe.

The preferred-model ladder is derived from nvh.core.local_models (chat and
code picks, largest loaded size first), so the expectations here are read from
that table too rather than typed in.
"""

from __future__ import annotations

from nvh.core import local_models
from nvh.integrations.local_chat import (
    PREFERRED_CHAT_MODELS,
    _rank_models,
    local_chat_smoke_status,
)

RETIRED_TAGS = (
    "nemotron-3-nano-omni",
    "nemotron-omni",
    "nemotron",
    "llama3.3:70b",
    "qwen2.5-coder:32b",
    "qwen2.5-coder:7b",
    "deepseek-r1:8b",
    "llama3.1:8b",
    "llava:7b",
    "minicpm-v",
    "nemotron-mini",
)


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, *, models: list[str], answer: str):
        self.models = models
        self.answer = answer

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, _url: str) -> _Response:
        return _Response({"models": [{"name": model} for model in self.models]})

    def post(self, _url: str, json: dict) -> _Response:
        return _Response({"message": {"content": self.answer}, "model": json["model"]})


def _chat_and_code_picks_largest_first() -> list[str]:
    picks: dict[str, local_models.LocalModelPick] = {}
    for tier in local_models.LOCAL_MODEL_TIERS:
        for use_case in ("chat", "code"):
            picks.setdefault(tier.picks[use_case].tag, tier.picks[use_case])
    return [p.tag for p in sorted(picks.values(), key=lambda p: (-p.runtime_gb, p.tag))]


def test_preferred_chat_models_are_the_tables_chat_and_code_picks():
    assert list(PREFERRED_CHAT_MODELS) == _chat_and_code_picks_largest_first()
    assert set(PREFERRED_CHAT_MODELS) <= set(local_models.all_tags())
    sizes = local_models.size_table()
    ordered = [sizes[tag] for tag in PREFERRED_CHAT_MODELS]
    assert ordered == sorted(ordered, reverse=True)
    assert "nomic-embed-text" not in PREFERRED_CHAT_MODELS
    for retired in RETIRED_TAGS:
        assert retired not in PREFERRED_CHAT_MODELS


def test_local_chat_smoke_verifies_real_output(monkeypatch, tmp_path):
    model = local_models.pick(8.0, "chat").tag
    monkeypatch.setattr(
        "nvh.integrations.local_chat.httpx.Client",
        lambda timeout: _FakeClient(models=[model, "tiny"], answer="NVHIVE_READY"),
    )

    result = local_chat_smoke_status(home_dir=tmp_path, force=True)

    assert result["ready"] is True
    assert result["status"] == "ready"
    assert result["model"] == model
    assert result["output_chars"] > 0


def test_local_chat_smoke_prefers_largest_installed_model(monkeypatch, tmp_path):
    smallest, largest = PREFERRED_CHAT_MODELS[-1], PREFERRED_CHAT_MODELS[0]
    monkeypatch.setattr(
        "nvh.integrations.local_chat.httpx.Client",
        lambda timeout: _FakeClient(models=[smallest, largest], answer="NVHIVE_READY"),
    )

    result = local_chat_smoke_status(home_dir=tmp_path, force=True)

    assert result["ready"] is True
    assert result["model"] == largest
    assert result["attempted_models"] == [largest]


def test_rank_models_keeps_unknown_tags_after_the_table():
    known = PREFERRED_CHAT_MODELS[-1]
    assert _rank_models(["mystery:latest", known]) == [known, "mystery:latest"]
    assert _rank_models([]) == []


def test_local_chat_smoke_reports_no_models(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "nvh.integrations.local_chat.httpx.Client",
        lambda timeout: _FakeClient(models=[], answer=""),
    )

    result = local_chat_smoke_status(home_dir=tmp_path, force=True)

    assert result["ready"] is False
    assert result["status"] == "no-models"
    assert result["next_action_id"] == "starter-models"
