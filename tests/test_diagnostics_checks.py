"""The checks registry itself: row backfill, shared probes, the new rows.

`tests/test_cli_status_and_dispatch.py` pins how `nvh status` renders the
registry; this file pins what the registry produces.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nvh.integrations.diagnostics import checks as diag


def _check_by_id(check_id: str) -> diag.Check:
    return next(c for c in diag.REGISTRY if c.id == check_id)


async def _rows(check_id: str, ctx: diag.CheckContext) -> list[diag.CheckResult]:
    return await diag.run_check(_check_by_id(check_id), ctx)


def _ctx(**attrs) -> diag.CheckContext:
    ctx = diag.CheckContext(api_url="http://127.0.0.1:1")
    for key, value in attrs.items():
        setattr(ctx, key, value)
    return ctx


# ---------------------------------------------------------------------------
# Rows and registry shape
# ---------------------------------------------------------------------------


def test_run_check_backfills_id_and_title_from_the_owning_check():
    def fn(ctx):
        return [diag.CheckResult(status="pass"), diag.CheckResult(title="Sub row", status="warn")]

    rows = asyncio.run(diag.run_check(diag.Check("x", "X", frozenset({diag.DEEP}), fn), diag.CheckContext()))
    assert [(r.id, r.title, r.status) for r in rows] == [("x", "X", "pass"), ("x", "Sub row", "warn")]


def test_every_row_id_belongs_to_a_registered_check():
    ids = {c.id for c in diag.REGISTRY}
    assert "ollama_required_models" in ids  # `--deep --fix` looks this row up by id
    assert _check_by_id("provider_health").title == "Advisors"


def test_smoke_tier_is_the_api_checks_and_report_includes_them():
    smoke = {c.id for c in diag.checks_for(diag.SMOKE)}
    assert smoke == {"api_health", "api_advisors", "api_proxy_health", "api_quota", "api_query", "api_proxy_chat"}
    assert smoke <= {c.id for c in diag.checks_for(diag.REPORT)}
    assert diag.SMOKE in diag.TIERS


def test_retired_providers_come_from_the_registry_module_not_the_cli():
    import inspect

    assert "from nvh.cli.setup import RETIRED_PROVIDERS" not in inspect.getsource(diag)


def test_run_checks_sync_works_inside_a_running_loop(monkeypatch):
    monkeypatch.setattr(diag, "REGISTRY", [diag.Check("x", "X", frozenset({"t"}), lambda ctx: diag.CheckResult(status="pass"))])

    async def inside_loop():
        return diag.run_checks_sync("t", diag.CheckContext())

    assert [r.id for r in asyncio.run(inside_loop())] == ["x"]
    assert [r.id for r in diag.run_checks_sync("t")] == ["x"]


# ---------------------------------------------------------------------------
# Shared probes
# ---------------------------------------------------------------------------


def test_gpus_are_detected_once_per_context(monkeypatch):
    import nvh.utils.gpu as gpu_mod

    calls = []
    gpu = SimpleNamespace(
        index=0, name="RTX 6000", vram_gb=48.0, vram_mb=49152, utilization_pct=3,
        driver_version="580.1", cuda_version="13.0", compute_capability=(8, 9), memory_free_mb=40000,
    )
    monkeypatch.setattr(gpu_mod, "detect_gpus", lambda: calls.append(1) or [gpu])
    ctx = _ctx()
    assert ctx.gpus == [gpu] and ctx.gpus == [gpu]
    rows = asyncio.run(_rows("gpu", ctx))
    assert rows[0].status == "pass" and "RTX 6000" in rows[0].detail
    assert len(calls) == 1


def test_engine_receives_the_precomputed_vram(monkeypatch):
    seen = {}

    class FakeEngine:
        def __init__(self, config=None):
            pass

        async def initialize(self, gpu_vram_gb=None):
            seen["vram"] = gpu_vram_gb
            return ["ollama"]

    import nvh.core.engine as engine_mod

    monkeypatch.setattr(engine_mod, "Engine", FakeEngine)
    ctx = _ctx(_config=SimpleNamespace(), _gpus=[SimpleNamespace(vram_gb=24.0), SimpleNamespace(vram_gb=24.0)])
    asyncio.run(ctx.engine())
    assert seen["vram"] == 48.0 and ctx.enabled_providers == ["ollama"]


def test_ollama_row_names_the_probed_url(monkeypatch):
    monkeypatch.setattr(diag, "probe_ollama_models", lambda timeout=5.0: None)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434")
    row = asyncio.run(_rows("ollama", _ctx()))[0]
    assert row.status == "warn" and "http://gpu-box:11434" in row.detail


def test_required_models_row_carries_the_missing_list():
    config = SimpleNamespace(providers={
        "ollama": SimpleNamespace(enabled=True, type="ollama", default_model="ollama/qwen3:8b", fallback_model="gemma3:4b"),
    })
    ctx = _ctx(_config=config, _ollama=["gemma3:4b"])
    row = asyncio.run(_rows("ollama_required_models", ctx))[0]
    assert row.id == "ollama_required_models" and row.status == "warn"
    assert row.data["missing"] == ["qwen3:8b"]
    assert asyncio.run(_rows("ollama_required_models", _ctx(_config=config, _ollama=None))) == []


# ---------------------------------------------------------------------------
# Advisors
# ---------------------------------------------------------------------------


class _Registry:
    def __init__(self, names, delay=0.0):
        self.names, self.delay = names, delay

    def has(self, name):
        return name in self.names

    def get(self, name):
        delay = self.delay

        class P:
            async def health_check(self):
                await asyncio.sleep(delay)
                return SimpleNamespace(healthy=True, latency_ms=int(delay * 1000), error=None)

        return P()


def _fake_engine(names, delay=0.0, budget=None):
    return SimpleNamespace(
        registry=_Registry(names, delay),
        rate_manager=SimpleNamespace(get_health_score=lambda n: 0.9),
        _get_fallback_chain=lambda primary: list(names),
        get_budget_status=budget,
        cache=SimpleNamespace(stats={"entries": 0, "max_size": 10}),
    )


def test_provider_health_checks_run_concurrently():
    names = ["a", "b", "c", "d"]
    ctx = _ctx(
        _engine=_fake_engine(names, delay=0.3), enabled_providers=names,
        _config=SimpleNamespace(defaults=SimpleNamespace(provider="a"), providers={}),
    )
    start = time.monotonic()
    rows = asyncio.run(_rows("provider_health", ctx))
    assert time.monotonic() - start < 1.0  # 4 × 0.3 s would be sequential
    assert [r.data["provider"] for r in rows] == names
    assert all(r.status == "pass" and r.data["chain_position"] for r in rows)


def test_provider_keys_use_the_shared_resolver(monkeypatch):
    for var in ("NVIDIA_API_KEY", "COUNCIL_NVIDIA_API_KEY", "HIVE_NVIDIA_API_KEY", "NVH_USE_KEYRING"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NIM_API_KEY", "nim")
    config = SimpleNamespace(providers={
        "nvidia": SimpleNamespace(enabled=True, type="", api_key="${NVIDIA_API_KEY}"),
        "ollama": SimpleNamespace(enabled=True, type="ollama", api_key=""),
    })
    rows = asyncio.run(_rows("provider_keys", _ctx(_config=config)))
    by_provider = {r.data["provider"]: r for r in rows}
    assert by_provider["nvidia"].status == "pass" and by_provider["nvidia"].data["source"] == "env:NIM_API_KEY"
    assert by_provider["ollama"].status == "pass" and "not needed" in by_provider["ollama"].detail


def test_budget_rows_serialise_to_json():
    async def budget():
        return {
            "daily_spend": Decimal("0.10"), "daily_limit": Decimal("0"), "monthly_spend": Decimal("1.5"),
            "monthly_limit": 10, "daily_queries": 2, "monthly_queries": 20, "local_queries": 5,
            "by_provider": {"groq": Decimal("0.10")}, "unavailable": False,
        }

    ctx = _ctx(_engine=_fake_engine([], budget=budget), _config=SimpleNamespace())
    rows = asyncio.run(_rows("budget", ctx))
    assert [r.id for r in rows] == ["budget", "savings"]
    assert "$0.10 spent today | $1.50 / $10.00 monthly" == rows[0].detail
    assert json.loads(json.dumps(diag.summarize(rows)))["checks"][0]["data"]["by_provider"] == {"groq": 0.1}


# ---------------------------------------------------------------------------
# Retired / sunset
# ---------------------------------------------------------------------------


def _sunset_ctx(monkeypatch):
    # A synthetic spec: no shipped row carries a sunset today (perplexity moved
    # to the Agent API), and the mechanism must stay pinned regardless.
    from nvh.providers.specs import PROVIDER_SPECS, ProviderSpec

    monkeypatch.setitem(PROVIDER_SPECS, "sunsetco", ProviderSpec(
        "sunsetco", "sunsetco/big", "sunsetco/small",
        sunset_date="2026-09-27", sunset_note="Legacy Chat API",
    ))
    return _ctx(_config=SimpleNamespace(providers={
        "sunsetco": SimpleNamespace(enabled=True, type="", default_model="sunsetco/big", fallback_model=""),
        "groq": SimpleNamespace(enabled=True, type="", default_model="groq/openai/gpt-oss-120b", fallback_model=""),
    }))


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 9, 2), "Legacy Chat API retires 2026-09-27 (25 day(s))"),
        (date(2026, 10, 1), "Legacy Chat API retired 2026-09-27"),
    ],
)
def test_sunset_within_30_days_or_past_warns(monkeypatch, today, expected):
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(diag, "date", FrozenDate)
    monkeypatch.setattr("nvh.cli.setup.stale_default_models", lambda providers: [])
    rows = asyncio.run(_rows("retired_models", _sunset_ctx(monkeypatch)))
    assert [(r.title, r.status, r.detail) for r in rows] == [("Advisor sunsetco", "warn", expected)]
    assert rows[0].data["sunset_date"] == "2026-09-27"


def test_sunset_far_away_is_silent(monkeypatch):
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return date(2026, 1, 1)

    monkeypatch.setattr(diag, "date", FrozenDate)
    monkeypatch.setattr("nvh.cli.setup.stale_default_models", lambda providers: [])
    assert asyncio.run(_rows("retired_models", _sunset_ctx(monkeypatch))) == []


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


def test_sandbox_row_when_isolation_required_but_docker_missing(monkeypatch):
    from nvh.sandbox.executor import SandboxExecutor

    async def no_docker(self):
        return False

    monkeypatch.setattr(SandboxExecutor, "_check_docker", no_docker)
    monkeypatch.delenv("NVH_SANDBOX_REQUIRE_DOCKER", raising=False)
    monkeypatch.setenv("NVH_SANDBOX", "1")
    row = asyncio.run(_rows("sandbox", _ctx()))[0]
    assert row.status == "warn"
    assert "isolation required but Docker unavailable — run_code/shell will refuse" in row.detail
    assert row.data == {"docker": False, "require_docker": True, "source": "NVH_SANDBOX"}

    monkeypatch.delenv("NVH_SANDBOX")
    row = asyncio.run(_rows("sandbox", _ctx()))[0]
    assert row.status == "info" and "fall back" in row.detail


# ---------------------------------------------------------------------------
# API smoke checks
# ---------------------------------------------------------------------------


def test_api_checks_skip_cleanly_when_nothing_listens():
    ctx = _ctx()  # port 1: connection refused
    rows = asyncio.run(diag.run_checks(diag.SMOKE, ctx))
    assert len(rows) == 6 and {r.status for r in rows} == {"skip"}
    assert all("no API listening at http://127.0.0.1:1" == r.detail for r in rows)
    summary = diag.summarize(rows)
    assert (summary["skipped"], summary["failed"], summary["fixes"]) == (6, 0, [])


def test_api_url_comes_from_env(monkeypatch):
    monkeypatch.setenv("NVH_API_URL", "http://api.example:9000/")
    assert diag.CheckContext().api_url == "http://api.example:9000"
    monkeypatch.delenv("NVH_API_URL")
    assert diag.CheckContext().api_url == diag.DEFAULT_API_URL


def _fake_async_client(status_code, payload, calls):
    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, json=None, headers=None):
            calls.append((method, url, json, headers))
            return SimpleNamespace(status_code=status_code, json=lambda: payload, text=json_text(payload))

    def json_text(value):
        import json as _json

        return _json.dumps(value)

    return Client


def test_api_query_posts_a_short_prompt_and_reports_the_route(monkeypatch):
    monkeypatch.setenv("HIVE_API_KEY", "secret")
    ctx = _ctx(_api=diag.ApiProbe("http://127.0.0.1:8000", reachable=True, health={"status": "ok"}))
    calls = []
    payload = {"status": "success", "data": {"provider": "groq", "model": "m", "latency_ms": 12, "cost_usd": "0"}}
    with patch("httpx.AsyncClient", _fake_async_client(200, payload, calls)):
        row = asyncio.run(_rows("api_query", ctx))[0]
    assert row.status == "pass" and row.detail == "groq/m in 12ms"
    method, url, body, headers = calls[0]
    assert (method, url) == ("POST", "http://127.0.0.1:8000/v1/query")
    assert body["max_tokens"] == 8 and body["prompt"]
    assert headers == {"Authorization": "Bearer secret"}


def test_api_auth_failure_is_a_fail_with_a_fix(monkeypatch):
    monkeypatch.delenv("HIVE_API_KEY", raising=False)
    ctx = _ctx(_api=diag.ApiProbe("http://127.0.0.1:8000", reachable=True))
    with patch("httpx.AsyncClient", _fake_async_client(401, {"detail": "nope"}, [])):
        row = asyncio.run(_rows("api_proxy_chat", ctx))[0]
    assert row.status == "fail" and "HTTP 401" in row.detail and "HIVE_API_KEY" in row.fix


# ---------------------------------------------------------------------------
# default_models_served (DEEP): the configured default against the live list
# ---------------------------------------------------------------------------


class _LiveRegistry:
    """A registry whose adapters answer ``live_model_ids`` with a fixed set (``None`` = unknown)."""

    def __init__(self, live_by_name):
        self.live_by_name = live_by_name
        self.probes = 0

    def has(self, name):
        return name in self.live_by_name

    def get(self, name):
        registry = self
        live = self.live_by_name[name]

        class Adapter:
            async def live_model_ids(self):
                registry.probes += 1
                return live

        return Adapter()


def _default_models_ctx(live_by_name, defaults):
    registry = _LiveRegistry(live_by_name)
    providers = {
        name: SimpleNamespace(enabled=True, type="", default_model=default, fallback_model="")
        for name, default in defaults.items()
    }
    ctx = _ctx(_engine=SimpleNamespace(registry=registry), _config=SimpleNamespace(providers=providers))
    return ctx, registry


def test_default_models_served_is_deep_only():
    check = _check_by_id("default_models_served")
    assert check.tiers == {diag.DEEP}
    assert check.title == "Default models"


def test_default_model_not_in_the_live_list_warns():
    ctx, registry = _default_models_ctx(
        {"groq": {"groq/openai/gpt-oss-120b", "groq/served-too"}, "nvidia": {"nvidia_nim/meta/llama-x"}},
        {"groq": "groq/gone-model", "nvidia": "nvidia_nim/meta/llama-x"},
    )
    rows = asyncio.run(_rows("default_models_served", ctx))
    assert [(r.title, r.status) for r in rows] == [("Advisor groq: default model", "warn")]
    assert rows[0].detail == "'groq/gone-model' not served by groq (2 live model(s))"
    assert rows[0].data == {"provider": "groq", "model": "groq/gone-model", "live_models": 2}
    assert "nvh model list" in rows[0].fix
    assert registry.probes == 2  # one probe per configured provider


def test_default_model_matches_by_tail_and_spec_default_fills_a_blank_config():
    from nvh.providers.specs import PROVIDER_SPECS

    spec = PROVIDER_SPECS["google"]
    bare = spec.default_model[len(spec.litellm_prefix):]
    ctx, _ = _default_models_ctx({"google": {f"gemini/models/{bare}"}}, {"google": ""})
    assert asyncio.run(_rows("default_models_served", ctx)) == []


def test_unknown_live_list_and_bespoke_adapters_say_nothing():
    ctx, _ = _default_models_ctx(
        {"groq": None, "ollama": {"llama3.1:8b"}}, {"groq": "groq/x", "ollama": "other"},
    )
    assert asyncio.run(_rows("default_models_served", ctx)) == []


def test_default_models_check_shares_the_server_probe_helper(monkeypatch):
    """The check and ``/v1/models`` run the same ``live_model_ids_for`` (no list_models fallback here)."""
    import nvh.providers.openai_compatible as oc

    seen = []

    async def fake_live_model_ids_for(provider, *, timeout, list_models_fallback=True):
        seen.append((timeout, list_models_fallback))
        return frozenset({"groq/served"})

    monkeypatch.setattr(oc, "live_model_ids_for", fake_live_model_ids_for)
    ctx, _ = _default_models_ctx({"groq": {"groq/served"}}, {"groq": "groq/gone"})
    rows = asyncio.run(_rows("default_models_served", ctx))
    assert [r.status for r in rows] == ["warn"] and "'groq/gone' not served by groq (1 live model(s))" == rows[0].detail
    assert seen == [(ctx.health_timeout, False)]


def test_default_models_check_tolerates_a_failing_probe():
    class Broken:
        def has(self, name):
            return True

        def get(self, name):
            class Adapter:
                async def live_model_ids(self):
                    raise RuntimeError("boom")

            return Adapter()

    providers = {"groq": SimpleNamespace(enabled=True, type="", default_model="groq/x", fallback_model="")}
    ctx = _ctx(_engine=SimpleNamespace(registry=Broken()), _config=SimpleNamespace(providers=providers))
    assert asyncio.run(_rows("default_models_served", ctx)) == []


def test_default_models_check_needs_an_engine():
    ctx = _ctx(_config=None)
    assert asyncio.run(_rows("default_models_served", ctx)) == []


# ---------------------------------------------------------------------------
# legacy_homes (DEEP): the pre-0.44 import, read-only — it never runs the import
# ---------------------------------------------------------------------------


@pytest.fixture()
def legacy_homes(tmp_path, monkeypatch):
    """A fake OS home plus a throwaway layout; the migration is on but nothing has run."""
    from nvh.integrations.workspace import migrate_legacy as ml
    from nvh.integrations.workspace.storage import storage_layout

    os_home = tmp_path / "home"
    os_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: os_home)
    monkeypatch.setenv(ml.LEGACY_MIGRATION_ENV, "1")  # conftest turns it off for every suite
    monkeypatch.delenv(ml.LEGACY_DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(ml, "_LAST_REPORTS", {})
    nvh_home = tmp_path / "nvh"
    return SimpleNamespace(
        hive=os_home / ".hive", layout=storage_layout(nvh_home), ctx=_ctx(home_dir=str(nvh_home)), ml=ml,
    )


def _forbid_migration(monkeypatch, ml):
    monkeypatch.setattr(
        ml, "migrate_legacy_homes", lambda *a, **k: pytest.fail("the check must never run the migration"),
    )


def test_legacy_homes_is_deep_only_and_sits_beside_legacy_knowledge():
    check = _check_by_id("legacy_homes")
    assert check.tiers == {diag.DEEP} and check.title == "Legacy homes (pre-0.44)"
    ids = [c.id for c in diag.REGISTRY]
    assert ids.index("legacy_homes") == ids.index("legacy_knowledge") + 1


def test_legacy_homes_is_silent_on_a_fresh_install(legacy_homes, monkeypatch):
    _forbid_migration(monkeypatch, legacy_homes.ml)
    assert asyncio.run(_rows("legacy_homes", legacy_homes.ctx)) == []
    assert not legacy_homes.layout.home.exists()


def test_legacy_homes_reports_pending_items_without_running_the_import(legacy_homes, monkeypatch):
    _forbid_migration(monkeypatch, legacy_homes.ml)
    legacy_homes.hive.mkdir()
    (legacy_homes.hive / "user.json").write_bytes(b"{}")
    row = asyncio.run(_rows("legacy_homes", legacy_homes.ctx))[0]
    assert row.status == "warn"
    assert row.detail == f"1 item(s) in {legacy_homes.hive} not yet imported into {legacy_homes.layout.home}"
    assert "nvh config init" in row.fix and "NVH_LEGACY_MIGRATION=0" in row.fix
    assert [e["label"] for e in row.data["pending"]] == ["user profile"]
    assert row.data["attempted"] is False and row.data["failed"] == []
    # Read-only: no marker, no layout directory, and the config was never loaded
    # (load_config() is one of the paths that runs the import).
    assert not legacy_homes.layout.home.exists()
    assert legacy_homes.ctx._config is diag._UNSET
    assert json.loads(json.dumps(diag.summarize([row])))["checks"][0]["id"] == "legacy_homes"


def test_legacy_homes_reports_a_recorded_import(legacy_homes, monkeypatch):
    _forbid_migration(monkeypatch, legacy_homes.ml)
    marker = legacy_homes.layout.state_dir / legacy_homes.ml.MARKER_NAME
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "ran_at": "2026-10-31T09:00:00+00:00",
        "moved": [{"label": "user profile"}, {"label": "plugins"}],
        "merged": [{"label": "API keys (.env)", "added": ["GROQ_API_KEY"]}],
        "skipped": [{"label": "knowledge store", "reason": "already imported"}],
    }))
    row = asyncio.run(_rows("legacy_homes", legacy_homes.ctx))[0]
    assert row.status == "pass"
    assert row.detail == "imported on 2026-10-31: 2 copied, 1 merged, 1 skipped"
    assert row.data["already_done"] is True and row.data["legacy_roots"] == {}
    legacy_homes.hive.mkdir()
    (legacy_homes.hive / "user.json").write_bytes(b"{}")  # would be pending without the marker
    row = asyncio.run(_rows("legacy_homes", legacy_homes.ctx))[0]
    assert row.status == "pass" and f"old roots still present ({legacy_homes.hive})" in row.detail
    assert row.data["pending"] == []  # a recorded run is never re-planned


def test_legacy_homes_reports_the_kill_switch_without_reading_home(legacy_homes, monkeypatch):
    _forbid_migration(monkeypatch, legacy_homes.ml)
    legacy_homes.hive.mkdir()
    monkeypatch.setenv(legacy_homes.ml.LEGACY_MIGRATION_ENV, "0")
    monkeypatch.setattr(Path, "home", lambda: pytest.fail("disabled: nothing under $HOME may be read"))
    row = asyncio.run(_rows("legacy_homes", legacy_homes.ctx))[0]
    assert row.status == "skip" and row.detail == "import disabled (NVH_LEGACY_MIGRATION=0)"
    assert row.data["disabled"] is True and row.data["legacy_roots"] == {} and row.data["pending"] == []


def test_legacy_homes_reports_failures_of_this_process_then_the_retry(legacy_homes, monkeypatch):
    ml = legacy_homes.ml
    legacy_homes.hive.mkdir()
    (legacy_homes.hive / "user.json").write_bytes(b"{}")
    publish = ml._publish_no_replace

    def unavailable(*args):
        raise OSError("no exclusive rename here")

    monkeypatch.setattr(ml, "_publish_no_replace", unavailable)
    report = ml.migrate_legacy_homes(legacy_homes.layout)  # the one real run of this test
    assert report.failed and not Path(report.marker).exists()
    real_migrate = ml.migrate_legacy_homes
    _forbid_migration(monkeypatch, ml)

    row = asyncio.run(_rows("legacy_homes", legacy_homes.ctx))[0]
    assert row.status == "warn"
    assert row.detail == "1 item(s) could not be imported: user profile: copy failed: no exclusive rename here"
    assert "retries" in row.fix and row.data["failed"] == report.failed and row.data["attempted"] is True

    monkeypatch.setattr(ml, "_publish_no_replace", publish)
    assert not real_migrate(legacy_homes.layout).failed
    row = asyncio.run(_rows("legacy_homes", legacy_homes.ctx))[0]
    assert row.status == "pass" and row.detail.startswith("imported on ")
    assert "1 copied, 0 merged, 0 skipped" in row.detail and "old roots still present" in row.detail
    assert row.data["failed"] == []


def test_legacy_homes_warns_about_the_retired_data_dir_knob(legacy_homes, monkeypatch):
    _forbid_migration(monkeypatch, legacy_homes.ml)
    monkeypatch.setenv(legacy_homes.ml.LEGACY_DATA_DIR_ENV, "/srv/old-data")
    row = asyncio.run(_rows("legacy_homes", legacy_homes.ctx))[0]
    assert row.status == "warn" and "NVH_STATE=/srv/old-data/state" in row.detail
    assert "NVH_STATE" in row.fix and row.data["legacy_roots"] == {}
    # The warning rides along with a recorded import too.
    marker = legacy_homes.layout.state_dir / legacy_homes.ml.MARKER_NAME
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"ran_at": "2026-10-31T09:00:00+00:00", "moved": []}))
    row = asyncio.run(_rows("legacy_homes", legacy_homes.ctx))[0]
    assert row.status == "warn" and row.detail.startswith("imported on 2026-10-31: 0 copied")
    assert "NVH_STATE=/srv/old-data/state" in row.detail
