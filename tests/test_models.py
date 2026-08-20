from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

import pytest

from a2y.cli import main
from a2y.doctor import DoctorOptions, check_models
from a2y.manifest import ManifestError, load_fleet
from a2y.models import _TokenSafeRedirect, pull_models
from a2y.render import render_fleet
from a2y.scaffold import init_workspace


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "f"
    root.mkdir()
    init_workspace(root, "f")
    agent = root / "agents" / "ana" / "agent.yaml"
    agent.write_text(agent.read_text() + "\ntoolkits: [transcribe]\n")
    render_fleet(load_fleet(root))
    return root


class Probe:
    def __init__(self, returncode=0):
        self.returncode = returncode


@pytest.mark.parametrize(("token", "tier", "count"), [("", "fallback", 1), ("secret", "community-1", 2)])
def test_models_pull_records_store_and_never_records_token(
    tmp_path: Path, monkeypatch, capsys, token: str, tier: str, count: int
) -> None:
    root = workspace(tmp_path)
    fleet = load_fleet(root)
    if token:
        monkeypatch.setenv("HF_TOKEN", token)
    else:
        monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr("a2y.models.subprocess.run", lambda *args, **kwargs: Probe())

    def fake_download(model, stage, passed_token):
        destination = stage / model["path"]
        destination.mkdir(parents=True)
        relative = model["files"][0]
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(model["name"].encode())
        return {
            "name": model["name"],
            "repo": model["repo"],
            "revision": "abc123",
            "path": model["path"],
            "sha256": {relative: hashlib.sha256(target.read_bytes()).hexdigest()},
            "tier": model.get("tier", "required"),
        }

    monkeypatch.setattr("a2y.models._download_model", fake_download)
    assert pull_models(fleet, ["ana"]) == 0

    store = root / "volumes/models"
    manifest_text = (store / "manifest.json").read_text()
    manifest = json.loads(manifest_text)
    assert manifest["tier"] == tier
    assert len(manifest["models"]) == count
    if token:
        assert token not in manifest_text
    compose = (root / "deploy/docker-compose.yaml").read_text()
    assert "../volumes/models:/models:ro" in compose
    for item in manifest["models"]:
        per_model = json.loads((store / item["path"] / "manifest.json").read_text())
        assert per_model["revision"] == "abc123" and per_model["pulled_at"]
    if not token:
        assert "valid ungated fallback" in capsys.readouterr().out


def test_models_pull_rejects_incompatible_download_without_changing_store(
    tmp_path: Path, monkeypatch
) -> None:
    root = workspace(tmp_path)
    fleet = load_fleet(root)
    monkeypatch.delenv("HF_TOKEN", raising=False)

    def fake_run(command, **_kwargs):
        return Probe(0 if command[1:3] == ["image", "inspect"] else 1)

    monkeypatch.setattr("a2y.models.subprocess.run", fake_run)

    def fake_download(model, stage, _token):
        destination = stage / model["path"]
        destination.mkdir(parents=True)
        (destination / "model.bin").write_bytes(b"incompatible")
        return {
            "name": model["name"], "repo": model["repo"], "revision": "bad",
            "path": model["path"], "sha256": {}, "tier": "required",
        }

    monkeypatch.setattr("a2y.models._download_model", fake_download)
    with pytest.raises(ManifestError, match="incompatible"):
        pull_models(fleet, ["ana"])
    assert not (root / "volumes/models/manifest.json").exists()


def test_doctor_displays_model_revision_tier_and_checksum(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    fleet = load_fleet(root)
    store = root / "volumes/models/transcribe/whisper-large-v3-turbo"
    store.mkdir(parents=True)
    payload = b"model"
    (store / "model.bin").write_bytes(payload)
    manifest = {
        "tier": "fallback",
        "pulled_at": "2026-08-20T10:00:00+00:00",
        "models": [{
            "name": "whisper-large-v3-turbo",
            "revision": "abcdef1234567890",
            "path": "transcribe/whisper-large-v3-turbo",
            "sha256": {"model.bin": hashlib.sha256(payload).hexdigest()},
        }],
    }
    (root / "volumes/models/manifest.json").write_text(json.dumps(manifest))
    level, message = check_models(fleet, {}, DoctorOptions(offline=True))
    assert level == "ok"
    assert "tier=fallback" in message and "abcdef123456" in message and "checksums match" in message


@pytest.mark.parametrize(
    ("arguments", "agent", "agents"),
    [(["--agent", "ana"], "ana", []), (["ana", "bob"], None, ["ana", "bob"])],
)
def test_models_cli_forwards_agent_selection(
    monkeypatch, arguments: list[str], agent: str | None, agents: list[str]
) -> None:
    seen = {}

    def fake_pull(ns):
        seen.update(agent=ns.agent, agents=ns.agents)
        return 0

    monkeypatch.setattr("a2y.models.cmd_models_pull", fake_pull)
    assert main(["models", "pull", *arguments]) == 0
    assert seen == {"agent": agent, "agents": agents}


def test_model_token_is_stripped_on_cross_origin_redirect() -> None:
    original = urllib.request.Request("https://huggingface.co/model/resolve/rev/model.bin")
    original.add_header("Authorization", "Bearer hf_secret")
    handler = _TokenSafeRedirect()
    redirected = handler.redirect_request(
        original,
        None,
        302,
        "Found",
        {},
        "https://cdn.example.invalid/signed-model.bin",
    )
    assert redirected is not None
    assert redirected.get_header("Authorization") is None


def test_models_pull_rejects_symlinked_store(tmp_path: Path, monkeypatch) -> None:
    root = workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    store = root / "volumes/models"
    store.parent.mkdir()
    store.symlink_to(outside, target_is_directory=True)
    fleet = load_fleet(root)
    monkeypatch.setattr("a2y.models.subprocess.run", lambda *args, **kwargs: Probe())
    with pytest.raises(ManifestError, match="not a symlink"):
        pull_models(fleet, ["ana"])
