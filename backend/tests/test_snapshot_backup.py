"""Real SQLite backups and bounded failure paths; no providers or production data."""
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import threading

import pytest
from tracker import state

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('snapshot_backup', ROOT / 'deploy/backup-snapshot.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'source #?.sqlite3'
    state.commit(path, {**state.empty(), 'generation': 8})
    return path


def test_backup_unchanged_source_and_private_output(source, tmp_path, capsys):
    before = source.read_bytes()
    output = tmp_path / 'backup.sqlite3'
    assert backup.main(['--db', str(source), '--output', str(output)]) == 0
    assert capsys.readouterr().out == str(output) + '\n'
    assert state.read(source) == state.read(output)
    assert source.read_bytes() == before
    assert output.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.snapshot-*'))


@pytest.mark.parametrize('kind', ['existing', 'dangling', 'same'])
def test_refuses_occupied_paths(source, tmp_path, kind):
    output = source if kind == 'same' else tmp_path / 'output'
    if kind == 'existing':
        output.write_bytes(b'keep')
    elif kind == 'dangling':
        output.symlink_to(tmp_path / 'missing-target')
    before = source.read_bytes()
    assert backup.main(['--db', str(source), '--output', str(output)]) == 2
    assert source.read_bytes() == before
    assert not (tmp_path / 'missing-target').exists()
    if kind == 'existing':
        assert output.read_bytes() == b'keep'


@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'empty', 'no-row', 'invalid-json', 'invalid-schema', 'boolean'])
def test_invalid_source_leaves_no_backup(tmp_path, kind):
    source = tmp_path / 'source'
    output = tmp_path / 'output'
    if kind == 'corrupt':
        source.write_bytes(b'broken')
    elif kind != 'missing':
        with closing(sqlite3.connect(source)) as db:
            if kind != 'empty':
                db.execute('CREATE TABLE snapshot (id INTEGER PRIMARY KEY, payload TEXT)')
                if kind != 'no-row':
                    payload = {'invalid-json': '{', 'invalid-schema': '{"schema": 2, "generation": 0}',
                               'boolean': '{"schema": true, "generation": false}'}[kind]
                    db.execute('INSERT INTO snapshot VALUES (1,?)', (payload,))
                db.commit()
    assert backup.main(['--db', str(source), '--output', str(output)]) == 2
    assert not output.exists() and not list(tmp_path.glob('.snapshot-*'))
    if kind == 'missing':
        assert not source.exists()


@pytest.mark.parametrize('timeout', ['nan', 'inf', '0', '-1'])
def test_timeout_must_be_finite_positive(source, tmp_path, timeout):
    output = tmp_path / 'output'
    assert backup.main(['--db', str(source), '--output', str(output), '--timeout-seconds', timeout]) == 2
    assert not output.exists()


def test_timeout_closes_connections_and_cleans_temp(source, tmp_path, monkeypatch):
    connections = []
    connect = sqlite3.connect
    clock = [0.0]

    class Slow(sqlite3.Connection):
        def backup(self, target, *, progress, **kwargs):
            clock[0] = 2.0
            progress(0, 1, 2)

    def tracked(*args, **kwargs):
        db = connect(*args, factory=Slow, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(backup.sqlite3, 'connect', tracked)
    monkeypatch.setattr(backup.time, 'monotonic', lambda: clock[0])
    output = tmp_path / 'output'
    assert backup.main(['--db', str(source), '--output', str(output), '--timeout-seconds', '1']) == 2
    assert not output.exists() and not list(tmp_path.glob('.snapshot-*'))
    assert len(connections) == 2
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            db.execute('SELECT 1')


def test_concurrent_output_is_not_overwritten(source, tmp_path, monkeypatch):
    output = tmp_path / 'output'
    link = backup.os.link

    def raced(src, dst):
        Path(dst).write_bytes(b'other process')
        link(src, dst)

    monkeypatch.setattr(backup.os, 'link', raced)
    assert backup.main(['--db', str(source), '--output', str(output)]) == 2
    assert output.read_bytes() == b'other process'
    assert not list(tmp_path.glob('.snapshot-*'))


def test_concurrent_writer_produces_one_complete_snapshot(source, tmp_path, monkeypatch):
    first = {**state.empty(), 'generation': 1, 'marker': 'a' * 2_000_000}
    second = {**state.empty(), 'generation': 2, 'marker': 'b' * 2_000_000}
    state.commit(source, first)
    started = threading.Event()
    writer_ready = threading.Event()
    errors = []
    connect = sqlite3.connect

    class Observed(sqlite3.Connection):
        def backup(self, target, *, progress, **kwargs):
            def observe(*args):
                started.set()
                assert writer_ready.wait(5)
                progress(*args)
            return super().backup(target, progress=observe, **kwargs)

    monkeypatch.setattr(backup.sqlite3, 'connect', lambda *args, **kwargs: connect(*args, factory=Observed, **kwargs))

    def write():
        try:
            assert started.wait(5)
            writer_ready.set()
            state.commit(source, second)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=write)
    worker.start()
    try:
        output = backup.backup(source, tmp_path / 'output', 10)
        assert state.read(output) in (first, second)
    finally:
        started.set()
        worker.join(15)
    assert not worker.is_alive() and not errors
    assert state.read(source) == second
