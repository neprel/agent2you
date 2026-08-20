"""Toolkits: install recipe and usage instructions travel together, derived
images are generated deterministically, and a missing toolkit fails loudly."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from a2y.manifest import ManifestError, load_fleet
from a2y.render import render_fleet
from a2y.scaffold import init_workspace


def make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "f"
    ws.mkdir()
    init_workspace(ws, name="f")
    return ws


def add_toolkit(ws: Path, name: str = "go") -> None:
    d = ws / "toolkits" / name
    d.mkdir(parents=True)
    (d / "toolkit.yaml").write_text(
        "description: Go toolchain\n"
        "apt: [golang-1.26]\n"
        "npm: [\"some-linter@1.2.3\"]\n"
        "uv_tools: [\"some-tool==2.0\"]\n"
        "env: {GOFLAGS: -mod=readonly}\n"
        "dockerfile: |\n"
        "  RUN echo custom-step\n"
    )
    (d / "USAGE.md").write_text("Use `go test ./...` before reporting a build green.\n")


def test_agent_toolkit_renders_derived_image_and_usage(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    add_toolkit(ws)
    ana = ws / "agents" / "ana" / "agent.yaml"
    ana.write_text(ana.read_text() + "\ntoolkits: [go]\n")

    render_fleet(load_fleet(ws))

    df = (ws / "deploy" / "build" / "agent-ana.dockerfile").read_text()
    assert "FROM agent2you/f:0.1.0\n" in df
    assert "golang-1.26" in df and "some-linter@1.2.3" in df
    assert "ENV GOFLAGS=-mod=readonly" in df
    assert "RUN echo custom-step" in df

    compose = (ws / "deploy" / "docker-compose.yaml").read_text()
    assert "image: ${A2Y_IMAGE}-ana" in compose

    soul = (ws / "deploy" / "agents" / "ana" / "SOUL.md").read_text()
    assert "## Toolkit: go" in soul
    assert "go test ./..." in soul


def test_fleet_toolkit_goes_into_fleet_image(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    add_toolkit(ws, "hintless")
    fleet_yaml = ws / "fleet.yaml"
    fleet_yaml.write_text(
        fleet_yaml.read_text().replace(
            "  tag: agent2you/f:0.1.0",
            "  tag: agent2you/f:0.1.0\n  toolkits: [hintless]",
        )
    )
    render_fleet(load_fleet(ws))
    df = (ws / "deploy" / "build" / "fleet.dockerfile").read_text()
    assert "FROM agent2you/f:0.1.0-base\n" in df
    # Plain agents keep the fleet tag -- the fleet image took it over.
    compose = (ws / "deploy" / "docker-compose.yaml").read_text()
    assert "image: ${A2Y_IMAGE}\n" in compose
    # Every agent gets the fleet toolkit's usage.
    soul = (ws / "deploy" / "agents" / "ana" / "SOUL.md").read_text()
    assert "## Toolkit: hintless" in soul


def test_missing_toolkit_is_loud(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    ana = ws / "agents" / "ana" / "agent.yaml"
    ana.write_text(ana.read_text() + "\ntoolkits: [ghost]\n")
    with pytest.raises(ManifestError, match="ghost"):
        load_fleet(ws)


def test_browser_is_agent_level_and_renders_protected_novnc(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    agent = ws / "agents" / "ana" / "agent.yaml"
    agent.write_text(agent.read_text() + "\ntoolkits: [browser]\nbrowser: {novnc: true}\n")
    render_fleet(load_fleet(ws))

    derived = (ws / "deploy/build/agent-ana.dockerfile").read_text()
    assert "@playwright/mcp@0.0.79" in derived
    assert "chromium-1237" in derived and "sha256sum -c" in derived
    acp_config = (ws / "deploy/agents/ana/acp2api.yaml").read_text()
    assert "a2y-browser-mcp" in acp_config
    compose = (ws / "deploy/docker-compose.yaml").read_text()
    assert "browser-ana:" in compose
    assert "127.0.0.1:${AGENT_ANA_BROWSER_NOVNC_PORT}:6080" in compose
    assert "AGENT_ANA_BROWSER_NOVNC_PASSWORD" in compose
    assert "../volumes/agent-ana/browser:/browser" in compose
    sidecar = yaml.safe_load(compose)["services"]["browser-ana"]
    assert sidecar["entrypoint"][-1] == "/usr/local/bin/a2y-browser-novnc"
    assert "command" not in sidecar
    example = (ws / "deploy/example.env").read_text()
    assert "AGENT_ANA_BROWSER_NOVNC_PORT=6080" in example
    assert "AGENT_ANA_BROWSER_NOVNC_PASSWORD=" in example
    soul = (ws / "deploy/agents/ana/SOUL.md").read_text()
    assert "consequential submit" in soul and "anti-bot" in soul


def test_browser_cannot_enter_fleet_base(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    fleet = ws / "fleet.yaml"
    fleet.write_text(
        fleet.read_text().replace(
            "  tag: agent2you/f:0.1.0",
            "  tag: agent2you/f:0.1.0\n  toolkits: [browser]",
        )
    )
    with pytest.raises(ManifestError, match="agent-level"):
        load_fleet(ws)


def test_transcribe_toolkit_is_offline_pinned_and_instructed(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    agent = ws / "agents" / "ana" / "agent.yaml"
    agent.write_text(agent.read_text() + "\ntoolkits: [transcribe]\n")
    render_fleet(load_fleet(ws))

    derived = (ws / "deploy/build/agent-ana.dockerfile").read_text()
    assert '"whisperx==3.8.6"' in derived
    assert '"faster-whisper==1.2.1"' in derived
    assert '"torch==2.8.0"' in derived and '"pyannote-audio==4.0.7"' in derived
    assert "snapshot_download" not in derived and "HF_TOKEN" not in derived
    assert "model weights live only" in derived.lower()
    compose = (ws / "deploy/docker-compose.yaml").read_text()
    assert "../volumes/models:/models:ro" in compose
    assert "HF_TOKEN" not in compose
    soul = (ws / "deploy/agents/ana/SOUL.md").read_text()
    assert "{{A2Y_DIARIZATION_TIER}}" in soul
    assert "do not download anything" in soul
    assert "Reply as a THREAD on the original file post" in soul
    assert "speaker labels may be unreliable" in soul.lower()
    assert "20 MB" in soul
    example = (ws / "deploy/example.env").read_text()
    assert "HF_TOKEN=" in example and "models pull" in example


def test_transcribe_cannot_enter_fleet_base(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    fleet = ws / "fleet.yaml"
    fleet.write_text(
        fleet.read_text().replace(
            "  tag: agent2you/f:0.1.0",
            "  tag: agent2you/f:0.1.0\n  toolkits: [transcribe]",
        )
    )
    with pytest.raises(ManifestError, match="agent-level"):
        load_fleet(ws)


@pytest.mark.parametrize("model_path", ["../../outside", "/host/path"])
def test_model_store_rejects_paths_outside_store(tmp_path: Path, model_path: str) -> None:
    ws = make_ws(tmp_path)
    toolkit = ws / "toolkits" / "unsafe"
    toolkit.mkdir()
    (toolkit / "toolkit.yaml").write_text(
        "models:\n"
        "  - name: unsafe\n"
        "    repo: example/model\n"
        f"    path: {model_path}\n"
        "    files: [model.bin]\n"
        "model_check: [true]\n"
    )
    agent = ws / "agents" / "ana" / "agent.yaml"
    agent.write_text(agent.read_text() + "\ntoolkits: [unsafe]\n")
    with pytest.raises(ManifestError, match="unsafe path"):
        load_fleet(ws)


def test_runtime_soul_names_built_diarization_tier(tmp_path: Path) -> None:
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    source = tmp_path / "SOUL.md"
    target = tmp_path / "runtime-SOUL.md"
    source.write_text("Diarization: {{A2Y_DIARIZATION_TIER}}\n")
    env = dict(os.environ, A2Y_DIARIZATION_TIER="community-1", A2Y_ROSTER_MODE="off")
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "a2y/image/fleet-roster.py"),
            str(fleet),
            str(source),
            str(target),
            "ana",
        ],
        env=env,
        check=True,
    )
    assert target.read_text() == "Diarization: community-1\n"


def test_voice_renders_local_stt_tts_and_cloud_key(tmp_path: Path) -> None:
    ws = make_ws(tmp_path)
    agent = ws / "agents" / "ana" / "agent.yaml"
    agent.write_text(
        agent.read_text()
        + "\nvoice:\n  enabled: true\n  provider: openai\n  language: ru\n"
        + "  model: gpt-4o-mini-transcribe\n  tts: true\n"
    )
    render_fleet(load_fleet(ws))

    config = (ws / "deploy/agents/ana/config.yaml").read_text()
    assert "provider: openai" in config
    assert "language: ru" in config
    assert "gpt-4o-mini-transcribe" in config
    assert "provider: edge" in config
    assert "auto_tts: true" in config
    compose = (ws / "deploy/docker-compose.yaml").read_text()
    assert "VOICE_TOOLS_OPENAI_KEY=${VOICE_TOOLS_OPENAI_KEY}" in compose
    example = (ws / "deploy/example.env").read_text()
    assert example.count("VOICE_TOOLS_OPENAI_KEY=") == 1


@pytest.mark.parametrize(
    ("voice", "message"),
    [
        ("voice: yes\n", "voice must be a mapping"),
        ("voice: {provider: mystery}\n", "voice.provider"),
        ('voice: {tts: "yes"}\n', "voice.tts"),
    ],
)
def test_invalid_voice_is_loud(tmp_path: Path, voice: str, message: str) -> None:
    ws = make_ws(tmp_path)
    agent = ws / "agents" / "ana" / "agent.yaml"
    agent.write_text(agent.read_text() + "\n" + voice)
    with pytest.raises(ManifestError, match=message):
        load_fleet(ws)
