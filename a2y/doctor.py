"""`a2y doctor` -- say what is wrong before a container has to."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .manifest import Fleet
from . import render as R


def _env_names(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.M)]


def run_doctor(fleet: Fleet) -> int:
    problems = 0

    def warn(msg: str) -> None:
        nonlocal problems
        problems += 1
        print(f"  ✗ {msg}")

    def ok(msg: str) -> None:
        print(f"  ✓ {msg}")

    deploy = fleet.root / "deploy"

    # 1. deploy tree freshness -- render into memory and compare.
    print("deploy tree")
    if not deploy.is_dir():
        warn("deploy/ does not exist -- run `a2y render`")
    else:
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            changed = R.render_fleet(fleet)
        if changed:
            warn(f"deploy/ was stale; render just refreshed: {', '.join(changed)}")
        else:
            ok("deploy/ matches the manifests")

    # 2. .env parity -- the invariant that a missing variable can break an
    #    UNRELATED service's compose render.
    print("environment")
    example = deploy / "example.env"
    dotenv = deploy / ".env"
    if not dotenv.is_file():
        warn("deploy/.env is missing -- `cp deploy/example.env deploy/.env` and fill it")
    elif example.is_file():
        have = dict.fromkeys(_env_names(dotenv.read_text()))
        missing = [n for n in _env_names(example.read_text()) if n not in have]
        if missing:
            warn(f".env is missing variable(s) from example.env: {', '.join(missing)}")
        else:
            ok(".env carries every variable example.env declares")
        empty = [
            line.split("=", 1)[0]
            for line in dotenv.read_text().splitlines()
            if re.match(r"^[A-Z][A-Z0-9_]*=$", line.strip())
        ]
        if empty:
            print(f"  · empty (fill or confirm deliberate): {', '.join(empty)}")

    # 3. docker
    print("docker")
    if not shutil.which("docker"):
        warn("docker is not on PATH")
    else:
        ok("docker present")
        if dotenv.is_file():
            proc = subprocess.run(
                ["docker", "compose", "-f", str(deploy / "docker-compose.yaml"),
                 "--env-file", str(dotenv), "config", "-q"],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                warn(f"compose config does not render:\n{proc.stderr.strip()}")
            else:
                ok("docker compose config renders")

    # 4. volumes
    print("volumes")
    missing_dirs = [
        str(Path("volumes") / a.container)
        for a in fleet.agents
        if not (fleet.root / "volumes" / a.container).is_dir()
    ]
    if missing_dirs:
        print(f"  · not created yet (a2y up will): {', '.join(missing_dirs)}")
    else:
        ok("state directories exist")

    # 5. logins -- the one step nobody can automate.
    print("brains (sign-in state is per volume; empty means not signed in yet)")
    for a in fleet.agents:
        base = fleet.root / "volumes" / a.container
        for ex in a.chain:
            kind = a.executors[ex].get("kind") or ex
            marker = {
                "claude": base / "claude" / ".credentials.json",
                "codex": base / "codex" / "auth.json",
            }.get(kind)
            if marker is None:
                continue
            state = "signed in" if marker.is_file() else "NOT signed in (a2y auth)"
            print(f"  · {a.name}/{ex}: {state}")

    print()
    if problems:
        print(f"{problems} problem(s).")
        return 1
    print("No problems found.")
    return 0
