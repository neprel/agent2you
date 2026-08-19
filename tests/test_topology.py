"""Brain topology follows the manifest: any chain length, litellm only when
there is a failover decision to make, acp2api only when a coding CLI exists,
and a bare endpoint brain runs neither."""

from __future__ import annotations

from pathlib import Path

import pytest

from a2y.manifest import ManifestError, load_fleet
from a2y.render import render_fleet
from a2y.scaffold import init_workspace


def make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "f"
    ws.mkdir()
    init_workspace(ws, name="f")
    return ws


def set_agent(ws: Path, name: str, body: str) -> None:
    d = ws / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.yaml").write_text(body)
    if not (d / "SOUL.md").is_file():
        (d / "SOUL.md").write_text(f"# {name}\n")


def test_default_two_brain_chain_keeps_litellm(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    render_fleet(load_fleet(ws))
    d = ws / "deploy" / "agents" / "ana"
    assert (d / "litellm.yaml").is_file() and (d / "acp2api.yaml").is_file()
    compose = (ws / "deploy" / "docker-compose.yaml").read_text()
    assert "LITELLM_PORT/health/liveliness" in compose


def test_five_brain_chain_renders_ordered_fallbacks(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    set_agent(ws, "ana", """\
name: ana
description: Five brains, ordered.
brains:
  chain: [codex, claude, opencode, qwen, local]
  executors:
    codex: {kind: codex}
    claude: {kind: claude, model: opus}
    opencode: {kind: opencode}
    qwen: {kind: custom, command: qwen-code, args: ["--acp"]}
    local: {kind: openai, base_url: "http://vllm:8000/v1", model: ai01, api_key_env: LOCAL_VLLM_KEY}
""")
    render_fleet(load_fleet(ws))
    d = ws / "deploy" / "agents" / "ana"
    lite = (d / "litellm.yaml").read_text()
    assert "fallbacks:" in lite
    for m in ("brain-claude", "brain-opencode", "brain-qwen", "brain-local"):
        assert m in lite
    assert "http://vllm:8000/v1" in lite
    assert "os.environ/LOCAL_VLLM_KEY" in lite
    # acp2api carries the four ACP executors and not the endpoint one.
    acp = (d / "acp2api.yaml").read_text()
    assert "qwen-code" in acp and "local" not in acp.split("agents:")[1]
    # The endpoint key is passed through compose and declared in example.env.
    compose = (ws / "deploy" / "docker-compose.yaml").read_text()
    assert "LOCAL_VLLM_KEY=${LOCAL_VLLM_KEY}" in compose
    assert "LOCAL_VLLM_KEY=" in (ws / "deploy" / "example.env").read_text()


def test_single_acp_brain_drops_litellm(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    set_agent(ws, "ana", """\
name: ana
description: One brain, no failover.
brains:
  chain: [claude]
  executors:
    claude: {kind: claude, model: opus}
""")
    render_fleet(load_fleet(ws))
    d = ws / "deploy" / "agents" / "ana"
    assert not (d / "litellm.yaml").exists()
    assert (d / "acp2api.yaml").is_file()
    cfg = (d / "config.yaml").read_text()
    assert "${ACP2API_BASE_URL}" in cfg and "LITELLM_BASE_URL" not in cfg
    assert "default: claude" in cfg
    compose = (ws / "deploy" / "docker-compose.yaml").read_text()
    assert "ACP2API_PORT/health" in compose
    assert "LITELLM_MASTER_KEY" not in compose
    assert "litellm" not in (d / "agent.yaml").read_text()


def test_endpoint_only_brain_runs_neither(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    set_agent(ws, "ana", """\
name: ana
description: Hermes straight at an endpoint.
brains:
  chain: [local]
  executors:
    local: {kind: openai, base_url: "http://vllm:8000/v1", model: ai01, api_key_env: LOCAL_VLLM_KEY}
""")
    render_fleet(load_fleet(ws))
    d = ws / "deploy" / "agents" / "ana"
    assert not (d / "litellm.yaml").exists()
    assert not (d / "acp2api.yaml").exists()
    cfg = (d / "config.yaml").read_text()
    assert "base_url: http://vllm:8000/v1" in cfg
    assert "default: ai01" in cfg
    assert "${LOCAL_VLLM_KEY}" in cfg
    compose = (ws / "deploy" / "docker-compose.yaml").read_text()
    assert "A2A_PORT/.well-known/agent-card.json" in compose


def test_litellm_off_with_long_chain_is_an_error(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    set_agent(ws, "ana", """\
name: ana
description: Broken topology.
brains:
  litellm: off
  chain: [claude, codex]
  executors:
    claude: {kind: claude}
    codex: {kind: codex}
""")
    with pytest.raises(ManifestError, match="litellm"):
        load_fleet(ws)


def test_openai_key_env_must_not_be_openai_api_key(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    set_agent(ws, "ana", """\
name: ana
description: Key collision.
brains:
  chain: [local]
  executors:
    local: {kind: openai, base_url: "http://x/v1", model: m, api_key_env: OPENAI_API_KEY}
""")
    with pytest.raises(ManifestError, match="OPENAI_API_KEY"):
        load_fleet(ws)
