"""Toolkits: install recipe and usage instructions travel together, derived
images are generated deterministically, and a missing toolkit fails loudly."""

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
