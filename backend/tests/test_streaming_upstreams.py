# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import tomllib
import tomlkit
import pytest
from tracker import collector, config as cfg, nv, state


def native_file(tmp_path, config):
    p = tmp_path / 'native.toml'
    p.write_text(tomlkit.dumps({'__config__': {'max_concurrency': 2, 'http_timeout': 5},
                               **config['native']}))
    config.update(nvpath=str(p),nv_digest=hashlib.sha256(p.read_bytes()).hexdigest())
    return p


def test_actual_native_fast_result_is_visible_before_slow_request_finishes(config,tmp_path):
    release=threading.Event();slow_started=threading.Event();events=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path=='/slow':slow_started.set();release.wait(4)
            body=b'{"stable_versions":["2.0"]}'
            self.send_response(200);self.end_headers();self.wfile.write(body)
        def log_message(self,*_):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        config['native']={n:{'source':'jq','url':f'http://127.0.0.1:{server.server_port}/{n}','filter':'first(.stable_versions[])'} for n in ['slow','fast']}
        config['collector']['nvchecker_timeout_seconds']=10;native_file(tmp_path,config)
        start=time.monotonic()
        def completed(facts):
            events.append((round(time.monotonic()-start,3),list(facts)))
            if 'fast' in facts:
                assert not release.is_set() and 'slow' not in facts
                release.set()
        result,error=nv.run(config,{},state.utcnow(),on_results=completed)
        print('STREAM_NATIVE',json.dumps({'events':events,'error':error,'versions':{n:f.get('version') for n,f in result.items()}}))
        assert not error and release.is_set() and all(v['version']=='2.0' for v in result.values())
        assert events[0][0]<3 and events[0][1]==['fast']
    finally:
        release.set();server.shutdown();server.server_close();thread.join()


def test_real_process_timeout_retains_published_success_and_old_other_value():
    now='2026-09-20T00:00:00Z';native={'fast':{'source':'manual'},'slow':{'source':'manual'}}
    old={'slow':{'version':'1.0','fetched_at':'2026-09-19T00:00:00Z','configuration_fingerprint':cfg.track_fingerprint(native['slow'])}}
    code='import time;print(\'{"name":"fast","event":"updated","version":"2.0"}\',flush=True);time.sleep(10)'
    published=[];result,error=nv.stream_command([sys.executable,'-u','-c',code],1,native,old,now,published.append)
    assert error=='nvchecker timeout' and published[0]['fast']['version']=='2.0'
    assert result['slow']['version']=='1.0' and result['slow']['fetched_at']==old['slow']['fetched_at']
    assert result['slow']['reported_at'] is None and result['fast']['reported_at']==now


