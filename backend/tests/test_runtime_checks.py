"""Preflight failures must occur before services or collectors are started."""
import importlib.util
from pathlib import Path

import pytest
from tracker import runtime_checks, state

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(runtime_checks.os, 'geteuid', lambda: 10001)
    return {'collector': {}, 'spec': {'repo': None}}


def test_first_start_does_not_create_database(runtime, tmp_path, monkeypatch):
    def probe(raw):
        assert b'Name: openruyi-runtime-probe' in raw
        return {'version': '1.0', 'version_error': None,
                'native_query': {'context': {'sandbox': {'landlock_abi': 6, 'seccomp': 'allow-list'}}}}
    monkeypatch.setattr(runtime_checks.native_spec, 'query', probe)
    runtime['collector']['native_spec_fallback'] = True
    db = tmp_path / 'new' / 'tracker.sqlite3'
    assert runtime_checks.check_runtime(runtime, db)
    assert not db.exists()
    assert list(db.parent.iterdir()) == []


def test_existing_snapshot_is_unchanged_and_native_disabled(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_checks.native_spec, 'query', lambda *_: pytest.fail('native disabled'))
    db = tmp_path / 'state.db'
    snapshot = {**state.empty(), 'generation': 7}
    state.commit(db, snapshot)
    before = db.read_bytes()
    runtime_checks.check_runtime(runtime, db)
    assert db.read_bytes() == before
    assert state.read(db) == snapshot


def test_root_rejected_before_load_or_mkdir(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_checks.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(runtime_checks, 'load', lambda *_: pytest.fail('must reject root first'))
    with pytest.raises(RuntimeError, match='non-root'):
        runtime_checks.load_runtime(tmp_path / 'missing.toml', tmp_path / 'new' / 'db')
    assert not (tmp_path / 'new').exists()


def test_write_failure_is_not_treated_as_writable(runtime, tmp_path, monkeypatch):
    def full(_):
        raise OSError('simulated disk write failure')
    monkeypatch.setattr(runtime_checks.os, 'fsync', full)
    with pytest.raises(OSError, match='write failure'):
        runtime_checks.check_runtime(runtime, tmp_path / 'db')
    assert not list(tmp_path.iterdir())


def test_corrupt_database_and_directory_refused(runtime, tmp_path):
    db = tmp_path / 'db'
    db.write_bytes(b'not sqlite')
    with pytest.raises(RuntimeError, match='database is unreadable'):
        runtime_checks.check_runtime(runtime, db)
    assert db.read_bytes() == b'not sqlite'
    with pytest.raises(RuntimeError, match='not a regular file'):
        runtime_checks.check_runtime(runtime, tmp_path)


@pytest.mark.parametrize('result', [
    {'version': None, 'version_error': 'unsupported'},
    {'version': '1.0', 'native_query': {'context': {'sandbox': {'landlock_abi': 5, 'seccomp': 'allow-list'}}}},
    {'version': '1.0', 'native_query': {'context': {'sandbox': {'landlock_abi': 6, 'seccomp': 'none'}}}},
])
def test_native_probe_fails_closed(runtime, tmp_path, monkeypatch, result):
    runtime['spec']['repo'] = str(tmp_path / 'repo.git')
    monkeypatch.setattr(runtime_checks.native_spec, 'query', lambda *_: result)
    with pytest.raises(RuntimeError):
        runtime_checks.check_runtime(runtime, tmp_path / 'db')
    assert not (tmp_path / 'repo.git').exists()


def test_cli_bad_config_exits_two(runtime, tmp_path, capsys):
    path = tmp_path / 'config.toml'
    path.write_text('broken = [')
    assert runtime_checks.main(['--config', str(path), '--db', str(tmp_path / 'db')]) == 2
    output = capsys.readouterr()
    assert not output.out and 'runtime preflight failed:' in output.err
    assert not (tmp_path / 'db').exists()


def test_supervisor_bad_config_starts_no_tasks(runtime, tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('preflight_entrypoint', ROOT / 'deploy/container-entrypoint.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    path = tmp_path / 'config.toml'
    path.write_text('broken = [')
    monkeypatch.setattr(entry, 'CONFIG', str(path))
    monkeypatch.setattr(entry, 'DB', str(tmp_path / 'db'))
    monkeypatch.setattr(entry.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(entry.threading, 'Thread', lambda **_: pytest.fail('no task before preflight'))
    assert entry.main() == 2
    assert 'runtime preflight failed:' in capsys.readouterr().out
