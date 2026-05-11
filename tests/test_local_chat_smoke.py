from __future__ import annotations

from nvh.integrations.local_chat import local_chat_smoke_status


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

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, _url: str) -> _Response:
        return _Response({"models": [{"name": model} for model in self.models]})

    def post(self, _url: str, json: dict) -> _Response:
        return _Response({"message": {"content": self.answer}, "model": json["model"]})


def test_local_chat_smoke_verifies_real_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "nvh.integrations.local_chat.httpx.Client",
        lambda timeout: _FakeClient(models=["qwen3:8b", "tiny"], answer="NVHIVE_READY"),
    )

    result = local_chat_smoke_status(home_dir=tmp_path, force=True)

    assert result["ready"] is True
    assert result["status"] == "ready"
    assert result["model"] == "qwen3:8b"
    assert result["output_chars"] > 0


def test_local_chat_smoke_reports_no_models(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "nvh.integrations.local_chat.httpx.Client",
        lambda timeout: _FakeClient(models=[], answer=""),
    )

    result = local_chat_smoke_status(home_dir=tmp_path, force=True)

    assert result["ready"] is False
    assert result["status"] == "no-models"
    assert result["next_action_id"] == "starter-models"
