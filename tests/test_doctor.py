from pathlib import Path

from a2y.doctor import DoctorOptions, check_deploy, parse_env
from a2y.manifest import load_fleet
from a2y.render import render_fleet
from a2y.scaffold import init_workspace


def test_env_parser_accepts_export_and_quotes():
    assert parse_env("export A=\"x\"\nB='y'\n") == {"A": "x", "B": "y"}


def test_deploy_check_is_read_only(tmp_path: Path):
    root = tmp_path / "f"
    root.mkdir()
    init_workspace(root, "f")
    fleet = load_fleet(root)
    render_fleet(fleet)
    compose = root / "deploy/docker-compose.yaml"
    compose.write_text(compose.read_text() + "# local drift\n")
    before = compose.read_bytes()
    level, _ = check_deploy(fleet, {}, DoctorOptions(offline=True))
    assert level == "problem"
    assert compose.read_bytes() == before
