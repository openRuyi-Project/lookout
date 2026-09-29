"""Real SQLite transactions, migration equivalence and row-sized updates."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3

import pytest

from tracker import state, storage


def legacy(db, snapshot, stamp=None):
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('CREATE TABLE snapshot (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        conn.execute('INSERT INTO snapshot VALUES (1, ?)', (json.dumps(snapshot),))
        conn.execute('CREATE TABLE snapshot_clock (id INTEGER PRIMARY KEY, revision TEXT, build_checked_at TEXT)')
        conn.execute('INSERT INTO snapshot_clock VALUES (1, ?, ?)', ('old', stamp))


def rows(db):
    with closing(sqlite3.connect(db)) as conn:
        return {tuple(row[:3]): tuple(row[3:]) for row in conn.execute('SELECT * FROM snapshot_records')}


def test_migration_preserves_observations_and_is_idempotent(tmp_path, snapshot):
    db = tmp_path / 'db'
    legacy(db, snapshot, state.utcnow())
    before = state.read(db)
    with pytest.raises(ValueError, match='migration required'):
        state.commit(db, before)
    assert storage.migrate(db)
    assert state.read(db) == before
    pristine = db.read_bytes()
    assert storage.migrate(db) is False
    assert db.read_bytes() == pristine


def test_only_changed_record_is_rewritten(tmp_path, snapshot):
    db = tmp_path / 'db'
    state.commit(db, snapshot)
    before = rows(db)
    after = deepcopy(snapshot)
    after['sources']['binutils']['version'] = 'next'
    after['generation'] += 1
    result = state.commit(db, after)
    changed = [key for key, value in rows(db).items() if before[key] != value]
    assert changed == [('sources', 'binutils', '')]
    assert result['changed_records'] == 1
    assert state.read(db) == after


def test_cached_read_reuses_unmodified_records_and_handles_deletion(tmp_path, snapshot):
    db = tmp_path / 'db'
    state.commit(db, snapshot)
    first, revision = state.read_cached(db)
    changed = deepcopy(snapshot)
    changed['sources']['binutils']['version'] = 'next'
    changed['sources'].pop('foo3')
    state.commit(db, changed)
    second, following = state.read_cached(db, (first, revision))
    assert second == changed and following != revision
    assert second['sources']['foo4'] is first['sources']['foo4']
    assert second['sources']['binutils'] is not first['sources']['binutils']
    assert first == snapshot


def test_cached_read_decodes_only_changed_rows_and_header(tmp_path, snapshot, monkeypatch):
    db = tmp_path / 'db'
    state.commit(db, snapshot)
    previous = state.read_cached(db)
    snapshot['sources']['binutils']['version'] = 'next'
    state.commit(db, snapshot)
    loads, decoded = storage.json.loads, []
    def record(text):
        decoded.append(text)
        return loads(text)
    monkeypatch.setattr(storage.json, 'loads', record)
    assert state.read_cached(db, previous)[0] == snapshot
    assert len(decoded) == 2


def test_empty_and_absent_collections_round_trip(tmp_path):
    for i, extra in enumerate(({}, {'monitors': {}}, {'monitors': {'widget': {}}})):
        value = {**state.empty(), **extra}
        db = tmp_path / str(i)
        state.commit(db, value)
        assert state.read(db) == value


def test_monitor_write_does_not_materialize_unchanged_build_heartbeat(tmp_path, snapshot):
    db = tmp_path / 'db'
    snapshot['components']['builds'] = state.success({}, {}, state.utcnow())
    state.commit(db, snapshot)
    before = rows(db)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('UPDATE snapshot_clock SET build_checked_at=?', (state.utcnow(),))
    updated = state.read(db)
    updated['sources']['binutils']['version'] = 'next'
    result = state.commit(db, updated)
    assert result['changed_records'] == 1
    assert state.read(db) == updated
    assert all(value == rows(db)[key] for key, value in before.items() if key[0] == 'builds')


def test_failed_build_does_not_inherit_shared_success_clock(tmp_path, snapshot):
    db = tmp_path / 'db'
    snapshot['components']['builds'] = state.success({}, {}, state.utcnow())
    state.commit(db, snapshot)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('UPDATE snapshot_clock SET build_checked_at=?', (state.utcnow(),))
    updated = state.read(db)
    updated['builds']['binutils']['rva23']['error'] = 'timeout'
    state.commit(db, updated)
    assert state.read(db) == updated
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute('SELECT build_checked_at FROM snapshot_clock').fetchone() == (None,)


def test_unknown_storage_version_is_never_overwritten(tmp_path, snapshot):
    db = tmp_path / 'db'
    state.commit(db, snapshot)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute('PRAGMA user_version=999')
    before = db.read_bytes()
    for operation in (state.read, storage.migrate):
        with pytest.raises(ValueError, match='unsupported'):
            operation(db)
    with pytest.raises(ValueError, match='migration required'):
        state.commit(db, snapshot)
    assert db.read_bytes() == before


def test_migration_rolls_back_ddl_and_rows_on_failure(tmp_path, snapshot, monkeypatch):
    db = tmp_path / 'db'
    legacy(db, snapshot)
    original = db.read_bytes()
    write = storage._write
    def fail(conn, header, records, snapshot):
        write(conn, header, records, snapshot)
        raise OSError('interrupted migration')
    monkeypatch.setattr(storage, '_write', fail)
    with pytest.raises(OSError, match='interrupted'):
        storage.migrate(db)
    assert state.read(db) == snapshot
    assert db.read_bytes() == original


def test_incremental_commit_encodes_only_changed_record_and_header(tmp_path, snapshot, monkeypatch):
    db = tmp_path / 'db'
    state.commit(db, snapshot)
    previous = state.read_cached(db)
    changed = deepcopy(previous[0])
    changed['sources']['binutils']['version'] = 'next'
    encoded, encode = [], storage.encode
    def record(value):
        encoded.append(value)
        return encode(value)
    monkeypatch.setattr(storage, 'encode', record)
    result = state.commit(db, changed, previous=previous)
    assert result['changed_records'] == 1 and len(encoded) == 2
    assert state.read(db) == changed


@pytest.mark.parametrize('change', ['payload', 'heartbeat'])
def test_stale_incremental_base_is_rejected_without_overwriting(tmp_path, snapshot, change):
    db = tmp_path / 'db'
    state.commit(db, snapshot)
    previous = state.read_cached(db)
    if change == 'payload':
        newer = deepcopy(snapshot)
        newer['sources']['binutils']['version'] = 'newer'
        state.commit(db, newer)
    else:
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE snapshot_clock SET build_checked_at='2026-01-01T00:00:00+00:00'")
    before = db.read_bytes()
    with pytest.raises(ValueError, match='changed since read'):
        state.commit(db, snapshot, previous=previous)
    assert db.read_bytes() == before
