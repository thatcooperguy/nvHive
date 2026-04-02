"""Tests for NemoClaw integration — proxy routing, council/throwdown models, privacy header."""

from nvh.api.proxy import (
    build_models_list,
    is_throwdown_model,
    parse_council_model,
    resolve_provider_from_model,
)

# ---------------------------------------------------------------------------
# parse_council_model
# ---------------------------------------------------------------------------

def test_council_default():
    assert parse_council_model("council") == 3


def test_council_with_count():
    assert parse_council_model("council:5") == 5


def test_council_clamp_low():
    assert parse_council_model("council:1") == 2


def test_council_clamp_high():
    assert parse_council_model("council:99") == 10


def test_council_invalid_number():
    assert parse_council_model("council:abc") == 3


def test_council_none_for_other():
    assert parse_council_model("gpt-4o") is None
    assert parse_council_model("auto") is None
    assert parse_council_model("") is None


# ---------------------------------------------------------------------------
# is_throwdown_model
# ---------------------------------------------------------------------------

def test_throwdown_true():
    assert is_throwdown_model("throwdown") is True


def test_throwdown_false():
    assert is_throwdown_model("auto") is False
    assert is_throwdown_model(None) is False
    assert is_throwdown_model("council") is False


# ---------------------------------------------------------------------------
# resolve_provider_from_model — council/throwdown passthrough
# ---------------------------------------------------------------------------

def test_resolve_council_returns_none():
    """Council models should return (None, None) so the caller handles them."""
    assert resolve_provider_from_model("council") == (None, None)
    assert resolve_provider_from_model("council:3") == (None, None)
    assert resolve_provider_from_model("council:5") == (None, None)


def test_resolve_throwdown_returns_none():
    assert resolve_provider_from_model("throwdown") == (None, None)


def test_resolve_safe_still_works():
    assert resolve_provider_from_model("safe") == ("ollama", None)
    assert resolve_provider_from_model("local") == ("ollama", None)


def test_resolve_auto_still_works():
    assert resolve_provider_from_model("auto") == (None, None)
    assert resolve_provider_from_model("nvhive") == (None, None)


def test_resolve_known_model():
    provider, model = resolve_provider_from_model("gpt-4o")
    assert provider == "openai"
    assert model == "gpt-4o"


# ---------------------------------------------------------------------------
# build_models_list — includes council/throwdown virtual models
# ---------------------------------------------------------------------------

class _MockRegistry:
    def list_enabled(self):
        return []


def test_models_list_includes_council():
    result = build_models_list(_MockRegistry())
    model_ids = [m["id"] for m in result["data"]]
    assert "council" in model_ids
    assert "council:3" in model_ids
    assert "throwdown" in model_ids
    assert "auto" in model_ids
    assert "safe" in model_ids


def test_models_list_council_owned_by_nvhive():
    result = build_models_list(_MockRegistry())
    for m in result["data"]:
        if m["id"] in ("council", "council:3", "throwdown"):
            assert m["owned_by"] == "nvhive"
