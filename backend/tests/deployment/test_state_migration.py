"""Migration is explicit, backed up, and observation-preserving."""
from contextlib import closing
from pathlib import Path
import runpy
import sqlite3

import pytest

from tests.test_storage import legacy
from tracker import runtime_checks, state, storage

ROOT = Path(__file__).resolve().parents[3]
migrate = runpy.run_path(str(ROOT / 'deploy/migrate-state.py'))


def test_cli_migrates_once_and_preserves_verified_backup(tmp_path, snapshot, capsys):
    source, backup = tmp_path / 'db', tmp_path / 'backup'
    legacy(source, snapshot, state.utcnow())
    before = state.read(source)
    args = ['--db', str(source), '--backup', str(backup)]
    assert migrate['main'](args) == 0
    assert capsys.readouterr().out == 'migrated\n'
    original_backup = backup.read_bytes()
    assert state.read(source) == state.read(backup) == before
    assert migrate['main'](args) == 0
    assert capsys.readouterr().out == 'unchanged\n'
    assert backup.read_bytes() == original_backup


def test_existing_backup_blocks_migration_before_data_change(tmp_path, snapshot):
    db, backup = tmp_path / 'db', tmp_path / 'backup'
    legacy(db, snapshot)
    backup.write_bytes(b'keep')
    before = db.read_bytes()
    assert migrate['main'](['--db', str(db), '--backup', str(backup)]) == 2
    assert db.read_bytes() == before and backup.read_bytes() == b'keep'


def test_newer_schema_is_not_automatically_rebuilt(tmp_path, snapshot):
    db, backup = tmp_path / 'db', tmp_path / 'backup'
    state.commit(db, snapshot)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute('PRAGMA user_version=999')
    before = db.read_bytes()
    assert migrate['main'](['--db', str(db), '--backup', str(backup)]) == 2
    assert db.read_bytes() == before and not backup.exists()


def test_preflight_refuses_implicit_migration(tmp_path, snapshot, monkeypatch):
    db = tmp_path / 'db'
    legacy(db, snapshot)
    before = db.read_bytes()
    monkeypatch.setattr(runtime_checks.os, 'geteuid', lambda: 10001)
    with pytest.raises(RuntimeError, match='migration required'):
        runtime_checks.check_runtime({'collector': {}, 'spec': {}}, db)
    assert db.read_bytes() == before
    storage.migrate(db)
    assert runtime_checks.check_runtime({'collector': {}, 'spec': {}}, db)
