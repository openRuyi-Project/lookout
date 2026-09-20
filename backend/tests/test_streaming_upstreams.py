# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import tomllib
import pytest
from tracker import collector, config as cfg, nv, state


def native_file(tmp_path, config):
    p=tmp_path/'native.toml';p.write_text('[__config__]\nmax_concurrency=2\nhttp_timeout=5\n'+''.join('\n['+json.dumps(n)+']\n'+''.join(k+'='+nv._toml_value(v)+'\n' for k,v in fields.items()) for n,fields in config['native'].items()))
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


def test_full_run_prioritizes_tracks_not_reported_before_deadline(config,tmp_path,monkeypatch):
    config['native']={n:{'source':'manual'} for n in ['reported','unreported']};native_file(tmp_path,config)
    old={'reported':{'reported_at':'2026-09-20T00:00:00Z'},'unreported':{'reported_at':None,'attempted_at':'2026-09-20T00:00:00Z'}}
    def execute(command,*args):
        assert list(tomllib.loads(Path(command[-1]).read_text()))==['__config__','unreported','reported']
        return {},None
    monkeypatch.setattr(nv,'stream_command',execute);nv.run(config,old,state.utcnow(),on_results=lambda _:None)


def test_partial_publication_retains_global_failure_and_newer_other_phases(config,snapshot,tmp_path,monkeypatch):
    config['nv_digest']='same';db=tmp_path/'state.db';snapshot['components']['nvchecker']['error']='prior failure';state.commit(db,snapshot)
    monkeypatch.setattr(cfg,'load',lambda _, **kwargs:config)
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


def test_config_drift_stops_publication_without_relabelling_completed_facts(config,snapshot,tmp_path,monkeypatch):
    config.update(nv_digest='same',config_digest='original');db=tmp_path/'state.db';state.commit(db,snapshot);current=[dict(config)]
    monkeypatch.setattr(cfg,'load',lambda _, **kwargs:current[0])
    def execute(config,previous,now,on_results):
        facts,_=nv.import_events('{"name":"binutils","event":"updated","version":"9.0"}',{'binutils':config['native']['binutils']},previous,now)
        on_results(facts);current[0]={**config,'config_digest':'operator-new'}
        on_results(facts)
        pytest.fail('drift must stop writer')
    with pytest.raises(ValueError,match='configuration changed'):
        collector.check_upstreams(config,'unused',db,run_nv=execute)
    assert state.read(db)['tracks']['binutils']['version']=='9.0'
    assert state.read(db)['components']['nvchecker']==snapshot['components']['nvchecker']