def process_terminated(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    # A container's PID 1 may not promptly reap an orphaned child. A zombie is
    # terminated and cannot retain the pipe or execute provider work.
    status = Path(f'/proc/{pid}/status')
    try:
        return any(line.startswith('State:') and 'Z' in line for line in status.read_text().splitlines())
    except FileNotFoundError:
        return False


@pytest.mark.parametrize('streaming', [False, True])
def test_timeout_kills_native_descendants(tmp_path, streaming):
    pid_path = tmp_path / 'child.pid'
    code = (
        'import pathlib, subprocess, sys, time\n'
        'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
        f'pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))\n'
        'print(\'{"name":"fast","event":"updated","version":"2.0"}\', flush=True)\n'
        'time.sleep(30)\n'
    )
    published = []
    native = {'fast': {'source': 'manual'}}
    started = time.monotonic()
    result, error = nv.stream_command(
        [sys.executable, '-u', '-c', code], 1, native, {}, state.utcnow(),
        published.append if streaming else None,
    )
    pid = int(pid_path.read_text())
    try:
        assert error == 'nvchecker timeout' and result['fast']['version'] == '2.0'
        assert bool(published) is streaming
        deadline = time.monotonic() + 2
        while not process_terminated(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process_terminated(pid), 'native helper survived the command timeout'
        assert time.monotonic() - started < 4
    finally:
        if not process_terminated(pid):
            os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('output', ['long_line', 'total_log'])
def test_real_process_output_limits_preserve_complete_results(streaming, output):
    now = state.utcnow()
    native = {'fast': {'source': 'manual'}, 'slow': {'source': 'manual'}}
    old = {'slow': {'version': '1.0', 'fetched_at': '2026-09-19T00:00:00Z',
                    'configuration_fingerprint': cfg.track_fingerprint(native['slow'])}}
    payload = "print('x' * (1024 * 1024 + 1), flush=True)" if output == 'long_line' else (
        "for _ in range(2200): print('{\"padding\":\"' + 'x' * 8192 + '\"}', flush=True)"
    )
    code = (
        'import time\n'
        'print(\'{"name":"fast","event":"updated","version":"2.0"}\', flush=True)\n'
        + payload + '\ntime.sleep(30)\n'
    )
    published = []
    started = time.monotonic()
    result, error = nv.stream_command(
        [sys.executable, '-u', '-c', code], 5, native, old, now,
        published.append if streaming else None,
    )
    assert error == 'nvchecker output limit exceeded'
    assert result['fast']['version'] == '2.0' and result['fast']['error'] is None
    assert result['slow']['version'] == '1.0' and result['slow']['fetched_at'] == old['slow']['fetched_at']
    assert result['slow']['error'] == error
    assert bool(published) is streaming
    assert time.monotonic() - started < 5


def test_full_run_prioritizes_tracks_not_reported_before_deadline(config,tmp_path,monkeypatch):
    config['native']={n:{'source':'manual'} for n in ['reported','unreported']};native_file(tmp_path,config)
    old={'reported':{'reported_at':'2026-09-20T00:00:00Z'},'unreported':{'reported_at':None,'attempted_at':'2026-09-20T00:00:00Z'}}
    def execute(command,*args):
        assert list(tomllib.loads(Path(command[-1]).read_text()))==['__config__','unreported','reported']
        return {},None
    monkeypatch.setattr(nv,'stream_command',execute);nv.run(config,old,state.utcnow(),on_results=lambda _:None)


def test_partial_publication_retains_global_failure_and_newer_other_phases(config,snapshot,tmp_path,monkeypatch):
    config['nv_digest']='same';db=tmp_path/'state.db';snapshot['components']['nvchecker']['error']='prior failure';state.commit(db,snapshot)
    monkeypatch.setattr(cfg,'require_unchanged',lambda *_args:None)
    def execute(config,previous,now,on_results):
        facts,_=nv.import_events('{"name":"binutils","event":"updated","version":"9.0"}',{'binutils':config['native']['binutils']},previous,now)
        on_results(facts)
        visible=state.read(db)
        assert visible['tracks']['binutils']['version']=='9.0'
        assert visible['components']['nvchecker']==snapshot['components']['nvchecker']
        visible['sources']['binutils']['version']='8.0';visible['generation']+=1;state.commit(db,visible)
        return {**previous,**facts},None
    result=collector.check_upstreams(config,'unused',db,run_nv=execute)
    assert result['sources']['binutils']['version']=='8.0'
    assert result['specs']==snapshot['specs'] and result['components']['nvchecker']['error'] is None


def test_config_drift_stops_publication_without_relabelling_completed_facts(config,snapshot,tmp_path,configured_path):
    db=tmp_path/'state.db';state.commit(db,snapshot)
    def execute(config,previous,now,on_results):
        facts,_=nv.import_events('{"name":"binutils","event":"updated","version":"9.0"}',{'binutils':config['native']['binutils']},previous,now)
        on_results(facts)
        configured_path.write_text(configured_path.read_text() + '\n# operator changed configuration\n')
        on_results(facts)
        pytest.fail('drift must stop writer')
    with pytest.raises(ValueError,match='configuration changed'):
        collector.check_upstreams(config,configured_path,db,run_nv=execute)
    assert state.read(db)['tracks']['binutils']['version']=='9.0'
    assert state.read(db)['components']['nvchecker']==snapshot['components']['nvchecker']
