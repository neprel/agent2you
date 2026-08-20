import json
from pathlib import Path

import pytest
import yaml

from a2y import outdated
from a2y.drill import evaluate, run_drills
from a2y.manifest import ManifestError, load_fleet
from a2y.render import render_fleet
from a2y.rotate import rotate_litellm
from a2y.scaffold import init_workspace


def ws(tmp_path: Path) -> Path:
    root = tmp_path / "fleet"
    root.mkdir()
    init_workspace(root, "fleet")
    return root


def test_duties_render_and_validation(tmp_path: Path):
    root = ws(tmp_path)
    agent = root / "agents/ana/agent.yaml"
    agent.write_text(
        agent.read_text()
        + """
duties:
  - name: metrics-round
    schedule: "0 */4 * * *"
    channel: ops
    instruction: Check every dashboard.
    quiet: true
"""
    )
    render_fleet(load_fleet(root))
    duties = yaml.safe_load((root / "deploy/agents/ana/duties.yaml").read_text().split("\n", 1)[1])
    assert duties["duties"][0]["deliver"] == "mattermost:ops"
    assert duties["duties"][0]["prompt"].endswith("[SILENT].")
    agent.write_text(agent.read_text().replace('"0 */4 * * *"', '"0 9 * * mon-fri"'))
    with pytest.raises(ManifestError, match="numeric five-field"):
        load_fleet(root)


def test_fleet_workflows_are_pack_owned(tmp_path: Path):
    root = ws(tmp_path)
    github = root / ".github/workflows/a2y-fleet.yml"
    gitea = root / ".gitea/workflows/a2y-fleet.yml"
    assert github.is_file() and gitea.is_file()
    text = github.read_text()
    assert ".a2y-version" in text and "doctor --offline" in text
    state = json.loads((root / ".a2y-upgrade.json").read_text())
    assert ".github/workflows/a2y-fleet.yml" in state["files"]


def test_drill_pass_and_deliberate_failure(tmp_path: Path, capsys):
    root = ws(tmp_path)
    suite = root / "drills/ana"
    suite.mkdir(parents=True)
    (suite / "pass.yaml").write_text("probe: delegate\nexpect: {mentions: [dev], answers: true}\n")
    (suite / "fail.yaml").write_text("probe: stay silent\nexpect: {silent: true}\n")
    passed, total = run_drills(
        load_fleet(root),
        "ana",
        exchange=lambda _fleet, probe: "I delegate to @dev" if probe == "delegate" else "unexpected",
    )
    assert (passed, total) == (1, 2)
    assert "About to spend 2 real turn(s)" in capsys.readouterr().out
    assert evaluate("I cannot do that; @dev can", {"refuses": True, "mentions": ["dev"]}) == []


def test_rotate_internal_changes_only_litellm_keys(tmp_path: Path):
    root = ws(tmp_path)
    render_fleet(load_fleet(root))
    env = root / "deploy/.env"
    env.write_text(
        (root / "deploy/example.env")
        .read_text()
        .replace("AGENT_ANA_LITELLM_MASTER_KEY=", "AGENT_ANA_LITELLM_MASTER_KEY=old")
    )
    fleet = load_fleet(root)
    assert rotate_litellm(fleet, ["ana"], recreate=False) == 0
    text = env.read_text()
    assert "AGENT_ANA_LITELLM_MASTER_KEY=sk-a2y-" in text and "=old" not in text
    assert env.stat().st_mode & 0o777 == 0o600


def test_outdated_reports_seeded_stale_pin(tmp_path: Path, monkeypatch):
    root = ws(tmp_path)
    dockerfile = root / "image/agent.dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace("ARG FASTAPI_VERSION=0.135.0", "ARG FASTAPI_VERSION=0.1.0")
    )
    monkeypatch.setattr(outdated, "_latest", lambda _kind, name: "9.0.0" if name == "fastapi" else "1.4.0")
    report = outdated.collect(load_fleet(root))
    fastapi = next(item for item in report["updates"] if item["name"] == "FASTAPI_VERSION")
    assert fastapi["outdated"] is True and report["network_used"] is True
