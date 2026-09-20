"""Uses the real nvchecker executable and jq source, with only HTTP input fixture substituted."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import re
import tomllib
from pathlib import Path
import subprocess
import threading
from tracker.nv import import_events
from tracker.state import utcnow

def test_native_jq_branch_selection(tmp_path):
    fixtures=Path(__file__).parent/'fixtures'
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(fixtures)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        config=tmp_path/'nvchecker.toml'
        config.write_text((fixtures/'nvchecker.toml').read_text().replace('FIXTURE_PORT',str(server.server_port)))
        p=subprocess.run(['nvchecker','--logger=json','--json-log-fd=1','-c',str(config)],capture_output=True,text=True,timeout=40)
        print('NVCHECKER STDOUT:',p.stdout,'STDERR:',p.stderr,'EXIT:',p.returncode)
        assert p.returncode==0
        result,error=import_events(p.stdout,{'widget@3':{},'widget@4':{}},{},utcnow())
        assert result['widget@3']['version']=='3.10.0'
        assert result['widget@4']['version']=='4.2.0'
        assert not error
    finally:
        server.shutdown();server.server_close();thread.join()

def test_upstream_rolls_without_rewriting_toml(tmp_path):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    payload=tmp_path/'versions.json'
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(tmp_path)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        path=tmp_path/'native.toml'
        path.write_text('[widget]\nsource="jq"\nurl="http://127.0.0.1:'+str(server.server_port)+'/versions.json"\nfilter=".stable_versions[]"\ninclude_regex="^3[.][0-9]+[.][0-9]+$"\n')
        digest=hashlib.sha256(path.read_bytes()).hexdigest();previous={}
        for version in ('3.10.0','3.11.0'):
            payload.write_text(json.dumps({'stable_versions':[version,'4.99.0']}))
            p=subprocess.run(['nvchecker','--logger=json','--json-log-fd=1','--failures','-c',str(path)],capture_output=True,text=True,timeout=40)
            print('ROLLING NVCHECKER:',p.stdout,'STDERR:',p.stderr,'EXIT:',p.returncode)
            assert p.returncode==0
            previous,error=import_events(p.stdout,{'widget':{}},previous,utcnow())
            assert previous['widget']['version']==version and not error
            assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
    finally:
        server.shutdown();server.server_close();thread.join()

def test_native_sparse_index_excludes_yanked_and_other_streams(tmp_path):
    native=__import__('tracker.version_rules',fromlist=['load']).load(Path(__file__).resolve().parents[2]/'config/versions/groups.toml').entries
    sparse=[v for v in native.values() if v.get('source')=='regex' and v.get('url','').startswith('https://index.crates.io/')]
    assert sparse, 'the actual production index rule is the test input'
    regex=sparse[0]['regex']
    versions=[('0.4.7',False),('0.4.8+metadata',False),('0.4.99',True),
              ('0.4.100-rc.1',False),('0.4.100-alpha.1',False),('0.4.100-beta',False),
              ('0.4.100-dev',False),('0.4.100-rc+meta',False),('0.5.0',False),('10.4.100',False)]
    records=[{'name':'fixture','vers':v,'deps':[],'yanked':y} for v,y in versions]
    payload='\n'.join(json.dumps(r,separators=(',',':')) for r in records)+'\n'
    assert '0.4.99' not in re.findall(regex,payload)
    # Cargo fields may be reordered; whitespace does not change their meaning.
    assert re.findall(regex,'{"yanked": false , "vers": "0.4.8", "features":{"vers":[]}}\n')==['0.4.8']
    assert re.findall(regex,'{"vers":"0.4.8", "yanked":true}\n')==[]
    (tmp_path/'index').write_text(payload)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(tmp_path)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        entry={'source':'regex','url':f'http://127.0.0.1:{server.server_port}/index',
               'regex':regex,'include_regex':r'^0\.4\.[0-9]+(?:\+[^ ]+)?$',
               'from_pattern':'[+].*$','to_pattern':''}
        path=tmp_path/'native.toml';path.write_text('[fixture]\n'+'\n'.join(k+' = '+json.dumps(v) for k,v in entry.items()))
        p=subprocess.run(['nvchecker','--logger=json','--json-log-fd=1','--failures','-c',str(path)],capture_output=True,text=True,timeout=40)
        print('SPARSE NVCHECKER:',p.stdout,'STDERR:',p.stderr,'EXIT:',p.returncode)
        assert p.returncode==0
        result,error=import_events(p.stdout,{'fixture':entry},{},utcnow())
        assert result['fixture']['version']=='0.4.8' and not error
        # A withdrawn maintenance line is an error, not proof of being current.
        (tmp_path/'index').write_text('{"name":"fixture","vers":"0.4.8","yanked":true}\n')
        p=subprocess.run(['nvchecker','--logger=json','--json-log-fd=1','--failures','-c',str(path)],capture_output=True,text=True,timeout=40)
        print('YANKED NVCHECKER:',p.stdout,'STDERR:',p.stderr,'EXIT:',p.returncode)
        assert p.returncode==3
        retained,_=import_events(p.stdout,{'fixture':entry},result,utcnow())
        assert retained['fixture']['version']=='0.4.8' and retained['fixture']['error']
        assert retained['fixture']['fetched_at']==result['fixture']['fetched_at']
    finally:
        server.shutdown();server.server_close();thread.join()
