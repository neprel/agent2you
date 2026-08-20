"""Host-side model store: explicit downloads, recorded revisions, offline load checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .doctor import parse_env
from .manifest import Fleet, ManifestError
from .render import ensure_volumes


class _TokenSafeRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a Hugging Face bearer token to a redirected storage host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(
            newurl
        ).netloc:
            redirected.remove_header("Authorization")
        return redirected


_OPENER = urllib.request.build_opener(_TokenSafeRedirect())


def _request(url: str, token: str = ""):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    return _OPENER.open(request, timeout=60)


def _download_model(model: dict, root: Path, token: str) -> dict:
    repo = str(model["repo"])
    encoded_repo = urllib.parse.quote(repo, safe="/")
    with _request(f"https://huggingface.co/api/models/{encoded_repo}?blobs=true", token) as response:
        metadata = json.loads(response.read())
    revision = str(metadata.get("sha") or "")
    if not revision:
        raise ManifestError(f"model registry returned no revision for {repo}")

    destination = root / str(model["path"])
    destination.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for relative in [str(item) for item in model["files"]]:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ManifestError(f"model {model['name']}: unsafe file path {relative!r}")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        url = (
            f"https://huggingface.co/{encoded_repo}/resolve/{revision}/"
            f"{urllib.parse.quote(relative, safe='/')}"
        )
        digest = hashlib.sha256()
        print(f"  downloading {model['name']}/{relative}")
        with _request(url, token) as response, target.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        hashes[relative] = digest.hexdigest()
    return {
        "name": str(model["name"]),
        "repo": repo,
        "revision": revision,
        "path": str(model["path"]),
        "sha256": hashes,
        "tier": str(model.get("tier") or "required"),
    }


def _image_for(fleet: Fleet, agent) -> str:
    root = os.environ.get("A2Y_IMAGE") or fleet.image_tag
    return f"{root}-{agent.name}" if agent.toolkits else root


def pull_models(fleet: Fleet, names: list[str]) -> int:
    selected = [agent for agent in fleet.agents if not names or agent.name in names]
    unknown = sorted(set(names) - {agent.name for agent in fleet.agents})
    if unknown:
        raise ManifestError(f"unknown agent(s): {', '.join(unknown)}")
    selected = [agent for agent in selected if agent.model_specs]
    if not selected:
        raise ManifestError("no selected agent carries a toolkit with models")

    for agent in selected:
        image = _image_for(fleet, agent)
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
            raise ManifestError(f"image {image} is absent; run `a2y build` before pulling models")

    ensure_volumes(fleet)
    store = fleet.root / "volumes" / "models"
    if store.is_symlink():
        raise ManifestError(f"model store {store} must be a real host directory, not a symlink")
    dotenv = fleet.root / "deploy" / ".env"
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token and dotenv.is_file():
        token = parse_env(dotenv.read_text()).get("HF_TOKEN", "").strip()

    specs = {}
    checks = {}
    for agent in selected:
        for model in agent.model_specs:
            specs[str(model["path"])] = model
        for toolkit in [*fleet.image_toolkits, *agent.toolkits]:
            spec = fleet.load_toolkit(toolkit)
            if spec.get("models"):
                checks[toolkit] = [str(part) for part in spec["model_check"]]

    tier = "community-1" if token and any(model.get("gated") for model in specs.values()) else "fallback"
    if tier == "fallback" and any(model.get("gated") for model in specs.values()):
        print(
            "INFO: models pull will use the valid ungated fallback diarization tier; "
            "for community-1 add HF_TOKEN (docs/provisioning.md, Voice notes)."
        )

    pulled_at = datetime.now(timezone.utc).isoformat()
    with tempfile.TemporaryDirectory(prefix=".pull-", dir=store.parent) as temporary:
        stage = Path(temporary)
        entries = []
        for model in specs.values():
            if model.get("gated") and not token:
                continue
            entry = _download_model(model, stage, token if model.get("gated") else "")
            entry["pulled_at"] = pulled_at
            entries.append(entry)
            (stage / str(model["path"]) / "manifest.json").write_text(
                json.dumps(entry, indent=2, sort_keys=True) + "\n"
            )
        stage_manifest = {
            "schema": 1,
            "pulled_at": pulled_at,
            "tier": tier,
            "models": entries,
        }
        (stage / "manifest.json").write_text(
            json.dumps(stage_manifest, indent=2, sort_keys=True) + "\n"
        )

        checked = set()
        for agent in selected:
            image = _image_for(fleet, agent)
            for toolkit in [*fleet.image_toolkits, *agent.toolkits]:
                command = checks.get(toolkit)
                key = (image, tuple(command or []))
                if not command or key in checked:
                    continue
                checked.add(key)
                probe = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "-e",
                        f"AGENT_NAME={agent.name}",
                        "-v",
                        f"{stage}:/models:ro",
                        "--entrypoint",
                        command[0],
                        image,
                        *command[1:],
                    ],
                    text=True,
                )
                if probe.returncode:
                    raise ManifestError(
                        f"downloaded models are incompatible with {image}; store was not changed"
                    )

        previous = {}
        manifest_path = store / "manifest.json"
        if manifest_path.is_file():
            try:
                previous = {
                    str(item["path"]): item
                    for item in (json.loads(manifest_path.read_text()).get("models") or [])
                }
            except (json.JSONDecodeError, KeyError, TypeError):
                previous = {}
        for model in specs.values():
            destination = store / str(model["path"])
            if destination.exists():
                shutil.rmtree(destination)
            staged = stage / str(model["path"])
            if staged.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged), destination)
            previous.pop(str(model["path"]), None)
        for entry in entries:
            previous[entry["path"]] = entry
        final_entries = [previous[key] for key in sorted(previous)]
        final_tier = (
            "community-1"
            if any(item.get("tier") == "community-1" for item in final_entries)
            else tier
        )
        final_manifest = {
            "schema": 1,
            "pulled_at": pulled_at,
            "tier": final_tier,
            "models": final_entries,
        }
        manifest_path.write_text(json.dumps(final_manifest, indent=2, sort_keys=True) + "\n")

    print(f"Models ready in {store}; tier={final_tier}; runtime access is read-only.")
    return 0


def cmd_models_pull(ns) -> int:
    from .manifest import load_fleet

    fleet = load_fleet(Path.cwd())
    if ns.agent and ns.agents:
        raise ManifestError("use either --agent or positional agent names, not both")
    names = ([ns.agent] if ns.agent else []) + list(ns.agents)
    return pull_models(fleet, names)
