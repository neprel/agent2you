"""`a2y agent add` is the surface another agent drives; its contract is that a
good call creates a complete, rendered agent and a bad call leaves nothing."""

from __future__ import annotations

import os
from pathlib import Path

from a2y.cli import main
from a2y.scaffold import init_workspace


def run(ws: Path, *argv: str) -> int:
    old = os.getcwd()
    os.chdir(ws)
    try:
        return main(list(argv))
    finally:
        os.chdir(old)


def test_agent_add_creates_and_renders(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "f"
    ws.mkdir()
    init_workspace(ws, name="f")
    rc = run(
        ws, "agent", "add", "pm",
        "--description", "Project manager: specs, tasks, sequencing.",
        "--github-token", "--projects", "demo",
    )
    assert rc == 0
    assert (ws / "agents" / "pm" / "agent.yaml").is_file()
    assert (ws / "agents" / "pm" / "SOUL.md").is_file()
    assert (ws / "deploy" / "agents" / "pm" / "config.yaml").is_file()
    out = capsys.readouterr().out
    assert "AGENT_PM_LITELLM_MASTER_KEY" in out
    assert "AGENT_PM_GH_TOKEN" in out
    # example.env picked the new agent up.
    assert "AGENT_PM_MATTERMOST_TOKEN" in (ws / "deploy" / "example.env").read_text()


def test_agent_add_rolls_back_on_invalid(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "f"
    ws.mkdir()
    init_workspace(ws, name="f")
    rc = run(
        ws, "agent", "add", "bad",
        "--description", "Broken chain.",
        "--chain", "claude,ghost",
    )
    assert rc == 2
    assert not (ws / "agents" / "bad").exists()


def test_agent_add_requires_description(tmp_path: Path) -> None:
    ws = tmp_path / "f"
    ws.mkdir()
    init_workspace(ws, name="f")
    assert run(ws, "agent", "add", "nameless") == 2
    assert not (ws / "agents" / "nameless").exists()
