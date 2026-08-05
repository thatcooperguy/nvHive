"""Contract tests for the Model Manager feature (roadmap: in-app model browser).

The feature unifies existing backend surfaces into one WebUI page + a
`nvh models` CLI. These tests pin the contract those surfaces depend on
so a refactor of the model-fit report or the API endpoints fails CI
instead of silently breaking the Model Manager.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_model_fit_report_shape() -> None:
    """The Model Manager reads fit_score / fits_vram / estimated_disk_gb /
    installed off each catalog entry. Pin those fields."""
    from nvh.integrations.diagnostics.model_fit import model_fit_report

    report = model_fit_report()
    models = report.get("models") or report.get("ranked") or []
    assert isinstance(models, list) and models, "fit report must return models"
    sample = models[0]
    # The Model Manager UI/CLI read these off every entry.
    for field in (
        "id",
        "title",
        "install_target",
        "fit_score",
        "fit_reasons",
        "use_case",
        "installed",
        "estimated_disk_gb",
    ):
        assert field in sample, f"model-fit entry missing {field}"
    # VRAM signal the browser labels rows with (every entry carries it).
    assert all("fits_vram" in m for m in models)


def test_model_endpoints_registered() -> None:
    """The four endpoints the Model Manager calls must exist on the app."""
    server = (ROOT / "nvh" / "api" / "server.py").read_text(encoding="utf-8")
    assert '"/v1/setup/model-fit"' in server
    assert '"/v1/ollama/models"' in server
    assert '"/v1/ollama/pull"' in server
    assert '@app.delete("/v1/ollama/models/{name:path}"' in server


def test_nvh_models_cli_registered() -> None:
    """`nvh models` subapp with list/pull/rm must be wired onto the CLI."""
    from nvh.cli import main as cli_main

    registered = [g.typer_instance for g in cli_main.app.registered_groups]
    assert cli_main.models_app in registered
    names = {c.name for c in cli_main.models_app.registered_commands}
    assert {"list", "pull", "rm"}.issubset(names)


def test_models_page_and_nav_present() -> None:
    """The WebUI page + sidebar entry + typed API client must exist."""
    page = ROOT / "web" / "app" / "models" / "page.tsx"
    assert page.exists(), "web/app/models/page.tsx missing"
    text = page.read_text(encoding="utf-8")
    assert "getModelFit" in text
    assert "pullModelStream" in text
    assert "deleteModel" in text

    sidebar = (ROOT / "web" / "components" / "Sidebar.tsx").read_text(encoding="utf-8")
    assert "href: '/models'" in sidebar

    api = (ROOT / "web" / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "export function pullModelStream" in api
    assert "'complete'" in api and "'error'" in api


def test_pull_stream_sends_auth() -> None:
    """The pull is auth-gated; the SSE client must attach auth headers or a
    keyed workspace would 401 the download."""
    api = (ROOT / "web" / "lib" / "api.ts").read_text(encoding="utf-8")
    block = api.split("export function pullModelStream", 1)[1]
    assert "getApiAuthHeaders()" in block.split("getReader", 1)[0]
