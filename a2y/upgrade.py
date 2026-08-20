"""Three-way, offline upgrades of pack-owned image and fleet CI files."""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import __version__
from .scaffold import ensure_gitignore


def normalized_hash(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def shipped_files(resource_root=None) -> dict[str, bytes]:
    root = resource_root or (resources.files("a2y") / "image")
    out: dict[str, bytes] = {}

    def walk(node, prefix: str = "image") -> None:
        for child in sorted(node.iterdir(), key=lambda item: item.name):
            rel = f"{prefix}/{child.name}"
            if child.is_dir():
                walk(child, rel)
            else:
                out[rel] = child.read_bytes()

    walk(root)
    if resource_root is None:
        workflow_root = resources.files("a2y") / "fleet_workflows"
        for forge in ("github", "gitea"):
            out[f".{forge}/workflows/a2y-fleet.yml"] = (workflow_root / forge / "a2y-fleet.yml").read_bytes()
    return out


@dataclass
class UpgradeResult:
    changed: list[str]
    conflicts: list[str]
    diffs: dict[str, str]


def upgrade_workspace(
    root: Path, *, dry_run: bool = False, force: bool = False, resource_root=None
) -> UpgradeResult:
    state_path = root / ".a2y-upgrade.json"
    try:
        old = json.loads(state_path.read_text()) if state_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        old = {}
    old_hashes = old.get("files") if isinstance(old.get("files"), dict) else {}
    shipped = shipped_files(resource_root)
    changed: list[str] = []
    conflicts: list[str] = []
    diffs: dict[str, str] = {}

    for rel, new_data in shipped.items():
        target = root / rel
        current = target.read_bytes() if target.is_file() else None
        if current == new_data:
            continue
        safe = current is None or (old_hashes.get(rel) and normalized_hash(current) == old_hashes[rel])
        if not safe and not force:
            conflicts.append(rel)
            before = current.decode(errors="replace").splitlines(keepends=True) if current else []
            after = new_data.decode(errors="replace").splitlines(keepends=True)
            diffs[rel] = "".join(
                difflib.unified_diff(before, after, fromfile=rel, tofile=f"{rel} (pack {__version__})")
            )
            continue
        changed.append(rel)
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new_data)

    if not dry_run:
        # State records only what is actually on disk. Conflicts retain their old
        # hashes so the next run can still distinguish user edits.
        hashes = dict(old_hashes)
        for rel in shipped:
            target = root / rel
            if target.is_file() and rel not in conflicts:
                hashes[rel] = normalized_hash(target.read_bytes())
        (root / ".a2y-version").write_text(__version__ + "\n")
        state_path.write_text(
            json.dumps({"version": __version__, "files": hashes}, indent=2, sort_keys=True) + "\n"
        )
        ensure_gitignore(root)
    return UpgradeResult(changed, conflicts, diffs)


def cmd_upgrade(ns) -> int:
    root = Path.cwd()
    result = upgrade_workspace(root, dry_run=ns.dry_run, force=ns.force)
    verb = "would update" if ns.dry_run else "updated"
    for rel in result.changed:
        print(f"  {verb} {rel}")
    for rel in result.conflicts:
        print(f"  conflict {rel} (locally modified; use --force to overwrite)")
        if result.diffs[rel]:
            print(result.diffs[rel], end="" if result.diffs[rel].endswith("\n") else "\n")
    if any(rel.startswith("image/") for rel in result.changed):
        print("Run `a2y build`, then `a2y render` and `a2y up`.")
    if not result.changed and not result.conflicts:
        print("workspace is already at the installed pack version")
    return 1 if result.conflicts else 0
