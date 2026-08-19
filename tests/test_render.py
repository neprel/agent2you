"""The pack's contract: rendering is deterministic, validation is loud, and the
compose file never references a variable example.env does not declare."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from a2y.manifest import ManifestError, load_fleet
from a2y.render import render_fleet
from a2y.scaffold import init_workspace


def make_workspace(tmp_path: Path, name: str = "demo") -> Path:
    ws = tmp_path / name
    ws.mkdir()
    init_workspace(ws, name=name, first_agent="ana")
    return ws


def read_tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_render_is_deterministic(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    render_fleet(load_fleet(ws))
    first = read_tree(ws / "deploy")
    assert first, "render produced nothing"
    # Second render over an existing tree: no changes reported, same bytes.
    changed = render_fleet(load_fleet(ws))
    assert changed == []
    assert read_tree(ws / "deploy") == first


def test_env_parity_invariant(tmp_path: Path) -> None:
    """Every ${VAR} the compose file references is declared in example.env."""
    ws = make_workspace(tmp_path)
    # Turn every optional feature on so the widest variable set is exercised.
    fleet_yaml = ws / "fleet.yaml"
    text = fleet_yaml.read_text()
    text = text.replace(
        "  kind: none\n  # kind: hindsight\n  # url: http://hindsight:8888",
        "  kind: hindsight\n  url: http://hindsight:8888",
    )
    text = text.replace(
        "  # phoenix_url: http://phoenix:6006   # traces: one project per agent",
        "  phoenix_url: http://phoenix:6006",
    )
    fleet_yaml.write_text(text)
    dev = ws / "agents" / "dev"
    dev.mkdir()
    (dev / "agent.yaml").write_text(
        "name: dev\ndescription: Engineer owning the demo repository.\n"
        "access: {ssh: true, github_token: true}\n"
    )
    (dev / "SOUL.md").write_text("# dev\n")

    render_fleet(load_fleet(ws))
    compose = (ws / "deploy" / "docker-compose.yaml").read_text()
    example = (ws / "deploy" / "example.env").read_text()
    declared = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", example, re.M))
    referenced = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", compose))
    missing = referenced - declared
    assert not missing, f"compose references undeclared variables: {sorted(missing)}"


def test_validation_is_loud(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)

    # Missing description.
    bad = ws / "agents" / "bad"
    bad.mkdir()
    (bad / "agent.yaml").write_text("name: bad\n")
    (bad / "SOUL.md").write_text("# bad\n")
    with pytest.raises(ManifestError, match="description"):
        load_fleet(ws)
    (bad / "agent.yaml").write_text("name: bad\ndescription: Something.\n")

    # Unknown executor kind.
    (bad / "agent.yaml").write_text(
        "name: bad\ndescription: Something.\n"
        "brains: {chain: [x], executors: {x: {kind: nonsense}}}\n"
    )
    with pytest.raises(ManifestError, match="unknown kind"):
        load_fleet(ws)

    # Shared namespace without port bases.
    (bad / "agent.yaml").write_text("name: bad\ndescription: Something.\n")
    fleet_yaml = ws / "fleet.yaml"
    fleet_yaml.write_text(
        fleet_yaml.read_text().replace("  mode: bridge", "  mode: container:vpn")
    )
    with pytest.raises(ManifestError, match="ports"):
        load_fleet(ws)


def test_overrides_replace_generated_files(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    override_dir = ws / "agents" / "ana" / "overrides"
    override_dir.mkdir()
    (override_dir / "litellm.yaml").write_text("# hand-written\n")
    render_fleet(load_fleet(ws))
    assert (ws / "deploy" / "agents" / "ana" / "litellm.yaml").read_text() == "# hand-written\n"


def test_shared_namespace_ports(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path)
    fleet_yaml = ws / "fleet.yaml"
    fleet_yaml.write_text(
        fleet_yaml.read_text().replace("  mode: bridge", "  mode: container:vpn\n  iface: wg0")
    )
    ana = ws / "agents" / "ana" / "agent.yaml"
    ana.write_text(ana.read_text() + "\nports:\n  base: 10030\n")
    fleet = load_fleet(ws)
    render_fleet(fleet)
    agent_yaml = (ws / "deploy" / "agents" / "ana" / "agent.yaml").read_text()
    assert "acp2api: 10031" in agent_yaml
    compose = (ws / "deploy" / "docker-compose.yaml").read_text()
    assert "network_mode: container:vpn" in compose
    assert "AGENT_VPN_IFACE=wg0" in compose
    # network_mode and networks are mutually exclusive; render must emit neither
    # a networks: key on the service nor a top-level networks: block.
    assert "networks:" not in compose


def test_bootstrap_ships_and_prints(capsys) -> None:
    from a2y.cli import main

    assert main(["bootstrap"]) == 0
    out = capsys.readouterr().out
    assert "# agent2you bootstrap" in out
    assert "supervisor" in out
