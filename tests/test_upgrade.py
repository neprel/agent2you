from pathlib import Path

from a2y.scaffold import init_workspace
from a2y.upgrade import normalized_hash, upgrade_workspace


def test_upgrade_three_way_and_dry_run(tmp_path: Path):
    ws = tmp_path / "fleet"
    ws.mkdir()
    init_workspace(ws, "fleet")
    target = ws / "image" / "entrypoint.sh"
    old = target.read_bytes()
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    (shipped / "entrypoint.sh").write_bytes(old + b"\n# new\n")

    dry = upgrade_workspace(ws, dry_run=True, resource_root=shipped)
    assert dry.changed == ["image/entrypoint.sh"]
    assert target.read_bytes() == old

    applied = upgrade_workspace(ws, resource_root=shipped)
    assert applied.conflicts == []
    assert target.read_bytes().endswith(b"# new\n")

    # The recorded baseline is the new shipped file; a local edit now conflicts.
    target.write_bytes(target.read_bytes() + b"# local\n")
    (shipped / "entrypoint.sh").write_bytes(old + b"# newer\n")
    conflict = upgrade_workspace(ws, resource_root=shipped)
    assert conflict.conflicts == ["image/entrypoint.sh"]
    assert "# local" in target.read_text()
    forced = upgrade_workspace(ws, force=True, resource_root=shipped)
    assert forced.changed == ["image/entrypoint.sh"]
    assert normalized_hash(target.read_bytes()) == normalized_hash((shipped / "entrypoint.sh").read_bytes())


def test_init_merges_gitignore_idempotently(tmp_path: Path):
    ws = tmp_path / "fleet"
    ws.mkdir()
    (ws / ".gitignore").write_text("custom.cache\n")
    init_workspace(ws, "fleet")
    first = (ws / ".gitignore").read_text()
    init_workspace(ws, "fleet")
    assert (ws / ".gitignore").read_text() == first
    assert "custom.cache" in first
    for pattern in ("deploy/.env", "volumes/", "backup/"):
        assert first.count(pattern) == 1
