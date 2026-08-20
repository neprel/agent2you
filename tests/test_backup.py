import tarfile
from pathlib import Path

from a2y.backup import create_backup, restore_backup
from a2y.manifest import load_fleet
from a2y.scaffold import init_workspace


def test_backup_restore_excludes_work_and_preserves_mode(tmp_path: Path):
    ws = tmp_path / "fleet"
    ws.mkdir()
    init_workspace(ws, "fleet")
    root = ws / "volumes" / "agent-ana"
    (root / "claude").mkdir(parents=True)
    secret = root / "claude" / ".credentials.json"
    secret.write_text("secret")
    secret.chmod(0o600)
    (root / "workspace").mkdir()
    (root / "workspace" / "huge").write_text("checkout")
    archive = create_backup(load_fleet(ws), "ana", ws / "backup")
    assert archive.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archive) as tf:
        names = tf.getnames()
    assert any("credentials" in name for name in names)
    assert not any("workspace" in name for name in names)
    secret.unlink()
    restore_backup(load_fleet(ws), archive, force=True)
    assert secret.read_text() == "secret"
    assert secret.stat().st_mode & 0o777 == 0o600
