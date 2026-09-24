"""A heartbeat publishes invalidations and new results, not redundant final reads."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tracker import config as cfg, monitor, nv, state


@pytest.fixture
def collection(config, snapshot, tmp_path, monkeypatch):
    checked = Mock(return_value={'status': 'ok', 'findings': [], 'note': None})
    adapter = SimpleNamespace(VERSION=1, TITLE='Fixture', HOSTS=set(),
                              inputs=lambda *args: {}, check=checked)
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', adapter)
    config['monitors'] = {'enabled': ['fixture'], 'workers': 1}
    path = tmp_path / 'tracker.toml'
    native = tmp_path / 'nvchecker.toml'
    native.write_text('\n'.join(json.dumps(name) + ' = ' + nv._toml_value(entry)
                               for name, entry in config['native'].items()))
    raw = {key: config[key] for key in ('obs', 'targets', 'collector', 'packages', 'monitors')}
    path.write_text('\n'.join(key + ' = ' + nv._toml_value(value) for key, value in raw.items()))
    loaded = cfg.load(path)
    snapshot['sources'] = {name: snapshot['sources'][name] for name in ('binutils', 'foo3')}
    db = tmp_path / 'snapshot.db'
    # Use one deterministic coalescing window, independent of worker completion order.
    monkeypatch.setattr(monitor, 'time', SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(cfg, 'load', lambda *args: pytest.fail('publication must not parse TOML again'))
    io = SimpleNamespace(for_hosts=lambda *args, **kwargs: None)
    return SimpleNamespace(config=loaded, path=path, db=db, snapshot=snapshot,
                           adapter=adapter, checked=checked, io=io)


def run(fixture):
    return monitor.collect(fixture.config, fixture.path, fixture.db, io=fixture.io)


def one_package(fixture):
    fixture.snapshot['sources'].pop('foo3')
    state.commit(fixture.db, fixture.snapshot)


def test_single_completed_result_has_no_duplicate_final_publish(collection):
    one_package(collection)
    with patch.object(cfg, 'require_unchanged', wraps=cfg.require_unchanged) as guards, \
         patch.object(state, 'read', wraps=state.read) as reads, \
         patch.object(state, 'commit', wraps=state.commit) as writes:
        result = run(collection)
    assert guards.call_count == 2  # Initial invalidation and completed result.
    assert reads.call_count == 3  # Initial snapshot plus one read per publication.
    assert writes.call_count == 2
    assert collection.checked.call_count == 1
    assert result['monitors']['binutils']['fixture']['status'] == 'ok'
    assert state.read(collection.db) == result


def test_last_result_in_coalescing_window_is_flushed(collection):
    state.commit(collection.db, collection.snapshot)
    with patch.object(cfg, 'require_unchanged', wraps=cfg.require_unchanged) as guards, \
         patch.object(state, 'read', wraps=state.read) as reads, \
         patch.object(state, 'commit', wraps=state.commit) as writes:
        result = run(collection)
    assert guards.call_count == 3  # Initial, first completion, final dirty batch.
    assert reads.call_count == 4
    assert writes.call_count == 3
    assert collection.checked.call_count == 2
    assert all(values['fixture']['status'] == 'ok' for values in result['monitors'].values())
    assert state.read(collection.db) == result


def test_idle_heartbeat_revalidates_once_without_writing_or_opening_pool(collection, monkeypatch):
    one_package(collection)
    initial = run(collection)
    monkeypatch.setattr(monitor, 'ThreadPoolExecutor', lambda **kwargs: pytest.fail('no work to submit'))
    with patch.object(cfg, 'require_unchanged', wraps=cfg.require_unchanged) as guards, \
         patch.object(state, 'read', wraps=state.read) as reads, \
         patch.object(state, 'commit', wraps=state.commit) as writes:
        result = run(collection)
    assert result == initial
    assert guards.call_count == 1 and reads.call_count == 2
    assert writes.call_count == 0 and collection.checked.call_count == 1


def test_no_jobs_still_publishes_catalog_input_invalidation_and_recovery(collection):
    one_package(collection)
    initial = run(collection)
    collection.adapter.TITLE = 'Renamed fixture'
    with patch.object(state, 'commit', wraps=state.commit) as writes:
        renamed = run(collection)
    assert writes.call_count == 1
    assert renamed['monitor_catalog']['fixture']['title'] == 'Renamed fixture'
    assert renamed['monitors'] == initial['monitors']

    renamed['sources']['binutils']['error'] = 'observation unavailable'
    state.commit(collection.db, renamed)
    with patch.object(state, 'commit', wraps=state.commit) as writes:
        blocked = run(collection)
    assert writes.call_count == 1
    assert blocked['monitors']['binutils']['fixture']['input_status'] == 'unsupported'
    blocked['sources']['binutils']['error'] = None
    state.commit(collection.db, blocked)
    with patch.object(state, 'commit', wraps=state.commit) as writes:
        recovered = run(collection)
    assert writes.call_count == 1
    assert recovered['monitors']['binutils']['fixture']['input_status'] == 'pending'
    assert recovered['monitors']['binutils']['fixture']['checked_at'] == initial['monitors']['binutils']['fixture']['checked_at']
    assert collection.checked.call_count == 1


@pytest.mark.parametrize('changed_file', ['tracker', 'native'])
def test_completed_result_checks_exact_config_bytes(collection, changed_file):
    one_package(collection)

    def change(*args):
        path = collection.path if changed_file == 'tracker' else Path(collection.config['nvpath'])
        path.write_text(path.read_text() + '\n# changed during provider request\n')
        return {'status': 'ok', 'findings': [], 'note': None}

    collection.adapter.check = change
    with pytest.raises(ValueError, match='configuration changed'):
        run(collection)
    assert state.read(collection.db)['monitors']['binutils']['fixture']['status'] == 'pending'


def test_config_drift_while_waiting_for_writer_lock_cannot_publish(collection, monkeypatch):
    one_package(collection)
    initial = run(collection)
    waiting = Event()
    flock = state.fcntl.flock

    def observed_flock(fd, operation):
        try:
            return flock(fd, operation)
        except BlockingIOError:
            waiting.set()
            raise

    monkeypatch.setattr(state.fcntl, 'flock', observed_flock)
    # Force a catalog-only write on an otherwise idle heartbeat, not provider IO.
    collection.adapter.TITLE = 'Changed fixture title'
    with ThreadPoolExecutor(max_workers=1) as pool:
        with state.writer_lock(collection.db):
            pending = pool.submit(run, collection)
            assert waiting.wait(3), 'monitor did not reach the database writer lock'
            assert not pending.done()
            collection.path.write_text(collection.path.read_text() + '\n# changed while waiting\n')
        with pytest.raises(ValueError, match='configuration changed'):
            pending.result(timeout=5)
    assert state.read(collection.db) == initial
    assert collection.checked.call_count == 1


@pytest.mark.parametrize('change', ['scope', 'input'])
def test_completed_result_revalidates_source_scope_and_inputs(collection, change):
    one_package(collection)

    def check(*args):
        latest = state.read(collection.db)
        if change == 'scope':
            latest['obs']['project'] = 'another-project'
        else:
            latest['sources']['binutils']['version'] = '9.0'
        state.commit(collection.db, latest)
        return {'status': 'ok', 'findings': [], 'note': None}

    collection.adapter.check = check
    if change == 'scope':
        with pytest.raises(ValueError, match='scope changed'):
            run(collection)
        result = state.read(collection.db)
    else:
        result = run(collection)
        assert result['monitors']['binutils']['fixture']['subject']['version'] == '9.0'
    assert result['monitors']['binutils']['fixture']['status'] == 'pending'
    assert not result['monitors']['binutils']['fixture'].get('checked_at')
