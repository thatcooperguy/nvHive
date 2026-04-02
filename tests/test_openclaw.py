"""Tests for OpenClaw integration config generation."""

import json

from nvh.integrations.openclaw import (
    generate_nemoclaw_agent_config,
    generate_openclaw_config,
    write_openclaw_config,
)


def test_openclaw_config_entry_point():
    config = generate_openclaw_config(use_entry_point=True)
    assert "nvhive" in config
    assert config["nvhive"]["command"] == "nvhive-mcp"


def test_openclaw_config_python_module():
    config = generate_openclaw_config(use_entry_point=False)
    assert config["nvhive"]["command"] == "python"
    assert "-m" in config["nvhive"]["args"]
    assert "nvh.mcp_server" in config["nvhive"]["args"]


def test_nemoclaw_agent_config():
    config = generate_nemoclaw_agent_config()
    assert config["name"] == "nvhive-enhanced"
    assert config["inference"]["provider"] == "nvhive"
    assert config["inference"]["model"] == "auto"
    assert "nvhive" in config["mcpServers"]
    assert "ask" in config["tools"]
    assert "council" in config["tools"]
    assert "throwdown" in config["tools"]


def test_nemoclaw_agent_config_custom():
    config = generate_nemoclaw_agent_config(
        agent_name="my-agent",
        default_model="council:5",
    )
    assert config["name"] == "my-agent"
    assert config["inference"]["model"] == "council:5"


def test_write_openclaw_config_creates_file(tmp_path):
    output = tmp_path / "openclaw.json"
    result = write_openclaw_config(output_path=output)
    assert result == output
    assert output.exists()

    data = json.loads(output.read_text())
    assert "mcpServers" in data
    assert "nvhive" in data["mcpServers"]


def test_write_openclaw_config_merges_existing(tmp_path):
    output = tmp_path / "openclaw.json"
    # Write existing config
    existing = {"mcpServers": {"other-tool": {"command": "other"}}, "foo": "bar"}
    output.write_text(json.dumps(existing))

    write_openclaw_config(output_path=output, merge_existing=True)

    data = json.loads(output.read_text())
    assert "nvhive" in data["mcpServers"]
    assert "other-tool" in data["mcpServers"]  # preserved
    assert data["foo"] == "bar"  # preserved


def test_write_openclaw_config_no_merge(tmp_path):
    output = tmp_path / "openclaw.json"
    existing = {"mcpServers": {"other-tool": {"command": "other"}}}
    output.write_text(json.dumps(existing))

    write_openclaw_config(output_path=output, merge_existing=False)

    data = json.loads(output.read_text())
    assert "nvhive" in data["mcpServers"]
    assert "other-tool" not in data["mcpServers"]  # overwritten
