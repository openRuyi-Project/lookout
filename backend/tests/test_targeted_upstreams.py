"""Targeted repairs use native nvchecker, without relabelling a full collection."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tomllib

import pytest
from tracker import collector, config as cfg, nv, state


def write_native(tmp_path, config, options=None):
    path = tmp_path / 'native.toml'
    tables = {'__config__': options or {}, **config['native']}
    path.write_text('\n\n'.join('[' + json.dumps(name) + ']\n' + '\n'.join(
        json.dumps(key) + ' = ' + nv._toml_value(value) for key, value in values.items())
        for name, values in tables.items()))
    config['nvpath'] = str(path)
    config['nv_digest'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return path


@pytest.mark.parametrize('tracks', [[], [''], ['unknown'], [None], ['binutils', 'unknown']])
def test_bad_names_rejected_before_lock_or_checker(config, tmp_path, tracks, monkeypatch):
    monkeypatch.setattr(state, 'writer_lock', lambda *_args, **_kwargs: pytest.fail('must not take a lock'))
    with pytest.raises(ValueError, match='configured names'):
        collector.check_upstreams(config, 'unused', tmp_path / 'absent.db', tracks=tracks)
    assert not list(tmp_path.iterdir())


def test_deduplicate_exact_names(config):
    assert nv.selected_names(config, None) is None
    assert nv.selected_names(config, ['widget@3', 'binutils', 'widget@3']) == ['widget@3', 'binutils']


def test_subset_config_native_options_and_no_operator_file_writes(config, tmp_path, monkeypatch):
    options = {'max_concurrency': 4, 'http_timeout': 30, 'keyfile': 'keys.toml',
               'oldver': 'old.json', 'newver': 'new.json', 'source': {'fixture': {'enabled': True, 'items': [1, 'x']}}}
    original = write_native(tmp_path, config, options)
    original_bytes = original.read_bytes()
    for name in ('keys.toml', 'old.json', 'new.json'):
        (tmp_path / name).write_text('operator-owned')
    seen = []
    def execute(command, timeout, native, previous, now, on_results):
        path = Path(command[-1]); seen.append(path)
        parsed = tomllib.loads(path.read_text())
        assert set(parsed) == {'__config__', 'binutils', 'widget@3'}
        assert parsed['binutils'] == config['native']['binutils']
        assert parsed['__config__'] == {**{k: v for k, v in options.items() if k not in ('oldver', 'newver')},
                                         'keyfile': str(tmp_path / 'keys.toml')}
        assert '--include' not in command and '--entry' not in command
        assert on_results is None
        return nv.import_events('\n'.join(json.dumps(item) for item in [
            {'name': 'binutils', 'event': 'updated', 'version': '3.11'},
            {'name': 'widget@3', 'event': 'updated', 'version': '3.12'},
            {'name': 'widget@4', 'event': 'updated', 'version': '999'}]), native, previous, now)
    monkeypatch.setattr(nv, 'stream_command', execute)
    result, error = nv.run(config, {}, state.utcnow(), tracks=['binutils', 'widget@3'])
    assert not error and set(result) == {'binutils', 'widget@3'}
    assert len(seen) == 1 and not seen[0].exists()
    assert original.read_bytes() == original_bytes
    assert all((tmp_path / name).read_text() == 'operator-owned' for name in ('keys.toml', 'old.json', 'new.json'))


def test_full_run_keeps_original_cli(config, tmp_path, monkeypatch):
    original = write_native(tmp_path, config)
    commands = []
    def execute(command, timeout, native, previous, now, on_results):
        commands.append(command)
        assert on_results is None
        return nv.import_events('', native, previous, now)
    monkeypatch.setattr(nv, 'stream_command', execute)
    tracks, _ = nv.run(config, {}, state.utcnow())
    assert commands == [['nvchecker', '--logger=json', '--json-log-fd=1', '--tries', '3', '-c', str(original)]]
    assert set(tracks) == set(config['native'])


def test_digest_change_before_selected_command_aborts(config, tmp_path, monkeypatch):
    path = write_native(tmp_path, config); path.write_text(path.read_text() + '\n# changed\n')
    monkeypatch.setattr(nv, 'stream_command', lambda *_a, **_k: pytest.fail('no checker permitted'))
    with pytest.raises(ValueError, match='changed before'):
        nv.run(config, {}, state.utcnow(), tracks=['binutils'])


def test_subset_timeout_keeps_completed_selected_results_only(config, snapshot, tmp_path, monkeypatch):
    write_native(tmp_path, config)
    config['collector']['nvchecker_timeout_seconds'] = 0.5
    paths = []
    popen = subprocess.Popen
    def execute(command, **kwargs):
        paths.append(Path(command[-1]))
        code = 'import time; print(\'{"name":"binutils","event":"updated","version":"4.0"}\', flush=True); time.sleep(10)'
        return popen([sys.executable, '-u', '-c', code], **kwargs)
    monkeypatch.setattr(nv.subprocess, 'Popen', execute)
    result, error = nv.run(config, snapshot['tracks'], state.utcnow(), tracks=['binutils', 'widget@3'])
    assert error == 'nvchecker timeout' and set(result) == {'binutils', 'widget@3'}
    assert result['binutils']['version'] == '4.0' and result['binutils']['error'] is None
    assert result['widget@3']['error'] == 'nvchecker timeout'
    assert result['widget@3']['fetched_at'] == snapshot['tracks']['widget@3']['fetched_at']
    assert not paths[0].exists()


def test_partial_merge_preserves_unselected_full_component_and_newer_obs(config, snapshot, tmp_path, monkeypatch):
    config['nv_digest'] = 'new-rule'
    config['native']['widget@3']['include_regex'] = '^3[.]'
    snapshot['components']['nvchecker'].update(error='nvchecker timeout', attempted_at='2026-01-01T00:00:00Z', extra={'kept': True})
    db = tmp_path / 'snapshot.db'; state.commit(db, snapshot)
    monkeypatch.setattr(cfg, 'require_unchanged', lambda *_args: None)
    def execute(c, previous, now, tracks):
        with state.writer_lock(db):
            latest = state.read(db)
            latest['sources']['binutils']['version'] = '4.0'
            latest['specs']['binutils'] = {'head': 'new-spec-head'}
            latest['generation'] += 1
            state.commit(db, latest)
        return nv.import_events('{"name":"binutils","event":"updated","version":"4.1"}',
                                {name: c['native'][name] for name in tracks}, previous, now)
    attempt = {}
    result = collector.check_upstreams(config, 'unused', db, run_nv=execute,
                                      tracks=['binutils', 'widget@3'], attempt=attempt)
    assert result == state.read(db)
    assert result['generation'] == snapshot['generation'] + 2
    assert result['sources']['binutils']['version'] == '4.0'
    assert result['specs']['binutils']['head'] == 'new-spec-head'
    assert result['builds'] == snapshot['builds']
    assert result['components'] == snapshot['components']
    for name in snapshot['tracks'].keys() - {'binutils', 'widget@3'}:
        assert result['tracks'][name] == snapshot['tracks'][name]
    assert result['tracks']['binutils']['version'] == '4.1'
    changed = result['tracks']['widget@3']
    assert 'version' not in changed and changed['error']
    assert changed['previous_configuration']['version'] == snapshot['tracks']['widget@3']['version']
    assert changed['configuration_fingerprint'] == cfg.track_fingerprint(config['native']['widget@3'])
    assert result['last_attempt'] == snapshot['last_attempt']
    assert attempt['selected_track_count'] == 2 and set(attempt['track_errors']) == {'widget@3'}


def test_partial_digest_change_before_commit_rejects_all_results(config, snapshot, tmp_path, configured_path):
    db = tmp_path / 'snapshot.db'; state.commit(db, snapshot)
    native = Path(config['nvpath'])
    native.write_text(native.read_text() + '\n# operator changed rule\n')
    with pytest.raises(ValueError, match='configuration changed'):
        collector.check_upstreams(config, configured_path, db, tracks=['binutils'],
            run_nv=lambda _c, old, _now, tracks: ({'binutils': old['binutils']}, None))
    assert state.read(db) == snapshot


def test_partial_unrequested_result_rejected(config, snapshot, tmp_path):
    db = tmp_path / 'snapshot.db'; state.commit(db, snapshot)
    with pytest.raises(ValueError, match='do not match'):
        collector.check_upstreams(config, 'unused', db, tracks=['binutils'],
            run_nv=lambda _c, old, _now, tracks: (old, None))
    assert state.read(db) == snapshot


def test_partial_respects_existing_upstream_lock(config, snapshot, tmp_path):
    db = tmp_path / 'snapshot.db'; state.commit(db, snapshot)
    with state.writer_lock(str(db) + '.upstreams'):
        with pytest.raises(BlockingIOError):
            collector.check_upstreams(config, 'unused', db, tracks=['binutils'],
                run_nv=lambda *_a, **_k: pytest.fail('checker must not start'))
    assert state.read(db) == snapshot


@pytest.mark.parametrize('extra', [['--track', 'binutils'], ['--only', 'obs', '--track', 'binutils'],
                                 ['--only', 'specs', '--track', 'binutils'], ['--only', 'upstreams', '--track', ''],
                                 ['--only', 'upstreams', '--track', 'unknown']])
def test_cli_invalid_scope_or_name_rejected_before_state(config, tmp_path, monkeypatch, extra):
    monkeypatch.setattr(cfg, 'load', lambda _path, **kwargs: config)
    monkeypatch.setattr(sys, 'argv', ['collector', '--config', 'unused', '--db', str(tmp_path / 'absent.db'), *extra])
    monkeypatch.setattr(collector, 'check_upstreams', lambda *_a, **_k: pytest.fail('must not collect'))
    monkeypatch.setattr(collector, 'collect_obs', lambda *_a, **_k: pytest.fail('must not collect OBS'))
    with pytest.raises(SystemExit) as caught:
        collector.main()
    assert caught.value.code == 2 and not list(tmp_path.iterdir())


@pytest.mark.parametrize('command_error,track_errors,expected', [(None, {}, 0), ('nvchecker timeout', {}, 2), (None, {'binutils':'failed'}, 2)])
def test_cli_partial_exit_uses_attempt_not_retained_global_error(config, snapshot, tmp_path, monkeypatch, capsys, command_error, track_errors, expected):
    snapshot['components']['nvchecker']['error'] = 'old whole-batch timeout'
    monkeypatch.setattr(cfg, 'load', lambda _path, **kwargs: config)
    def collect(*_args, tracks, attempt):
        assert tracks == ['binutils']
        attempt.update(selected_track_count=1, command_error=command_error, track_errors=track_errors)
        return snapshot
    monkeypatch.setattr(collector, 'check_upstreams', collect)
    monkeypatch.setattr(sys, 'argv', ['collector','--config','unused','--db',str(tmp_path/'absent.db'),
                                    '--only','upstreams','--track','binutils'])
    assert collector.main() == expected
    result = json.loads(capsys.readouterr().out)
    assert result['selected_track_count'] == 1
    assert result['collection_errors'] == ['old whole-batch timeout']
    assert bool(result['errors']) == bool(expected)


def test_real_native_cli_two_selected_tracks_and_error_exit(config, snapshot, tmp_path):
    """No internet: actual nvchecker2.22 and formal collector CLI consume HTTP fixtures."""
    requested = []; fail = [False]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requested.append(self.path)
            assert self.path in ('/one', '/two'), 'unselected track was requested'
            versions = [] if fail[0] and self.path == '/two' else ['3.9', '3.10']
            body = json.dumps({'stable_versions': versions}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        config['native'] = {name: {'source':'jq', 'url':f'http://127.0.0.1:{server.server_port}/{path}',
                                   'filter':'.stable_versions[]'}
                            for name, path in [('binutils','one'),('widget@3','two'),('widget@4','unselected')]}
        write_native(tmp_path, config, {'max_concurrency':2,'http_timeout':2})
        tracker = tmp_path / 'tracker.toml'
        text = '[obs]\n' + '\n'.join(f'{k} = {nv._toml_value(v)}' for k,v in config['obs'].items())
        for target in config['targets']:
            text += '\n[[targets]]\n' + '\n'.join(f'{k} = {nv._toml_value(v)}' for k,v in target.items())
        text += '\n[collector]\nnvchecker_config="native.toml"\nnvchecker_timeout_seconds=30\n[packages]\n'
        text += '\n'.join(json.dumps(k)+' = '+nv._toml_value(v) for k,v in config['packages'].items())
        tracker.write_text(text)
        for name, fact in snapshot['tracks'].items():
            fact['configuration_fingerprint'] = cfg.track_fingerprint(config['native'][name])
        snapshot['components']['nvchecker']['error'] = 'old whole-batch timeout'
        db = tmp_path / 'state.db'; state.commit(db, snapshot)
        command = [sys.executable,'-m','tracker.collector','--config',str(tracker),'--db',str(db),
                   '--only','upstreams','--track','binutils','--track','widget@3']
        env = {k:v for k,v in os.environ.items() if k not in ('TRACKER_CONFIG','TRACKER_DB','TRACKER_SPEC_REPO','HOST','PORT','API_PORT')}
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
        p = subprocess.run(command, capture_output=True, text=True, timeout=45, env=env)
        print('NATIVE SELECTED COMMAND:', command, 'STDOUT:', p.stdout, 'STDERR:', p.stderr, 'EXIT:', p.returncode)
        assert p.returncode == 0 and set(requested) == {'/one','/two'}
        first = state.read(db)
        assert first['tracks']['binutils']['version'] == '3.10'
        assert first['tracks']['widget@3']['version'] == '3.10'
        assert first['tracks']['widget@4'] == snapshot['tracks']['widget@4']
        assert first['components'] == snapshot['components']
        assert first['sources'] == snapshot['sources'] and first['specs'] == snapshot['specs']
        assert json.loads(p.stdout)['selected_track_count'] == 2
        fail[0] = True
        p = subprocess.run(command, capture_output=True, text=True, timeout=45, env=env)
        print('NATIVE SELECTED ERROR STDOUT:', p.stdout, 'STDERR:', p.stderr, 'EXIT:', p.returncode)
        assert p.returncode == 2 and set(json.loads(p.stdout)['track_errors']) == {'widget@3'}
        second = state.read(db)
        assert second['tracks']['widget@3']['version'] == first['tracks']['widget@3']['version']
        assert second['tracks']['widget@3']['fetched_at'] == first['tracks']['widget@3']['fetched_at']
        assert second['tracks']['widget@4'] == snapshot['tracks']['widget@4']
        assert second['components'] == snapshot['components']
    finally:
        server.shutdown(); server.server_close(); thread.join()
