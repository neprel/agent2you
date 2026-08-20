"""Secret rotation with an explicit boundary at provider-owned credentials."""

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path

from .doctor import run_doctor
from .manifest import Fleet, ManifestError


def _replace_env(path: Path, replacements: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.is_file() else []
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in replacements:
            out.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in replacements.items() if key not in seen)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)


def rotate_litellm(fleet: Fleet, names: list[str], *, recreate: bool = True) -> int:
    selected = [a for a in fleet.agents if not names or a.name in names]
    missing = sorted(set(names) - {a.name for a in selected})
    if missing:
        raise ManifestError("unknown agent(s): " + ", ".join(missing))
    changes = {
        f"{a.env_prefix}_LITELLM_MASTER_KEY": "sk-a2y-" + secrets.token_urlsafe(32)
        for a in selected
        if a.litellm_enabled
    }
    _replace_env(fleet.root / "deploy/.env", changes)
    print("rotated internal LiteLLM keys: " + ", ".join(changes))
    if recreate and selected:
        cmd = ["docker", "compose", "-f", str(fleet.root / "deploy/docker-compose.yaml"), "up", "-d"]
        if (fleet.root / "deploy/.env").is_file():
            cmd[4:4] = ["--env-file", str(fleet.root / "deploy/.env")]
        cmd += [a.container for a in selected]
        if subprocess.call(cmd):
            return 1
        for agent in selected:
            if not agent.litellm_enabled:
                continue
            key = changes[f"{agent.env_prefix}_LITELLM_MASTER_KEY"]
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    agent.container,
                    "curl",
                    "-fsS",
                    "-H",
                    f"Authorization: Bearer {key}",
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    '{"model":"brain","messages":[{"role":"user","content":"Reply only ROTATION_OK"}]}',
                    f"http://127.0.0.1:{agent.ports()['litellm']}/v1/chat/completions",
                ],
                capture_output=True,
                text=True,
            )
            if probe.returncode or "ROTATION_OK" not in probe.stdout:
                print(f"rotation probe failed for {agent.name}: {probe.stderr or probe.stdout}")
                return 1
            print(f"  {agent.name}: authenticated LiteLLM probe turn passed")
        return run_doctor(fleet)
    return 0


def cmd_rotate(ns, fleet: Fleet) -> int:
    if ns.all_internal or ns.rotation_class == "litellm-keys":
        return rotate_litellm(fleet, ns.agents, recreate=not ns.no_recreate)
    if not ns.rotation_class:
        raise ManifestError("choose a rotation class or pass --all-internal")
    if not ns.agents:
        raise ManifestError(f"rotate {ns.rotation_class} needs an agent")
    agent = next((a for a in fleet.agents if a.name == ns.agents[0]), None)
    if not agent:
        raise ManifestError(f"unknown agent {ns.agents[0]!r}")
    platform_key = {
        "mattermost": f"{agent.env_prefix}_MATTERMOST_TOKEN",
        "telegram": f"{agent.env_prefix}_TELEGRAM_BOT_TOKEN",
        "slack": f"{agent.env_prefix}_SLACK_BOT_TOKEN and {agent.env_prefix}_SLACK_APP_TOKEN",
        "discord": f"{agent.env_prefix}_DISCORD_BOT_TOKEN",
        "teams": "A2Y_TEAMS_CLIENT_SECRET",
    }.get(fleet.platform_kind, "the platform credential")
    key = {
        "platform-token": platform_key,
        "github-token": f"{agent.env_prefix}_GH_TOKEN",
        "ssh-key": f"volumes/{agent.container}/ssh/id_ed25519",
    }[ns.rotation_class]
    where = (
        "the platform admin console"
        if ns.rotation_class == "platform-token"
        else "GitHub settings"
        if ns.rotation_class == "github-token"
        else "every forge deploy-key page"
    )
    print(f"HUMAN STEP REQUIRED: revoke the old {ns.rotation_class} for {agent.name} in {where}.")
    print(f"Create the replacement there, then update {key} in deploy/.env and run `a2y up {agent.name}`.")
    print("Not rotated: provider-owned credential (agent2you never invents or copies external secrets).")
    return 2
