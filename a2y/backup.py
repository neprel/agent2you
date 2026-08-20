"""Credential-bearing state archives for fleet agents."""

from __future__ import annotations

import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from .manifest import Fleet, ManifestError


def _archive_name(fleet: Fleet, agent: str, out: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return out / f"{fleet.name}-{agent}-{stamp}.tar.gz"


def create_backup(fleet: Fleet, agent: str, out: Path, *, include_work: bool = False) -> Path:
    source = fleet.root / "volumes" / f"agent-{agent}"
    if not source.is_dir():
        raise ManifestError(f"{source} does not exist; start the agent once before backing it up")
    if source == out or source in out.parents:
        raise ManifestError("backup output must not be inside the agent volume")
    out.mkdir(parents=True, exist_ok=True)
    archive = _archive_name(fleet, agent, out)
    try:
        with tarfile.open(archive, "w:gz") as tf:
            for path in sorted(source.rglob("*")):
                rel = path.relative_to(source)
                if not include_work and (rel == Path("workspace") or Path("workspace") in rel.parents):
                    continue
                tf.add(path, arcname=str(Path(f"agent-{agent}") / rel), recursive=False)
    except PermissionError as exc:
        archive.unlink(missing_ok=True)
        raise ManifestError(f"permission denied reading {source}; re-run with sudo") from exc
    archive.chmod(0o600)
    return archive


def restore_backup(fleet: Fleet, archive: Path, *, agent: str | None = None, force: bool = False) -> Path:
    if not archive.is_file():
        raise ManifestError(f"backup archive not found: {archive}")
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        roots = {Path(m.name).parts[0] for m in members if Path(m.name).parts}
        if len(roots) != 1 or not next(iter(roots)).startswith("agent-"):
            raise ManifestError("archive must contain exactly one agent-<name> root")
        source_root = next(iter(roots))
        target_name = agent or source_root.removeprefix("agent-")
        target = fleet.root / "volumes" / f"agent-{target_name}"
        try:
            import subprocess

            running = (
                subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Running}}", f"agent-{target_name}"],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                == "true"
            )
        except FileNotFoundError:
            running = False
        if running:
            raise ManifestError(f"agent-{target_name} is running; stop it before restore")
        if target.exists() and any(target.iterdir()) and not force:
            raise ManifestError(f"{target} already contains state; pass --force to restore over it")
        target.mkdir(parents=True, exist_ok=True)
        for member in members:
            rel = Path(member.name).relative_to(source_root)
            resolved = (target / rel).resolve()
            if resolved != target.resolve() and target.resolve() not in resolved.parents:
                raise ManifestError(f"unsafe archive member: {member.name}")
            member.name = str(Path(target.name) / rel)
        tf.extractall(fleet.root / "volumes", members=members, filter="data")
    return target


def _stop(fleet: Fleet, agent: str, action: str) -> int:
    from .cli import _compose

    return _compose(fleet, action, f"agent-{agent}")


def cmd_backup(ns, fleet: Fleet) -> int:
    names = ns.agents or [a.name for a in fleet.agents]
    known = {a.name for a in fleet.agents}
    unknown = set(names) - known
    if unknown:
        raise ManifestError(f"unknown agent(s): {', '.join(sorted(unknown))}")
    if ns.out == "-" and len(names) != 1:
        raise ManifestError("--out - requires exactly one agent")
    out = fleet.root / "backup" if ns.out is None else Path(ns.out).resolve()
    for name in names:
        if ns.cold and _stop(fleet, name, "stop"):
            return 1
        try:
            archive = create_backup(fleet, name, out, include_work=ns.include_work)
        finally:
            if ns.cold:
                _stop(fleet, name, "up")
        if ns.out == "-":
            os.write(1, archive.read_bytes())
            archive.unlink()
        else:
            print(f"  wrote {archive}")
    print("WARNING: backup archives contain live credentials; protect them like private keys.")
    return 0


def cmd_restore(ns, fleet: Fleet) -> int:
    target = restore_backup(fleet, Path(ns.archive).resolve(), agent=ns.agent, force=ns.force)
    print(f"  restored {target}")
    if ns.agent:
        print(
            "WARNING: platform identity, memory bank id, and observability "
            "project do not rename automatically."
        )
    return 0
