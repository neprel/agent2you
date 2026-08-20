from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from a2y.cli import cmd_build
from a2y.doctor import DoctorOptions, check_version
from a2y.manifest import load_fleet
from a2y.render import render_fleet
from a2y.scaffold import init_workspace


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "f"
    root.mkdir()
    init_workspace(root, "f")
    toolkit = root / "toolkits" / "tiny"
    toolkit.mkdir()
    (toolkit / "toolkit.yaml").write_text("dockerfile: |\n  RUN true\n")
    agent = root / "agents" / "ana" / "agent.yaml"
    agent.write_text(agent.read_text() + "\ntoolkits: [tiny]\n")
    return root


@pytest.mark.parametrize(
    ("installed", "override", "expected", "note"),
    [
        ("2.3.4", None, "2.3.4", False),
        ("2.3.5.dev1", None, "1.5.0", True),
        ("2.3.5.dev1", "9.8.7", "9.8.7", False),
    ],
)
def test_build_injects_version_into_every_image(
    tmp_path: Path, monkeypatch, capsys, installed: str, override: str | None, expected: str, note: bool
) -> None:
    root = workspace(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr("a2y.cli._daemon_guard", lambda _: None)
    monkeypatch.setattr("a2y.cli.importlib.metadata.version", lambda _: installed)
    calls = []
    monkeypatch.setattr("a2y.cli.subprocess.call", lambda cmd: calls.append(cmd) or 0)
    ns = argparse.Namespace(
        no_cache=False, parallel=1, i_know_my_mounts=False, a2y_version=override
    )

    assert cmd_build(ns) == 0
    assert len(calls) == 2  # base plus the per-agent derived image
    assert all(
        ["--build-arg", f"AGENT2YOU_VERSION={expected}"]
        == cmd[cmd.index("--build-arg") : cmd.index("--build-arg") + 2]
        for cmd in calls
    )
    assert ("NOTE: running a2y" in capsys.readouterr().err) is note


def test_doctor_flags_mislabeled_image(tmp_path: Path, monkeypatch) -> None:
    root = workspace(tmp_path)
    fleet = load_fleet(root)
    render_fleet(fleet)
    (root / ".a2y-version").write_text("1.5.0\n")

    class Probe:
        returncode = 0
        stdout = "1.3.0\n"

    monkeypatch.setattr("a2y.doctor.importlib.metadata.version", lambda _: "1.5.0")
    monkeypatch.setattr("a2y.doctor.subprocess.run", lambda *args, **kwargs: Probe())
    level, message = check_version(fleet, {"A2Y_IMAGE": "agent2you/f:0.1.0"}, DoctorOptions())
    assert level == "problem"
    assert "label=1.3.0" in message and "a2y build" in message


def test_build_never_passes_model_token(tmp_path: Path, monkeypatch) -> None:
    root = workspace(tmp_path)
    agent = root / "agents" / "ana" / "agent.yaml"
    agent.write_text(agent.read_text().replace("toolkits: [tiny]", "toolkits: [transcribe]"))
    monkeypatch.chdir(root)
    monkeypatch.setattr("a2y.cli._daemon_guard", lambda _: None)
    monkeypatch.setattr("a2y.cli.importlib.metadata.version", lambda _: "1.5.0")
    token = "hf_test_secret"
    monkeypatch.setenv("HF_TOKEN", token)
    calls = []

    def record(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return 0

    monkeypatch.setattr("a2y.cli.subprocess.call", record)
    ns = argparse.Namespace(no_cache=False, parallel=1, i_know_my_mounts=False, a2y_version=None)
    assert cmd_build(ns) == 0

    command, kwargs = calls[-1]
    assert token not in " ".join(command)
    assert "HF_TOKEN" not in " ".join(command)
    assert "--secret" not in command
    assert kwargs == {}
